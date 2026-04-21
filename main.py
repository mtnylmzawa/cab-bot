from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from binance.um_futures import UMFutures
from binance.error import ClientError
import os, json, httpx, asyncio, math, time
from datetime import datetime, timezone, timedelta

app = FastAPI()

# ============ KONFIGÜRASYON ============
TEST_MODE = False  # 🟢 CANLI MOD
MAX_POSITIONS_DEFAULT = 7  # v6.1: 5 → 7 (akıllı slot ile pratikte daha fazla açık olabilir)
MAX_POSITIONS_MIN = 3      # v6.6 Lite Patch 8: Hard limit
MAX_POSITIONS_MAX = 12     # v6.6 Lite Patch 8: Hard limit
DATA_FILE = os.environ.get("DATA_FILE", "/tmp/cab_data.json")
TIMEOUT_HOURS = 12  # pozisyon timeout süresi (asgari)
TIMEOUT_ABSOLUTE_HOURS = 24  # v6.4: mutlak limit — slot baskısı olmasa bile zorla kapat
TIMEOUT_PRESSURE_THRESHOLD = 5  # v6.4: aktif_risk bu eşikten azsa timeout pas geç (slot bol)
TIMEOUT_CHECK_INTERVAL_SEC = 300  # her 5 dakika

# v6.6 Lite Patch 5: KILL SWITCH / PAUSE MODE ayarları
KILL_SWITCH_ENABLED = True       # Otomatik durdurma açık mı?
STOP_STREAK_WINDOW = 5           # Son kaç pozu kontrol et?
STOP_STREAK_THRESHOLD = 4        # Bu sayıda stop varsa pause (5 pozda 4+ stop)
DAILY_LOSS_LIMIT = -150.0        # Günlük net zarar bu eşiği geçerse pause (USDT)

client = UMFutures(
    key=os.environ.get("BINANCE_API_KEY"),
    secret=os.environ.get("BINANCE_SECRET_KEY"),
    base_url="https://fapi.binance.com"
)

INITIAL_DATA = {
    "open_positions": {}, "closed_positions": [], "skipped_signals": [],
    "shadow_positions": {}, "shadow_closed": [], "shadow_skipped": [],  # v6.5: RAM Shadow Mode
    "pause_state": {  # v6.6 Lite Patch 5: Kill Switch
        "paused": False,
        "reason": None,          # manual / auto_stop_streak / auto_daily_loss
        "reason_text": None,     # insan okuyabilir açıklama
        "paused_at": None,       # TR saat string
        "auto_triggered": False  # aynı gün içinde birden çok kez tetiklenmesin
    },
    "max_pos_state": {  # v6.6 Lite Patch 8: Dinamik MAX pozisyon
        "current": MAX_POSITIONS_DEFAULT,
        "last_change_at": None,
        "change_history": [],   # [{"ts": "...", "from": X, "to": Y, "reason": "..."}]
        "auto_reduced": False,  # oto-düşürme yapıldı mı?
    }
}

# ============ VERİ YÖNETİMİ ============
def load_data():
    try:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, "r") as f:
                loaded = json.load(f)
            # v6.6 Lite Patch 5: Eski data'da pause_state yoksa ekle
            if "pause_state" not in loaded:
                loaded["pause_state"] = {
                    "paused": False, "reason": None, "reason_text": None,
                    "paused_at": None, "auto_triggered": False
                }
            # v6.6 Lite Patch 8: max_pos_state migration
            if "max_pos_state" not in loaded:
                loaded["max_pos_state"] = {
                    "current": MAX_POSITIONS_DEFAULT,
                    "last_change_at": None,
                    "change_history": [],
                    "auto_reduced": False,
                }
            return loaded
    except Exception as e:
        print(f"[LOAD ERR] {e}")
    return {k: (v.copy() if isinstance(v, (dict, list)) else v) for k, v in INITIAL_DATA.items()}

def save_data(data):
    try:
        with open(DATA_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        print(f"[SAVE ERR] {e}")

data = load_data()


# ============ KILL SWITCH / PAUSE MANAGER (v6.6 Lite Patch 5) ============

def is_paused():
    """Bot şu anda pause modunda mı?"""
    ps = data.get("pause_state", {})
    return bool(ps.get("paused", False))


def get_pause_info():
    """Pause durumu hakkında bilgi (dashboard için)"""
    return data.get("pause_state", {
        "paused": False, "reason": None, "reason_text": None,
        "paused_at": None, "auto_triggered": False
    })


def pause_bot(reason, reason_text):
    """Bot'u pause moduna al. reason: manual / auto_stop_streak / auto_daily_loss"""
    data["pause_state"] = {
        "paused": True,
        "reason": reason,
        "reason_text": reason_text,
        "paused_at": now_tr(),
        "auto_triggered": reason.startswith("auto_"),
    }
    save_data(data)
    print(f"[PAUSE] Bot durduruldu — {reason}: {reason_text}")


def resume_bot():
    """Bot'u tekrar aktif et"""
    data["pause_state"] = {
        "paused": False, "reason": None, "reason_text": None,
        "paused_at": None, "auto_triggered": False,
    }
    save_data(data)
    print("[PAUSE] Bot tekrar aktif")


def check_auto_pause_triggers():
    """
    Pozisyon her kapandığında çağrılır.
    Ardışık stop sayısı VEYA günlük zarar eşiği aşıldıysa otomatik pause.
    """
    if not KILL_SWITCH_ENABLED:
        return
    if is_paused():
        return  # Zaten pause'da, tetikleme yok

    closed = data.get("closed_positions", [])
    if not closed:
        return

    # 1) Ardışık stop kontrolü — son N poz içinde K+ stop
    recent = closed[-STOP_STREAK_WINDOW:]
    stop_count = sum(1 for c in recent if c.get("kar", 0) < 0 and ("Stop" in c.get("sonuc", "") or "Timeout" in c.get("sonuc", "")))
    if len(recent) >= STOP_STREAK_WINDOW and stop_count >= STOP_STREAK_THRESHOLD:
        pause_bot(
            "auto_stop_streak",
            f"Son {STOP_STREAK_WINDOW} pozda {stop_count} stop/timeout — üst üste kayıp koruması"
        )
        return

    # 2) Günlük zarar kontrolü — bugünün net karı eşik altında mı
    today = now_tr()[:10]
    bugun = [c for c in closed if c.get("kapanis", "").startswith(today)]
    if bugun:
        daily_net = sum(c.get("kar", 0) for c in bugun)
        if daily_net <= DAILY_LOSS_LIMIT:
            pause_bot(
                "auto_daily_loss",
                f"Günlük zarar ${daily_net:.1f} (limit: ${DAILY_LOSS_LIMIT}) — günlük kayıp koruması"
            )


# ============ DİNAMİK MAX POSITIONS (v6.6 Lite Patch 8) ============

def get_max_positions():
    """Şu anki MAX pozisyon limitini getir (dinamik, data'dan okunur)"""
    mp = data.get("max_pos_state", {})
    val = mp.get("current", MAX_POSITIONS_DEFAULT)
    # Hard limitler
    val = max(MAX_POSITIONS_MIN, min(MAX_POSITIONS_MAX, int(val)))
    return val


def set_max_positions(new_val, reason="manual"):
    """MAX pozisyon limitini değiştir (API ve oto-düşürme tarafından çağrılır)"""
    new_val = max(MAX_POSITIONS_MIN, min(MAX_POSITIONS_MAX, int(new_val)))
    if "max_pos_state" not in data:
        data["max_pos_state"] = {
            "current": MAX_POSITIONS_DEFAULT,
            "last_change_at": None,
            "change_history": [],
            "auto_reduced": False,
        }
    old_val = data["max_pos_state"].get("current", MAX_POSITIONS_DEFAULT)
    if new_val == old_val:
        return {"changed": False, "value": new_val}

    data["max_pos_state"]["current"] = new_val
    data["max_pos_state"]["last_change_at"] = now_tr()
    history = data["max_pos_state"].get("change_history", [])
    history.append({
        "ts": now_tr(),
        "from": old_val,
        "to": new_val,
        "reason": reason,
    })
    # Sadece son 50 değişiklik tut
    data["max_pos_state"]["change_history"] = history[-50:]
    if reason.startswith("auto_"):
        data["max_pos_state"]["auto_reduced"] = True
    save_data(data)
    print(f"[MAX_POS] {old_val} → {new_val} ({reason})")
    return {"changed": True, "value": new_val, "from": old_val}


def check_auto_reduce_max_pos():
    """
    Pozisyon kapanışında çağrılır. Ardışık stop/zarar varsa MAX'ı KÜÇÜLT.
    ASLA artırmaz (sadece sen manuel artırırsın).
    Kill Switch'ten önce devreye girer — bu daha yumuşak bir koruma.
    """
    if is_paused():
        return  # zaten pause'da, max'a dokunma

    closed = data.get("closed_positions", [])
    if len(closed) < 3:
        return  # yeterli veri yok

    current_max = get_max_positions()

    # Kural 1: Son 3 pozda 2+ stop/timeout → MAX'ı 2 düşür (min 3)
    last3 = closed[-3:]
    stops_in_3 = sum(1 for c in last3 if c.get("kar", 0) < 0 and ("Stop" in c.get("sonuc", "") or "Timeout" in c.get("sonuc", "")))
    if stops_in_3 >= 2 and current_max > MAX_POSITIONS_MIN:
        new_val = max(MAX_POSITIONS_MIN, current_max - 2)
        if new_val < current_max:
            set_max_positions(new_val, f"auto_stop_streak_3in2")
            return

    # Kural 2: Bugün 4+ stop yediyse → MAX = 3 (min)
    today = now_tr()[:10]
    bugun = [c for c in closed if c.get("kapanis", "").startswith(today)]
    stops_today = sum(1 for c in bugun if c.get("kar", 0) < 0 and ("Stop" in c.get("sonuc", "") or "Timeout" in c.get("sonuc", "")))
    if stops_today >= 4 and current_max > MAX_POSITIONS_MIN:
        set_max_positions(MAX_POSITIONS_MIN, f"auto_daily_{stops_today}stops")
        return


# ============ VERİ YÖNETİMİ (devam) ============# v6.1: Skipped_signals key'i eski verilerde olmayabilir, garanti et
if "skipped_signals" not in data:
    data["skipped_signals"] = []
# v6.5: Shadow Mode alanları
if "shadow_positions" not in data:
    data["shadow_positions"] = {}
if "shadow_closed" not in data:
    data["shadow_closed"] = []
if "shadow_skipped" not in data:
    data["shadow_skipped"] = []
    save_data(data)

def now_tr():
    return (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M")

def now_tr_short():
    return (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%H:%M")

def now_tr_dt():
    """v6.1: Naive datetime olarak TR saatini döndür (parse edilen değerle uyumlu olsun)"""
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=3)

# ============ AKILLI SLOT SAYIMI ============
def count_active_risk():
    """v6.1: Aktif risk taşıyan pozisyonları say (TP1 vurmuş VE timeout-BE'liler exempt)"""
    aktif = 0
    garantili_tp1 = 0
    garantili_timeout = 0
    for p in data["open_positions"].values():
        if p.get("tp1_hit"):
            garantili_tp1 += 1
        elif p.get("timeout_be"):
            garantili_timeout += 1
        else:
            aktif += 1
    return aktif, garantili_tp1, garantili_timeout

# ============ BINANCE HELPERS ============
lot_cache = {}
invalid_symbols_cache = set()  # v6.4: Geçersiz sembolleri cache'le (USDTTRY gibi futures'ta olmayan)

def get_symbol_info(symbol):
    """Sembol için lot step ve price precision al, cache'le"""
    if symbol in lot_cache:
        return lot_cache[symbol]
    try:
        info = client.exchange_info()
        for s in info.get("symbols", []):
            if s["symbol"] == symbol:
                lot_step = 1.0
                price_precision = 2
                qty_precision = 0
                for f in s.get("filters", []):
                    if f["filterType"] == "LOT_SIZE":
                        lot_step = float(f["stepSize"])
                        step_str = f["stepSize"].rstrip('0')
                        if '.' in step_str:
                            qty_precision = len(step_str.split('.')[1])
                        else:
                            qty_precision = 0
                    if f["filterType"] == "PRICE_FILTER":
                        tick_str = f["tickSize"].rstrip('0')
                        if '.' in tick_str:
                            price_precision = len(tick_str.split('.')[1])
                        else:
                            price_precision = 0
                result = {"lot_step": lot_step, "qty_precision": qty_precision, "price_precision": price_precision}
                lot_cache[symbol] = result
                return result
    except Exception as e:
        print(f"[SYMBOL INFO ERR] {symbol}: {e}")
    return {"lot_step": 1.0, "qty_precision": 0, "price_precision": 2}

def round_qty(qty, info):
    step = info["lot_step"]
    precision = info["qty_precision"]
    rounded = math.floor(qty / step) * step
    return round(rounded, precision)

def round_price(price, info):
    return round(price, info["price_precision"])

def binance_set_leverage(symbol, lev):
    try:
        result = client.change_leverage(symbol=symbol, leverage=lev)
        print(f"[BINANCE] Leverage {symbol} → {lev}x ✓")
        return True
    except ClientError as e:
        if "No need to change" in str(e):
            print(f"[BINANCE] Leverage {symbol} zaten {lev}x")
            return True
        print(f"[BINANCE ERR] Leverage {symbol}: {e}")
        return False

def binance_set_margin_type(symbol, margin_type="ISOLATED"):
    try:
        result = client.change_margin_type(symbol=symbol, marginType=margin_type)
        print(f"[BINANCE] Margin type {symbol} → {margin_type} ✓")
        return True
    except ClientError as e:
        if "No need to change" in str(e):
            print(f"[BINANCE] Margin type {symbol} zaten {margin_type}")
            return True
        print(f"[BINANCE ERR] Margin type {symbol}: {e}")
        return False

def binance_market_buy(symbol, qty):
    try:
        result = client.new_order(
            symbol=symbol, side="BUY", type="MARKET", quantity=qty
        )
        filled_qty = float(result.get("executedQty", 0))
        avg_price = float(result.get("avgPrice", 0))
        if avg_price == 0 and filled_qty > 0:
            cum_quote = float(result.get("cumQuote", result.get("cumQty", 0)))
            if cum_quote > 0:
                avg_price = cum_quote / filled_qty
        if avg_price == 0 and result.get("fills"):
            prices = [float(f["price"]) for f in result["fills"] if float(f.get("qty", 0)) > 0]
            if prices:
                avg_price = sum(prices) / len(prices)
        print(f"[BINANCE] MARKET BUY {symbol} qty:{filled_qty} avgPx:{avg_price} ✓")
        return {"success": True, "avg_price": avg_price, "filled_qty": filled_qty, "order": result}
    except ClientError as e:
        print(f"[BINANCE ERR] Market buy {symbol}: {e}")
        return {"success": False, "error": str(e)}

def binance_market_sell(symbol, qty):
    try:
        result = client.new_order(
            symbol=symbol, side="SELL", type="MARKET", quantity=qty, reduceOnly="true"
        )
        filled_qty = float(result.get("executedQty", 0))
        avg_price = float(result.get("avgPrice", 0))
        if avg_price == 0 and filled_qty > 0:
            cum_quote = float(result.get("cumQuote", result.get("cumQty", 0)))
            if cum_quote > 0:
                avg_price = cum_quote / filled_qty
        if avg_price == 0 and result.get("fills"):
            prices = [float(f["price"]) for f in result["fills"] if float(f.get("qty", 0)) > 0]
            if prices:
                avg_price = sum(prices) / len(prices)
        print(f"[BINANCE] MARKET SELL {symbol} qty:{filled_qty} avgPx:{avg_price} ✓")
        return {"success": True, "avg_price": avg_price, "filled_qty": filled_qty}
    except ClientError as e:
        print(f"[BINANCE ERR] Market sell {symbol}: {e}")
        return {"success": False, "error": str(e)}

def binance_stop_loss(symbol, qty, stop_price, info):
    try:
        sp = round_price(stop_price, info)
        result = client.new_order(
            symbol=symbol, side="SELL", type="STOP_MARKET",
            quantity=qty, stopPrice=sp, reduceOnly="true",
            workingType="MARK_PRICE"
        )
        order_id = result.get("orderId")
        print(f"[BINANCE] STOP_MARKET {symbol} qty:{qty} stop:{sp} orderId:{order_id} ✓")
        return {"success": True, "order_id": order_id}
    except ClientError as e:
        print(f"[BINANCE ERR] Stop loss {symbol}: {e}")
        return {"success": False, "error": str(e)}

def binance_cancel_all(symbol):
    try:
        result = client.cancel_open_orders(symbol=symbol)
        print(f"[BINANCE] Cancel all orders {symbol} ✓")
        return True
    except ClientError as e:
        if "No open orders" in str(e) or "Unknown order" in str(e):
            return True
        print(f"[BINANCE ERR] Cancel orders {symbol}: {e}")
        return False

def binance_get_position_qty(symbol):
    try:
        positions = client.get_position_risk(symbol=symbol)
        for p in positions:
            if p["symbol"] == symbol:
                qty = float(p.get("positionAmt", 0))
                return abs(qty)
    except Exception as e:
        print(f"[BINANCE ERR] Get position {symbol}: {e}")
    return 0

def binance_close_position(symbol):
    """v6.4: Pozisyonu tamamen kapat. closePosition=true MARKET emirde çalışmıyor (-4136),
    bu yüzden gerçek positionAmt'i alıp DOĞRU precision ile market sell yapıyoruz.
    Lot kalıntısı bırakmamak için 2 sell denemesi:
      1. Tam qty market sell
      2. Eğer kalıntı varsa ikinci tur sell
    """
    qty = binance_get_position_qty(symbol)
    if qty <= 0:
        return {"success": True, "msg": "no position"}

    info = get_symbol_info(symbol)
    binance_cancel_all(symbol)

    # 1. Tur: tam miktarı sat (precision'a yuvarla)
    qty_rounded = round_qty(qty, info)
    if qty_rounded <= 0:
        print(f"[BINANCE WARN] {symbol} qty {qty} → rounded 0, kapatma başarısız")
        return {"success": False, "error": "qty rounded to 0"}

    result = binance_market_sell(symbol, qty_rounded)
    if not result["success"]:
        return result

    # 2. Tur kontrol: kalıntı kaldı mı?
    time.sleep(0.5)  # Binance'in pozisyonu güncellemesi için kısa bekleme
    remaining = binance_get_position_qty(symbol)
    if remaining > 0:
        # Kalıntı için ikinci tur — hassas precision ile dene
        precision = info["qty_precision"]
        # Lot step'in altındaki kalıntıyı yakalamak için bir alt precision dene
        remaining_str = f"{remaining:.{precision + 2}f}".rstrip('0').rstrip('.')
        try:
            remaining_qty = float(remaining_str)
            # Yine round_qty ile dene
            remaining_rounded = round_qty(remaining_qty, info)
            if remaining_rounded > 0:
                print(f"[BINANCE] {symbol} kalıntı {remaining} → 2. tur sell {remaining_rounded}")
                result2 = binance_market_sell(symbol, remaining_rounded)
                if result2["success"]:
                    print(f"[BINANCE] {symbol} 2. tur sell başarılı, pozisyon temiz ✓")
            else:
                # Kalıntı lot step'in altında — toz miktar, görmezden gel
                print(f"[BINANCE] {symbol} ihmal edilebilir kalıntı: {remaining} (lot step altı)")
        except Exception as e:
            print(f"[BINANCE WARN] {symbol} kalıntı temizleme hatası: {e}")

    return result

def binance_get_mark_price(symbol):
    """v6.1: Sembol için mark price çek (timeout kontrolü için)"""
    try:
        result = client.mark_price(symbol=symbol)
        return float(result.get("markPrice", 0))
    except Exception as e:
        print(f"[BINANCE ERR] Mark price {symbol}: {e}")
        return None


def fetch_binance_realized_pnl(symbol, start_time_ms=None, end_time_ms=None):
    """
    v6.6 Lite: Binance'ten sembol için realized PNL çek (fee dahil).
    Önce get_income dener, fail olursa get_account_trades ile hesaplar.
    """
    # === YÖNTEM 1: get_income (income history) ===
    try:
        kwargs = {"symbol": symbol, "incomeType": "REALIZED_PNL", "limit": 20}
        if start_time_ms:
            kwargs["startTime"] = int(start_time_ms)
        if end_time_ms:
            kwargs["endTime"] = int(end_time_ms)
        incomes = client.get_income_history(**kwargs)
        if incomes is not None:
            pnl_total = sum(float(i.get("income", 0)) for i in incomes)

            fee_kwargs = {"symbol": symbol, "incomeType": "COMMISSION", "limit": 20}
            if start_time_ms:
                fee_kwargs["startTime"] = int(start_time_ms)
            if end_time_ms:
                fee_kwargs["endTime"] = int(end_time_ms)
            fees = client.get_income_history(**fee_kwargs)
            fee_total = sum(float(f.get("income", 0)) for f in fees) if fees else 0

            if len(incomes) > 0 or pnl_total != 0:
                net_pnl = pnl_total + fee_total
                return {
                    "success": True, "method": "income",
                    "realized_pnl": round(pnl_total, 2),
                    "fee": round(fee_total, 2),
                    "net_pnl": round(net_pnl, 2),
                    "count": len(incomes),
                }
    except Exception as e:
        print(f"[BINANCE-PNL M1] get_income fail {symbol}: {e}")

    # === YÖNTEM 2: get_account_trades (alternatif, bazen daha iyi izin) ===
    try:
        kwargs = {"symbol": symbol, "limit": 100}
        if start_time_ms:
            kwargs["startTime"] = int(start_time_ms)
        if end_time_ms:
            kwargs["endTime"] = int(end_time_ms)
        trades = client.get_account_trades(**kwargs)
        if trades:
            realized_pnl = sum(float(t.get("realizedPnl", 0)) for t in trades)
            commission = sum(float(t.get("commission", 0)) for t in trades)
            # Binance commission pozitif döner (bot için gider), negatife çevir
            fee_total = -commission
            net_pnl = realized_pnl + fee_total
            return {
                "success": True, "method": "trades",
                "realized_pnl": round(realized_pnl, 2),
                "fee": round(fee_total, 2),
                "net_pnl": round(net_pnl, 2),
                "count": len(trades),
            }
    except Exception as e:
        print(f"[BINANCE-PNL M2] get_account_trades fail {symbol}: {e}")
        return {"success": False, "error": f"Her iki yöntem de fail: {e}"}

    return {"success": False, "error": "Veri bulunamadı (ikisi de boş döndü)"}



def binance_get_klines(symbol, interval="1m", limit=60):
    """v6.4: Geçmiş bar verilerini çek (high/low takibi için)
    Public endpoint, auth gerekmez.
    Dönen: [[time, open, high, low, close, volume, ...], ...]
    v6.4: Geçersiz semboller cache'lenir, tekrar denenmez (USDTTRY gibi)
    """
    if symbol in invalid_symbols_cache:
        return None
    try:
        klines = client.klines(symbol=symbol, interval=interval, limit=limit)
        return klines
    except Exception as e:
        err_str = str(e)
        if "Invalid symbol" in err_str or "-1121" in err_str:
            invalid_symbols_cache.add(symbol)
            print(f"[KLINES] {symbol} geçersiz sembol — cache'lendi, tekrar denenmeyecek")
        else:
            print(f"[KLINES ERR] {symbol} {interval}: {e}")
        return None

def get_high_low_since(symbol, since_ms, interval="1m"):
    """v6.4: Belirli bir zamandan beri görülen MAX high ve MIN low'u döndür.
    since_ms: milisaniye cinsinden timestamp
    """
    # Şu anki zamandan since_ms'e olan farkı dakikaya çevir
    now_ms = int(time.time() * 1000)
    minutes_passed = max(1, (now_ms - since_ms) // 60000 + 5)  # +5 buffer

    # 1m bar için max 1500 limit, daha uzunsa 5m'ye geçelim
    if minutes_passed > 1000:
        interval = "5m"
        limit = min(1500, max(50, minutes_passed // 5 + 5))
    elif minutes_passed > 500:
        interval = "5m"
        limit = max(100, minutes_passed // 5 + 5)
    else:
        interval = "1m"
        limit = max(50, minutes_passed)

    klines = binance_get_klines(symbol, interval=interval, limit=int(limit))
    if not klines:
        return None, None

    max_high = 0
    min_low = float('inf')
    for k in klines:
        bar_time = int(k[0])
        if bar_time < since_ms:
            continue
        high = float(k[2])
        low = float(k[3])
        if high > max_high:
            max_high = high
        if low < min_low:
            min_low = low

    if max_high == 0 or min_low == float('inf'):
        return None, None
    return max_high, min_low

# ============ KAR HESAPLAMA ============
def calc_tp1_kar(pos, tp1_px=None):
    giris = pos["giris"]
    tp1 = tp1_px if tp1_px is not None else pos.get("tp1", giris)
    pos_size = pos["marj"] * pos["lev"]
    kapat_oran = pos.get("kapat_oran", 60)
    return round(pos_size * (kapat_oran / 100.0) * (tp1 - giris) / giris, 2)

def calc_tp2_kar(pos, tp2_px=None):
    giris = pos["giris"]
    tp2 = tp2_px if tp2_px is not None else pos.get("tp2", giris)
    pos_size = pos["marj"] * pos["lev"]
    return round(pos_size * 0.25 * (tp2 - giris) / giris, 2)

def calc_trail_kar(pos, trail_px, tp_type="TP1"):
    giris = pos["giris"]
    pos_size = pos["marj"] * pos["lev"]
    kapat_oran = pos.get("kapat_oran", 60)
    kalan_oran = (100 - kapat_oran - 25) if tp_type == "TP2" else (100 - kapat_oran)
    return round(pos_size * (kalan_oran / 100.0) * (trail_px - giris) / giris, 2)

def is_recently_closed(ticker, n=20):
    return ticker in [c["ticker"] for c in data["closed_positions"][-n:]]

# ============ TRADE EXECUTION ============
def execute_entry(ticker, parsed):
    symbol = ticker
    lev = parsed["lev"]
    marj = parsed["marj"]
    giris_px = parsed["giris"]
    stop_px = parsed["stop"]

    info = get_symbol_info(symbol)
    pos_size = marj * lev
    qty = pos_size / giris_px
    qty = round_qty(qty, info)

    if qty <= 0:
        print(f"[TRADE ERR] {symbol} qty=0, pos_size:{pos_size} px:{giris_px}")
        return False

    if not binance_set_leverage(symbol, lev):
        return False
    binance_set_margin_type(symbol, "ISOLATED")

    result = binance_market_buy(symbol, qty)
    if not result["success"]:
        return False

    actual_qty = result["filled_qty"]
    actual_price = result["avg_price"]

    if actual_price == 0 or actual_price < 0.0000001:
        try:
            positions = client.get_position_risk(symbol=symbol)
            for p in positions:
                if p["symbol"] == symbol and float(p.get("positionAmt", 0)) != 0:
                    actual_price = float(p.get("entryPrice", 0))
                    print(f"[TRADE] {symbol} entryPrice from position: {actual_price}")
                    break
        except Exception as e:
            print(f"[TRADE WARN] entryPrice fetch failed: {e}")

    if actual_price == 0 or actual_price < 0.0000001:
        actual_price = giris_px
        print(f"[TRADE WARN] {symbol} avgPrice=0, Pine fiyatı kullanıldı: {actual_price}")

    sl_result = binance_stop_loss(symbol, actual_qty, stop_px, info)
    sl_order_id = sl_result.get("order_id") if sl_result["success"] else None

    print(f"[TRADE] GIRIS OK: {symbol} | qty:{actual_qty} | px:{actual_price} | SL:{stop_px}")
    return {"qty": actual_qty, "avg_price": actual_price, "sl_order_id": sl_order_id}

def execute_tp1_close(ticker, pos):
    symbol = ticker
    info = get_symbol_info(symbol)
    kapat_oran = pos.get("kapat_oran", 60)

    total_qty = binance_get_position_qty(symbol)
    if total_qty <= 0:
        print(f"[TRADE WARN] TP1 ama {symbol} pozisyon yok")
        return False

    close_qty = round_qty(total_qty * (kapat_oran / 100.0), info)
    if close_qty <= 0:
        print(f"[TRADE WARN] TP1 close_qty=0 for {symbol}")
        return False

    binance_cancel_all(symbol)
    result = binance_market_sell(symbol, close_qty)
    if not result["success"]:
        return False

    remaining_qty = round_qty(total_qty - close_qty, info)
    if remaining_qty > 0:
        be_price = pos["giris"]
        binance_stop_loss(symbol, remaining_qty, be_price, info)

    print(f"[TRADE] TP1 OK: {symbol} | Kapatılan:{close_qty} | Kalan:{remaining_qty} | BE:{pos['giris']}")
    return True

def execute_tp2_close(ticker, pos):
    symbol = ticker
    info = get_symbol_info(symbol)

    total_qty = binance_get_position_qty(symbol)
    if total_qty <= 0:
        print(f"[TRADE WARN] TP2 ama {symbol} pozisyon yok")
        return False

    close_qty = round_qty(total_qty * 0.625, info)
    if close_qty <= 0 or close_qty > total_qty:
        close_qty = round_qty(total_qty * 0.5, info)

    if close_qty <= 0:
        return False

    binance_cancel_all(symbol)
    result = binance_market_sell(symbol, close_qty)
    if not result["success"]:
        return False

    remaining_qty = round_qty(total_qty - close_qty, info)
    if remaining_qty > 0:
        be_price = pos["giris"]
        binance_stop_loss(symbol, remaining_qty, be_price, info)

    print(f"[TRADE] TP2 OK: {symbol} | Kapatılan:{close_qty} | Kalan:{remaining_qty}")
    return True

def execute_full_close(ticker, reason="TRAIL"):
    symbol = ticker
    binance_cancel_all(symbol)
    result = binance_close_position(symbol)
    print(f"[TRADE] {reason} CLOSE: {symbol} | {result}")
    return result.get("success", False)

# ============ AKILLI TIMEOUT ============
def execute_smart_timeout(ticker, pos):
    """
    v6.1: Akıllı timeout — pozisyonun durumuna göre karar ver
    - Mark price çek → şu anki kar/zarar hesapla
    - Kârda → BE stop'a çek (slot serbest, poz devam)
    - Zardada → market kapat (slot serbest, poz biter)
    - Geri dönüş: ('be', kar) veya ('close', kar)
    """
    symbol = ticker
    giris = pos["giris"]
    pos_size = pos["marj"] * pos["lev"]

    # 1. Şu anki fiyatı çek
    current_px = binance_get_mark_price(symbol)
    if current_px is None or current_px <= 0:
        print(f"[TIMEOUT WARN] {symbol} mark price alınamadı, full close yapılacak")
        if not TEST_MODE:
            execute_full_close(ticker, "TIMEOUT_FALLBACK")
        return ('close', 0)

    # 2. Kar/zarar hesapla
    pct = (current_px - giris) / giris * 100.0
    unrealized = pos_size * (current_px - giris) / giris

    print(f"[TIMEOUT] {symbol} mark:{current_px} giris:{giris} pct:{pct:.2f}% unreal:{unrealized:+.1f}$")

    # 3. Karar ver
    if unrealized > 0:
        # KÂRDA → BE stop'a çek, pozisyon devam
        if not TEST_MODE:
            info = get_symbol_info(symbol)
            qty = binance_get_position_qty(symbol)
            if qty > 0:
                qty = round_qty(qty, info)
                # Mevcut SL iptal et, yeni SL = giriş fiyatı
                binance_cancel_all(symbol)
                sl_result = binance_stop_loss(symbol, qty, giris, info)
                if not sl_result["success"]:
                    print(f"[TIMEOUT ERR] {symbol} BE stop koyulamadı, full close yapılıyor")
                    execute_full_close(ticker, "TIMEOUT_BE_FAIL")
                    return ('close', round(unrealized, 2))
            else:
                print(f"[TIMEOUT WARN] {symbol} qty=0, pozisyon zaten kapalı?")
                return ('close', 0)
        print(f"[TIMEOUT] {symbol} → BE stop'a çekildi (kârda +{unrealized:.1f}$, slot serbest)")
        return ('be', round(unrealized, 2))
    else:
        # ZARARDA → kapat
        if not TEST_MODE:
            execute_full_close(ticker, "TIMEOUT_LOSS")
        print(f"[TIMEOUT] {symbol} → kapatıldı (zararda {unrealized:.1f}$)")
        return ('close', round(unrealized, 2))

# ============ PARSE ============
def parse_giris(msg):
    try:
        parts = [p.strip() for p in msg.split("|")]
        ticker = parts[1].strip()
        detaylar = parts[3]
        kv = {}
        for tok in detaylar.split():
            if ":" in tok:
                k, v = tok.split(":", 1)
                kv[k] = v.replace("$", "").replace("x", "")
        return {
            "type": "GIRIS", "ticker": ticker,
            "giris": float(kv.get("Giris", 0)), "stop": float(kv.get("Stop", 0)),
            "tp1": float(kv.get("TP1", 0)), "tp2": float(kv.get("TP2", 0)),
            "marj": float(kv.get("Marj", 0)), "lev": int(float(kv.get("Lev", 10))),
            "risk": float(kv.get("Risk", 0)),
            "kapat_oran": int(float(kv.get("Kapat", 60))),
            "atr_skor": float(kv.get("ATR", 100)) / 100.0,
            "rs_spread": float(kv.get("RS", 0)),  # v6.5: RAM için göreceli güç spread
        }
    except Exception as e:
        print(f"[PARSE ERR GIRIS] {e}")
        return None

def parse_tp1(msg):
    try:
        parts = [p.strip() for p in msg.split("|")]
        ticker = parts[1].strip()
        detaylar = parts[4] if len(parts) > 4 else parts[3]
        kv = {}
        for tok in detaylar.split():
            if ":" in tok:
                k, v = tok.split(":", 1)
                kv[k] = v
        return {
            "type": "TP1", "ticker": ticker,
            "tp1": float(kv.get("TP1", 0)), "stop": float(kv.get("YeniStop", 0)),
            "kapat_oran": int(float(kv.get("Kapat", 60))),
            "tp1_kar": float(kv.get("TP1Kar", 0)),
        }
    except Exception as e:
        print(f"[PARSE ERR TP1] {e}")
        return None

def parse_tp2(msg):
    try:
        parts = [p.strip() for p in msg.split("|")]
        ticker = parts[1].strip()
        detaylar = parts[4] if len(parts) > 4 else parts[3]
        kv = {}
        for tok in detaylar.split():
            if ":" in tok:
                k, v = tok.split(":", 1)
                kv[k] = v
        return {
            "type": "TP2", "ticker": ticker,
            "tp2": float(kv.get("TP2", 0)), "tp2_kar": float(kv.get("TP2Kar", 0)),
        }
    except Exception as e:
        print(f"[PARSE ERR TP2] {e}")
        return None

def parse_trail(msg):
    try:
        parts = [p.strip() for p in msg.split("|")]
        ticker = parts[1].strip()
        kalan_str = parts[3] if len(parts) > 3 else "0"
        kalan = 0
        for tok in kalan_str.split():
            if tok.startswith("%"):
                try: kalan = int(tok.replace("%", ""))
                except: pass
        detaylar = parts[4] if len(parts) > 4 else ""
        kv = {}
        for tok in detaylar.split():
            if ":" in tok:
                k, v = tok.split(":", 1)
                kv[k] = v
        return {
            "type": "TRAIL", "ticker": ticker, "kalan": kalan,
            "trail_px": float(kv.get("Trailing", 0)),
            "trail_kar": float(kv.get("TrailKar", 0)),
            "tp_type": kv.get("Tip", "TP1"),
        }
    except Exception as e:
        print(f"[PARSE ERR TRAIL] {e}")
        return None

def parse_stop(msg):
    try:
        parts = [p.strip() for p in msg.split("|")]
        ticker = parts[1].strip()
        aciklama = parts[2].strip() if len(parts) > 2 else ""
        detaylar = parts[4] if len(parts) > 4 else parts[-1]
        kv = {}
        for tok in detaylar.split():
            if ":" in tok:
                k, v = tok.split(":", 1)
                kv[k] = v
        return {
            "type": "STOP", "ticker": ticker, "aciklama": aciklama,
            "stop": float(kv.get("Stop", 0)),
        }
    except Exception as e:
        print(f"[PARSE ERR STOP] {e}")
        return None

# ============ ROUTES ============
@app.get("/", response_class=HTMLResponse)
async def root():
    mode = "🟡 TEST MODU" if TEST_MODE else "🟢 CANLI MOD"
    return f"<h3>🤖 CAB Bot v6.6 Lite çalışıyor</h3><p>{mode}</p><p>MAX_POSITIONS: {get_max_positions()} | TIMEOUT: {TIMEOUT_HOURS}s | HL_TRACKER: {HIGH_LOW_CHECK_INTERVAL_SEC}s</p><p><a href='/dashboard'>Dashboard</a> | <a href='/test_binance'>Binance Test</a> | <a href='/api/timeout_check'>Manuel Timeout Check</a></p>"

@app.get("/ip")
async def get_ip():
    try:
        async with httpx.AsyncClient() as c:
            r = await c.get("https://api.ipify.org?format=json")
            return r.json()
    except Exception as e:
        return {"error": str(e)}

@app.get("/test_binance")
async def test_binance():
    results = {}
    try:
        balance = client.balance()
        usdt_balance = None
        for b in balance:
            if b["asset"] == "USDT":
                usdt_balance = {
                    "free": float(b["balance"]),
                    "used": float(b.get("crossUnPnl", 0)),
                    "available": float(b.get("availableBalance", b["balance"]))
                }
        results["balance"] = usdt_balance or "USDT bulunamadı"
        results["balance_status"] = "✅ OK"
    except Exception as e:
        results["balance"] = str(e)
        results["balance_status"] = "❌ HATA"

    try:
        positions = client.get_position_risk()
        open_pos = [p for p in positions if float(p.get("positionAmt", 0)) != 0]
        results["open_positions"] = len(open_pos)
        results["positions_status"] = "✅ OK"
    except Exception as e:
        results["open_positions"] = str(e)
        results["positions_status"] = "❌ HATA"

    try:
        info = get_symbol_info("BTCUSDT")
        results["exchange_info"] = info
        results["exchange_info_status"] = "✅ OK"
    except Exception as e:
        results["exchange_info"] = str(e)
        results["exchange_info_status"] = "❌ HATA"

    try:
        orders = client.get_orders(symbol="BTCUSDT", limit=1)
        results["orders_api"] = "erişilebilir"
        results["orders_status"] = "✅ OK"
    except Exception as e:
        results["orders_api"] = str(e)
        results["orders_status"] = "❌ HATA"

    all_ok = all("✅" in str(v) for k, v in results.items() if k.endswith("_status"))
    results["SONUC"] = "🟢 TÜM TESTLER BAŞARILI — Gerçek mod için hazır!" if all_ok else "🔴 BAZI TESTLER BAŞARISIZ — Kontrol et!"
    results["test_mode"] = TEST_MODE
    results["max_positions"] = get_max_positions()
    results["timeout_hours"] = TIMEOUT_HOURS

    return JSONResponse(results)

@app.post("/update_hh")
async def update_hh(req: Request):
    body = await req.json()
    ticker = body.get("ticker")
    current_price = body.get("price")
    if not ticker or not current_price:
        return {"status": "missing"}
    if ticker in data["open_positions"]:
        pos = data["open_positions"][ticker]
        giris = pos["giris"]
        pct = (current_price - giris) / giris * 100.0
        old_hh = pos.get("hh_pct", 0)
        if pct > old_hh:
            pos["hh_pct"] = round(pct, 2)
            save_data(data)
            print(f"[HH] {ticker}: {old_hh:.2f}% -> {pct:.2f}%")
    return {"status": "ok"}

@app.get("/api/data")
async def api_data():
    return JSONResponse(load_data())

@app.post("/api/fix_giris")
async def fix_giris(req: Request):
    body = await req.json()
    ticker = body.get("ticker")
    new_giris = body.get("giris")
    if ticker and new_giris and ticker in data["open_positions"]:
        data["open_positions"][ticker]["giris"] = float(new_giris)
        save_data(data)
        return {"status": "fixed", "ticker": ticker, "giris": new_giris}
    return {"status": "not_found"}

@app.get("/api/fix_zero_giris")
async def fix_zero_giris():
    fixed = []
    for ticker, pos in data["open_positions"].items():
        try:
            positions = client.get_position_risk(symbol=ticker)
            for p in positions:
                if p["symbol"] == ticker and float(p.get("positionAmt", 0)) != 0:
                    entry_price = float(p.get("entryPrice", 0))
                    if entry_price > 0 and entry_price != pos["giris"]:
                        old = pos["giris"]
                        pos["giris"] = entry_price
                        fixed.append({"ticker": ticker, "old": old, "new": entry_price})
                        print(f"[FIX] {ticker} giris: {old} → {entry_price}")
        except Exception as e:
            print(f"[FIX ERR] {ticker}: {e}")
    if fixed:
        save_data(data)
    return {"fixed": fixed, "count": len(fixed)}

@app.post("/api/clear_old")
async def api_clear_old(req: Request):
    body = await req.json()
    days = int(body.get("days", 30))
    cutoff = (datetime.now(timezone.utc) + timedelta(hours=3)) - timedelta(days=days)
    cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M")
    before = len(data["closed_positions"])
    data["closed_positions"] = [c for c in data["closed_positions"] if c.get("kapanis", "") >= cutoff_str]
    after = len(data["closed_positions"])
    save_data(data)
    return {"removed": before - after, "remaining": after}

@app.post("/api/clear_skipped")
async def api_clear_skipped():
    if "skipped_signals" in data:
        data["skipped_signals"] = []
        save_data(data)
    return {"status": "ok"}

@app.post("/api/clear_shadow")
async def api_clear_shadow():
    """v6.5: Tüm RAM Shadow verilerini temizle"""
    cleared = 0
    if "shadow_positions" in data:
        cleared += len(data["shadow_positions"])
        data["shadow_positions"] = {}
    if "shadow_closed" in data:
        cleared += len(data["shadow_closed"])
        data["shadow_closed"] = []
    if "shadow_skipped" in data:
        data["shadow_skipped"] = []
    save_data(data)
    print(f"[SHADOW] Temizlendi ({cleared} kayıt)")
    return {"status": "ok", "cleared": cleared}

@app.get("/api/timeout_check")
async def manual_timeout_check():
    """v6.1: Manuel timeout taraması — debugging için"""
    result = await timeout_scan_once()
    return JSONResponse(result)


@app.get("/api/export_report")
async def export_report():
    """
    v6.6 Lite Patch 10: Tüm sistem verisini rapor olarak döner.
    Metin bu JSON'u Claude'a atar, Claude analiz yapar.
    """
    closed = data.get("closed_positions", [])
    shadow_closed = data.get("shadow_closed", [])
    open_pos = data.get("open_positions", {})
    shadow_open = data.get("shadow_positions", {})
    skipped = data.get("skipped_signals", [])
    shadow_skipped = data.get("shadow_skipped", [])
    
    def stats_from_closed(arr, use_binance=False):
        if not arr:
            return {"count": 0, "wins": 0, "losses": 0, "wr": 0, "total_pnl": 0,
                    "avg_win": 0, "avg_loss": 0, "profit_factor": 0}
        kar_fn = lambda c: (c.get("binance_pnl") if use_binance and c.get("binance_pnl") is not None else c.get("kar", 0))
        total = sum(kar_fn(c) for c in arr)
        wins = [c for c in arr if kar_fn(c) > 0]
        losses = [c for c in arr if kar_fn(c) < 0]
        total_win = sum(kar_fn(c) for c in wins)
        total_loss = sum(kar_fn(c) for c in losses)
        return {
            "count": len(arr),
            "wins": len(wins),
            "losses": len(losses),
            "wr": round(len(wins) / len(arr) * 100, 1) if arr else 0,
            "total_pnl": round(total, 2),
            "avg_win": round(total_win / len(wins), 2) if wins else 0,
            "avg_loss": round(total_loss / len(losses), 2) if losses else 0,
            "profit_factor": round(abs(total_win / total_loss), 2) if total_loss else 0,
        }
    
    today = now_tr()[:10]
    cab_today = [c for c in closed if c.get("kapanis", "").startswith(today)]
    ram_today = [c for c in shadow_closed if c.get("kapanis", "").startswith(today)]
    
    # Son 7 gün
    try:
        d7 = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        d7 = ""
    cab_7d = [c for c in closed if c.get("kapanis", "") >= d7]
    ram_7d = [c for c in shadow_closed if c.get("kapanis", "") >= d7]
    
    return JSONResponse({
        "report_generated_at": now_tr(),
        "version": "v6.6 Lite Patch 10",
        "config": {
            "max_positions": get_max_positions(),
            "max_pos_min": MAX_POSITIONS_MIN,
            "max_pos_max": MAX_POSITIONS_MAX,
            "timeout_hours": TIMEOUT_HOURS,
            "kill_switch_enabled": KILL_SWITCH_ENABLED,
            "stop_streak_window": STOP_STREAK_WINDOW,
            "stop_streak_threshold": STOP_STREAK_THRESHOLD,
            "daily_loss_limit": DAILY_LOSS_LIMIT,
        },
        "pause_state": get_pause_info(),
        "max_pos_state": data.get("max_pos_state", {}),
        "cab": {
            "open_count": len(open_pos),
            "open_positions": open_pos,
            "closed_count": len(closed),
            "closed_positions": closed,  # tamamı
            "skipped_count": len(skipped),
            "skipped_signals": skipped[-100:],  # son 100
            "stats_all_time_dashboard": stats_from_closed(closed, use_binance=False),
            "stats_all_time_binance": stats_from_closed(closed, use_binance=True),
            "stats_today_dashboard": stats_from_closed(cab_today, use_binance=False),
            "stats_today_binance": stats_from_closed(cab_today, use_binance=True),
            "stats_7d_dashboard": stats_from_closed(cab_7d, use_binance=False),
            "stats_7d_binance": stats_from_closed(cab_7d, use_binance=True),
        },
        "ram": {
            "open_count": len(shadow_open),
            "open_positions": shadow_open,
            "closed_count": len(shadow_closed),
            "closed_positions": shadow_closed,  # tamamı
            "skipped_count": len(shadow_skipped),
            "skipped_signals": shadow_skipped[-100:],
            "stats_all_time": stats_from_closed(shadow_closed),
            "stats_today": stats_from_closed(ram_today),
            "stats_7d": stats_from_closed(ram_7d),
        },
    })


@app.get("/api/inspect_pos/{ticker}")
async def inspect_position(ticker: str):
    """
    v6.6 Lite Patch 9: Bir pozisyon için Binance'ten TÜM income kayıtlarını getir.
    Teşhis için — migrate fonksiyonunun neden eksik hesapladığını gör.
    """
    ticker = ticker.upper()
    # Closed'da bu ticker için son kayıt
    closed_positions = data.get("closed_positions", [])
    target = None
    for c in reversed(closed_positions):
        if c.get("ticker") == ticker:
            target = c
            break
    if not target:
        return JSONResponse({"error": f"{ticker} closed_positions'da yok"}, status_code=404)
    
    result = {
        "ticker": ticker,
        "dashboard_record": {
            "kar": target.get("kar"),
            "sonuc": target.get("sonuc"),
            "tp1_kar": target.get("tp1_kar"),
            "tp2_kar": target.get("tp2_kar"),
            "trail_kar": target.get("trail_kar"),
            "acilis": target.get("acilis"),
            "kapanis": target.get("kapanis"),
            "binance_pnl": target.get("binance_pnl"),
            "binance_fee": target.get("binance_fee"),
        },
    }
    
    # Zaman penceresi
    try:
        acilis_str = target.get("acilis", "")
        kapanis_str = target.get("kapanis", "")
        dt_open = datetime.strptime(acilis_str, "%Y-%m-%d %H:%M")
        dt_open_utc = dt_open - timedelta(hours=3)
        start_ms = int(dt_open_utc.timestamp() * 1000) - 60000
        
        end_ms = None
        if kapanis_str:
            dt_close = datetime.strptime(kapanis_str, "%Y-%m-%d %H:%M")
            dt_close_utc = dt_close - timedelta(hours=3)
            end_ms = int(dt_close_utc.timestamp() * 1000) + 300000  # 5 dakika tampon
        
        result["window"] = {
            "start_ms": start_ms,
            "end_ms": end_ms,
            "acilis": acilis_str,
            "kapanis": kapanis_str,
        }
        
        # Binance'ten SYMBOL bazlı TÜM income kayıtları
        kwargs = {"symbol": ticker, "limit": 1000}
        if start_ms:
            kwargs["startTime"] = start_ms
        if end_ms:
            kwargs["endTime"] = end_ms
        
        # REALIZED_PNL
        pnl_records = client.get_income_history(**{**kwargs, "incomeType": "REALIZED_PNL"}) or []
        # COMMISSION
        fee_records = client.get_income_history(**{**kwargs, "incomeType": "COMMISSION"}) or []
        # Tüm income (tip belirtmeden)
        all_records = client.get_income_history(**kwargs) or []
        
        result["binance_records"] = {
            "realized_pnl": {
                "count": len(pnl_records),
                "total": round(sum(float(r.get("income", 0)) for r in pnl_records), 4),
                "records": pnl_records,
            },
            "commission": {
                "count": len(fee_records),
                "total": round(sum(float(r.get("income", 0)) for r in fee_records), 4),
                "records": fee_records,
            },
            "all_income": {
                "count": len(all_records),
                "types": list(set(r.get("incomeType", "?") for r in all_records)),
            },
        }
        
        # Hesaplanan toplam
        pnl_sum = result["binance_records"]["realized_pnl"]["total"]
        fee_sum = result["binance_records"]["commission"]["total"]
        net = pnl_sum + fee_sum
        result["calculated"] = {
            "pnl_sum": pnl_sum,
            "fee_sum": fee_sum,
            "net": round(net, 2),
            "dashboard_binance_pnl": target.get("binance_pnl"),
            "fark": round(net - (target.get("binance_pnl") or 0), 2),
        }
        
    except Exception as e:
        result["error"] = str(e)
    
    return JSONResponse(result)


@app.get("/api/binance_test_income")
async def test_binance_income():
    """
    v6.6 Lite Patch: Binance API izin testi — İKİ YÖNTEM kontrol eder.
    Migrate çalışmıyorsa önce buna bak.
    """
    result = {"now_tr": now_tr(), "methods": {}}
    start_ms = int((datetime.now(timezone.utc) - timedelta(days=3)).timestamp() * 1000)

    # Yöntem 1: get_income
    try:
        incomes = client.get_income_history(incomeType="REALIZED_PNL", startTime=start_ms, limit=10)
        result["methods"]["get_income"] = {
            "ok": True,
            "count": len(incomes) if incomes else 0,
            "sample": incomes[:2] if incomes else []
        }
    except Exception as e:
        result["methods"]["get_income"] = {"ok": False, "error": str(e)}

    # Yöntem 2: get_account_trades (BTCUSDT deneyelim, çoğu hesapta vardır)
    try:
        trades = client.get_account_trades(symbol="BTCUSDT", startTime=start_ms, limit=10)
        result["methods"]["get_account_trades"] = {
            "ok": True,
            "count": len(trades) if trades else 0,
        }
    except Exception as e:
        result["methods"]["get_account_trades"] = {"ok": False, "error": str(e)}

    # Genel verdict
    m1 = result["methods"]["get_income"].get("ok")
    m2 = result["methods"]["get_account_trades"].get("ok")
    if m1 or m2:
        result["verdict"] = f"✓ Çalışıyor — method1:{m1}, method2:{m2}"
        result["api_ok"] = True
    else:
        result["verdict"] = "❌ Her iki yöntem de fail — API key ayarlarına bak (Enable Futures + IP whitelist)"
        result["api_ok"] = False

    return JSONResponse(result)


@app.get("/api/pause_status")
async def get_pause_status():
    """v6.6 Lite Patch 5: Bot pause durumunu sorgula"""
    return JSONResponse({
        **get_pause_info(),
        "kill_switch_enabled": KILL_SWITCH_ENABLED,
        "stop_streak_window": STOP_STREAK_WINDOW,
        "stop_streak_threshold": STOP_STREAK_THRESHOLD,
        "daily_loss_limit": DAILY_LOSS_LIMIT,
    })


@app.post("/api/toggle_pause")
async def toggle_pause(req: Request):
    """
    v6.6 Lite Patch 5: Manuel pause toggle.
    Body: {"action": "pause" | "resume", "reason_text": "..." (opsiyonel)}
    """
    body = {}
    try:
        body = await req.json()
    except Exception:
        pass
    action = body.get("action", "").lower()
    reason_text = body.get("reason_text", "Manuel durdurma")

    if action == "pause":
        pause_bot("manual", reason_text)
        return JSONResponse({"success": True, "state": get_pause_info(), "msg": "Bot durduruldu"})
    elif action == "resume":
        resume_bot()
        return JSONResponse({"success": True, "state": get_pause_info(), "msg": "Bot tekrar aktif"})
    else:
        # toggle: mevcut durumun tersine geç
        if is_paused():
            resume_bot()
            return JSONResponse({"success": True, "state": get_pause_info(), "msg": "Bot tekrar aktif"})
        else:
            pause_bot("manual", reason_text)
            return JSONResponse({"success": True, "state": get_pause_info(), "msg": "Bot durduruldu"})


@app.get("/api/max_pos_status")
async def get_max_pos_status():
    """v6.6 Lite Patch 8: MAX pozisyon durumu + geçmiş"""
    mp = data.get("max_pos_state", {})
    history = mp.get("change_history", [])
    today = now_tr()[:10]
    changes_today = [h for h in history if h.get("ts", "").startswith(today)]
    return JSONResponse({
        "current": get_max_positions(),
        "min": MAX_POSITIONS_MIN,
        "max": MAX_POSITIONS_MAX,
        "default": MAX_POSITIONS_DEFAULT,
        "last_change_at": mp.get("last_change_at"),
        "auto_reduced": mp.get("auto_reduced", False),
        "changes_today_count": len(changes_today),
        "changes_today": changes_today,
        "recent_history": history[-10:],
    })


@app.post("/api/set_max_pos")
async def api_set_max_pos(req: Request):
    """v6.6 Lite Patch 8: MAX pozisyon limiti değiştir (manuel)
    Body: {"value": 7, "reason": "manuel" (opsiyonel)}"""
    body = {}
    try:
        body = await req.json()
    except Exception:
        pass
    new_val = body.get("value")
    reason = body.get("reason", "manual")
    if new_val is None:
        return JSONResponse({"success": False, "error": "value gerekli"}, status_code=400)
    try:
        new_val = int(new_val)
    except Exception:
        return JSONResponse({"success": False, "error": "value sayı olmalı"}, status_code=400)
    if new_val < MAX_POSITIONS_MIN or new_val > MAX_POSITIONS_MAX:
        return JSONResponse({
            "success": False,
            "error": f"value {MAX_POSITIONS_MIN}-{MAX_POSITIONS_MAX} aralığında olmalı"
        }, status_code=400)
    result = set_max_positions(new_val, reason)
    return JSONResponse({"success": True, **result, "current": get_max_positions()})


@app.post("/api/force_reopen")
async def force_reopen_position(req: Request):
    """
    v6.6 Lite Patch: Dashboard'da yanlışlıkla kapalı gözüken ama Binance'te hala açık olan pozu
    geri açık listesine al. FLUXUSDT state divergence örneğinde kullanılır.
    Body: {"ticker": "FLUXUSDT"}
    """
    body = {}
    try:
        body = await req.json()
    except Exception:
        pass
    ticker = (body.get("ticker") or "").upper()
    if not ticker:
        return JSONResponse({"success": False, "error": "ticker boş"})

    # Binance'te gerçekten açık mı kontrol et
    try:
        positions = client.get_position_risk(symbol=ticker)
        binance_open = False
        binance_qty = 0
        binance_entry = 0
        for p in positions:
            if p["symbol"] == ticker:
                amt = float(p.get("positionAmt", 0))
                if abs(amt) > 0:
                    binance_open = True
                    binance_qty = abs(amt)
                    binance_entry = float(p.get("entryPrice", 0))
                    break
    except Exception as e:
        return JSONResponse({"success": False, "error": f"Binance kontrol hatası: {e}"})

    if not binance_open:
        return JSONResponse({"success": False, "error": f"{ticker} Binance'te zaten kapalı, işlem yapılmadı"})

    # Dashboard'da açıksa bir şey yapma
    if ticker in data.get("open_positions", {}):
        return JSONResponse({"success": False, "error": f"{ticker} dashboard'da zaten açık"})

    # Kapanan listesinde bul — en yenisini al, open_positions'a taşı
    found_closed = None
    for idx in range(len(data.get("closed_positions", [])) - 1, -1, -1):
        if data["closed_positions"][idx].get("ticker") == ticker:
            found_closed = data["closed_positions"][idx]
            break

    if not found_closed:
        # Kapanan listesinde de yok, minimal bilgiyle yeni kayıt oluştur
        reopened = {
            "giris": binance_entry,
            "stop": 0, "tp1": 0, "tp2": 0,
            "marj": 100, "lev": 10, "risk": 5, "kapat_oran": 60, "atr_skor": 1.0,
            "durum": "♻️ Reopen", "hh_pct": 0.0,
            "tp1_hit": False, "tp2_hit": False, "timeout_be": False,
            "tp1_kar": 0.0, "tp2_kar": 0.0, "qty": binance_qty,
            "zaman": now_tr_short(), "zaman_full": now_tr(),
            "reopened_manual": True,
        }
    else:
        # Eski bilgilerle yeniden aç
        reopened = {
            "giris": found_closed.get("giris", binance_entry),
            "stop": found_closed.get("stop_saved", 0),
            "tp1": found_closed.get("tp1_saved", 0),
            "tp2": found_closed.get("tp2_saved", 0),
            "marj": found_closed.get("marj", 100),
            "lev": found_closed.get("lev", 10),
            "risk": 5,
            "kapat_oran": found_closed.get("kapat_oran", 60),
            "atr_skor": found_closed.get("atr_skor", 1.0),
            "durum": "♻️ Reopen",
            "hh_pct": found_closed.get("hh_pct", 0),
            "tp1_hit": found_closed.get("tp1_kar_added", False),
            "tp2_hit": found_closed.get("tp2_kar_added", False),
            "timeout_be": False,
            "tp1_kar": found_closed.get("tp1_kar", 0),
            "tp2_kar": found_closed.get("tp2_kar", 0),
            "qty": binance_qty,
            "zaman": found_closed.get("acilis", now_tr())[-5:] if found_closed.get("acilis") else now_tr_short(),
            "zaman_full": found_closed.get("acilis", now_tr()),
            "reopened_manual": True,
        }
        # Kapanan listesinden sil
        data["closed_positions"].remove(found_closed)

    data["open_positions"][ticker] = reopened
    save_data(data)
    print(f"[FORCE-REOPEN] {ticker} tekrar açık listesine alındı (qty:{binance_qty}, entry:{binance_entry})")
    return JSONResponse({
        "success": True,
        "ticker": ticker,
        "binance_qty": binance_qty,
        "binance_entry": binance_entry,
        "restored_from_closed": found_closed is not None
    })


@app.post("/api/migrate_pnl")
async def migrate_old_pnl(req: Request):
    """
    v6.6 Lite Patch 11: PER-SYMBOL migrate (inspect_pos mantığı).
    Her kapanan poz için ayrı ayrı, symbol+zaman penceresi ile çek.
    Bulk yöntem 1000 limit nedeniyle kayıt ATLIYORDU — bu onu çözer.
    """
    import asyncio
    body = {}
    try:
        body = await req.json()
    except Exception:
        pass
    dry_run = body.get("dry_run", True)

    results = {
        "migrated": [], "skipped": [], "failed": [],
        "dry_run": dry_run, "method": "per_symbol",
        "closed_total": 0, "debug": []
    }
    closed_positions = data.get("closed_positions", [])
    results["closed_total"] = len(closed_positions)
    results["debug"].append(f"closed_positions sayısı: {len(closed_positions)}")

    if not closed_positions:
        return JSONResponse({**results, "msg": "Kapanan pozisyon yok"})

    # Force-refresh opsiyonu: var olan binance_pnl'i bile yeniden hesapla
    force_refresh = bool(body.get("force_refresh", False))
    if force_refresh:
        results["debug"].append("🔄 force_refresh aktif — mevcut binance_pnl'ler de yeniden hesaplanacak")

    processed = 0
    for c in closed_positions:
        ticker = c.get("ticker")
        
        # binance_pnl zaten var VE force_refresh kapalıysa atla
        if c.get("binance_pnl") is not None and not force_refresh:
            results["skipped"].append({"ticker": ticker, "reason": "already_has_binance_pnl"})
            continue

        try:
            acilis = c.get("acilis", "")
            kapanis = c.get("kapanis", "")
            if not acilis:
                results["skipped"].append({"ticker": ticker, "reason": "no_acilis"})
                continue

            dt_open = datetime.strptime(acilis, "%Y-%m-%d %H:%M")
            dt_open_utc = dt_open - timedelta(hours=3)
            pos_start_ms = int(dt_open_utc.timestamp() * 1000) - 60000  # 1dk tampon

            pos_end_ms = None
            if kapanis:
                try:
                    dt_close = datetime.strptime(kapanis, "%Y-%m-%d %H:%M")
                    dt_close_utc = dt_close - timedelta(hours=3)
                    pos_end_ms = int(dt_close_utc.timestamp() * 1000) + 300000  # 5dk tampon
                except Exception:
                    pass

            # PER-SYMBOL sorgu — inspect_pos ile birebir aynı mantık
            # Rate limit: her istek arasında 200ms bekle
            await asyncio.sleep(0.2)

            pnl_kwargs = {"symbol": ticker, "incomeType": "REALIZED_PNL", "limit": 1000}
            if pos_start_ms:
                pnl_kwargs["startTime"] = pos_start_ms
            if pos_end_ms:
                pnl_kwargs["endTime"] = pos_end_ms

            pnl_records = client.get_income_history(**pnl_kwargs) or []

            await asyncio.sleep(0.2)

            fee_kwargs = {"symbol": ticker, "incomeType": "COMMISSION", "limit": 1000}
            if pos_start_ms:
                fee_kwargs["startTime"] = pos_start_ms
            if pos_end_ms:
                fee_kwargs["endTime"] = pos_end_ms

            fee_records = client.get_income_history(**fee_kwargs) or []

            if not pnl_records and not fee_records:
                results["skipped"].append({"ticker": ticker, "reason": "no_binance_record_in_window"})
                continue

            pnl_total = sum(float(r.get("income", 0)) for r in pnl_records)
            fee_total = sum(float(r.get("income", 0)) for r in fee_records)
            net_pnl = pnl_total + fee_total
            old_pnl = c.get("kar", 0)
            old_binance = c.get("binance_pnl")

            if not dry_run:
                c["binance_pnl"] = round(net_pnl, 2)
                c["binance_fee"] = round(fee_total, 2)

            mig_entry = {
                "ticker": ticker,
                "acilis": acilis,
                "dashboard_kar": old_pnl,
                "binance_pnl": round(net_pnl, 2),
                "fark": round(net_pnl - old_pnl, 2),
                "fee": round(fee_total, 2),
                "records": len(pnl_records),
            }
            # Eğer force refresh idiyse, eski binance_pnl'i de göster
            if force_refresh and old_binance is not None:
                mig_entry["old_binance_pnl"] = old_binance
                mig_entry["refresh_change"] = round(net_pnl - old_binance, 2)

            results["migrated"].append(mig_entry)
            processed += 1

        except Exception as e:
            results["failed"].append({"ticker": ticker, "reason": str(e)})

    if not dry_run and results["migrated"]:
        save_data(data)
        print(f"[MIGRATE] {len(results['migrated'])} poz (per_symbol) güncellendi (force_refresh={force_refresh})")

    results["debug"].append(f"İşlenen: {processed}, toplam süre ~{processed*0.4:.1f}sn")
    return JSONResponse(results)


# ============ SHADOW MODE HANDLERS (v6.5 — RAM v14 için) ============
# Shadow pozisyonlar: Binance'e emir göndermez, sadece sanal takip
# Amaç: Yeni stratejileri canlı piyasada risksiz test etmek

SHADOW_FEE_PCT = 0.1  # Taker fee (0.05 giriş + 0.05 çıkış = toplam 0.1%)
SHADOW_SLIPPAGE_PCT = 0.05  # Yaklaşık slippage

def shadow_calc_kar(marj, lev, giris, exit_px, oran_pct):
    """Shadow pozisyon için kar/zarar hesapla (fee + slippage dahil)"""
    if giris <= 0:
        return 0.0
    pos_size = marj * lev
    kapatilan = pos_size * (oran_pct / 100.0)
    ham_kar = kapatilan * (exit_px - giris) / giris
    # Fee ve slippage düş
    fee_cost = kapatilan * (SHADOW_FEE_PCT / 100.0)
    slip_cost = kapatilan * (SHADOW_SLIPPAGE_PCT / 100.0)
    net_kar = ham_kar - fee_cost - slip_cost
    return round(net_kar, 2)

def shadow_handle_giris(msg, system_tag):
    """RAM v14 GIRIS mesajını işle — sanal pozisyon aç"""
    parsed = parse_giris(msg)
    if not parsed:
        return {"status": "parse_error", "shadow": True}
    print(f"[SHADOW PARSE] {parsed}")
    ticker = parsed["ticker"]

    if ticker in data["shadow_positions"]:
        print(f"[SHADOW DUP] {ticker} zaten shadow'da açık")
        return {"status": "duplicate", "shadow": True}

    # v6.6 Lite Patch 10: RAM'e de aynı MAX_POS kısıtı — adil karşılaştırma
    shadow_aktif = len(data.get("shadow_positions", {}))
    max_limit = get_max_positions()
    if shadow_aktif >= max_limit:
        if "shadow_skipped" not in data:
            data["shadow_skipped"] = []
        data["shadow_skipped"].append({
            "ticker": ticker,
            "giris": parsed["giris"],
            "stop": parsed["stop"],
            "tp1": parsed["tp1"],
            "tp2": parsed["tp2"],
            "marj": parsed["marj"],
            "lev": parsed["lev"],
            "risk": parsed["risk"],
            "kapat_oran": parsed["kapat_oran"],
            "atr_skor": parsed["atr_skor"],
            "zaman": now_tr(),
            "sebep": f"Max {max_limit} shadow poz dolu",
            "max_seen": parsed["giris"], "min_seen": parsed["giris"],
            "system": system_tag,
        })
        # Rolling limit
        if len(data["shadow_skipped"]) > 200:
            data["shadow_skipped"] = data["shadow_skipped"][-200:]
        save_data(data)
        print(f"[SHADOW LIMIT] {system_tag} | {ticker} atlandı (aktif:{shadow_aktif}/{max_limit})")
        return {"status": "shadow_skipped_max", "shadow": True, "ticker": ticker, "max": max_limit}

    # Sanal pozisyon kaydı
    data["shadow_positions"][ticker] = {
        "ticker": ticker,
        "system": system_tag,  # "RAM v14" etc
        "giris": parsed["giris"],
        "stop": parsed["stop"],
        "tp1": parsed["tp1"],
        "tp2": parsed["tp2"],
        "marj": parsed["marj"],
        "lev": parsed["lev"],
        "risk": parsed["risk"],
        "kapat_oran": parsed["kapat_oran"],
        "atr_skor": parsed["atr_skor"],
        "rs_spread": parsed.get("rs_spread", 0),
        "zaman": now_tr(),
        "zaman_full": now_tr(),
        "tp1_hit": False,
        "tp2_hit": False,
        "hh_pct": 0,
        "max_seen": 0,
        "min_seen": 0,
        "current_stop": parsed["stop"],
        "tp1_kar": 0,
        "tp2_kar": 0,
    }
    save_data(data)
    print(f"[SHADOW GIRIS] {system_tag} | {ticker} @ {parsed['giris']} | Stop:{parsed['stop']} TP1:{parsed['tp1']} TP2:{parsed['tp2']} | Marj:{parsed['marj']}$ Lev:{parsed['lev']}x")
    return {"status": "shadow_opened", "shadow": True, "ticker": ticker}

def shadow_handle_tp1(msg, system_tag):
    """RAM v14 TP1 mesajını işle — sanal kısmi kapama + stop BE"""
    parsed = parse_tp1(msg)
    if not parsed:
        return {"status": "parse_error", "shadow": True}
    ticker = parsed["ticker"]

    if ticker not in data["shadow_positions"]:
        print(f"[SHADOW] TP1 geldi ama {ticker} shadow'da yok")
        return {"status": "not_found", "shadow": True}

    pos = data["shadow_positions"][ticker]
    if pos.get("tp1_hit"):
        print(f"[SHADOW] TP1 duplicate {ticker}")
        return {"status": "tp1_duplicate", "shadow": True}

    tp1_px = parsed.get("tp1") or pos["tp1"]
    yeni_stop = parsed.get("stop") or pos["giris"]  # Genelde BE = giris
    kapat_oran = parsed.get("kapat_oran", pos.get("kapat_oran", 50))

    # Sanal kar hesapla
    tp1_kar = shadow_calc_kar(pos["marj"], pos["lev"], pos["giris"], tp1_px, kapat_oran)
    pos["tp1_hit"] = True
    pos["tp1_kar"] = tp1_kar
    pos["current_stop"] = yeni_stop
    pos["tp1_time"] = now_tr()

    save_data(data)
    print(f"[SHADOW TP1] {system_tag} | {ticker} @ {tp1_px} | %{kapat_oran} kapat → +{tp1_kar}$ | Stop→{yeni_stop}")
    return {"status": "shadow_tp1", "shadow": True, "kar": tp1_kar}

def shadow_handle_tp2(msg, system_tag):
    """RAM v14 TP2 mesajını işle — sanal kısmi kapama"""
    parsed = parse_tp2(msg)
    if not parsed:
        return {"status": "parse_error", "shadow": True}
    ticker = parsed["ticker"]

    if ticker not in data["shadow_positions"]:
        print(f"[SHADOW] TP2 geldi ama {ticker} shadow'da yok")
        return {"status": "not_found", "shadow": True}

    pos = data["shadow_positions"][ticker]
    if pos.get("tp2_hit"):
        print(f"[SHADOW] TP2 duplicate {ticker}")
        return {"status": "tp2_duplicate", "shadow": True}

    tp2_px = parsed.get("tp2") or pos["tp2"]
    kapat_oran = parsed.get("kapat_oran", 25)

    tp2_kar = shadow_calc_kar(pos["marj"], pos["lev"], pos["giris"], tp2_px, kapat_oran)
    pos["tp2_hit"] = True
    pos["tp2_kar"] = tp2_kar
    pos["current_stop"] = pos["tp1"]  # RAM v14 mantığı: TP2 sonrası stop→TP1 seviyesine
    pos["tp2_time"] = now_tr()

    save_data(data)
    print(f"[SHADOW TP2] {system_tag} | {ticker} @ {tp2_px} | %{kapat_oran} kapat → +{tp2_kar}$ | Stop→{pos['tp1']}")
    return {"status": "shadow_tp2", "shadow": True, "kar": tp2_kar}

def shadow_handle_stop_or_trail(msg, system_tag, kind="STOP"):
    """RAM v14 STOP/TRAIL mesajını işle — sanal tam kapama"""
    # STOP ve TRAIL aynı mantık: kalan pozisyonu kapat
    try:
        parts = [p.strip() for p in msg.split("|")]
        ticker = parts[1].strip()
    except Exception as e:
        print(f"[SHADOW PARSE ERR {kind}] {e}")
        return {"status": "parse_error", "shadow": True}

    if ticker not in data["shadow_positions"]:
        print(f"[SHADOW] {kind} geldi ama {ticker} shadow'da yok")
        return {"status": "not_found", "shadow": True}

    pos = data["shadow_positions"][ticker]
    # Kapanış fiyatı: current_stop (TP1/TP2 sonrası BE/TP1'e çekilmiş olabilir)
    exit_px = pos.get("current_stop", pos["stop"])

    # Kalan pozisyon oranı
    kapat_oran = 100
    if pos.get("tp2_hit"):
        kapat_oran = 100 - pos.get("kapat_oran", 50) - 25  # tp1 sonra tp2 sonra kalan
    elif pos.get("tp1_hit"):
        kapat_oran = 100 - pos.get("kapat_oran", 50)
    # Tp hiç vurmamışsa 100 (tamamını kapat)

    # Kalan pozisyonun kar/zararı (fee+slippage dahil)
    remaining_kar = shadow_calc_kar(pos["marj"], pos["lev"], pos["giris"], exit_px, kapat_oran)

    # Toplam kar: tp1 + tp2 + kalan
    total_kar = round(pos.get("tp1_kar", 0) + pos.get("tp2_kar", 0) + remaining_kar, 2)

    # Sonuç tag (TP2 sonrası stop = trailing kapama mantığında)
    if pos.get("tp2_hit"):
        sonuc = "TP1+TP2+Trail"
        kind = "TRAIL"  # otomatik düzelt — TP2 sonrası stop = trailing
    elif pos.get("tp1_hit"):
        sonuc = "TP1+" + ("Trail" if kind == "TRAIL" else "Stop")
    else:
        sonuc = kind

    closed_rec = {
        **pos,
        "sonuc": sonuc,
        "kar": total_kar,
        "trail_kar": remaining_kar,
        "exit_px": exit_px,
        "kapanis": now_tr(),
        "kind": kind,
    }
    data["shadow_closed"].append(closed_rec)
    del data["shadow_positions"][ticker]

    # Rolling limit — son 200 kapalı shadow pozisyon
    if len(data["shadow_closed"]) > 200:
        data["shadow_closed"] = data["shadow_closed"][-200:]

    save_data(data)
    print(f"[SHADOW {kind}] {system_tag} | {ticker} | {sonuc} | Toplam:{total_kar}$ (tp1:{pos.get('tp1_kar',0)} tp2:{pos.get('tp2_kar',0)} kalan:{remaining_kar})")
    return {"status": "shadow_closed", "shadow": True, "kar": total_kar, "sonuc": sonuc}


@app.post("/webhook")
async def webhook(req: Request):
    msg = (await req.body()).decode()
    print(f"[ALERT] {msg}")
    mode_tag = "[CANLI]" if not TEST_MODE else "[TEST]"

    # === v6.5: RAM v14 SHADOW MODE ===
    # RAM v14 mesajları Binance'e gitmez, sadece sanal takip
    if msg.startswith("RAM v14 |"):
        return shadow_handle_giris(msg, "RAM v14")
    if msg.startswith("RAM v14 TP1 |"):
        return shadow_handle_tp1(msg, "RAM v14")
    if msg.startswith("RAM v14 TP2 |"):
        return shadow_handle_tp2(msg, "RAM v14")
    if msg.startswith("RAM v14 STOP |"):
        # TP2 sonrası stop'sa kind=TRAIL olarak işaretle (handler içinde algılanır)
        # Diğer durumlarda STOP olarak işle
        return shadow_handle_stop_or_trail(msg, "RAM v14", "STOP")

    # === GIRIS ===
    if msg.startswith("CAB v13 |"):
        parsed = parse_giris(msg)
        if not parsed:
            return {"status": "parse_error"}
        print(f"[PARSE] {parsed}")
        ticker = parsed["ticker"]

        # v6.6 Lite Patch 5: Pause kontrolü — bot pause'daysa yeni poz açma
        if is_paused():
            pi = get_pause_info()
            if "skipped_signals" not in data:
                data["skipped_signals"] = []
            data["skipped_signals"].append({
                "ticker": ticker,
                "giris": parsed["giris"],
                "stop": parsed["stop"],
                "tp1": parsed["tp1"],
                "tp2": parsed["tp2"],
                "marj": parsed["marj"],
                "lev": parsed["lev"],
                "risk": parsed.get("risk", 0),
                "kapat_oran": parsed.get("kapat_oran", 50),
                "atr_skor": parsed.get("atr_skor", 1.0),
                "zaman": now_tr(),
                "sebep": f"🛑 BOT PAUSE: {pi.get('reason_text','manuel')}",
                "max_seen": parsed["giris"], "min_seen": parsed["giris"],
                "paused_skip": True,
            })
            save_data(data)
            print(f"{mode_tag} PAUSE: {ticker} sinyali reddedildi — {pi.get('reason')}")
            return {"status": "paused", "reason": pi.get("reason"), "ticker": ticker}

        # v6.1: Akıllı slot — TP1 vurmuş VE timeout-BE'liler exempt
        aktif_risk, gar_tp1, gar_to = count_active_risk()
        garantili = gar_tp1 + gar_to

        if aktif_risk >= get_max_positions():
            if "skipped_signals" not in data:
                data["skipped_signals"] = []
            data["skipped_signals"].append({
                "ticker": ticker,
                "giris": parsed["giris"],
                "stop": parsed["stop"],
                "tp1": parsed["tp1"],
                "tp2": parsed["tp2"],
                "marj": parsed["marj"],
                "lev": parsed["lev"],
                "risk": parsed["risk"],
                "kapat_oran": parsed["kapat_oran"],
                "atr_skor": parsed["atr_skor"],
                "zaman": now_tr(),
                "sebep": f"Max {get_max_positions()} aktif risk dolu (+{garantili} garantili)"
            })
            if len(data["skipped_signals"]) > 50:
                data["skipped_signals"] = data["skipped_signals"][-50:]
            save_data(data)
            print(f"[LIMIT] Aktif risk {aktif_risk}/{get_max_positions()} (+{gar_tp1} TP1 +{gar_to} TO-BE) — {ticker} atlandı (kaydedildi)")
            return {"status": "limit"}
        if ticker in data["open_positions"]:
            print(f"[DUP] {ticker} zaten açık")
            return {"status": "duplicate"}

        trade_result = None
        if not TEST_MODE:
            trade_result = execute_entry(ticker, parsed)
            if not trade_result:
                print(f"[TRADE FAIL] {ticker} giriş başarısız!")
                return {"status": "trade_failed"}

        data["open_positions"][ticker] = {
            "giris": trade_result["avg_price"] if (trade_result and trade_result["avg_price"] > 0) else parsed["giris"],
            "stop": parsed["stop"],
            "tp1": parsed["tp1"], "tp2": parsed["tp2"],
            "marj": parsed["marj"], "lev": parsed["lev"],
            "risk": parsed["risk"], "kapat_oran": parsed["kapat_oran"],
            "atr_skor": parsed["atr_skor"], "durum": "Açık",
            "hh_pct": 0.0, "tp1_hit": False, "tp2_hit": False,
            "timeout_be": False,  # v6.1: yeni flag
            "tp1_kar": 0.0, "tp2_kar": 0.0,
            "qty": trade_result["qty"] if trade_result else 0,
            "sl_order_id": trade_result.get("sl_order_id") if trade_result else None,
            "zaman": now_tr_short(), "zaman_full": now_tr(),
        }
        save_data(data)
        print(f"{mode_tag} GIRIS: {ticker} | {parsed['giris']} | Marj:{parsed['marj']}$ | {parsed['lev']}x")
        return {"status": "opened"}

    # === TP1 ===
    elif msg.startswith("CAB v13 TP1"):
        parsed = parse_tp1(msg)
        if not parsed:
            return {"status": "parse_error"}
        print(f"[PARSE] {parsed}")
        ticker = parsed["ticker"]

        if is_recently_closed(ticker) and ticker not in data["open_positions"]:
            for c in reversed(data["closed_positions"][-20:]):
                if c["ticker"] == ticker and not c.get("tp1_kar_added"):
                    pos_size = c["marj"] * c.get("lev", 10)
                    tp1_kar = round(pos_size * (parsed["kapat_oran"] / 100.0) * (parsed["tp1"] - c["giris"]) / c["giris"], 2)
                    c["kar"] = round(c["kar"] + tp1_kar, 2)
                    c["tp1_kar_added"] = True
                    c["tp1_kar"] = tp1_kar
                    save_data(data)
                    print(f"[RECONCILE] TP1 geç: {ticker} +{tp1_kar}$")
                    return {"status": "reconciled"}
            return {"status": "already_reconciled"}

        if ticker not in data["open_positions"]:
            return {"status": "not_found"}

        pos = data["open_positions"][ticker]

        if not TEST_MODE:
            execute_tp1_close(ticker, pos)

        if parsed["tp1_kar"] == 0:
            parsed["tp1_kar"] = calc_tp1_kar(pos, parsed["tp1"])

        pos["tp1_hit"] = True
        pos["tp1_kar"] = parsed["tp1_kar"]
        pos["stop"] = parsed["stop"]
        pos["durum"] = "✓ TP1 Alındı"
        pos["kapat_oran"] = parsed["kapat_oran"]
        pos["tp1_zaman"] = now_tr()
        # v6.1: TP1 vurursa timeout-BE flag'i temizle (zaten geçildi)
        pos["timeout_be"] = False
        save_data(data)
        print(f"{mode_tag} TP1: {ticker} | +{parsed['tp1_kar']}$")
        return {"status": "tp1"}

    # === TP2 ===
    elif msg.startswith("CAB v13 TP2"):
        parsed = parse_tp2(msg)
        if not parsed:
            return {"status": "parse_error"}
        print(f"[PARSE] {parsed}")
        ticker = parsed["ticker"]

        if is_recently_closed(ticker) and ticker not in data["open_positions"]:
            for c in reversed(data["closed_positions"][-20:]):
                if c["ticker"] == ticker and not c.get("tp2_kar_added"):
                    pos_size = c["marj"] * c.get("lev", 10)
                    tp2_kar = round(pos_size * 0.25 * (parsed["tp2"] - c["giris"]) / c["giris"], 2)
                    c["kar"] = round(c["kar"] + tp2_kar, 2)
                    c["tp2_kar_added"] = True
                    c["tp2_kar"] = tp2_kar
                    if "TP1+TP2" not in c["sonuc"]:
                        c["sonuc"] = "★ TP1+TP2+Trail"
                    save_data(data)
                    print(f"[RECONCILE] TP2 geç: {ticker} +{tp2_kar}$")
                    return {"status": "reconciled"}
            return {"status": "already_reconciled"}

        if ticker not in data["open_positions"]:
            return {"status": "not_found"}

        pos = data["open_positions"][ticker]

        if not TEST_MODE:
            execute_tp2_close(ticker, pos)

        if parsed["tp2_kar"] == 0:
            parsed["tp2_kar"] = calc_tp2_kar(pos, parsed["tp2"])

        pos["tp2_hit"] = True
        pos["tp2_kar"] = parsed["tp2_kar"]
        pos["durum"] = "✓✓ TP2 Alındı"
        save_data(data)
        print(f"{mode_tag} TP2: {ticker} | +{parsed['tp2_kar']}$")
        return {"status": "tp2"}

    # === TRAIL ===
    elif msg.startswith("CAB v13 TRAIL"):
        parsed = parse_trail(msg)
        if not parsed:
            return {"status": "parse_error"}
        print(f"[PARSE] {parsed}")
        ticker = parsed["ticker"]

        if is_recently_closed(ticker):
            print(f"[WARN] TRAIL: {ticker} zaten kapalı")
            return {"status": "already_closed"}
        if ticker not in data["open_positions"]:
            return {"status": "not_found"}

        pos = data["open_positions"][ticker]

        if not TEST_MODE:
            execute_full_close(ticker, "TRAIL")

        trail_kar = parsed["trail_kar"]
        if trail_kar == 0:
            trail_kar = calc_trail_kar(pos, parsed["trail_px"], parsed["tp_type"])

        tp1_kar = pos.get("tp1_kar", 0)
        tp2_kar = pos.get("tp2_kar", 0)
        total_kar = round(tp1_kar + tp2_kar + trail_kar, 2)
        sonuc = "★ TP1+TP2+Trail" if (parsed["tp_type"] == "TP2" or pos.get("tp2_hit")) else "≈ TP1+Trail"

        # v6.6 Lite: Binance'ten gerçek realized PNL çek
        binance_pnl = None
        binance_fee = None
        try:
            acilis_str = pos.get("zaman_full", "")
            if acilis_str:
                dt = datetime.strptime(acilis_str, "%Y-%m-%d %H:%M")
                dt_utc = dt - timedelta(hours=3)
                start_ms = int(dt_utc.timestamp() * 1000) - 60000
                pnl_result = fetch_binance_realized_pnl(ticker, start_ms)
                if pnl_result.get("success") and pnl_result.get("count", 0) > 0:
                    binance_pnl = pnl_result.get("net_pnl")
                    binance_fee = pnl_result.get("fee", 0)
                    print(f"[BINANCE-PNL] {ticker} gerçek PNL: {binance_pnl}$ (dashboard: {total_kar}$)")
        except Exception as e:
            print(f"[BINANCE-PNL ERR] {ticker}: {e}")

        closed = {
            "ticker": ticker, "giris": pos["giris"], "marj": pos["marj"], "lev": pos["lev"],
            "sonuc": sonuc, "kar": total_kar,
            "tp1_kar": tp1_kar, "tp2_kar": tp2_kar, "trail_kar": trail_kar,
            "tp1_kar_added": pos.get("tp1_hit", False), "tp2_kar_added": pos.get("tp2_hit", False),
            "hh_pct": pos.get("hh_pct", 0), "atr_skor": pos.get("atr_skor", 1.0),
            "kapat_oran": pos.get("kapat_oran", 60),
            "acilis": pos.get("zaman_full", ""), "kapanis": now_tr(),
            "binance_pnl": binance_pnl, "binance_fee": binance_fee,  # v6.6 Lite
        }
        data["closed_positions"].append(closed)
        del data["open_positions"][ticker]
        save_data(data)
        print(f"{mode_tag} TRAIL({parsed['tp_type']}): {ticker} | {sonuc} | dashboard:+{total_kar}$ binance:{binance_pnl}$")
        check_auto_pause_triggers()  # v6.6 Lite Patch 5
        check_auto_reduce_max_pos()   # v6.6 Lite Patch 8
        return {"status": "trail_closed"}

    # === STOP ===
    elif msg.startswith("CAB v13 STOP"):
        parsed = parse_stop(msg)
        if not parsed:
            return {"status": "parse_error"}
        print(f"[PARSE] {parsed}")
        ticker = parsed["ticker"]

        if is_recently_closed(ticker):
            return {"status": "already_closed"}
        if ticker not in data["open_positions"]:
            return {"status": "not_found"}

        pos = data["open_positions"][ticker]

        if not TEST_MODE:
            remaining = binance_get_position_qty(ticker)
            if remaining > 0:
                print(f"[TRADE WARN] STOP geldi ama {ticker} hala açık ({remaining} qty), zorla kapatılıyor")
                execute_full_close(ticker, "STOP_FORCE")

        stop_px = parsed["stop"]
        giris = pos["giris"]
        pos_size = pos["marj"] * pos["lev"]
        tp1_kar = pos.get("tp1_kar", 0)
        tp2_kar = pos.get("tp2_kar", 0)
        kapat_oran = pos.get("kapat_oran", 60)

        if pos.get("tp2_hit"):
            kalan = 100 - kapat_oran - 25
            stop_kar = pos_size * (kalan / 100.0) * ((stop_px - giris) / giris)
            sonuc = "↓ TP2+Stop"
        elif pos.get("tp1_hit"):
            kalan = 100 - kapat_oran
            stop_kar = pos_size * (kalan / 100.0) * ((stop_px - giris) / giris)
            sonuc = "~ TP1+BE Stop"
        elif pos.get("timeout_be"):
            # v6.1: Timeout-BE pozisyonu — BE stopta bekliyordu, şimdi tetiklendi
            stop_kar = pos_size * ((stop_px - giris) / giris)
            sonuc = "⏰ Timeout-BE Stop"
        else:
            stop_kar = pos_size * ((stop_px - giris) / giris)
            sonuc = "✗ Stop"

        total_kar = round(tp1_kar + tp2_kar + stop_kar, 2)

        # v6.6 Lite: Binance'ten gerçek realized PNL çek
        binance_pnl = None
        binance_fee = None
        try:
            acilis_str = pos.get("zaman_full", "")
            if acilis_str:
                dt = datetime.strptime(acilis_str, "%Y-%m-%d %H:%M")
                dt_utc = dt - timedelta(hours=3)
                start_ms = int(dt_utc.timestamp() * 1000) - 60000
                pnl_result = fetch_binance_realized_pnl(ticker, start_ms)
                if pnl_result.get("success") and pnl_result.get("count", 0) > 0:
                    binance_pnl = pnl_result.get("net_pnl")
                    binance_fee = pnl_result.get("fee", 0)
                    print(f"[BINANCE-PNL] {ticker} gerçek PNL: {binance_pnl}$ (dashboard: {total_kar}$)")
        except Exception as e:
            print(f"[BINANCE-PNL ERR] {ticker}: {e}")

        closed = {
            "ticker": ticker, "giris": giris, "marj": pos["marj"], "lev": pos["lev"],
            "sonuc": sonuc, "kar": total_kar,
            "tp1_kar": tp1_kar, "tp2_kar": tp2_kar, "trail_kar": round(stop_kar, 2),
            "tp1_kar_added": pos.get("tp1_hit", False), "tp2_kar_added": pos.get("tp2_hit", False),
            "hh_pct": pos.get("hh_pct", 0), "atr_skor": pos.get("atr_skor", 1.0),
            "kapat_oran": kapat_oran,
            "acilis": pos.get("zaman_full", ""), "kapanis": now_tr(),
            "binance_pnl": binance_pnl, "binance_fee": binance_fee,  # v6.6 Lite
        }
        data["closed_positions"].append(closed)
        del data["open_positions"][ticker]
        save_data(data)
        print(f"{mode_tag} STOP: {ticker} | {sonuc} | dashboard:{total_kar}$ binance:{binance_pnl}$")
        check_auto_pause_triggers()  # v6.6 Lite Patch 5
        check_auto_reduce_max_pos()   # v6.6 Lite Patch 8
        return {"status": "stopped"}

    else:
        print(f"[UNKNOWN] {msg[:80]}")
        return {"status": "unknown"}

# ============ TIMEOUT BACKGROUND TASK (v6.1: FIX EDİLDİ) ============
async def timeout_scan_once():
    """v6.1: Tek seferlik timeout taraması — manuel ve background task tarafından çağrılır"""
    scanned = 0
    actioned = []
    errors = []

    try:
        now = now_tr_dt()  # v6.1: NAIVE datetime — strptime ile uyumlu
        for ticker in list(data["open_positions"].keys()):
            pos = data["open_positions"][ticker]

            # TP1 vurmuş veya zaten timeout-BE → atla
            if pos.get("tp1_hit") or pos.get("timeout_be"):
                continue

            scanned += 1

            try:
                zaman_str = pos.get("zaman_full", "")
                if not zaman_str:
                    continue
                acilis = datetime.strptime(zaman_str, "%Y-%m-%d %H:%M")  # naive
                age_hours = (now - acilis).total_seconds() / 3600
            except Exception as e:
                errors.append(f"{ticker}: zaman parse hatası: {e}")
                continue

            if age_hours < TIMEOUT_HOURS:
                continue

            # v6.4: KOŞULLU TIMEOUT — slot baskısına göre karar ver
            # MUTLAK LİMİT (24s+) → ne olursa olsun timeout
            # 12s ≤ yaş < 24s arası → slot baskısı varsa timeout, yoksa skip
            aktif_risk, gar_tp1, gar_to = count_active_risk()
            slot_doluluk_pct = (aktif_risk / get_max_positions()) * 100

            if age_hours < TIMEOUT_ABSOLUTE_HOURS:
                # Henüz mutlak limite gelmedi → slot baskısına bak
                if aktif_risk < TIMEOUT_PRESSURE_THRESHOLD:
                    # Slot baskısı yok → pozisyona şans tanı, timeout YAPMA
                    if scanned == 1:  # İlk kontrol için log yaz, spam etmeyelim
                        print(f"[TIMEOUT-SKIP] {ticker} {age_hours:.1f}s açık ama aktif_risk:{aktif_risk}/{get_max_positions()} (eşik:{TIMEOUT_PRESSURE_THRESHOLD}) — bekle")
                    continue
                else:
                    print(f"[TIMEOUT] {ticker} {age_hours:.1f}s açık | aktif_risk:{aktif_risk}/{get_max_positions()} (eşik:{TIMEOUT_PRESSURE_THRESHOLD}) — slot baskısı, akıllı kapama")
            else:
                print(f"[TIMEOUT] {ticker} {age_hours:.1f}s açık (MUTLAK limit:{TIMEOUT_ABSOLUTE_HOURS}s) — zorla akıllı kapama")

            try:
                action, kar = execute_smart_timeout(ticker, pos)
            except Exception as e:
                errors.append(f"{ticker}: smart timeout hatası: {e}")
                print(f"[TIMEOUT ERR] {ticker}: {e}")
                continue

            if action == 'be':
                # KÂRDA → BE stop'a çekildi, pozisyon devam, slot serbest
                pos["timeout_be"] = True
                pos["timeout_zaman"] = now_tr()
                pos["timeout_kar_initial"] = kar
                pos["stop"] = pos["giris"]  # BE
                pos["durum"] = f"⏰ Timeout-BE (+{kar:.1f}$)"
                save_data(data)
                actioned.append({"ticker": ticker, "action": "BE", "unrealized_kar": kar, "age_hours": round(age_hours, 1)})
                print(f"[TIMEOUT-BE] {ticker} → slot serbest, pozisyon BE'de bekliyor (kâr +{kar:.1f}$)")

            elif action == 'close':
                # ZARARDA veya hata → kapat, kapanan tabloya ekle
                # v6.6 Lite: Binance'ten gerçek realized PNL çek
                binance_pnl = None
                binance_fee = None
                try:
                    acilis_str = pos.get("zaman_full", "")
                    if acilis_str:
                        dt = datetime.strptime(acilis_str, "%Y-%m-%d %H:%M")
                        dt_utc = dt - timedelta(hours=3)
                        start_ms = int(dt_utc.timestamp() * 1000) - 60000
                        pnl_result = fetch_binance_realized_pnl(ticker, start_ms)
                        if pnl_result.get("success") and pnl_result.get("count", 0) > 0:
                            binance_pnl = pnl_result.get("net_pnl")
                            binance_fee = pnl_result.get("fee", 0)
                except Exception as e:
                    print(f"[BINANCE-PNL ERR] {ticker} timeout: {e}")

                closed = {
                    "ticker": ticker, "giris": pos["giris"], "marj": pos["marj"], "lev": pos["lev"],
                    "sonuc": "⏰ Timeout", "kar": round(kar, 2),
                    "tp1_kar": 0, "tp2_kar": 0, "trail_kar": round(kar, 2),
                    "tp1_kar_added": False, "tp2_kar_added": False,
                    "hh_pct": pos.get("hh_pct", 0), "atr_skor": pos.get("atr_skor", 1.0),
                    "kapat_oran": pos.get("kapat_oran", 60),
                    "acilis": pos.get("zaman_full", ""), "kapanis": now_tr(),
                    "binance_pnl": binance_pnl, "binance_fee": binance_fee,  # v6.6 Lite
                }
                data["closed_positions"].append(closed)
                del data["open_positions"][ticker]
                save_data(data)
                actioned.append({"ticker": ticker, "action": "CLOSE", "kar": kar, "age_hours": round(age_hours, 1)})
                print(f"[TIMEOUT-CLOSE] {ticker} → kapatıldı ({kar:+.1f}$)")
                check_auto_pause_triggers()  # v6.6 Lite Patch 5
                check_auto_reduce_max_pos()   # v6.6 Lite Patch 8

    except Exception as e:
        errors.append(f"global: {e}")
        print(f"[TIMEOUT GLOBAL ERR] {e}")

    return {
        "scanned": scanned,
        "actioned": actioned,
        "actioned_count": len(actioned),
        "errors": errors,
        "now_tr": now_tr(),
        "timeout_hours": TIMEOUT_HOURS
    }


async def check_timeouts():
    """v6.1: Background task — sürekli çalışır"""
    while True:
        await asyncio.sleep(TIMEOUT_CHECK_INTERVAL_SEC)
        try:
            result = await timeout_scan_once()
            if result["actioned_count"] > 0 or result["errors"]:
                print(f"[TIMEOUT-SCAN] scanned:{result['scanned']} actioned:{result['actioned_count']} errors:{len(result['errors'])}")
        except Exception as e:
            print(f"[TIMEOUT TASK ERR] {e}")


# ============ HIGH/LOW TRACKER (v6.4) ============
HIGH_LOW_CHECK_INTERVAL_SEC = 60  # Her 1 dakikada bir tarama

def parse_zaman_to_ms(zaman_str):
    """'2026-04-18 10:30' formatını TR saati varsayıp UTC ms'e çevir"""
    try:
        # zaman_full TR saati (UTC+3)
        dt = datetime.strptime(zaman_str, "%Y-%m-%d %H:%M")
        # TR saati → UTC
        dt_utc = dt - timedelta(hours=3)
        return int(dt_utc.replace(tzinfo=timezone.utc).timestamp() * 1000)
    except Exception:
        return None

async def update_position_highs_lows():
    """
    v6.4: Açık pozisyonlar için HH güncelle, kaçırılan sinyaller için max_seen/min_seen güncelle.
    Klines API'den geçmiş bar verilerini çekerek browser bağımsız çalışır.
    """
    while True:
        await asyncio.sleep(HIGH_LOW_CHECK_INTERVAL_SEC)
        try:
            updated_open = 0
            updated_skipped = 0

            # Açık pozisyonlar için
            for ticker in list(data["open_positions"].keys()):
                pos = data["open_positions"][ticker]
                zaman_ms = parse_zaman_to_ms(pos.get("zaman_full", ""))
                if not zaman_ms:
                    continue

                max_high, min_low = get_high_low_since(ticker, zaman_ms)
                if max_high is None:
                    continue

                giris = pos["giris"]
                if giris <= 0:
                    continue

                # HH% güncelle
                hh_pct = (max_high - giris) / giris * 100.0
                old_hh = pos.get("hh_pct", 0)
                if hh_pct > old_hh:
                    pos["hh_pct"] = round(hh_pct, 2)
                    updated_open += 1

                # max_seen ve min_seen kaydet (gerçek poz için, ekstra bilgi)
                pos["max_seen"] = max_high
                pos["min_seen"] = min_low

            # Kaçırılan sinyaller için
            if "skipped_signals" in data:
                for s in data["skipped_signals"]:
                    zaman_ms = parse_zaman_to_ms(s.get("zaman", ""))
                    if not zaman_ms:
                        continue

                    ticker = s.get("ticker")
                    if not ticker:
                        continue

                    max_high, min_low = get_high_low_since(ticker, zaman_ms)
                    if max_high is None:
                        continue

                    # max_seen ve min_seen güncelle
                    old_max = s.get("max_seen", 0)
                    old_min = s.get("min_seen", float('inf'))

                    if max_high > old_max:
                        s["max_seen"] = max_high
                        updated_skipped += 1
                    if min_low < old_min:
                        s["min_seen"] = min_low

                    # Stateful TP/Stop tespiti
                    tp1 = s.get("tp1", 0)
                    tp2 = s.get("tp2", 0)
                    stop = s.get("stop", 0)
                    cur_max = s.get("max_seen", max_high)
                    cur_min = s.get("min_seen", min_low)

                    if not s.get("tp1_hit_seen") and tp1 > 0 and cur_max >= tp1:
                        s["tp1_hit_seen"] = True
                    if not s.get("tp2_hit_seen") and tp2 > 0 and cur_max >= tp2:
                        s["tp2_hit_seen"] = True
                    if not s.get("stop_hit_seen") and stop > 0 and cur_min <= stop:
                        s["stop_hit_seen"] = True

            # v6.5: SHADOW POZİSYONLARI için HH ve otomatik TP/Stop tespiti
            # (Pine alert göndermezse bile high/low'a bakıp durum güncelle)
            shadow_updated = 0
            if "shadow_positions" in data:
                for ticker in list(data["shadow_positions"].keys()):
                    sp = data["shadow_positions"][ticker]
                    zaman_ms = parse_zaman_to_ms(sp.get("zaman_full", ""))
                    if not zaman_ms:
                        continue

                    max_high, min_low = get_high_low_since(ticker, zaman_ms)
                    if max_high is None:
                        continue

                    giris = sp.get("giris", 0)
                    if giris <= 0:
                        continue

                    # HH% güncelle
                    hh_pct = (max_high - giris) / giris * 100.0
                    old_hh = sp.get("hh_pct", 0)
                    if hh_pct > old_hh:
                        sp["hh_pct"] = round(hh_pct, 2)
                        shadow_updated += 1

                    sp["max_seen"] = max_high
                    sp["min_seen"] = min_low

            if updated_open > 0 or updated_skipped > 0 or shadow_updated > 0:
                save_data(data)
                if updated_open > 0:
                    print(f"[HIGH-LOW] Açık poz güncellendi: {updated_open}")
                if updated_skipped > 0:
                    print(f"[HIGH-LOW] Kaçırılan güncellendi: {updated_skipped}")
                if shadow_updated > 0:
                    print(f"[HIGH-LOW] Shadow poz güncellendi: {shadow_updated}")

        except Exception as e:
            print(f"[HIGH-LOW TASK ERR] {e}")


@app.on_event("startup")
async def startup():
    asyncio.create_task(check_timeouts())
    asyncio.create_task(update_position_highs_lows())
    print(f"[BOOT] CAB Bot v6.6 Lite | Mode:{'CANLI' if not TEST_MODE else 'TEST'} | MaxPos:{get_max_positions()} | Timeout:{TIMEOUT_HOURS}s (mutlak:{TIMEOUT_ABSOLUTE_HOURS}s, eşik:{TIMEOUT_PRESSURE_THRESHOLD}) | HL:{HIGH_LOW_CHECK_INTERVAL_SEC}s | RAM Shadow:ON")


# ============ DASHBOARD v6.1 PRO ============
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    mod_badge = "🟡 TEST MODU" if TEST_MODE else "🟢 CANLI MOD"
    mod_text = "TEST" if TEST_MODE else "CANLI"
    mod_color = "#fbbf24" if TEST_MODE else "#4ade80"

    html = f"""<!DOCTYPE html>
<html lang="tr"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CAB Bot v6.6 Lite Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
*{{box-sizing:border-box}}
body{{font-family:-apple-system,system-ui,sans-serif;background:#0f172a;color:#e5e7eb;margin:0;padding:10px}}
h1{{font-size:18px;margin:0 0 4px}}
.badge{{display:inline-block;padding:4px 10px;background:#1e293b;border:1px solid {mod_color};border-radius:4px;font-size:12px;color:{mod_color};font-weight:bold}}
.subtitle{{color:#9ca3af;font-size:11px;margin:4px 0 10px}}
.stats{{display:flex;flex-wrap:wrap;gap:8px;margin:10px 0}}
.stat{{padding:10px 14px;background:#1e293b;border-radius:6px;min-width:85px}}
.stat-val{{font-size:16px;font-weight:bold}}
.stat-lbl{{font-size:10px;color:#9ca3af;margin-top:2px}}
.bugun-box{{background:#312e81;padding:6px 10px;border-radius:4px;margin:8px 0;font-size:12px}}
.section{{background:#1e293b;border-radius:6px;padding:10px;margin:10px 0}}
.section-head{{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:8px}}
.section-head h2{{font-size:14px;margin:0}}
.toolbar{{display:flex;gap:6px;flex-wrap:wrap;align-items:center;font-size:11px}}
.toolbar label{{color:#9ca3af}}
.toolbar input,.toolbar select{{background:#0f172a;color:#e5e7eb;border:1px solid #334155;padding:4px 8px;border-radius:4px;font-size:11px}}
.btn{{background:#1e40af;color:#fff;border:none;padding:5px 10px;border-radius:4px;cursor:pointer;font-size:11px;font-weight:600}}
.btn:hover{{background:#2563eb}}
.btn-alt{{background:#059669}}.btn-alt:hover{{background:#047857}}
.btn-danger{{background:#7c2d12}}.btn-danger:hover{{background:#991b1b}}
.btn-warn{{background:#b45309}}.btn-warn:hover{{background:#92400e}}
table{{width:100%;border-collapse:collapse;font-size:11px}}
th{{background:#334155;padding:6px 4px;text-align:left;position:sticky;top:0;cursor:pointer;user-select:none;white-space:nowrap}}
th:hover{{background:#475569}}
th.sorted-asc::after{{content:" ↑";color:#fbbf24}}
th.sorted-desc::after{{content:" ↓";color:#fbbf24}}
td{{padding:5px 4px;border-bottom:1px solid #334155;white-space:nowrap}}
tr:hover{{background:#334155}}
.warn-row{{background:rgba(239,68,68,0.08)!important}}
.ozet{{padding:6px 10px;background:#0f172a;border-radius:4px;margin:6px 0;font-size:12px}}
small{{color:#9ca3af}}
.warn-box{{background:rgba(239,68,68,0.12);border-left:3px solid #f87171;padding:6px 10px;margin-bottom:4px;border-radius:3px;font-size:11px}}
.modal-overlay{{display:none;position:fixed;inset:0;background:rgba(0,0,0,0.7);z-index:1000;justify-content:center;align-items:center;padding:20px}}
.modal-overlay.show{{display:flex}}
.modal{{background:#1e293b;border-radius:8px;padding:20px;max-width:720px;width:100%;max-height:90vh;overflow-y:auto}}
.modal-head{{display:flex;justify-content:space-between;align-items:center;margin-bottom:14px}}
.modal-head h2{{font-size:16px;margin:0}}
.modal-close{{background:#475569;color:#fff;border:none;border-radius:50%;width:28px;height:28px;cursor:pointer;font-size:16px}}
.stats-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:8px;margin-bottom:14px}}
.stat-card{{background:#0f172a;padding:10px;border-radius:6px}}
.stat-card-lbl{{font-size:10px;color:#9ca3af;text-transform:uppercase}}
.stat-card-val{{font-size:18px;font-weight:bold;margin-top:3px}}
.chart-wrap{{background:#0f172a;padding:10px;border-radius:6px;margin-bottom:10px}}
.chart-wrap h3{{font-size:12px;margin:0 0 8px;color:#9ca3af}}
.toast{{position:fixed;bottom:20px;right:20px;background:#059669;color:#fff;padding:10px 16px;border-radius:6px;font-weight:600;font-size:12px;opacity:0;transition:opacity 0.3s;z-index:2000}}
.toast.show{{opacity:1}}.toast.err{{background:#991b1b}}
@media(max-width:640px){{.stat{{min-width:70px;padding:8px 10px}}.stat-val{{font-size:14px}}table{{font-size:10px}}td,th{{padding:4px 3px}}}}
</style>
</head>
<body>

<h1>🤖 CAB Bot v6.6 Lite Dashboard</h1>
<div>
  <span class="badge">{mod_badge}</span>
  <small style="color:#9ca3af">MAX:<span id="maxPosDisplay" style="font-weight:700;color:#4ade80">{get_max_positions()}</span> aktif | Timeout:{TIMEOUT_HOURS}s→{TIMEOUT_ABSOLUTE_HOURS}s (koşullu: aktif≥{TIMEOUT_PRESSURE_THRESHOLD} ise akıllı kapama)</small>
  <button class="btn btn-warn" style="margin-left:8px;padding:3px 8px;font-size:10px" onclick="requestNotif()">🔔 Bildirim İzni</button>
  <button class="btn" style="padding:3px 8px;font-size:10px" onclick="manualTimeoutCheck()">⏰ Timeout Kontrol</button>
  <!-- v6.6 Lite Patch 8: MAX POS +/- Kontrol -->
  <span style="margin-left:8px;padding:3px 6px;background:#1e293b;border-radius:6px;font-size:11px;border:1px solid #475569">
    <span style="color:#9ca3af">MAX:</span>
    <button class="btn" style="padding:1px 6px;font-size:11px;background:#475569" onclick="changeMaxPos(-1)">−</button>
    <b id="maxPosBtnVal" style="color:#4ade80;padding:0 6px;min-width:16px;display:inline-block;text-align:center">7</b>
    <button class="btn" style="padding:1px 6px;font-size:11px;background:#475569" onclick="changeMaxPos(1)">+</button>
    <small id="maxPosSubtext" style="color:#9ca3af;margin-left:4px"></small>
  </span>
  <button class="btn" style="padding:3px 8px;font-size:10px;background:#0891b2;margin-left:8px" onclick="downloadReport()">📊 Rapor</button>
  <button id="pauseBtn" class="btn" style="padding:3px 8px;font-size:10px;background:#dc2626;margin-left:8px" onclick="togglePause()">🛑 BOT'U DURDUR</button>
</div>
<div class="subtitle">⟳ Son güncelleme: <span id="lastUpdate">—</span> <small>(5sn fiyat / 20sn sayfa)</small></div>

<!-- v6.6 Lite Patch 5: PAUSE / KILL SWITCH BANNER -->
<div id="pauseBanner" style="display:none;background:linear-gradient(90deg,#7f1d1d,#991b1b);border:2px solid #dc2626;border-radius:8px;padding:14px 18px;margin:14px 0;color:#fecaca;font-size:13px;box-shadow:0 0 20px rgba(220,38,38,0.3)">
  <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px">
    <div>
      <b style="color:#fff;font-size:15px">🛑 BOT DURDURULDU</b>
      <div id="pauseBannerReason" style="margin-top:4px;color:#fecaca">—</div>
      <small id="pauseBannerTime" style="color:#fca5a5">—</small>
    </div>
    <button class="btn" style="background:#16a34a;font-size:13px;padding:8px 16px" onclick="togglePause()">▶️ BOT'U BAŞLAT</button>
  </div>
  <small style="display:block;margin-top:8px;color:#fca5a5">💡 Mevcut açık pozisyonlar dokunulmadı. Sadece yeni sinyaller kabul edilmiyor.</small>
</div>

<!-- v6.5: SİSTEM SEKMELERI — CAB CANLI vs RAM SHADOW -->
<div id="systemTabs" style="display:flex;gap:8px;margin:14px 0 18px;border-bottom:2px solid #334155">
  <button id="tabCab" onclick="switchSystem('cab')" style="flex:1;padding:14px;border:none;border-radius:8px 8px 0 0;cursor:pointer;font-size:14px;font-weight:700;background:#16a34a;color:white;border-bottom:3px solid #4ade80;transition:all 0.2s">
    🤖 <span style="display:inline-block">CAB v13 — CANLI</span> <span id="tabCabBadge" style="background:rgba(0,0,0,0.3);padding:2px 8px;border-radius:10px;font-size:11px;margin-left:6px">0</span>
  </button>
  <button id="tabRam" onclick="switchSystem('ram')" style="flex:1;padding:14px;border:none;border-radius:8px 8px 0 0;cursor:pointer;font-size:14px;font-weight:700;background:#1e293b;color:#94a3b8;border-bottom:3px solid transparent;transition:all 0.2s">
    🌓 <span style="display:inline-block">RAM v14 — SHADOW</span> <span id="tabRamBadge" style="background:rgba(0,0,0,0.3);padding:2px 8px;border-radius:10px;font-size:11px;margin-left:6px">0</span>
  </button>
</div>

<!-- v6.6 Lite Patch: CAB vs RAM YAN YANA KARŞILAŞTIRMA -->
<div id="compareBox" style="display:none;background:linear-gradient(135deg,#1e293b,#0f172a);border:1px solid #475569;border-radius:8px;padding:12px 16px;margin-bottom:14px;font-size:12px">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
    <b style="color:#a78bfa;font-size:13px">⚖️ CAB (Canlı) vs RAM (Shadow) — Karşılaştırma</b>
    <small style="color:#9ca3af">bugün / son 7g</small>
  </div>
  <div id="compareContent" style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
    <div style="background:rgba(22,163,74,0.1);border-left:3px solid #16a34a;padding:8px 12px;border-radius:4px">
      <div style="color:#4ade80;font-weight:700;font-size:11px;margin-bottom:4px">🤖 CAB v13 — CANLI</div>
      <div id="compareCabStats" style="line-height:1.8;color:#e5e7eb">Yükleniyor...</div>
    </div>
    <div style="background:rgba(168,139,250,0.1);border-left:3px solid #a78bfa;padding:8px 12px;border-radius:4px">
      <div style="color:#c4b5fd;font-weight:700;font-size:11px;margin-bottom:4px">🌓 RAM v14 — SHADOW</div>
      <div id="compareRamStats" style="line-height:1.8;color:#e5e7eb">Veri bekliyor...</div>
    </div>
  </div>
  <div id="compareVerdict" style="margin-top:8px;padding:6px 10px;background:rgba(0,0,0,0.2);border-radius:4px;text-align:center;font-size:11px;color:#9ca3af"></div>
</div>

<!-- v6.6 Lite Patch: BINANCE DIVERGENCE UYARI -->
<div id="divergenceAlert" style="display:none;background:rgba(239,68,68,0.15);border:1px solid #dc2626;border-left:4px solid #dc2626;border-radius:6px;padding:10px 14px;margin-bottom:14px;font-size:12px">
  <b style="color:#fca5a5">⚠️ Binance Senkron Uyarısı</b>
  <div id="divergenceContent" style="margin-top:6px;color:#e5e7eb"></div>
</div>

<!-- ============ CAB MODE (CANLI) ============ -->
<div id="cabMode">

<div class="stats" id="statsBar"></div>
<div class="bugun-box" id="bugunBox"></div>

<div class="section">
  <div class="section-head">
    <h2>📊 Açık Pozisyonlar <small id="openCount"></small></h2>
    <div class="toolbar">
      <input type="text" id="searchOpen" placeholder="Coin ara..." oninput="renderOpen()">
      <button class="btn" onclick="exportCSV('open')">📥 CSV</button>
    </div>
  </div>
  <div id="warnBox"></div>
  <div style="overflow-x:auto;">
    <table id="openTable"><thead><tr>
      <th data-sort="ticker">Coin</th><th data-sort="marj">Marjin</th><th data-sort="giris">Giriş</th>
      <th data-sort="px">Şu An</th><th data-sort="tp1kalan">TP1'e Kalan</th><th data-sort="stopkalan">Stop'a Kalan</th><th data-sort="pct">Durum</th>
      <th data-sort="hh_pct">HH%</th><th data-sort="atr_skor">ATR/Kapat</th><th data-sort="trail">Trailing</th>
      <th data-sort="stop">Stop</th><th data-sort="tp1">TP1</th><th data-sort="tp2">TP2</th><th data-sort="zaman_full">Zaman</th>
    </tr></thead><tbody id="openBody"><tr><td colspan="14" style="text-align:center;color:#9ca3af">Yükleniyor...</td></tr></tbody></table>
  </div>
</div>

<div class="section">
  <div class="section-head">
    <h2>📋 Kapanan Pozisyonlar <small id="closedCount"></small></h2>
    <div class="toolbar">
      <label>Tarih:</label>
      <select id="filterDate" onchange="renderClosed()">
        <option value="all">Tüm Zamanlar</option><option value="today" selected>Bugün</option>
        <option value="yesterday">Dün</option><option value="7d">Son 7 Gün</option><option value="30d">Son 30 Gün</option>
      </select>
      <label>Sonuç:</label>
      <select id="filterResult" onchange="renderClosed()">
        <option value="all">Hepsi</option><option value="stop">✗ Stop</option>
        <option value="tp1trail">≈ TP1+Trail</option><option value="tp2trail">★ TP1+TP2+Trail</option>
        <option value="be">~ TP1+BE</option><option value="timeout">⏰ Timeout</option>
      </select>
      <input type="text" id="searchClosed" placeholder="Coin ara..." oninput="renderClosed()">
      <button class="btn" onclick="showAnalysis()">📊 Analiz</button>
      <button class="btn btn-alt" onclick="exportCSV('closed')">📥 CSV</button>
      <button class="btn" style="background:#7c3aed" onclick="migratePnl()">🔥 Binance PNL Çek</button>
      <button class="btn btn-danger" onclick="clearOld()">🗑 30g+ Sil</button>
    </div>
  </div>
  <div class="ozet" id="closedOzet"></div>
  <div style="overflow-x:auto;">
    <table id="closedTable"><thead><tr>
      <th data-sort="ticker">Coin</th><th data-sort="marj">Marjin</th><th data-sort="giris">Giriş</th>
      <th data-sort="sonuc">Sonuç</th><th data-sort="kar">Dashboard K/Z</th><th data-sort="binance_pnl">🔥 Binance K/Z</th><th data-sort="hh_pct">HH%</th>
      <th data-sort="atr_skor">ATR/Kapat</th><th data-sort="sure_dk">Süre</th><th data-sort="kapanis">Zaman</th>
    </tr></thead><tbody id="closedBody"><tr><td colspan="10" style="text-align:center;color:#9ca3af">Yükleniyor...</td></tr></tbody></table>
  </div>
</div>

<!-- KAÇIRILAN SİNYALLER -->
<div class="section" id="skippedSection" style="display:none">
  <div class="section-head">
    <h2>⏭ Kaçırılan Sinyaller <small id="skippedCount"></small></h2>
    <div class="toolbar">
      <label>Tarih:</label>
      <select id="filterSkippedDate" onchange="renderSkipped()">
        <option value="all">Tüm Zamanlar</option>
        <option value="today" selected>Bugün</option>
        <option value="yesterday">Dün</option>
        <option value="7d">Son 7 Gün</option>
        <option value="30d">Son 30 Gün</option>
      </select>
      <label>Durum:</label>
      <select id="filterSkipped" onchange="renderSkipped()">
        <option value="all">Hepsi</option>
        <option value="tp">✓ TP Vurmuş</option>
        <option value="stop">✗ Stop Olmuş</option>
        <option value="active">⏳ Hala Aktif</option>
      </select>
      <button class="btn btn-danger" onclick="clearSkipped()">🗑 Temizle</button>
    </div>
  </div>
  <div class="ozet" id="skippedOzet"></div>
  <div style="overflow-x:auto;">
    <table id="skippedTable"><thead><tr>
      <th>Coin</th><th>Marjin</th><th>Sinyal Fiyat</th><th>Şu An</th>
      <th>Durum</th><th>TP1'e Kalan</th><th>Sanal Kar/Zarar</th>
      <th>R:R</th><th>ATR</th><th>Zaman</th>
    </tr></thead><tbody id="skippedBody"></tbody></table>
  </div>
</div>

<!-- v6.5: RAM SHADOW MODE -->
</div>
<!-- ============ /CAB MODE ============ -->

<!-- ============ RAM MODE (SHADOW) ============ -->
<div id="ramMode" style="display:none">

<!-- RAM özet kart -->
<div class="stats" id="ramStatsBar"></div>

<div class="section" id="shadowSection" style="display:block !important">
  <div class="section-head">
    <h2>📊 Sanal Pozisyonlar <small id="shadowCount" style="color:#fb923c"></small></h2>
    <div class="toolbar">
      <button class="btn" onclick="switchShadowTab('open')" id="shadowTabOpen">Açık</button>
      <button class="btn" onclick="switchShadowTab('closed')" id="shadowTabClosed">Kapanan</button>
      <button class="btn btn-danger" onclick="clearShadow()">🗑 Temizle</button>
    </div>
  </div>
  <div class="ozet" id="shadowOzet" style="background:#431407;border-left:3px solid #fb923c"></div>
  <div id="shadowOpenView" style="overflow-x:auto;">
    <table id="shadowOpenTable"><thead><tr>
      <th>Coin</th><th>Giriş</th><th>Şu An</th><th>HH%</th>
      <th>Stop</th><th>TP1 / TP2</th><th>Durum</th>
      <th>Sanal Kar</th><th>RS%</th><th>Zaman</th>
    </tr></thead><tbody id="shadowOpenBody"><tr><td colspan="10" style="text-align:center;color:#9ca3af">Henüz RAM sinyali gelmedi</td></tr></tbody></table>
  </div>
  <div id="shadowClosedView" style="overflow-x:auto;display:none">
    <table id="shadowClosedTable"><thead><tr>
      <th>Coin</th><th>Sonuç</th><th>Giriş</th><th>Çıkış</th>
      <th>Sanal Kar</th><th>TP1$ / TP2$ / Trail$</th>
      <th>RS%</th><th>Açılış</th><th>Kapanış</th>
    </tr></thead><tbody id="shadowClosedBody"><tr><td colspan="9" style="text-align:center;color:#9ca3af">Henüz kapalı shadow pozisyonu yok</td></tr></tbody></table>
  </div>
</div>

</div>
<!-- ============ /RAM MODE ============ -->

<div class="modal-overlay" id="analysisModal" onclick="if(event.target.id=='analysisModal')closeModal()">
  <div class="modal">
    <div class="modal-head"><h2>📊 Performans Analizi</h2><button class="modal-close" onclick="closeModal()">×</button></div>
    <div id="analysisBody"></div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
const MODE="{mod_text}",MC="{mod_color}";
let openPositions={{}},closedPositions=[],skippedSignals=[],openPrices={{}},lastOpenState={{}};
let shadowPositions={{}},shadowClosed=[],shadowPrices={{}};let shadowTab='open';
let currentSystem='cab'; // 'cab' veya 'ram' — aktif sekme
let sortState={{open:{{col:null,dir:'asc'}},closed:{{col:'kapanis',dir:'desc'}}}};

async function loadData(){{loadPauseState();loadMaxPosState();try{{const r=await fetch('/api/data');const d=await r.json();openPositions=d.open_positions||{{}};closedPositions=d.closed_positions||[];skippedSignals=d.skipped_signals||[];shadowPositions=d.shadow_positions||{{}};shadowClosed=d.shadow_closed||[];renderAll();detectChanges()}}catch(e){{console.error(e)}}}}

async function fp(sym){{try{{const r=await fetch('https://fapi.binance.com/fapi/v1/ticker/price?symbol='+sym);if(!r.ok)return null;return parseFloat((await r.json()).price)}}catch(e){{return null}}}}

let skippedUpdateCounter=0;
async function updatePrices(){{for(const sym of Object.keys(openPositions)){{const px=await fp(sym);if(px===null)continue;openPrices[sym]=px;try{{await fetch('/update_hh',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{ticker:sym,price:px}})}})}}catch(e){{}}}}renderOpen();
if(skippedUpdateCounter===0||skippedUpdateCounter%6===0){{if(skippedSignals.length>0)updateSkippedPrices();if(Object.keys(shadowPositions).length>0)updateShadowPrices()}}
skippedUpdateCounter++;
document.getElementById('lastUpdate').textContent=new Date().toTimeString().slice(0,8);setTimeout(updatePrices,5000)}}

function now_tr(){{return new Date().toISOString().replace('T',' ').slice(0,16)}}
function sureDk(c){{try{{const a=new Date(c.acilis.replace(' ','T')+':00+03:00'),k=new Date(c.kapanis.replace(' ','T')+':00+03:00');return Math.round((k-a)/60000)}}catch(e){{return 0}}}}
function sureFmt(dk){{if(dk>=1440)return Math.floor(dk/1440)+'g '+Math.floor((dk%1440)/60)+'s';if(dk>=60)return Math.floor(dk/60)+'s '+(dk%60)+'dk';return dk+'dk'}}
function dateMatches(k,f){{if(f==='all')return true;const today=now_tr().slice(0,10);if(f==='today')return k.startsWith(today);const d=new Date();if(f==='yesterday'){{d.setDate(d.getDate()-1);return k.startsWith(d.toISOString().slice(0,10))}}if(f==='7d'){{d.setDate(d.getDate()-7);return k>=d.toISOString().slice(0,16).replace('T',' ')}}if(f==='30d'){{d.setDate(d.getDate()-30);return k>=d.toISOString().slice(0,16).replace('T',' ')}}return true}}
function resultMatches(s,f){{if(f==='all')return true;if(f==='stop')return s==='✗ Stop';if(f==='tp1trail')return s.includes('TP1+Trail')&&!s.includes('TP2');if(f==='tp2trail')return s.includes('TP1+TP2');if(f==='be')return s.includes('BE');if(f==='timeout')return s.includes('Timeout');return true}}
function openTV(t){{window.open('https://www.tradingview.com/chart/?symbol=BINANCE:'+t+'.P','_blank')}}
function toast(msg,err){{const t=document.getElementById('toast');t.textContent=msg;t.className='toast show'+(err?' err':'');setTimeout(()=>t.classList.remove('show'),2500)}}

async function manualTimeoutCheck(){{
toast('Timeout taraması başlatıldı...');
try{{const r=await fetch('/api/timeout_check');const j=await r.json();
if(j.actioned_count>0){{const summary=j.actioned.map(a=>`${{a.ticker}}: ${{a.action}}`).join(', ');toast(`✓ ${{j.actioned_count}} aksiyon: ${{summary}}`);loadData()}}
else{{toast(`✓ Tarandı: ${{j.scanned}} poz, hiçbiri timeout aşmadı`)}}
}}catch(e){{toast('Hata: '+e.message,true)}}}}

function renderAll(){{renderStats();renderOpen();renderClosed();renderSkipped();renderShadow();updateTabBadges();if(currentSystem==='ram')renderRamStatsBar();checkWarnings();renderCompare();detectDivergence();renderPauseState();renderMaxPosState()}}

function renderCompare(){{
  const box=document.getElementById('compareBox');
  if(!box)return;
  // Sadece veriler varsa göster
  const cabOpen=Object.keys(openPositions||{{}}).length;
  const ramOpen=Object.keys(shadowPositions||{{}}).length;
  const cabClosedAll=closedPositions||[];
  const ramClosedAll=shadowClosed||[];
  if(cabClosedAll.length===0 && ramClosedAll.length===0 && cabOpen===0 && ramOpen===0){{box.style.display='none';return}}
  box.style.display='block';

  const today=now_tr().slice(0,10);
  const cabBugun=cabClosedAll.filter(c=>c.kapanis&&c.kapanis.startsWith(today));
  const ramBugun=ramClosedAll.filter(c=>c.kapanis&&c.kapanis.startsWith(today));

  // Son 7 gün
  const d7=new Date();d7.setDate(d7.getDate()-7);
  const d7str=d7.toISOString().slice(0,16).replace('T',' ');
  const cab7=cabClosedAll.filter(c=>c.kapanis&&c.kapanis>=d7str);
  const ram7=ramClosedAll.filter(c=>c.kapanis&&c.kapanis>=d7str);

  // CAB stats — Binance PNL varsa onu kullan
  const cabKar=(arr)=>arr.reduce((s,c)=>s+(c.binance_pnl!=null?c.binance_pnl:c.kar),0);
  const cabWR=(arr)=>arr.length?((arr.filter(c=>(c.binance_pnl!=null?c.binance_pnl:c.kar)>0).length/arr.length)*100).toFixed(0):0;

  // RAM stats — shadow kar zaten net
  const ramKar=(arr)=>arr.reduce((s,c)=>s+(c.kar||0),0);
  const ramWR=(arr)=>arr.length?((arr.filter(c=>c.kar>0).length/arr.length)*100).toFixed(0):0;

  const fmtKar=(v)=>{{const c=v>0?'#4ade80':(v<0?'#f87171':'#e5e7eb');return`<span style="color:${{c}};font-weight:700">${{v>=0?'+':''}}${{v.toFixed(1)}}$</span>`}};
  const fmtWR=(v)=>{{const c=v>=50?'#4ade80':'#f87171';return`<span style="color:${{c}}">${{v}}%</span>`}};

  const cabBugunKar=cabKar(cabBugun),cabBugunWR=cabWR(cabBugun);
  const cab7Kar=cabKar(cab7),cab7WR=cabWR(cab7);
  const ramBugunKar=ramKar(ramBugun),ramBugunWR=ramWR(ramBugun);
  const ram7Kar=ramKar(ram7),ram7WR=ramWR(ram7);

  document.getElementById('compareCabStats').innerHTML=
    `<div>Açık: <b>${{cabOpen}}</b> poz</div>`+
    `<div>Bugün: <b>${{cabBugun.length}}</b> kapandı | WR:${{fmtWR(cabBugunWR)}} | ${{fmtKar(cabBugunKar)}}</div>`+
    `<div>Son 7g: <b>${{cab7.length}}</b> kapandı | WR:${{fmtWR(cab7WR)}} | ${{fmtKar(cab7Kar)}}</div>`;

  document.getElementById('compareRamStats').innerHTML=ramClosedAll.length===0 && ramOpen===0
    ? `<div style="color:#9ca3af">⏳ RAM v14 henüz sinyal göndermedi. Alarm aktif, bekleniyor...</div>`
    : `<div>Açık: <b>${{ramOpen}}</b> sanal poz</div>`+
      `<div>Bugün: <b>${{ramBugun.length}}</b> kapandı | WR:${{fmtWR(ramBugunWR)}} | ${{fmtKar(ramBugunKar)}}</div>`+
      `<div>Son 7g: <b>${{ram7.length}}</b> kapandı | WR:${{fmtWR(ram7WR)}} | ${{fmtKar(ram7Kar)}}</div>`;

  // Verdict
  const v=document.getElementById('compareVerdict');
  if(ramClosedAll.length<3){{
    v.innerHTML=`<span style="color:#9ca3af">RAM için yeterli veri yok (${{ramClosedAll.length}} poz) — en az 10+ poz birikince anlamlı karşılaştırma yapılır</span>`;
  }} else {{
    const farkBugun=ramBugunKar-cabBugunKar;
    const fark7=ram7Kar-cab7Kar;
    const yonder=fark7>5?'🌓 RAM ÖNDE':(fark7<-5?'🤖 CAB ÖNDE':'⚖️ Başa baş');
    const c=fark7>5?'#a78bfa':(fark7<-5?'#4ade80':'#9ca3af');
    v.innerHTML=`Son 7g farkı: <b style="color:${{c}}">${{yonder}}</b> (RAM - CAB: ${{fark7>=0?'+':''}}${{fark7.toFixed(1)}}$)`;
  }}
}}

function detectDivergence(){{
  // Bu frontend-only heuristic bir tespit: son 2 saat içinde "Timeout +0.0$" olarak kapanmış bir poz var mı?
  const alert=document.getElementById('divergenceAlert');
  if(!alert)return;
  const suspects=[];
  const now=Date.now();
  for(const c of (closedPositions||[]).slice(-15)){{
    if(!c.kapanis)continue;
    try{{
      const kapanis=new Date(c.kapanis.replace(' ','T')+':00+03:00');
      const hoursAgo=(now-kapanis.getTime())/3600000;
      if(hoursAgo>24)continue;
      // Şüpheli: Timeout sonuçlu + kar neredeyse 0 (+- 0.5$) + binance_pnl yok
      if(c.sonuc&&c.sonuc.includes('Timeout')&&Math.abs(c.kar||0)<0.5&&c.binance_pnl==null){{
        suspects.push(c.ticker);
      }}
    }}catch(e){{}}
  }}
  if(suspects.length===0){{alert.style.display='none';return}}
  alert.style.display='block';
  const uniqueList=[...new Set(suspects)];
  document.getElementById('divergenceContent').innerHTML=
    `Son 24 saatte <b>${{uniqueList.length}} pozisyon</b> "Timeout +0$" olarak kapanmış — Binance'te hala açık olabilir!<br>`+
    `Şüpheli: ${{uniqueList.map(t=>`<code style="background:#0f172a;padding:2px 6px;border-radius:3px;margin:0 2px">${{t}}</code>`).join('')}}<br>`+
    `<small style="color:#9ca3af">Binance'te kontrol et. Hala açıksa:</small> `+
    uniqueList.map(t=>`<button class="btn" style="padding:3px 8px;font-size:10px;background:#dc2626;margin:2px" onclick="forceReopen('${{t}}')">♻️ ${{t}} Geri Aç</button>`).join('');
}}

let skippedPrices={{}};
async function updateSkippedPrices(){{
const promises=skippedSignals.map(async s=>{{const px=await fp(s.ticker);if(px!==null)skippedPrices[s.ticker]=px;return{{sym:s.ticker,px}}}});
try{{await Promise.all(promises)}}catch(e){{console.error('[skipped fetch err]',e)}}
renderSkipped()}}

function renderSkipped(){{
const sec=document.getElementById('skippedSection');
if(!skippedSignals.length){{sec.style.display='none';return}}
sec.style.display='block';
const filter=document.getElementById('filterSkipped').value;

let rows=skippedSignals.slice().reverse().map(s=>{{
const px=skippedPrices[s.ticker]||null;
const giris=s.giris,stop=s.stop,tp1=s.tp1,tp2=s.tp2;
let status='active',statusTxt='⏳ Aktif',statusColor='#fbbf24';
let sanalKar=0;
const posSize=s.marj*s.lev;

// v6.4: Önce backend flag'leri kontrol et (stateful — geri dönse bile işaretli kalır)
const tp2HitSeen=s.tp2_hit_seen===true;
const tp1HitSeen=s.tp1_hit_seen===true;
const stopHitSeen=s.stop_hit_seen===true;
const maxSeen=s.max_seen||null;

if(tp2HitSeen){{
status='tp2';statusTxt='★ TP2 Vurmuş!';statusColor='#14b8a6';
const cur=px||maxSeen||tp2;
sanalKar=posSize*0.5*((tp1-giris)/giris)+posSize*0.25*((tp2-giris)/giris)+posSize*0.25*((cur-giris)/giris);
}}else if(tp1HitSeen){{
status='tp1';statusTxt='✓ TP1 Vurmuş';statusColor='#84cc16';
const cur=px||maxSeen||tp1;
sanalKar=posSize*0.5*((tp1-giris)/giris)+posSize*0.5*((cur-giris)/giris);
}}else if(stopHitSeen){{
status='stop';statusTxt='✗ Stop Olmuş';statusColor='#f87171';
sanalKar=posSize*((stop-giris)/giris);
}}else if(px){{
// Backend henüz işaretlemediyse anlık fiyatla kontrol (geçici)
if(px<=stop){{status='stop';statusTxt='✗ Stop Olmuş';statusColor='#f87171';sanalKar=posSize*((stop-giris)/giris)}}
else if(px>=tp2){{status='tp2';statusTxt='★ TP2 Vurmuş!';statusColor='#14b8a6';sanalKar=posSize*0.5*((tp1-giris)/giris)+posSize*0.25*((tp2-giris)/giris)+posSize*0.25*((px-giris)/giris)}}
else if(px>=tp1){{status='tp1';statusTxt='✓ TP1 Vurmuş';statusColor='#84cc16';sanalKar=posSize*0.5*((tp1-giris)/giris)+posSize*0.5*((px-giris)/giris)}}
else{{sanalKar=posSize*((px-giris)/giris);if(sanalKar>0)statusTxt='📈 Kârda';else statusTxt='📉 Zararda'}}
}}
const rr=stop>0?((tp1-giris)/(giris-stop)).toFixed(1):'—';
const tp1kalan=px?((tp1-px)/px*100):null;
return{{...s,px,status,statusTxt,statusColor,sanalKar:Math.round(sanalKar*100)/100,rr,tp1kalan,maxSeen}}}});

// v6.6 Lite: Tarih filtresi
const dateFilter=document.getElementById('filterSkippedDate').value;
rows=rows.filter(r=>dateMatches(r.zaman||'',dateFilter));

if(filter==='tp')rows=rows.filter(r=>r.status==='tp1'||r.status==='tp2');
else if(filter==='stop')rows=rows.filter(r=>r.status==='stop');
else if(filter==='active')rows=rows.filter(r=>r.status==='active');

const tpCount=rows.filter(r=>r.status==='tp1'||r.status==='tp2').length;
const stopCount=rows.filter(r=>r.status==='stop').length;
const activeCount=rows.filter(r=>r.status==='active').length;
const missedKar=rows.filter(r=>r.status==='tp1'||r.status==='tp2').reduce((s,r)=>s+r.sanalKar,0);
const dodgedZarar=rows.filter(r=>r.status==='stop').reduce((s,r)=>s+Math.abs(r.sanalKar),0);
document.getElementById('skippedOzet').innerHTML=`✓ TP:${{tpCount}} <span style="color:#f87171">(<b>-${{missedKar.toFixed(0)}}$</b> kaçırılan kar)</span> | ✗ Stop:${{stopCount}} <span style="color:#4ade80">(+${{dodgedZarar.toFixed(0)}}$ korunulan zarar)</span> | ⏳ Aktif:${{activeCount}}`;
document.getElementById('skippedCount').textContent=`(${{skippedSignals.length}})`;

const body=document.getElementById('skippedBody');
if(!rows.length){{body.innerHTML='<tr><td colspan="10" style="text-align:center;color:#9ca3af">Filtre sonucu boş</td></tr>';return}}
body.innerHTML=rows.map(s=>{{
const pxStr=s.px?s.px.toFixed(6):'—';
const pxColor=s.status==='tp2'?'#14b8a6':s.status==='tp1'?'#84cc16':s.status==='stop'?'#f87171':'#e5e7eb';
const tp1Str=s.tp1kalan!=null?(s.tp1kalan<=0?'<span style="color:#4ade80">✓ Geçti</span>':`%${{s.tp1kalan.toFixed(2)}} uzak`):'—';
const karStr=s.sanalKar!==0?`<span style="color:${{s.sanalKar>0?'#4ade80':'#f87171'}}">${{s.sanalKar>=0?'+':''}}${{s.sanalKar.toFixed(1)}}$</span>`:'—';
let rowBg='';
if(s.status==='tp2')rowBg='background:rgba(20,184,166,0.12);border-left:3px solid #14b8a6;';
else if(s.status==='tp1')rowBg='background:rgba(132,204,22,0.08);border-left:3px solid #84cc16;';
else if(s.status==='stop')rowBg='background:rgba(239,68,68,0.08);border-left:3px solid #f87171;';
return`<tr style="${{rowBg}}">
<td><a href="javascript:void(0)" onclick="openTV('${{s.ticker}}')" style="color:#60a5fa;text-decoration:none">${{s.ticker}}</a> 🔗</td>
<td>${{s.marj}}$ (${{s.lev}}x)</td>
<td>${{s.giris.toFixed(6)}}</td>
<td style="color:${{pxColor}}">${{pxStr}}</td>
<td style="color:${{s.statusColor}};font-weight:bold">${{s.statusTxt}}</td>
<td>${{tp1Str}}</td>
<td>${{karStr}}</td>
<td>${{s.rr}}</td>
<td>${{(s.atr_skor||1).toFixed(2)}}x</td>
<td>${{s.zaman.slice(5)}}</td>
</tr>`}}).join('')}}

async function clearSkipped(){{if(!confirm('Kaçırılan sinyal kayıtlarını temizlemek istiyor musun?'))return;try{{await fetch('/api/clear_skipped',{{method:'POST'}});skippedSignals=[];skippedPrices={{}};renderSkipped();toast('✓ Temizlendi')}}catch(e){{toast('Hata',true)}}}}

// === v6.5: SHADOW MODE FONKSIYONLARI ===
function switchSystem(sys){{
  currentSystem=sys;
  // Sekme görsel güncelle
  const tCab=document.getElementById('tabCab');
  const tRam=document.getElementById('tabRam');
  if(sys==='cab'){{
    tCab.style.background='#16a34a';tCab.style.color='white';tCab.style.borderBottomColor='#4ade80';
    tRam.style.background='#1e293b';tRam.style.color='#94a3b8';tRam.style.borderBottomColor='transparent';
    document.getElementById('cabMode').style.display='block';
    document.getElementById('ramMode').style.display='none';
  }}else{{
    tCab.style.background='#1e293b';tCab.style.color='#94a3b8';tCab.style.borderBottomColor='transparent';
    tRam.style.background='#ea580c';tRam.style.color='white';tRam.style.borderBottomColor='#fb923c';
    document.getElementById('cabMode').style.display='none';
    document.getElementById('ramMode').style.display='block';
    renderRamStatsBar();
  }}
  // localStorage'a kaydet (sayfa yenileyince hatırlasın)
  try{{localStorage.setItem('cabbot_system',sys)}}catch(e){{}}
}}

function updateTabBadges(){{
  const cabActive=Object.keys(openPositions).length;
  const ramActive=Object.keys(shadowPositions).length;
  const cabBadge=document.getElementById('tabCabBadge');
  const ramBadge=document.getElementById('tabRamBadge');
  if(cabBadge)cabBadge.textContent=cabActive;
  if(ramBadge)ramBadge.textContent=ramActive;
}}

function renderRamStatsBar(){{
  // RAM moduna özel üst özet kart
  const openCount=Object.keys(shadowPositions).length;
  const closedCount=shadowClosed.length;
  let totalSanalKar=0,tp1Count=0,tp2Count=0,stopCount=0,trailCount=0;
  shadowClosed.forEach(c=>{{
    totalSanalKar+=parseFloat(c.kar||0);
    if(c.kind==='TRAIL')trailCount++;
    if(c.sonuc&&c.sonuc.includes('TP2'))tp2Count++;
    else if(c.sonuc&&c.sonuc.includes('TP1'))tp1Count++;
    else if(c.sonuc==='STOP')stopCount++;
  }});
  let openSanalKar=0;
  Object.entries(shadowPositions).forEach(([sym,p])=>{{
    const px=shadowPrices[sym];
    if(px&&p.giris){{const posSize=p.marj*p.lev;openSanalKar+=posSize*((px-p.giris)/p.giris)}}
  }});
  const total=totalSanalKar+openSanalKar;
  const totalColor=total>=0?'#4ade80':'#f87171';
  const openColor=openSanalKar>=0?'#4ade80':'#f87171';
  const closedColor=totalSanalKar>=0?'#4ade80':'#f87171';
  const winRate=closedCount>0?Math.round((tp1Count+tp2Count)/closedCount*100):0;
  const wrColor=winRate>=60?'#4ade80':(winRate>=40?'#fbbf24':'#f87171');
  document.getElementById('ramStatsBar').innerHTML=`
    <div class="stat-card" style="background:linear-gradient(135deg,#7c2d12,#431407);border-left:3px solid #fb923c"><div class="stat-label">🌓 RAM Toplam (sanal)</div><div class="stat-value" style="color:${{totalColor}}">${{total>=0?'+':''}}${{total.toFixed(1)}}$</div></div>
    <div class="stat-card"><div class="stat-label">📂 Açık Pozisyon</div><div class="stat-value">${{openCount}}</div><div class="stat-sub" style="color:${{openColor}}">${{openSanalKar>=0?'+':''}}${{openSanalKar.toFixed(1)}}$ sanal</div></div>
    <div class="stat-card"><div class="stat-label">📁 Kapanan</div><div class="stat-value">${{closedCount}}</div><div class="stat-sub" style="color:${{closedColor}}">${{totalSanalKar>=0?'+':''}}${{totalSanalKar.toFixed(1)}}$ realize</div></div>
    <div class="stat-card"><div class="stat-label">🎯 Win Rate</div><div class="stat-value" style="color:${{wrColor}}">${{winRate}}%</div><div class="stat-sub">${{tp1Count+tp2Count}}/${{closedCount||0}} kazanç</div></div>
    <div class="stat-card"><div class="stat-label">✓ TP1 / ★ TP2</div><div class="stat-value">${{tp1Count}} / ${{tp2Count}}</div></div>
    <div class="stat-card"><div class="stat-label">✗ Stop / ↓ Trail</div><div class="stat-value">${{stopCount}} / ${{trailCount}}</div></div>
  `;
}}

function switchShadowTab(tab){{shadowTab=tab;document.getElementById('shadowTabOpen').style.background=tab==='open'?'#ea580c':'#1e40af';document.getElementById('shadowTabClosed').style.background=tab==='closed'?'#ea580c':'#1e40af';document.getElementById('shadowOpenView').style.display=tab==='open'?'block':'none';document.getElementById('shadowClosedView').style.display=tab==='closed'?'block':'none';renderShadow()}}

async function clearShadow(){{if(!confirm('TÜM Shadow (RAM v14) kayıtlarını temizlemek istiyor musun? Açık sanal pozlar dahil!'))return;try{{await fetch('/api/clear_shadow',{{method:'POST'}});shadowPositions={{}};shadowClosed=[];shadowPrices={{}};renderShadow();toast('✓ Shadow temizlendi')}}catch(e){{toast('Hata',true)}}}}

async function updateShadowPrices(){{const tickers=Object.keys(shadowPositions);if(tickers.length===0)return;const promises=tickers.map(async sym=>{{const px=await fp(sym);if(px!==null)shadowPrices[sym]=px;return{{sym,px}}}});await Promise.all(promises);renderShadow()}}

function renderShadow(){{
const openCount=Object.keys(shadowPositions).length;
const closedCount=shadowClosed.length;
document.getElementById('shadowCount').textContent=`(${{openCount}} açık / ${{closedCount}} kapalı)`;

// Özet
let totalSanalKar=0;let tp1Count=0;let tp2Count=0;let stopCount=0;let trailCount=0;
shadowClosed.forEach(c=>{{totalSanalKar+=parseFloat(c.kar||0);if(c.sonuc&&c.sonuc.includes('TP2'))tp2Count++;else if(c.sonuc&&c.sonuc.includes('TP1'))tp1Count++;else if(c.sonuc==='STOP')stopCount++;if(c.kind==='TRAIL')trailCount++}});
let openSanalKar=0;
Object.entries(shadowPositions).forEach(([sym,p])=>{{const px=shadowPrices[sym];if(px&&p.giris){{const posSize=p.marj*p.lev;const ham=posSize*((px-p.giris)/p.giris);openSanalKar+=ham}}}});
const karColor=totalSanalKar>=0?'#4ade80':'#f87171';
const openColor=openSanalKar>=0?'#4ade80':'#f87171';
document.getElementById('shadowOzet').innerHTML=`<b>Açık Sanal Kar:</b> <span style="color:${{openColor}}">${{openSanalKar>=0?'+':''}}${{openSanalKar.toFixed(1)}}$</span> | <b>Kapanan Toplam:</b> <span style="color:${{karColor}}">${{totalSanalKar>=0?'+':''}}${{totalSanalKar.toFixed(1)}}$</span> | TP1:${{tp1Count}} TP2:${{tp2Count}} Stop:${{stopCount}} Trail:${{trailCount}}`;

if(shadowTab==='open'){{
const tb=document.getElementById('shadowOpenBody');
if(openCount===0){{tb.innerHTML='<tr><td colspan="10" style="text-align:center;color:#9ca3af">Henüz RAM sinyali gelmedi (Pine\\'da alarm kurulmuş mu?)</td></tr>';return}}
const rows=Object.entries(shadowPositions).map(([sym,p])=>{{
const px=shadowPrices[sym]||p.max_seen||p.giris;
const giris=p.giris;
const posSize=p.marj*p.lev;
let durum='⏳ Aktif';let durumColor='#fbbf24';
if(p.tp2_hit){{durum='★ TP2 Vurmuş';durumColor='#14b8a6'}}else if(p.tp1_hit){{durum='✓ TP1 Vurmuş';durumColor='#84cc16'}}
const sanalKar=p.tp1_hit||p.tp2_hit?(p.tp1_kar||0)+(p.tp2_kar||0)+posSize*(1-((p.kapat_oran||50)/100)-(p.tp2_hit?0.25:0))*((px-giris)/giris):posSize*((px-giris)/giris);
const stopShow=p.current_stop||p.stop;
return{{sym,giris,px,hh:p.hh_pct||0,stop:stopShow,tp1:p.tp1,tp2:p.tp2,durum,durumColor,sanalKar:Math.round(sanalKar*100)/100,rs:p.rs_spread||0,zaman:p.zaman||'-'}};
}});
tb.innerHTML=rows.map(r=>`<tr><td><b>${{r.sym}}</b> <a href="https://www.tradingview.com/chart/?symbol=BINANCE:${{r.sym}}.P" target="_blank" style="color:#60a5fa;text-decoration:none">🔗</a></td><td>${{r.giris}}</td><td>${{r.px}}</td><td style="color:${{r.hh>0?'#4ade80':'#9ca3af'}}">${{r.hh.toFixed(2)}}%</td><td style="color:#f87171">${{r.stop}}</td><td>${{r.tp1}} / ${{r.tp2}}</td><td style="color:${{r.durumColor}}">${{r.durum}}</td><td style="color:${{r.sanalKar>=0?'#4ade80':'#f87171'}}"><b>${{r.sanalKar>=0?'+':''}}${{r.sanalKar.toFixed(1)}}$</b></td><td style="color:#fb923c">${{r.rs.toFixed(1)}}%</td><td style="font-size:11px;color:#9ca3af">${{r.zaman}}</td></tr>`).join('')}}else{{
const tb=document.getElementById('shadowClosedBody');
if(closedCount===0){{tb.innerHTML='<tr><td colspan="9" style="text-align:center;color:#9ca3af">Henüz kapalı shadow pozisyonu yok</td></tr>';return}}
const sorted=[...shadowClosed].reverse();
tb.innerHTML=sorted.map(c=>{{
const karColor=c.kar>=0?'#4ade80':'#f87171';
let sonucColor='#fbbf24';
if(c.sonuc&&c.sonuc.includes('TP2'))sonucColor='#14b8a6';else if(c.sonuc&&c.sonuc.includes('TP1'))sonucColor='#84cc16';else if(c.sonuc==='STOP')sonucColor='#f87171';
return`<tr><td><b>${{c.ticker}}</b></td><td style="color:${{sonucColor}}">${{c.sonuc||'-'}}</td><td>${{c.giris}}</td><td>${{c.exit_px||'-'}}</td><td style="color:${{karColor}}"><b>${{c.kar>=0?'+':''}}${{c.kar}}$</b></td><td style="font-size:11px">${{c.tp1_kar||0}} / ${{c.tp2_kar||0}} / ${{c.trail_kar||0}}</td><td style="color:#fb923c">${{(c.rs_spread||0).toFixed(1)}}%</td><td style="font-size:11px;color:#9ca3af">${{c.zaman||'-'}}</td><td style="font-size:11px;color:#9ca3af">${{c.kapanis||'-'}}</td></tr>`;
}}).join('')}}
}}

function renderStats(){{
// v6.6 Lite Patch 6: Binance PNL varsa onu kullan, yoksa dashboard kar
const realKar=(c)=>(c.binance_pnl!=null?c.binance_pnl:c.kar);
const tk=closedPositions.reduce((s,c)=>s+realKar(c),0),ts=closedPositions.length,ws=closedPositions.filter(c=>realKar(c)>0).length,wr=ts>0?(ws/ts*100).toFixed(1):0;
const today=now_tr().slice(0,10),bg=closedPositions.filter(c=>c.kapanis.startsWith(today)),bk=bg.reduce((s,c)=>s+realKar(c),0),bt=bg.filter(c=>realKar(c)>0).length,bs=bg.filter(c=>realKar(c)<=0).length,bw=bg.length>0?(bt/bg.length*100).toFixed(1):0;
const su=closedPositions.map(sureDk),os=su.length>0?su.reduce((a,b)=>a+b,0)/su.length:0,oss=os===0?'—':sureFmt(Math.round(os));
const nr=tk>0?'#4ade80':(tk<0?'#f87171':'#e5e7eb'),wrr=wr>=50?'#4ade80':(ts>0?'#f87171':'#e5e7eb'),bkr=bk>0?'#4ade80':(bk<0?'#f87171':'#e5e7eb'),bwr=bw>=50?'#4ade80':(bg.length>0?'#f87171':'#e5e7eb');
// Kaç pozda gerçek Binance verisi var, gösterge için
const binanceCount=closedPositions.filter(c=>c.binance_pnl!=null).length;
const karSubLbl=binanceCount===ts?'Net Kar (Binance)':(binanceCount>0?`Net Kar (${{binanceCount}}/${{ts}} gerçek)`:'Net Kar');
// v6.1: Akıllı slot — TP1 vurmuş VE timeout-BE'liler exempt
let aktifRisk=0,gar_tp1=0,gar_to=0;
for(const p of Object.values(openPositions)){{if(p.tp1_hit)gar_tp1++;else if(p.timeout_be)gar_to++;else aktifRisk++}}
const garantili=gar_tp1+gar_to;
const openLbl=garantili>0?`${{aktifRisk}}+${{gar_tp1}}★${{gar_to>0?'+'+gar_to+'⏰':''}}`:`${{aktifRisk}}`;
const openSubLbl=garantili>0?`Aktif+TP1${{gar_to>0?'+TO':''}}`:'Açık';
document.getElementById('statsBar').innerHTML=
`<div class="stat"><div class="stat-val">${{openLbl}}</div><div class="stat-lbl">${{openSubLbl}}</div></div>`+
`<div class="stat"><div class="stat-val">${{ts}}</div><div class="stat-lbl">Kapanan</div></div>`+
`<div class="stat"><div class="stat-val" style="color:${{nr}}">${{tk>=0?'+':''}}${{tk.toFixed(1)}}$</div><div class="stat-lbl">${{karSubLbl}}</div></div>`+
`<div class="stat"><div class="stat-val" style="color:${{wrr}}">${{wr}}%</div><div class="stat-lbl">Win Rate</div></div>`+
`<div class="stat"><div class="stat-val" style="color:${{bwr}}">${{bw}}%</div><div class="stat-lbl">Bugün WR</div></div>`+
`<div class="stat"><div class="stat-val">${{oss}}</div><div class="stat-lbl">Ort. Süre</div></div>`+
`<div class="stat"><div class="stat-val" style="color:${{MC}}">${{MODE}}</div><div class="stat-lbl">Mod</div></div>`;
document.getElementById('bugunBox').innerHTML=`📅 <b>Bugün (${{today}}):</b> ${{bg.length}} kapanan | ${{bt}} TP | ${{bs}} Stop | <span style="color:${{bkr}}">${{bk>=0?'+':''}}${{bk.toFixed(1)}}$</span>`}}

function renderOpen(){{
const search=document.getElementById('searchOpen').value.toLowerCase();
let rows=Object.entries(openPositions).filter(([t])=>t.toLowerCase().includes(search)).map(([t,p])=>{{
const px=openPrices[t]||null,pct=px?(px-p.giris)/p.giris*100:null,tp1k=px?(p.tp1-px)/px*100:null;
const stopk=(px&&p.stop)?((px-p.stop)/px*100):null;
return{{ticker:t,pos:p,px,pct,tp1kalan:tp1k,stopkalan:stopk,marj:p.marj,giris:p.giris,stop:p.stop,tp1:p.tp1,tp2:p.tp2,hh_pct:p.hh_pct||0,atr_skor:p.atr_skor||1.0,trail:p.tp1_hit?(p.tp1_kar||0):-999,zaman_full:p.zaman_full||''}}}});
const s=sortState.open;if(s.col)rows.sort((a,b)=>{{let av=a[s.col],bv=b[s.col];if(av==null)av=-Infinity;if(bv==null)bv=-Infinity;return typeof av==='string'?s.dir==='asc'?av.localeCompare(bv):bv.localeCompare(av):s.dir==='asc'?av-bv:bv-av}});
const body=document.getElementById('openBody');
if(!rows.length){{body.innerHTML='<tr><td colspan="14" style="text-align:center;color:#9ca3af">Açık pozisyon yok</td></tr>'}}
else{{body.innerHTML=rows.map(r=>{{const p=r.pos,t=r.ticker,ps=p.marj*p.lev,sz=ps*(p.stop-p.giris)/p.giris,k=p.kapat_oran||60,t1k=ps*(k/100)*(p.tp1-p.giris)/p.giris,t2k=ps*0.25*(p.tp2-p.giris)/p.giris;
let pxS='—',pxC='#e5e7eb',pcS='—';if(r.px){{const pr=ps*r.pct/100;pxS=r.px.toFixed(6);if(r.pct>0){{pxC='#4ade80';pcS=`<span style="color:#4ade80">▲ +${{pr.toFixed(1)}}$ (+${{r.pct.toFixed(2)}}%)</span>`}}else{{pxC='#f87171';pcS=`<span style="color:#f87171">▼ ${{pr.toFixed(1)}}$ (${{r.pct.toFixed(2)}}%)</span>`}}}}
const tkS=r.tp1kalan!=null?(r.tp1kalan<=0?'<span style="color:#4ade80">✓ Geçildi</span>':`%${{r.tp1kalan.toFixed(2)}} uzak`):'—';
let skS='—';if(r.stopkalan!=null){{const sk=r.stopkalan;let skC='#4ade80';if(sk<=0)skC='#dc2626';else if(sk<2)skC='#f87171';else if(sk<5)skC='#fbbf24';skS=`<span style="color:${{skC}};font-weight:600">%${{sk.toFixed(2)}} ${{sk<0?'↓':'uzak'}}</span>`}}
let trS='—';if(p.tp2_hit)trS=`✓ TP2+ (+${{((p.tp1_kar||0)+(p.tp2_kar||0)).toFixed(1)}}$)`;else if(p.tp1_hit)trS=`✓ TP1+ (+${{(p.tp1_kar||0).toFixed(1)}}$)`;else if(p.timeout_be)trS=`⏰ TO-BE (+${{(p.timeout_kar_initial||0).toFixed(1)}}$)`;
let rb='';if(p.tp2_hit)rb='background:rgba(20,184,166,0.15);border-left:3px solid #14b8a6;';else if(p.tp1_hit)rb='background:rgba(132,204,22,0.12);border-left:3px solid #84cc16;';else if(p.timeout_be)rb='background:rgba(249,115,22,0.12);border-left:3px solid #f97316;';
let wc='';try{{const ac=new Date(p.zaman_full.replace(' ','T')+':00+03:00');if((Date.now()-ac.getTime())/3600000>6&&!p.tp1_hit&&!p.timeout_be)wc='warn-row'}}catch(e){{}}
const hd=(p.hh_pct||0)>0?`%${{p.hh_pct.toFixed(2)}}`:'—',ad=`${{(p.atr_skor||1.0).toFixed(2)}}x (${{k}}%)`;
return`<tr class="${{wc}}" style="${{rb}}"><td><a href="javascript:void(0)" onclick="openTV('${{t}}')" style="color:#60a5fa;text-decoration:none;">${{t}}</a> 🔗</td><td>${{p.marj.toFixed(0)}}$ <small>(${{p.lev}}x)</small></td><td>${{p.giris.toFixed(6)}}</td><td style="color:${{pxC}}">${{pxS}}</td><td>${{tkS}}</td><td>${{skS}}</td><td>${{p.durum&&(p.durum.includes('TP')||p.durum.includes('Timeout'))?p.durum:pcS}}</td><td>${{hd}}</td><td>${{ad}}</td><td>${{trS}}</td><td>${{p.stop.toFixed(6)}} <small style="color:#f87171">(${{sz>=0?'+':''}}${{sz.toFixed(1)}}$)</small></td><td>${{p.tp1.toFixed(6)}} <small style="color:#4ade80">(+${{t1k.toFixed(1)}}$)</small></td><td>${{p.tp2.toFixed(6)}} <small style="color:#4ade80">(+${{t2k.toFixed(1)}}$)</small></td><td>${{p.zaman||''}}</td></tr>`}}).join('')}}
document.getElementById('openCount').textContent=`(${{rows.length}})`;updateSortArrows('open')}}

function renderClosed(){{
const df=document.getElementById('filterDate').value,rf=document.getElementById('filterResult').value,search=document.getElementById('searchClosed').value.toLowerCase();
let rows=closedPositions.filter(c=>dateMatches(c.kapanis,df)&&resultMatches(c.sonuc,rf)&&c.ticker.toLowerCase().includes(search)).map(c=>({{...c,sure_dk:sureDk(c)}}));
const s=sortState.closed;if(s.col)rows.sort((a,b)=>{{let av=a[s.col],bv=b[s.col];if(av==null)av=-Infinity;if(bv==null)bv=-Infinity;return typeof av==='string'?s.dir==='asc'?av.localeCompare(bv):bv.localeCompare(av):s.dir==='asc'?av-bv:bv-av}});
const body=document.getElementById('closedBody');
if(!rows.length){{body.innerHTML='<tr><td colspan="10" style="text-align:center;color:#9ca3af">Veri yok</td></tr>'}}
else{{body.innerHTML=rows.map(c=>{{const rk=c.kar>0?'#4ade80':'#f87171',ks=(c.kar>=0?'+':'')+c.kar.toFixed(1)+'$',hd=(c.hh_pct||0)>0?`%${{c.hh_pct.toFixed(2)}}`:'—',ad=`${{(c.atr_skor||1.0).toFixed(2)}}x/${{c.kapat_oran||60}}%`,su=sureFmt(c.sure_dk),zm=c.acilis.slice(5)+'→'+c.kapanis.slice(11);
let wc='';if(c.sonuc==='✗ Stop'&&(c.hh_pct||0)>=5)wc='warn-row';
let scolor=rk;if(c.sonuc.includes('Timeout'))scolor='#f97316';
// v6.6 Lite: Binance gerçek PNL
let bpS='<span style="color:#6b7280">—</span>';
if(c.binance_pnl!=null){{const bp=c.binance_pnl,bpc=bp>0?'#4ade80':'#f87171',bps=(bp>=0?'+':'')+bp.toFixed(2)+'$';const fark=bp-c.kar;const farkStr=Math.abs(fark)>0.5?` <small style="color:#9ca3af">(${{fark>=0?'+':''}}${{fark.toFixed(1)}})</small>`:'';bpS=`<span style="color:${{bpc}};font-weight:bold">${{bps}}</span>${{farkStr}}`}}
return`<tr class="${{wc}}"><td><a href="javascript:void(0)" onclick="openTV('${{c.ticker}}')" style="color:#60a5fa;text-decoration:none;">${{c.ticker}}</a> 🔗</td><td>${{c.marj.toFixed(0)}}$</td><td>${{c.giris.toFixed(6)}}</td><td style="color:${{scolor}}">${{c.sonuc}}</td><td style="color:${{rk}};font-weight:bold">${{ks}}</td><td>${{bpS}}</td><td>${{hd}}</td><td>${{ad}}</td><td>${{su}}</td><td>${{zm}}</td></tr>`}}).join('')}}
const tk=rows.filter(c=>c.kar>0).reduce((s,c)=>s+c.kar,0),tz=rows.filter(c=>c.kar<0).reduce((s,c)=>s+c.kar,0),nt=tk+tz,nc=nt>0?'#4ade80':(nt<0?'#f87171':'#e5e7eb');
// v6.6 Lite: Binance toplam
const binanceRows=rows.filter(c=>c.binance_pnl!=null);
const btk=binanceRows.reduce((s,c)=>s+c.binance_pnl,0);
const btc=btk>0?'#4ade80':(btk<0?'#f87171':'#e5e7eb');
const binanceStr=binanceRows.length>0?` | 🔥 <b>Binance NET:</b> <span style="color:${{btc}}">${{btk>=0?'+':''}}${{btk.toFixed(2)}}$</span> <small style="color:#9ca3af">(${{binanceRows.length}}/${{rows.length}} poz)</small>`:'';
document.getElementById('closedOzet').innerHTML=rows.length>0?`<b>${{rows.length}}</b> poz | <span style="color:#4ade80">Kar:+${{tk.toFixed(1)}}$</span> | <span style="color:#f87171">Zarar:${{tz.toFixed(1)}}$</span> | <span style="color:${{nc}}">Dashboard NET:${{nt>=0?'+':''}}${{nt.toFixed(1)}}$</span>${{binanceStr}}`:'Veri yok';
document.getElementById('closedCount').textContent=`(${{rows.length}}/${{closedPositions.length}})`;updateSortArrows('closed')}}

function checkWarnings(){{
const warns=[];
for(const[t,p]of Object.entries(openPositions)){{if(p.tp1_hit||p.timeout_be)continue;try{{const ac=new Date(p.zaman_full.replace(' ','T')+':00+03:00');const h=((Date.now()-ac.getTime())/3600000).toFixed(1);if(h>6)warns.push(`⏰ <b>${{t}}</b> ${{sureFmt(Math.round(h*60))}}'dir açık`)}}catch(e){{}}}}
const son5=closedPositions.slice(-5);if(son5.length===5&&son5.every(c=>c.sonuc==='✗ Stop'))warns.push('🚨 <b>Son 5 pozisyon üst üste STOP!</b> Piyasa riskli');
const hh_stop=closedPositions.slice(-15).filter(c=>c.sonuc==='✗ Stop'&&(c.hh_pct||0)>=5);
if(hh_stop.length>=2)warns.push(`⚠️ <b>${{hh_stop.length}} poz</b> TP1'e yaklaşıp stop yedi — trailing/stop ayarını incele`);
document.getElementById('warnBox').innerHTML=warns.length>0?warns.map(w=>`<div class="warn-box">${{w}}</div>`).join(''):''}}

function showAnalysis(){{
const filtered=closedPositions.filter(c=>dateMatches(c.kapanis,document.getElementById('filterDate').value)&&resultMatches(c.sonuc,document.getElementById('filterResult').value));
if(!filtered.length){{toast('Analiz için veri yok',true);return}}
const wins=filtered.filter(c=>c.kar>0),losses=filtered.filter(c=>c.kar<0),tk=filtered.reduce((s,c)=>s+c.kar,0);
const wr=(wins.length/filtered.length*100).toFixed(1),aw=wins.length?wins.reduce((s,c)=>s+c.kar,0)/wins.length:0,al=losses.length?losses.reduce((s,c)=>s+c.kar,0)/losses.length:0;
const mw=wins.length?Math.max(...wins.map(c=>c.kar)):0,ml=losses.length?Math.min(...losses.map(c=>c.kar)):0;
const pf=losses.length&&Math.abs(al)>0?(wins.reduce((s,c)=>s+c.kar,0)/Math.abs(losses.reduce((s,c)=>s+c.kar,0))):0;
const ev=tk/filtered.length;
let peak=0,dd=0,mdd=0,cum=0;for(const c of filtered){{cum+=c.kar;if(cum>peak)peak=cum;dd=cum-peak;if(dd<mdd)mdd=dd}}
const byH={{}};for(const c of filtered){{const h=c.acilis.slice(11,13);if(!byH[h])byH[h]={{w:0,t:0}};byH[h].t++;if(c.kar>0)byH[h].w++}}
const hWR=Object.entries(byH).filter(([,v])=>v.t>=2).map(([h,v])=>({{h,wr:v.w/v.t*100,t:v.t}})).sort((a,b)=>b.wr-a.wr);
const bestH=hWR.length?`${{hWR[0].h}}:00 (%${{hWR[0].wr.toFixed(0)}} - ${{hWR[0].t}} işlem)`:'—';
const dist={{}};for(const c of filtered)dist[c.sonuc]=(dist[c.sonuc]||0)+1;
document.getElementById('analysisBody').innerHTML=`
<div class="stats-grid">
<div class="stat-card"><div class="stat-card-lbl">Toplam</div><div class="stat-card-val">${{filtered.length}}</div></div>
<div class="stat-card"><div class="stat-card-lbl">Win Rate</div><div class="stat-card-val" style="color:${{wr>=50?'#4ade80':'#f87171'}}">${{wr}}%</div></div>
<div class="stat-card"><div class="stat-card-lbl">Net Kar</div><div class="stat-card-val" style="color:${{tk>0?'#4ade80':'#f87171'}}">${{tk>=0?'+':''}}${{tk.toFixed(1)}}$</div></div>
<div class="stat-card"><div class="stat-card-lbl">Avg Win</div><div class="stat-card-val" style="color:#4ade80">+${{aw.toFixed(1)}}$</div></div>
<div class="stat-card"><div class="stat-card-lbl">Avg Loss</div><div class="stat-card-val" style="color:#f87171">${{al.toFixed(1)}}$</div></div>
<div class="stat-card"><div class="stat-card-lbl">Max Win</div><div class="stat-card-val" style="color:#4ade80">+${{mw.toFixed(1)}}$</div></div>
<div class="stat-card"><div class="stat-card-lbl">Max Loss</div><div class="stat-card-val" style="color:#f87171">${{ml.toFixed(1)}}$</div></div>
<div class="stat-card"><div class="stat-card-lbl">Profit Factor</div><div class="stat-card-val" style="color:${{pf>=1?'#4ade80':'#f87171'}}">${{pf.toFixed(2)}}</div></div>
<div class="stat-card"><div class="stat-card-lbl">Beklenen Değer</div><div class="stat-card-val" style="color:${{ev>0?'#4ade80':'#f87171'}}">${{ev>=0?'+':''}}${{ev.toFixed(2)}}$/iş</div></div>
<div class="stat-card"><div class="stat-card-lbl">Max Drawdown</div><div class="stat-card-val" style="color:#f87171">${{mdd.toFixed(1)}}$</div></div>
<div class="stat-card"><div class="stat-card-lbl">En İyi Saat</div><div class="stat-card-val" style="font-size:12px">${{bestH}}</div></div>
</div>
<div class="chart-wrap"><h3>Sonuç Dağılımı</h3><div style="max-width:280px;margin:0 auto"><canvas id="distChart"></canvas></div></div>
<div class="chart-wrap"><h3>Kümülatif Kar/Zarar Eğrisi</h3><canvas id="cumChart" style="max-height:200px"></canvas></div>
<div class="chart-wrap"><h3>Saat Bazlı Win Rate (2+ işlem)</h3><canvas id="hourChart" style="max-height:200px"></canvas></div>`;
document.getElementById('analysisModal').classList.add('show');
setTimeout(()=>{{
const dLabels=Object.keys(dist),dData=Object.values(dist),dColors=dLabels.map(k=>k.includes('TP2')?'#14b8a6':k.includes('TP1+Trail')?'#84cc16':k.includes('BE')?'#fbbf24':k.includes('Timeout')?'#f97316':'#f87171');
new Chart(document.getElementById('distChart'),{{type:'doughnut',data:{{labels:dLabels,datasets:[{{data:dData,backgroundColor:dColors}}]}},options:{{plugins:{{legend:{{labels:{{color:'#e5e7eb',font:{{size:11}}}}}}}},responsive:true}}}});
let c2=0;const cD=filtered.slice().sort((a,b)=>a.kapanis.localeCompare(b.kapanis)).map(c=>{{c2+=c.kar;return c2}});
const cColor=cD[cD.length-1]>0?'#4ade80':'#f87171';
new Chart(document.getElementById('cumChart'),{{type:'line',data:{{labels:cD.map((_,i)=>i+1),datasets:[{{label:'Net Kar ($)',data:cD,borderColor:cColor,backgroundColor:cColor+'1a',fill:true,tension:0.2}}]}},options:{{plugins:{{legend:{{labels:{{color:'#e5e7eb'}}}}}},scales:{{x:{{ticks:{{color:'#9ca3af'}},grid:{{color:'#334155'}}}},y:{{ticks:{{color:'#9ca3af'}},grid:{{color:'#334155'}}}}}}}}}});
if(hWR.length){{const sH=hWR.slice().sort((a,b)=>a.h.localeCompare(b.h));new Chart(document.getElementById('hourChart'),{{type:'bar',data:{{labels:sH.map(h=>h.h+':00'),datasets:[{{label:'Win Rate %',data:sH.map(h=>h.wr.toFixed(0)),backgroundColor:sH.map(h=>h.wr>=50?'#4ade80':'#f87171')}}]}},options:{{plugins:{{legend:{{labels:{{color:'#e5e7eb'}}}}}},scales:{{x:{{ticks:{{color:'#9ca3af'}},grid:{{color:'#334155'}}}},y:{{ticks:{{color:'#9ca3af'}},grid:{{color:'#334155'}},max:100}}}}}}}})}}
}},100)}}
function closeModal(){{document.getElementById('analysisModal').classList.remove('show')}}

async function clearOld(){{if(!confirm('30 günden eski kapanan pozisyonları silmek istiyor musun?'))return;try{{const r=await fetch('/api/clear_old',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{days:30}})}});const j=await r.json();toast(`✓ ${{j.removed}} kayıt silindi`);loadData()}}catch(e){{toast('Hata',true)}}}}

async function migratePnl(){{
  // v6.6 Lite Patch 11: Force refresh sor
  const refreshExisting=confirm('Geçmiş kapanan pozların gerçek Binance PNL\\'ini çek?\\n\\n• TAMAM: TÜM pozları yeniden hesapla (force refresh — eski hatalı değerleri düzeltir)\\n• İPTAL: Sadece güncellenmemiş pozları çek');
  toast('Binance\\'ten veri çekiliyor, per-symbol mod (~'+(refreshExisting?'30':'10')+'sn)...');
  try{{
    const r=await fetch('/api/migrate_pnl',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{dry_run:true, force_refresh:refreshExisting}})}});
    if(!r.ok){{toast('API yanıt vermedi: HTTP '+r.status,true);return}}
    if(!r.ok){{toast('API yanıt vermedi: HTTP '+r.status,true);return}}
    const j=await r.json();
    const mig=j.migrated||[],skp=j.skipped||[],fail=j.failed||[];
    console.log('[MIGRATE-DRY]',j);
    let dbg='📊 Deneme Sonucu — Detaylı\\n\\n';
    if(j.closed_total!==undefined)dbg+='Kapanan poz (veritabanında): '+j.closed_total+'\\n';
    if(j.debug&&j.debug.length)dbg+='Debug:\\n'+j.debug.map(x=>'  • '+x).join('\\n')+'\\n\\n';
    dbg+='✓ Güncellenebilir: '+mig.length+'\\n';
    dbg+='⏭ Atlanan: '+skp.length+'\\n';
    dbg+='❌ Başarısız: '+fail.length+'\\n\\n';
    if(skp.length>0){{dbg+='Atlanma sebepleri (ilk 5):\\n';skp.slice(0,5).forEach(s=>{{dbg+='  '+s.ticker+': '+s.reason+'\\n'}});dbg+='\\n'}}
    if(fail.length>0){{dbg+='❌ Hatalar (ilk 5):\\n';fail.slice(0,5).forEach(f=>{{dbg+='  '+f.ticker+': '+f.reason+'\\n'}});dbg+='\\n'}}
    if(mig.length===0){{
      dbg+='⚠ Hiçbir poz güncellenemedi.\\n\\nMuhtemel nedenler:\\n• API key Futures income izni yok\\n• Zaman parse hatası\\n• Binance IP kısıtı\\n\\nRailway log: [MIGRATE] satırlarına bak';
      alert(dbg);return;
    }}
    dbg+='Güncellenecek (ilk 5):\\n';
    mig.slice(0,5).forEach(m=>{{dbg+='  '+m.ticker+': '+(m.dashboard_kar||0).toFixed(1)+' → '+(m.binance_pnl||0).toFixed(2)+' (fark: '+(m.fark||0).toFixed(1)+')\\n'}});
    const sumOld=mig.reduce((s,m)=>s+(m.dashboard_kar||0),0);
    const sumNew=mig.reduce((s,m)=>s+(m.binance_pnl||0),0);
    dbg+='\\nDashboard toplam: '+sumOld.toFixed(2)+'$\\nBinance toplam:    '+sumNew.toFixed(2)+'$\\nFARK: '+(sumNew-sumOld).toFixed(2)+'$\\n\\nGerçek kayıt yapılsın mı?';
    if(!confirm(dbg))return;
    const r2=await fetch('/api/migrate_pnl',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{dry_run:false, force_refresh:refreshExisting}})}});
    const j2=await r2.json();
    console.log('[MIGRATE-REAL]',j2);
    toast('✓ '+(j2.migrated||[]).length+' poz güncellendi');
    setTimeout(()=>loadData(),800);
  }}catch(e){{console.error('[MIGRATE ERR]',e);alert('Hata: '+e.message+'\\nRailway log\\'una bak.')}}
}}

async function forceReopen(ticker){{
  if(!confirm('Binance\\'te hala açık '+ticker+' dashboard\\'a geri alınsın mı?'))return;
  try{{
    const r=await fetch('/api/force_reopen',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{ticker:ticker}})}});
    const j=await r.json();
    if(j.success){{toast('✓ '+ticker+' geri açıldı (qty:'+j.binance_qty.toFixed(2)+')');setTimeout(()=>loadData(),500)}}
    else{{alert('İşlem yapılamadı: '+j.error)}}
  }}catch(e){{alert('Hata: '+e.message)}}
}}

// v6.6 Lite Patch 5: Pause / Kill Switch
let pauseState = {{paused:false}};

async function loadPauseState(){{
  try{{
    const r=await fetch('/api/pause_status');
    const j=await r.json();
    pauseState=j;
    renderPauseState();
  }}catch(e){{console.error('Pause state:',e)}}
}}

function renderPauseState(){{
  const banner=document.getElementById('pauseBanner');
  const btn=document.getElementById('pauseBtn');
  if(!banner||!btn)return;
  if(pauseState.paused){{
    banner.style.display='block';
    document.getElementById('pauseBannerReason').textContent=pauseState.reason_text||'Manuel durdurma';
    document.getElementById('pauseBannerTime').textContent='Durdurulma: '+(pauseState.paused_at||'—')+' | Sebep: '+(pauseState.reason||'manual');
    btn.textContent='▶️ BAŞLAT';
    btn.style.background='#16a34a';
  }}else{{
    banner.style.display='none';
    btn.textContent='🛑 BOT DURDUR';
    btn.style.background='#dc2626';
  }}
}}

async function togglePause(){{
  const action=pauseState.paused?'resume':'pause';
  let reason_text='Manuel durdurma';
  if(action==='pause'){{
    const r=prompt('Bot neden durduruluyor? (opsiyonel not)','Manuel — piyasa kontrolü');
    if(r===null)return;
    if(r.trim())reason_text=r.trim();
  }}else{{
    if(!confirm('Bot tekrar aktif edilsin mi?\\nYeni sinyaller kabul edilmeye başlayacak.'))return;
  }}
  try{{
    const r=await fetch('/api/toggle_pause',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{action:action,reason_text:reason_text}})}});
    const j=await r.json();
    if(j.success){{
      toast('✓ '+j.msg);
      pauseState=j.state;
      renderPauseState();
    }}
  }}catch(e){{alert('Hata: '+e.message)}}
}}

// v6.6 Lite Patch 10: MAX POS kontrolü
let maxPosState = {{current:7, min:3, max:12, changes_today_count:0}};

async function loadMaxPosState(){{
  try{{
    const r=await fetch('/api/max_pos_status');
    const j=await r.json();
    maxPosState=j;
    renderMaxPosState();
  }}catch(e){{console.error('Max pos state:',e)}}
}}

function renderMaxPosState(){{
  const val=maxPosState.current||7;
  const count=maxPosState.changes_today_count||0;
  const autoReduced=maxPosState.auto_reduced||false;
  const btnVal=document.getElementById('maxPosBtnVal');
  const disp=document.getElementById('maxPosDisplay');
  const sub=document.getElementById('maxPosSubtext');
  if(btnVal)btnVal.textContent=val;
  if(disp)disp.textContent=val;
  if(sub){{
    let txt='';
    if(autoReduced)txt='🤖 oto';
    if(count>=5)txt+=(txt?' | ':'')+`⚠️ ${{count}} değişim bugün`;
    sub.innerHTML=txt;
  }}
}}

async function changeMaxPos(delta){{
  const current=maxPosState.current||7;
  const newVal=current+delta;
  const min=maxPosState.min||3;
  const max=maxPosState.max||12;
  if(newVal<min){{toast(`Min ${{min}}, daha az olamaz`);return}}
  if(newVal>max){{toast(`Max ${{max}}, daha fazla olamaz`);return}}
  // Günlük hatırlatma: 5'ten fazla değişim yapmışsa uyar
  const count=maxPosState.changes_today_count||0;
  if(count>=5){{
    if(!confirm(`⚠️ Bugün ${{count}} kez MAX değiştirdin, aşırı tepki verme. Devam?`))return;
  }}
  try{{
    const r=await fetch('/api/set_max_pos',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{value:newVal,reason:'manual_ui'}})}});
    const j=await r.json();
    if(j.success){{
      toast(`✓ MAX: ${{current}} → ${{newVal}}`);
      loadMaxPosState();
    }}else{{alert('Hata: '+(j.error||'bilinmiyor'))}}
  }}catch(e){{alert('Hata: '+e.message)}}
}}

// v6.6 Lite Patch 10: Rapor indir
async function downloadReport(){{
  toast('Rapor hazırlanıyor...');
  try{{
    const r=await fetch('/api/export_report');
    if(!r.ok){{toast('Rapor alınamadı: HTTP '+r.status,true);return}}
    const j=await r.json();
    const blob=new Blob([JSON.stringify(j,null,2)],{{type:'application/json'}});
    const url=URL.createObjectURL(blob);
    const a=document.createElement('a');
    const ts=new Date().toISOString().replace(/[:.]/g,'-').slice(0,19);
    a.href=url;a.download=`cab_ram_report_${{ts}}.json`;
    document.body.appendChild(a);a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    toast('✓ Rapor indirildi');
  }}catch(e){{alert('Hata: '+e.message)}}
}}

function requestNotif(){{if(!('Notification' in window)){{toast('Bildirim desteklenmiyor',true);return}}Notification.requestPermission().then(p=>{{if(p==='granted'){{toast('✓ Bildirimler açık');new Notification('CAB Bot v6.6 Lite',{{body:'v6.6 Lite — TP1/TP2/STOP + gerçek Binance PNL!'}})}}}})}}

function detectChanges(){{
for(const[t,p]of Object.entries(openPositions)){{const prev=lastOpenState[t];if(prev){{if(p.tp1_hit&&!prev.tp1_hit)notify('🎯 TP1 Vurdu!',t+' TP1 alındı +'+((p.tp1_kar||0).toFixed(1))+'$');if(p.tp2_hit&&!prev.tp2_hit)notify('🎯🎯 TP2!',t+' TP2 alındı!');if(p.timeout_be&&!prev.timeout_be)notify('⏰ Timeout-BE',t+' BE\\'ye çekildi (+'+(p.timeout_kar_initial||0).toFixed(1)+'$)')}}else if(Object.keys(lastOpenState).length>0)notify('🆕 Yeni Pozisyon',t+' açıldı')}}
for(const t of Object.keys(lastOpenState)){{if(!openPositions[t]){{const c=closedPositions.find(x=>x.ticker===t);if(c){{if(c.kar>0)notify('✓ KAR',t+': '+c.sonuc+' +'+c.kar.toFixed(1)+'$');else notify('✗ ZARAR',t+': '+c.sonuc+' '+c.kar.toFixed(1)+'$')}}}}}}
lastOpenState=JSON.parse(JSON.stringify(openPositions))}}

function notify(title,body){{if('Notification'in window&&Notification.permission==='granted')try{{new Notification(title,{{body,icon:'https://www.tradingview.com/favicon.ico'}})}}catch(e){{}}}}

function setupSort(){{document.querySelectorAll('#openTable th[data-sort]').forEach(th=>{{th.onclick=()=>{{const c=th.dataset.sort;if(sortState.open.col===c)sortState.open.dir=sortState.open.dir==='asc'?'desc':'asc';else{{sortState.open.col=c;sortState.open.dir='desc'}}renderOpen()}}}});document.querySelectorAll('#closedTable th[data-sort]').forEach(th=>{{th.onclick=()=>{{const c=th.dataset.sort;if(sortState.closed.col===c)sortState.closed.dir=sortState.closed.dir==='asc'?'desc':'asc';else{{sortState.closed.col=c;sortState.closed.dir='desc'}}renderClosed()}}}})}}
function updateSortArrows(w){{const tid=w==='open'?'openTable':'closedTable',s=sortState[w];document.querySelectorAll('#'+tid+' th').forEach(th=>{{th.classList.remove('sorted-asc','sorted-desc');if(th.dataset.sort===s.col)th.classList.add('sorted-'+s.dir)}})}}

function exportCSV(type){{let rows,fn;if(type==='open'){{rows=Object.entries(openPositions).map(([t,p])=>({{Coin:t,Marjin:p.marj,Lev:p.lev,Giris:p.giris,Stop:p.stop,TP1:p.tp1,TP2:p.tp2,HH:p.hh_pct||0,ATR:p.atr_skor||1.0,Kapat:p.kapat_oran||60,Durum:p.durum||'',Zaman:p.zaman_full||''}}));fn='acik_'+now_tr().slice(0,10)+'.csv'}}else{{rows=closedPositions.map(c=>({{Coin:c.ticker,Marjin:c.marj,Lev:c.lev||10,Giris:c.giris,Sonuc:c.sonuc,Kar:c.kar,HH:c.hh_pct||0,Acilis:c.acilis,Kapanis:c.kapanis}}));fn='kapanan_'+now_tr().slice(0,10)+'.csv'}}if(!rows.length){{toast('Veri yok',true);return}}const h=Object.keys(rows[0]),csv=[h.join(','),...rows.map(r=>h.map(k=>r[k]).join(','))].join('\\n');const b=new Blob(['\\ufeff'+csv],{{type:'text/csv'}});const a=document.createElement('a');a.href=URL.createObjectURL(b);a.download=fn;a.click();toast('✓ '+fn)}}

async function init(){{setupSort();await loadData();lastOpenState=JSON.parse(JSON.stringify(openPositions));
// v6.5: Son kullanılan sekmeyi geri yükle
try{{const savedSys=localStorage.getItem('cabbot_system');if(savedSys==='ram')switchSystem('ram')}}catch(e){{}}
if(Object.keys(openPositions).length>0||skippedSignals.length>0){{updatePrices()}}else{{setInterval(async()=>{{await loadData();document.getElementById('lastUpdate').textContent=new Date().toTimeString().slice(0,8)}},10000)}}
setTimeout(()=>location.reload(),20000)}}
init();
</script>
</body></html>"""
    return html
