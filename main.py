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
    # ═══════════════ CAB sistemi ═══════════════
    # Ana alanlar (CAB için) — eski kodla uyumlu kalır
    "open_positions": {}, "closed_positions": [], "skipped_signals": [],
    "cab_mode": "shadow",  # v6.7: "live" | "shadow" — başta güvenli
    "pause_state": {
        "paused": False, "reason": None, "reason_text": None,
        "paused_at": None, "auto_triggered": False
    },
    "max_pos_state": {
        "current": MAX_POSITIONS_DEFAULT,
        "last_change_at": None, "change_history": [], "auto_reduced": False,
    },

    # ═══════════════ RAM sistemi (v6.7: Derin Simetri) ═══════════════
    "ram_open_positions": {},
    "ram_closed_positions": [],
    "ram_skipped_signals": [],
    "ram_mode": "shadow",  # v6.7: "live" | "shadow"
    "ram_pause_state": {
        "paused": False, "reason": None, "reason_text": None,
        "paused_at": None, "auto_triggered": False
    },
    "ram_max_pos_state": {
        "current": MAX_POSITIONS_DEFAULT,
        "last_change_at": None, "change_history": [], "auto_reduced": False,
    },

    # ═══════════════ v6.5 Legacy Shadow (RAM v14 için, artık KULLANILMAYACAK) ═══════════════
    "shadow_positions": {}, "shadow_closed": [], "shadow_skipped": [],

    # ═══════════════ Arşiv ═══════════════
    "archive": [],  # [{archived_at, description, snapshot: {...}}]
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
            # v6.7: Mode alanları
            if "cab_mode" not in loaded:
                loaded["cab_mode"] = "shadow"
            if "ram_mode" not in loaded:
                loaded["ram_mode"] = "shadow"
            # v6.7: RAM kendi alanları
            for k, default in [
                ("ram_open_positions", {}),
                ("ram_closed_positions", []),
                ("ram_skipped_signals", []),
            ]:
                if k not in loaded:
                    loaded[k] = default
            if "ram_pause_state" not in loaded:
                loaded["ram_pause_state"] = {
                    "paused": False, "reason": None, "reason_text": None,
                    "paused_at": None, "auto_triggered": False
                }
            if "ram_max_pos_state" not in loaded:
                loaded["ram_max_pos_state"] = {
                    "current": MAX_POSITIONS_DEFAULT,
                    "last_change_at": None, "change_history": [], "auto_reduced": False,
                }
            # v6.7: Arşiv alanı
            if "archive" not in loaded:
                loaded["archive"] = []
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
# v6.6 Lite Patch 14: Background migrate task state
_migrate_task_state = None




# ============ KILL SWITCH / PAUSE MANAGER (v6.6 Lite Patch 5) ============

# ============ v6.7: Sistem-aware erişim yardımcıları ============
def _sys_key(system, base):
    """system='cab' → 'open_positions', system='ram' → 'ram_open_positions'"""
    if system == "cab":
        return base
    return f"ram_{base}"

def get_system_mode(system):
    """CAB veya RAM için mode: 'live' | 'shadow'"""
    if system == "cab":
        return data.get("cab_mode", "shadow")
    return data.get("ram_mode", "shadow")

def set_system_mode(system, mode):
    """CAB veya RAM için mode'u değiştir. mode: 'live' | 'shadow'"""
    assert mode in ("live", "shadow"), "mode 'live' veya 'shadow' olmalı"
    if system == "cab":
        data["cab_mode"] = mode
    else:
        data["ram_mode"] = mode
    save_data(data)
    print(f"[MODE] {system.upper()} → {mode}")

def get_open_positions(system):
    return data.get(_sys_key(system, "open_positions"), {})

def get_closed_positions(system):
    return data.get(_sys_key(system, "closed_positions"), [])

def get_skipped_signals(system):
    return data.get(_sys_key(system, "skipped_signals"), [])

# ============ KILL SWITCH / PAUSE (v6.7: sistem-aware) ============
def is_paused(system="cab"):
    """Belirtilen sistem pause'da mı?"""
    ps = data.get(_sys_key(system, "pause_state"), {})
    return bool(ps.get("paused", False))


def get_pause_info(system="cab"):
    """Pause durumu hakkında bilgi"""
    return data.get(_sys_key(system, "pause_state"), {
        "paused": False, "reason": None, "reason_text": None,
        "paused_at": None, "auto_triggered": False
    })


def pause_bot(reason, reason_text, system="cab"):
    """Belirtilen sistemi pause'a al"""
    key = _sys_key(system, "pause_state")
    data[key] = {
        "paused": True, "reason": reason, "reason_text": reason_text,
        "paused_at": now_tr(),
        "auto_triggered": reason.startswith("auto_"),
    }
    save_data(data)
    print(f"[PAUSE:{system.upper()}] {reason}: {reason_text}")


def resume_bot(system="cab"):
    """Belirtilen sistemi tekrar aktif et"""
    key = _sys_key(system, "pause_state")
    data[key] = {
        "paused": False, "reason": None, "reason_text": None,
        "paused_at": None, "auto_triggered": False,
    }
    save_data(data)
    print(f"[PAUSE:{system.upper()}] Tekrar aktif")


def check_auto_pause_triggers(system="cab"):
    """
    v6.7: Sistem-aware. Her sistem kendi closed_positions'una göre karar verir.
    """
    if not KILL_SWITCH_ENABLED:
        return
    if is_paused(system):
        return

    closed = get_closed_positions(system)
    if not closed:
        return

    recent = closed[-STOP_STREAK_WINDOW:]
    stop_count = sum(1 for c in recent if c.get("kar", 0) < 0 and ("Stop" in c.get("sonuc", "") or "Timeout" in c.get("sonuc", "")))
    if len(recent) >= STOP_STREAK_WINDOW and stop_count >= STOP_STREAK_THRESHOLD:
        pause_bot(
            "auto_stop_streak",
            f"Son {STOP_STREAK_WINDOW} pozda {stop_count} stop/timeout — üst üste kayıp koruması",
            system=system
        )
        return

    today = now_tr()[:10]
    bugun = [c for c in closed if c.get("kapanis", "").startswith(today)]
    if bugun:
        daily_net = sum(c.get("kar", 0) for c in bugun)
        if daily_net <= DAILY_LOSS_LIMIT:
            pause_bot(
                "auto_daily_loss",
                f"Günlük zarar ${daily_net:.1f} (limit: ${DAILY_LOSS_LIMIT}) — günlük kayıp koruması",
                system=system
            )


# ============ DİNAMİK MAX POSITIONS (v6.6 Lite Patch 8) ============

def get_max_positions(system="cab"):
    """v6.7: Sistem-aware MAX pozisyon limiti"""
    mp = data.get(_sys_key(system, "max_pos_state"), {})
    val = mp.get("current", MAX_POSITIONS_DEFAULT)
    val = max(MAX_POSITIONS_MIN, min(MAX_POSITIONS_MAX, int(val)))
    return val


def set_max_positions(new_val, reason="manual", system="cab"):
    """v6.7: Sistem-aware. Her sistem kendi MAX_POS'unu tutar."""
    new_val = max(MAX_POSITIONS_MIN, min(MAX_POSITIONS_MAX, int(new_val)))
    key = _sys_key(system, "max_pos_state")
    if key not in data:
        data[key] = {
            "current": MAX_POSITIONS_DEFAULT,
            "last_change_at": None, "change_history": [], "auto_reduced": False,
        }
    old_val = data[key].get("current", MAX_POSITIONS_DEFAULT)
    if new_val == old_val:
        return {"changed": False, "value": new_val}

    data[key]["current"] = new_val
    data[key]["last_change_at"] = now_tr()
    history = data[key].get("change_history", [])
    history.append({"ts": now_tr(), "from": old_val, "to": new_val, "reason": reason})
    data[key]["change_history"] = history[-50:]
    if reason.startswith("auto_"):
        data[key]["auto_reduced"] = True
    save_data(data)
    print(f"[MAX_POS:{system.upper()}] {old_val} → {new_val} ({reason})")
    return {"changed": True, "value": new_val, "from": old_val}


def check_auto_reduce_max_pos(system="cab"):
    """v6.7: Sistem-aware. Her sistem kendi closed_positions'una göre"""
    if is_paused(system):
        return

    closed = get_closed_positions(system)
    if len(closed) < 3:
        return

    current_max = get_max_positions(system)

    last3 = closed[-3:]
    stops_in_3 = sum(1 for c in last3 if c.get("kar", 0) < 0 and ("Stop" in c.get("sonuc", "") or "Timeout" in c.get("sonuc", "")))
    if stops_in_3 >= 2 and current_max > MAX_POSITIONS_MIN:
        new_val = max(MAX_POSITIONS_MIN, current_max - 2)
        if new_val < current_max:
            set_max_positions(new_val, f"auto_stop_streak_3in2", system=system)
            return

    today = now_tr()[:10]
    bugun = [c for c in closed if c.get("kapanis", "").startswith(today)]
    stops_today = sum(1 for c in bugun if c.get("kar", 0) < 0 and ("Stop" in c.get("sonuc", "") or "Timeout" in c.get("sonuc", "")))
    if stops_today >= 4 and current_max > MAX_POSITIONS_MIN:
        set_max_positions(MAX_POSITIONS_MIN, f"auto_daily_{stops_today}stops", system=system)
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
            "market_regime": _parse_field(msg, "MktRej"),  # v6.7: market rejim
            "market_detail": _parse_market_detail(msg),    # v6.7: detay
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
    return f"<h3>🤖 CAB Bot v6.7 — Dual System Edition (CAB+RAM symmetric)</h3><p>{mode}</p><p>MAX_POSITIONS: {get_max_positions()} | TIMEOUT: {TIMEOUT_HOURS}s | HL_TRACKER: {HIGH_LOW_CHECK_INTERVAL_SEC}s</p><p><a href='/dashboard'>Dashboard</a> | <a href='/test_binance'>Binance Test</a> | <a href='/api/timeout_check'>Manuel Timeout Check</a></p>"

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
    # v6.7: CAB ana alanlardan
    closed = data.get("closed_positions", [])
    open_pos = data.get("open_positions", {})
    skipped = data.get("skipped_signals", [])
    # v6.7: RAM yeni alanlar (ram_*)
    ram_closed = data.get("ram_closed_positions", [])
    ram_open = data.get("ram_open_positions", {})
    ram_skipped = data.get("ram_skipped_signals", [])
    # Legacy shadow (RAM v14, eski)
    legacy_shadow_closed = data.get("shadow_closed", [])
    legacy_shadow_open = data.get("shadow_positions", {})
    legacy_shadow_skipped = data.get("shadow_skipped", [])
    # Birleşik RAM (yeni + legacy) — eski v14 verileri de görünsün
    shadow_closed = ram_closed + legacy_shadow_closed
    shadow_open = {**ram_open, **legacy_shadow_open}
    shadow_skipped = ram_skipped + legacy_shadow_skipped
    
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
        "version": "v6.7 Dual System (symmetric, mode toggle, archive)",
        "config": {
            "cab_max_positions": get_max_positions("cab"),
            "ram_max_positions": get_max_positions("ram"),
            "cab_mode": get_system_mode("cab"),
            "ram_mode": get_system_mode("ram"),
            "max_pos_min": MAX_POSITIONS_MIN,
            "max_pos_max": MAX_POSITIONS_MAX,
            "timeout_hours": TIMEOUT_HOURS,
            "kill_switch_enabled": KILL_SWITCH_ENABLED,
            "stop_streak_window": STOP_STREAK_WINDOW,
            "stop_streak_threshold": STOP_STREAK_THRESHOLD,
            "daily_loss_limit": DAILY_LOSS_LIMIT,
        },
        "cab_pause_state": get_pause_info("cab"),
        "ram_pause_state": get_pause_info("ram"),
        "cab_max_pos_state": data.get("max_pos_state", {}),
        "ram_max_pos_state": data.get("ram_max_pos_state", {}),
        "archive_count": len(data.get("archive", [])),
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



@app.get("/api/migrate_test")
async def migrate_test():
    """
    v6.6 Lite Patch 13 HOTFIX: Migrate'in ilk batch'ini doğrudan test et.
    Tarayıcıya GET at, sonucu gör. Frontend problemi mi backend problemi mi ayırt edilir.
    """
    import asyncio
    closed_positions = data.get("closed_positions", [])
    if not closed_positions:
        return JSONResponse({"error": "Kapanan poz yok", "closed_total": 0})
    
    # İlk 3 pozla test
    test_batch = closed_positions[:3]
    results = {
        "closed_total": len(closed_positions),
        "test_batch_size": len(test_batch),
        "results": []
    }
    
    for c in test_batch:
        ticker = c.get("ticker")
        entry = {"ticker": ticker, "existing_binance_pnl": c.get("binance_pnl")}
        try:
            acilis = c.get("acilis", "")
            kapanis = c.get("kapanis", "")
            dt_open = datetime.strptime(acilis, "%Y-%m-%d %H:%M")
            dt_open_utc = dt_open - timedelta(hours=3)
            pos_start_ms = int(dt_open_utc.timestamp() * 1000) - 60000
            pos_end_ms = None
            if kapanis:
                dt_close = datetime.strptime(kapanis, "%Y-%m-%d %H:%M")
                dt_close_utc = dt_close - timedelta(hours=3)
                pos_end_ms = int(dt_close_utc.timestamp() * 1000) + 300000
            
            await asyncio.sleep(0.2)
            pnl_records = client.get_income_history(
                symbol=ticker, incomeType="REALIZED_PNL",
                limit=1000, startTime=pos_start_ms, endTime=pos_end_ms
            ) or []
            await asyncio.sleep(0.2)
            fee_records = client.get_income_history(
                symbol=ticker, incomeType="COMMISSION",
                limit=1000, startTime=pos_start_ms, endTime=pos_end_ms
            ) or []
            
            pnl_total = sum(float(r.get("income", 0)) for r in pnl_records)
            fee_total = sum(float(r.get("income", 0)) for r in fee_records)
            entry["pnl_count"] = len(pnl_records)
            entry["pnl_sum"] = round(pnl_total, 4)
            entry["fee_count"] = len(fee_records)
            entry["fee_sum"] = round(fee_total, 4)
            entry["net"] = round(pnl_total + fee_total, 2)
            entry["dashboard_kar"] = c.get("kar")
        except Exception as e:
            entry["error"] = str(e)
        results["results"].append(entry)
    
    return JSONResponse(results)


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
async def get_pause_status(system: str = "cab"):
    """v6.7: Sistem-aware pause durumu. ?system=cab | ram"""
    if system not in ("cab", "ram"):
        system = "cab"
    return JSONResponse({
        **get_pause_info(system),
        "system": system,
        "kill_switch_enabled": KILL_SWITCH_ENABLED,
        "stop_streak_window": STOP_STREAK_WINDOW,
        "stop_streak_threshold": STOP_STREAK_THRESHOLD,
        "daily_loss_limit": DAILY_LOSS_LIMIT,
    })


@app.post("/api/toggle_pause")
async def toggle_pause(req: Request):
    """v6.7: Sistem-aware manuel pause toggle.
    Body: {"action": "pause"|"resume", "reason_text": "...", "system": "cab"|"ram"}
    """
    body = {}
    try:
        body = await req.json()
    except Exception:
        pass
    action = body.get("action", "").lower()
    reason_text = body.get("reason_text", "Manuel durdurma")
    system = body.get("system", "cab")
    if system not in ("cab", "ram"):
        system = "cab"

    if action == "pause":
        pause_bot("manual", reason_text, system=system)
    elif action == "resume":
        resume_bot(system=system)
    else:
        # toggle
        if is_paused(system):
            resume_bot(system=system)
        else:
            pause_bot("manual", reason_text, system=system)
    return JSONResponse({
        "success": True,
        "state": get_pause_info(system),
        "system": system,
        "msg": f"{system.upper()} " + ("durduruldu" if is_paused(system) else "tekrar aktif")
    })


@app.get("/api/max_pos_status")
async def get_max_pos_status(system: str = "cab"):
    """v6.7: Sistem-aware MAX POS durumu. ?system=cab | ram"""
    if system not in ("cab", "ram"):
        system = "cab"
    mp = data.get(_sys_key(system, "max_pos_state"), {})
    history = mp.get("change_history", [])
    today = now_tr()[:10]
    changes_today = [h for h in history if h.get("ts", "").startswith(today)]
    return JSONResponse({
        "current": get_max_positions(system),
        "system": system,
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
    """v6.7: Sistem-aware MAX POS değiştir.
    Body: {"value": 7, "reason": "...", "system": "cab"|"ram"}"""
    body = {}
    try:
        body = await req.json()
    except Exception:
        pass
    new_val = body.get("value")
    reason = body.get("reason", "manual")
    system = body.get("system", "cab")
    if system not in ("cab", "ram"):
        system = "cab"
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
    result = set_max_positions(new_val, reason, system=system)
    return JSONResponse({
        "success": True, **result,
        "system": system,
        "current": get_max_positions(system)
    })


# ═══════════════ v6.7: YENİ ENDPOINT'LER ═══════════════
@app.get("/api/mode_status")
async def get_mode_status():
    """v6.7: Hem CAB hem RAM için mode durumu"""
    return JSONResponse({
        "cab_mode": get_system_mode("cab"),
        "ram_mode": get_system_mode("ram"),
    })


@app.post("/api/toggle_mode")
async def toggle_mode(req: Request):
    """v6.7: CAB veya RAM sisteminin mode'unu değiştir.
    Body: {"system": "cab"|"ram", "mode": "live"|"shadow"}
    Eğer sistemde açık poz varsa uyarı döner.
    """
    body = {}
    try:
        body = await req.json()
    except Exception:
        pass
    system = body.get("system", "cab")
    mode = body.get("mode", "shadow")
    force = bool(body.get("force", False))

    if system not in ("cab", "ram"):
        return JSONResponse({"success": False, "error": "system 'cab' veya 'ram' olmalı"}, status_code=400)
    if mode not in ("live", "shadow"):
        return JSONResponse({"success": False, "error": "mode 'live' veya 'shadow' olmalı"}, status_code=400)

    current = get_system_mode(system)
    if current == mode:
        return JSONResponse({"success": True, "changed": False, "mode": mode, "msg": "zaten bu mode"})

    # Açık poz varsa uyarı
    open_positions = get_open_positions(system)
    if open_positions and not force:
        return JSONResponse({
            "success": False,
            "error": "open_positions_exist",
            "msg": f"{system.upper()} sisteminde {len(open_positions)} açık poz var. "
                   "Önce kapat veya force=true gönder.",
            "open_count": len(open_positions),
            "open_tickers": list(open_positions.keys()),
        }, status_code=409)

    set_system_mode(system, mode)
    return JSONResponse({
        "success": True, "changed": True,
        "system": system, "mode": mode,
        "msg": f"{system.upper()} → {mode.upper()}"
    })


@app.post("/api/archive_and_reset")
async def archive_and_reset(req: Request):
    """v6.7: Mevcut verileri arşivle ve temiz başlangıç yap.
    Body: {"description": "...", "reset_cab": true, "reset_ram": true}
    """
    body = {}
    try:
        body = await req.json()
    except Exception:
        pass
    description = body.get("description", "Manuel arşiv")
    reset_cab = body.get("reset_cab", True)
    reset_ram = body.get("reset_ram", True)

    # Snapshot al
    snapshot = {
        "archived_at": now_tr(),
        "description": description,
        "cab": {
            "open_positions": dict(data.get("open_positions", {})),
            "closed_positions": list(data.get("closed_positions", [])),
            "skipped_signals": list(data.get("skipped_signals", [])),
            "pause_state": dict(data.get("pause_state", {})),
            "max_pos_state": dict(data.get("max_pos_state", {})),
        },
        "ram": {
            "open_positions": dict(data.get("ram_open_positions", {})),
            "closed_positions": list(data.get("ram_closed_positions", [])),
            "skipped_signals": list(data.get("ram_skipped_signals", [])),
            "pause_state": dict(data.get("ram_pause_state", {})),
            "max_pos_state": dict(data.get("ram_max_pos_state", {})),
        },
        "legacy_shadow": {
            "shadow_positions": dict(data.get("shadow_positions", {})),
            "shadow_closed": list(data.get("shadow_closed", [])),
            "shadow_skipped": list(data.get("shadow_skipped", [])),
        },
    }

    if "archive" not in data:
        data["archive"] = []
    data["archive"].append(snapshot)
    # Sadece son 10 arşivi tut (disk yer kaplamasın)
    if len(data["archive"]) > 10:
        data["archive"] = data["archive"][-10:]

    archived_counts = {
        "cab_closed": len(snapshot["cab"]["closed_positions"]),
        "ram_closed": len(snapshot["ram"]["closed_positions"]),
        "legacy_closed": len(snapshot["legacy_shadow"]["shadow_closed"]),
    }

    # Reset
    if reset_cab:
        # Sadece kapanmış + kaçırılan sinyalleri sil (açık pozlar dokunulmaz!)
        data["closed_positions"] = []
        data["skipped_signals"] = []
    if reset_ram:
        data["ram_closed_positions"] = []
        data["ram_skipped_signals"] = []
        data["ram_open_positions"] = {}  # RAM açık pozlar sanaldır, sil
        # legacy shadow da temizlensin
        data["shadow_positions"] = {}
        data["shadow_closed"] = []
        data["shadow_skipped"] = []

    save_data(data)
    return JSONResponse({
        "success": True,
        "archived_at": snapshot["archived_at"],
        "archived_counts": archived_counts,
        "archive_index": len(data["archive"]) - 1,
        "reset_cab": reset_cab,
        "reset_ram": reset_ram,
    })


@app.get("/api/archive_list")
async def archive_list():
    """v6.7: Arşivlenen snapshot'ları listele (özet bilgi)"""
    archives = data.get("archive", [])
    summary = []
    for i, a in enumerate(archives):
        summary.append({
            "index": i,
            "archived_at": a.get("archived_at"),
            "description": a.get("description"),
            "cab_closed_count": len(a.get("cab", {}).get("closed_positions", [])),
            "ram_closed_count": len(a.get("ram", {}).get("closed_positions", [])),
            "legacy_closed_count": len(a.get("legacy_shadow", {}).get("shadow_closed", [])),
        })
    return JSONResponse({"archives": summary, "total": len(archives)})


@app.get("/api/archive_get/{index}")
async def archive_get(index: int):
    """v6.7: Belirli bir arşiv snapshot'ını tam olarak getir"""
    archives = data.get("archive", [])
    if index < 0 or index >= len(archives):
        return JSONResponse({"success": False, "error": "invalid index"}, status_code=404)
    return JSONResponse(archives[index])


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
    v6.6 Lite Patch 14: BACKGROUND migrate — arka planda çalışır.
    POST ile başlatır, hemen döner. GET /api/migrate_status ile durumu kontrol et.
    """
    import asyncio
    body = {}
    try:
        body = await req.json()
    except Exception:
        pass
    force_refresh = bool(body.get("force_refresh", False))
    action = body.get("action", "start")  # start | status | dry_run

    global _migrate_task_state

    if action == "status":
        # Sadece durum döner
        return JSONResponse(_migrate_task_state or {"status": "idle"})

    if action == "dry_run":
        # Senkron ÇABUK dry-run: sadece SAY, detayları verme
        closed_positions = data.get("closed_positions", [])
        total = len(closed_positions)
        to_process = 0
        already_have = 0
        no_acilis = 0
        for c in closed_positions:
            if not c.get("acilis"):
                no_acilis += 1
                continue
            if c.get("binance_pnl") is not None and not force_refresh:
                already_have += 1
                continue
            to_process += 1
        return JSONResponse({
            "dry_run": True,
            "total": total,
            "to_process": to_process,
            "already_have": already_have,
            "no_acilis": no_acilis,
            "force_refresh": force_refresh,
            "estimated_seconds": to_process,  # her poz ~1 sn
        })

    # action == "start" - background task başlat
    if _migrate_task_state and _migrate_task_state.get("status") == "running":
        return JSONResponse({"error": "Migrate zaten çalışıyor"}, status_code=409)

    _migrate_task_state = {
        "status": "running",
        "started_at": now_tr(),
        "force_refresh": force_refresh,
        "total": 0,
        "processed": 0,
        "migrated": 0,
        "skipped": 0,
        "failed": 0,
        "current_ticker": None,
        "errors": [],
        "completed_at": None,
        "summary": None,
    }

    # Background task olarak çalıştır
    asyncio.create_task(_run_migrate_background(force_refresh))

    return JSONResponse({"status": "started", "state": _migrate_task_state})


async def _run_migrate_background(force_refresh):
    """Arka planda pozları tek tek migrate eder. _migrate_task_state'i günceller."""
    import asyncio
    global _migrate_task_state

    try:
        closed_positions = data.get("closed_positions", [])
        _migrate_task_state["total"] = len(closed_positions)

        total_old_kar = 0.0
        total_new_binance = 0.0

        for idx, c in enumerate(closed_positions):
            ticker = c.get("ticker")
            _migrate_task_state["current_ticker"] = ticker
            _migrate_task_state["processed"] = idx + 1

            # Atlanacak mı?
            if c.get("binance_pnl") is not None and not force_refresh:
                _migrate_task_state["skipped"] += 1
                continue
            if not c.get("acilis"):
                _migrate_task_state["skipped"] += 1
                continue

            try:
                acilis = c.get("acilis", "")
                kapanis = c.get("kapanis", "")
                dt_open = datetime.strptime(acilis, "%Y-%m-%d %H:%M")
                dt_open_utc = dt_open - timedelta(hours=3)
                pos_start_ms = int(dt_open_utc.timestamp() * 1000) - 60000

                pos_end_ms = None
                if kapanis:
                    dt_close = datetime.strptime(kapanis, "%Y-%m-%d %H:%M")
                    dt_close_utc = dt_close - timedelta(hours=3)
                    pos_end_ms = int(dt_close_utc.timestamp() * 1000) + 300000

                await asyncio.sleep(0.2)
                pnl_records = client.get_income_history(
                    symbol=ticker, incomeType="REALIZED_PNL",
                    limit=1000, startTime=pos_start_ms, endTime=pos_end_ms
                ) or []

                await asyncio.sleep(0.2)
                fee_records = client.get_income_history(
                    symbol=ticker, incomeType="COMMISSION",
                    limit=1000, startTime=pos_start_ms, endTime=pos_end_ms
                ) or []

                if not pnl_records and not fee_records:
                    _migrate_task_state["skipped"] += 1
                    continue

                pnl_total = sum(float(r.get("income", 0)) for r in pnl_records)
                fee_total = sum(float(r.get("income", 0)) for r in fee_records)
                net_pnl = pnl_total + fee_total

                total_old_kar += c.get("kar", 0)
                total_new_binance += net_pnl

                c["binance_pnl"] = round(net_pnl, 2)
                c["binance_fee"] = round(fee_total, 2)
                _migrate_task_state["migrated"] += 1

                # Her 5 pozda bir data kaydet (crash safety)
                if (idx + 1) % 5 == 0:
                    save_data(data)

            except Exception as e:
                _migrate_task_state["failed"] += 1
                err = {"ticker": ticker, "reason": str(e)[:200]}
                _migrate_task_state["errors"].append(err)
                if len(_migrate_task_state["errors"]) > 20:
                    _migrate_task_state["errors"] = _migrate_task_state["errors"][-20:]

        # Final save
        save_data(data)

        _migrate_task_state["status"] = "completed"
        _migrate_task_state["completed_at"] = now_tr()
        _migrate_task_state["current_ticker"] = None
        _migrate_task_state["summary"] = {
            "dashboard_total": round(total_old_kar, 2),
            "binance_total": round(total_new_binance, 2),
            "fark": round(total_new_binance - total_old_kar, 2),
        }
        print(f"[MIGRATE-BG] TAMAMLANDI: {_migrate_task_state['migrated']} poz, fark {_migrate_task_state['summary']['fark']:+.2f}$")

    except Exception as e:
        _migrate_task_state["status"] = "error"
        _migrate_task_state["error"] = str(e)[:500]
        _migrate_task_state["completed_at"] = now_tr()
        print(f"[MIGRATE-BG] HATA: {e}")


@app.get("/api/migrate_status")
async def get_migrate_status():
    """Background migrate task'ının durumunu döner."""
    global _migrate_task_state
    return JSONResponse(_migrate_task_state or {"status": "idle"})


# v6.7 Patch 1: Market regime parser helpers
def _parse_field(msg, key):
    """Alarm mesajından 'key:VALUE' şeklinde değer çıkar. key yoksa None."""
    try:
        import re as _re
        m = _re.search(rf"{key}:([A-Z_]+)", msg)
        return m.group(1) if m else None
    except Exception:
        return None

def _parse_market_detail(msg):
    """USDTD:X|BTCD:Y|OTHD:Z|ETHBTC:W şeklindeki detayı parse eder."""
    try:
        import re as _re
        result = {}
        for key in ["USDTD", "BTCD", "OTHD", "ETHBTC"]:
            m = _re.search(rf"{key}:([A-Z]+)", msg)
            if m:
                result[key.lower()] = m.group(1)
        return result if result else None
    except Exception:
        return None


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

def shadow_handle_giris(msg, system_tag, system_code=None):
    """v6.7: Sanal poz aç. system_code='cab' → ram_* alanları kullanma, 
    system_code='ram' → ram_* alanları.
    Eğer system_code None ise system_tag'den tahmin et:
      'CAB v14' veya 'CAB v13' → 'cab'
      'RAM v14' veya 'RAM v15' → 'ram'
    """
    if system_code is None:
        system_code = "cab" if system_tag.upper().startswith("CAB") else "ram"

    parsed = parse_giris(msg)
    if not parsed:
        return {"status": "parse_error"}

    ticker = parsed["ticker"]
    now = now_tr()

    # v6.7: Pause kontrolü (system-aware)
    if is_paused(system_code):
        pi = get_pause_info(system_code)
        sk_key = _sys_key(system_code, "skipped_signals")
        if sk_key not in data:
            data[sk_key] = []
        data[sk_key].append({
            "ticker": ticker, "sebep": f"[SHADOW] BOT PAUSE: {pi.get('reason', 'N/A')}",
            "zaman": now, "giris": parsed.get("giris"), "system": system_tag,
        })
        save_data(data)
        return {"status": "shadow_paused", "system_tag": system_tag}

    # Max poz sınırı (system-aware)
    open_key = _sys_key(system_code, "open_positions")
    if open_key not in data:
        data[open_key] = {}
    max_allowed = get_max_positions(system_code)
    if len(data[open_key]) >= max_allowed:
        sk_key = _sys_key(system_code, "skipped_signals")
        if sk_key not in data:
            data[sk_key] = []
        data[sk_key].append({
            "ticker": ticker, "sebep": f"[SHADOW] MAX {max_allowed} dolu",
            "zaman": now, "giris": parsed.get("giris"), "system": system_tag,
        })
        save_data(data)
        return {"status": "shadow_max_full", "system_tag": system_tag}

    # Zaten açık mı kontrolü
    if ticker in data[open_key]:
        return {"status": "shadow_already_open", "ticker": ticker}

    pos = {
        "ticker": ticker, "giris": parsed.get("giris", 0),
        "marj": parsed.get("marj", 100), "lev": parsed.get("lev", 10),
        "stop": parsed.get("stop", 0),
        "original_stop": parsed.get("stop", 0),
        "current_stop": parsed.get("stop", 0),
        "tp1": parsed.get("tp1", 0), "tp2": parsed.get("tp2", 0),
        "kapat_oran": parsed.get("kapat_oran", 60),
        "atr_skor": parsed.get("atr_skor", 100),
        "tp1_hit": False, "tp2_hit": False,
        "tp1_kar": 0, "tp2_kar": 0,
        "zaman": now, "acilis": now,
        "hh": parsed.get("giris", 0), "ll": parsed.get("giris", 0),
        "hh_pct": 0.0,
        "system": system_tag,
        "system_code": system_code,  # v6.7: cab veya ram
        # v6.7 market regime (Pine mesajından gelirse)
        "market_regime": _parse_field(msg, "MktRej"),
        "market_detail": _parse_market_detail(msg),
    }

    data[open_key][ticker] = pos
    save_data(data)
    print(f"[SHADOW:{system_code.upper()}:{system_tag}] GIRIS {ticker} @ {pos['giris']} | MR:{pos.get('market_regime')}")
    return {"status": "shadow_opened", "shadow": True, "system_tag": system_tag, "ticker": ticker}


def shadow_handle_tp1(msg, system_tag, system_code=None):
    """v6.7: TP1 sanal kısmi kapama"""
    if system_code is None:
        system_code = "cab" if system_tag.upper().startswith("CAB") else "ram"

    parsed = parse_tp1(msg)
    if not parsed:
        return {"status": "parse_error"}
    ticker = parsed["ticker"]

    open_key = _sys_key(system_code, "open_positions")
    positions = data.get(open_key, {})
    if ticker not in positions:
        print(f"[SHADOW:{system_code.upper()}:{system_tag}] TP1 ignore — {ticker} yok")
        return {"status": "shadow_not_found", "ticker": ticker}

    pos = positions[ticker]
    tp1_px = parsed.get("tp1") or pos["tp1"]
    kapat = parsed.get("kapat_oran", pos.get("kapat_oran", 60))

    tp1_kar = shadow_calc_kar(pos["marj"], pos["lev"], pos["giris"], tp1_px, kapat)
    pos["tp1_hit"] = True
    pos["tp1_kar"] = tp1_kar
    pos["current_stop"] = pos["giris"]  # BE
    pos["kapat_oran"] = kapat
    save_data(data)
    print(f"[SHADOW:{system_code.upper()}:{system_tag}] TP1 {ticker} | +{tp1_kar}$ | BE:{pos['giris']}")
    return {"status": "shadow_tp1", "shadow": True, "kar": tp1_kar}


def shadow_handle_tp2(msg, system_tag, system_code=None):
    """v6.7: TP2 sanal kısmi kapama"""
    if system_code is None:
        system_code = "cab" if system_tag.upper().startswith("CAB") else "ram"

    parsed = parse_tp2(msg)
    if not parsed:
        return {"status": "parse_error"}
    ticker = parsed["ticker"]

    open_key = _sys_key(system_code, "open_positions")
    positions = data.get(open_key, {})
    if ticker not in positions:
        return {"status": "shadow_not_found", "ticker": ticker}

    pos = positions[ticker]
    tp2_px = parsed.get("tp2") or pos["tp2"]
    # TP2 hep %25 kapat
    tp2_kar = shadow_calc_kar(pos["marj"], pos["lev"], pos["giris"], tp2_px, 25)
    pos["tp2_hit"] = True
    pos["tp2_kar"] = tp2_kar
    pos["current_stop"] = pos["tp1"]  # TP1 seviyesine çek
    save_data(data)
    print(f"[SHADOW:{system_code.upper()}:{system_tag}] TP2 {ticker} | +{tp2_kar}$")
    return {"status": "shadow_tp2", "shadow": True, "kar": tp2_kar}


def shadow_handle_stop_or_trail(msg, system_tag, kind="STOP", system_code=None):
    """v6.7: Sanal tam kapama + sistem-aware closed_positions kayıt"""
    if system_code is None:
        system_code = "cab" if system_tag.upper().startswith("CAB") else "ram"

    # Mesaj içinden "TRAIL" kelimesi varsa trail olarak işle
    if "TRAIL" in msg.upper() or "trailing" in msg:
        kind = "TRAIL"
        parsed = parse_trail(msg) or parse_stop(msg)
    else:
        parsed = parse_stop(msg) or parse_trail(msg)

    if not parsed:
        return {"status": "parse_error"}
    ticker = parsed["ticker"]

    open_key = _sys_key(system_code, "open_positions")
    closed_key = _sys_key(system_code, "closed_positions")
    positions = data.get(open_key, {})
    if ticker not in positions:
        return {"status": "shadow_not_found", "ticker": ticker}

    pos = positions[ticker]

    # TP1 önceden oldu mu?
    tp1_kar = pos.get("tp1_kar", 0)
    tp2_kar = pos.get("tp2_kar", 0)
    kapat_oran = pos.get("kapat_oran", 60)

    # Exit fiyat (stop veya trail)
    exit_px = parsed.get("stop") or parsed.get("trail") or pos.get("current_stop", pos["giris"])

    # Kalan oranı kapat
    if pos.get("tp2_hit"):
        kalan = 100 - kapat_oran - 25  # TP1 + TP2 düşülür
    elif pos.get("tp1_hit"):
        kalan = 100 - kapat_oran  # sadece TP1 düşülür
    else:
        kalan = 100  # hiçbiri olmadı, hepsi

    remaining_kar = shadow_calc_kar(pos["marj"], pos["lev"], pos["giris"], exit_px, kalan)
    total_kar = round(tp1_kar + tp2_kar + remaining_kar, 2)

    # Sonuç metni
    if pos.get("tp2_hit"):
        sonuc = f"TP1+TP2+{'Trail' if kind=='TRAIL' else 'Stop'}"
    elif pos.get("tp1_hit"):
        sonuc = f"TP1+{'Trail' if kind=='TRAIL' else 'Stop'}"
    else:
        sonuc = f"{'Trail' if kind=='TRAIL' else 'Stop'}"

    closed_pos = {
        "ticker": ticker, "giris": pos["giris"],
        "marj": pos["marj"], "lev": pos["lev"],
        "sonuc": sonuc, "kar": total_kar,
        "tp1_kar": tp1_kar, "tp2_kar": tp2_kar, "trail_kar": remaining_kar,
        "hh_pct": pos.get("hh_pct", 0),
        "atr_skor": pos.get("atr_skor", 100),
        "kapat_oran": kapat_oran,
        "acilis": pos.get("acilis"), "kapanis": now_tr(),
        "system": system_tag,
        "system_code": system_code,
        "market_regime": pos.get("market_regime"),
        "market_detail": pos.get("market_detail"),
        # Shadow'da binance_pnl olmaz
        "binance_pnl": None, "binance_fee": None,
        "shadow": True,  # işaretle
    }

    if closed_key not in data:
        data[closed_key] = []
    data[closed_key].append(closed_pos)

    # Açık pozdan sil
    del data[open_key][ticker]

    # Auto-pause kontrolü (system-aware)
    check_auto_reduce_max_pos(system_code)
    check_auto_pause_triggers(system_code)

    save_data(data)
    print(f"[SHADOW:{system_code.upper()}:{system_tag}] {kind} {ticker} | Toplam:{total_kar}$")
    return {"status": "shadow_closed", "shadow": True, "kar": total_kar, "sonuc": sonuc}


@app.post("/webhook")
async def webhook(req: Request):
    msg = (await req.body()).decode()
    print(f"[ALERT] {msg}")
    mode_tag = "[CANLI]" if not TEST_MODE else "[TEST]"

    # ═══════════════ v6.7: MODE-AWARE ROUTING ═══════════════
    # Prefix'ten hangi sistem olduğunu anla:
    #   CAB v13, CAB v14  → cab
    #   RAM v14, RAM v15  → ram
    # Sonra data[f"{system}_mode"]'a göre canlı/shadow karar ver.
    
    system_code = None
    system_tag = None
    msg_kind = None  # "giris" | "tp1" | "tp2" | "stop" | "trail"
    
    # Prefix tespit
    for tag, code_sys in [("CAB v14", "cab"), ("CAB v13", "cab"),
                          ("RAM v15", "ram"), ("RAM v14", "ram")]:
        if msg.startswith(f"{tag} TP1 |"):
            system_tag, system_code, msg_kind = tag, code_sys, "tp1"; break
        if msg.startswith(f"{tag} TP2 |"):
            system_tag, system_code, msg_kind = tag, code_sys, "tp2"; break
        if msg.startswith(f"{tag} STOP |"):
            system_tag, system_code, msg_kind = tag, code_sys, "stop"; break
        if msg.startswith(f"{tag} TRAIL |"):
            system_tag, system_code, msg_kind = tag, code_sys, "trail"; break
        if msg.startswith(f"{tag} |"):
            system_tag, system_code, msg_kind = tag, code_sys, "giris"; break

    if system_code is None:
        # Setup/Hazır mesajları
        if msg.startswith("CAB SETUP") or msg.startswith("CAB HAZIR") or            msg.startswith("RAM SETUP") or msg.startswith("RAM HAZIR"):
            return {"status": "info_ignored"}
        print(f"[UNKNOWN] {msg[:80]}")
        return {"status": "unknown"}

    # Mode kontrol
    current_mode = get_system_mode(system_code)
    
    # SHADOW MODE → shadow handler
    if current_mode == "shadow":
        if msg_kind == "giris":
            return shadow_handle_giris(msg, system_tag, system_code)
        elif msg_kind == "tp1":
            return shadow_handle_tp1(msg, system_tag, system_code)
        elif msg_kind == "tp2":
            return shadow_handle_tp2(msg, system_tag, system_code)
        elif msg_kind == "stop":
            return shadow_handle_stop_or_trail(msg, system_tag, "STOP", system_code)
        elif msg_kind == "trail":
            return shadow_handle_stop_or_trail(msg, system_tag, "TRAIL", system_code)

    # LIVE MODE → canlı handler
    # NOT: Şu an canlı handler sadece CAB için. RAM için canlı
    # handler YAPILMADI (henüz test edilmemiş bir strateji).
    # RAM mode="live" denerse uyarı veririz.
    if system_code == "ram" and current_mode == "live":
        print(f"[WARN] RAM live mode henüz test edilmedi — shadow'a düşürüyorum")
        if msg_kind == "giris":
            return shadow_handle_giris(msg, system_tag, system_code)
        elif msg_kind == "tp1":
            return shadow_handle_tp1(msg, system_tag, system_code)
        elif msg_kind == "tp2":
            return shadow_handle_tp2(msg, system_tag, system_code)
        elif msg_kind == "stop":
            return shadow_handle_stop_or_trail(msg, system_tag, "STOP", system_code)
        elif msg_kind == "trail":
            return shadow_handle_stop_or_trail(msg, system_tag, "TRAIL", system_code)

    # === CAB LIVE MODE (mevcut kod) ===
    # CAB v13 veya CAB v14 mesajı + cab_mode="live"
    # Eski kod "CAB v13 |" kontrolü yapıyor — onu bypass edip aynı kodu çalıştır
    if msg_kind == "giris":
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
            # v6.7: market regime ve sistem etiketi
            "market_regime": parsed.get("market_regime"),
            "market_detail": parsed.get("market_detail"),
            "system": system_tag,
            "system_code": system_code,
        }
        save_data(data)
        print(f"{mode_tag} GIRIS: {ticker} | {parsed['giris']} | Marj:{parsed['marj']}$ | {parsed['lev']}x")
        return {"status": "opened"}

    # === TP1 ===
    elif msg_kind == "tp1":
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
    elif msg_kind == "tp2":
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
    elif msg_kind == "trail":
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
        # v6.7: market regime + system bilgileri pos'tan al
        if isinstance(closed, dict):
            closed.setdefault("market_regime", pos.get("market_regime") if isinstance(pos, dict) else None)
            closed.setdefault("market_detail", pos.get("market_detail") if isinstance(pos, dict) else None)
            closed.setdefault("system", pos.get("system", "CAB v14") if isinstance(pos, dict) else "CAB v14")
            closed.setdefault("system_code", pos.get("system_code", "cab") if isinstance(pos, dict) else "cab")
        data["closed_positions"].append(closed)
        del data["open_positions"][ticker]
        save_data(data)
        print(f"{mode_tag} TRAIL({parsed['tp_type']}): {ticker} | {sonuc} | dashboard:+{total_kar}$ binance:{binance_pnl}$")
        check_auto_pause_triggers()  # v6.6 Lite Patch 5
        check_auto_reduce_max_pos()   # v6.6 Lite Patch 8
        return {"status": "trail_closed"}

    # === STOP ===
    elif msg_kind == "stop":
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
        # v6.7: market regime + system bilgileri pos'tan al
        if isinstance(closed, dict):
            closed.setdefault("market_regime", pos.get("market_regime") if isinstance(pos, dict) else None)
            closed.setdefault("market_detail", pos.get("market_detail") if isinstance(pos, dict) else None)
            closed.setdefault("system", pos.get("system", "CAB v14") if isinstance(pos, dict) else "CAB v14")
            closed.setdefault("system_code", pos.get("system_code", "cab") if isinstance(pos, dict) else "cab")
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
                # v6.7: market regime + system bilgileri pos'tan al
                if isinstance(closed, dict):
                    closed.setdefault("market_regime", pos.get("market_regime") if isinstance(pos, dict) else None)
                    closed.setdefault("market_detail", pos.get("market_detail") if isinstance(pos, dict) else None)
                    closed.setdefault("system", pos.get("system", "CAB v14") if isinstance(pos, dict) else "CAB v14")
                    closed.setdefault("system_code", pos.get("system_code", "cab") if isinstance(pos, dict) else "cab")
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
    print(f"[BOOT] CAB Bot v6.7 Patch 1 | Mode:{'CANLI' if not TEST_MODE else 'TEST'} | MaxPos:{get_max_positions()} | Timeout:{TIMEOUT_HOURS}s (mutlak:{TIMEOUT_ABSOLUTE_HOURS}s, eşik:{TIMEOUT_PRESSURE_THRESHOLD}) | HL:{HIGH_LOW_CHECK_INTERVAL_SEC}s | RAM Shadow:ON")


# ============ DASHBOARD v6.1 PRO ============
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    """v6.7: Simetrik iki sistem (CAB + RAM) dashboard"""
    cab_mode = get_system_mode("cab")
    ram_mode = get_system_mode("ram")
    cab_mod_color = "#4ade80" if cab_mode == "live" else "#94a3b8"
    ram_mod_color = "#4ade80" if ram_mode == "live" else "#94a3b8"
    cab_mod_text = "CANLI" if cab_mode == "live" else "SHADOW"
    ram_mod_text = "CANLI" if ram_mode == "live" else "SHADOW"

    html = r"""<!DOCTYPE html>
<html lang="tr"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CAB Bot v6.7 — Dual System Dashboard</title>
<style>
*{box-sizing:border-box}
body{font-family:-apple-system,system-ui,sans-serif;background:#0f172a;color:#e5e7eb;margin:0;padding:10px}
h1{font-size:18px;margin:0 0 4px}
.muted{color:#94a3b8;font-size:11px}
.bar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:6px 0 10px}
.btn{background:#1e40af;color:#fff;border:none;padding:8px 12px;border-radius:6px;cursor:pointer;font-size:12px;font-weight:600}
.btn:hover{background:#2563eb}
.btn-sm{padding:5px 9px;font-size:11px}
.btn-red{background:#dc2626}.btn-red:hover{background:#ef4444}
.btn-green{background:#059669}.btn-green:hover{background:#10b981}
.btn-orange{background:#ea580c}.btn-orange:hover{background:#f97316}
.btn-purple{background:#7c3aed}.btn-purple:hover{background:#8b5cf6}
.btn-grey{background:#475569}.btn-grey:hover{background:#64748b}

/* SİSTEM SEKMELERİ */
#systemTabs{display:flex;gap:4px;margin:14px 0 0;border-bottom:2px solid #334155}
.sysTab{flex:1;padding:12px;border:none;border-radius:8px 8px 0 0;cursor:pointer;font-size:14px;font-weight:700;background:#1e293b;color:#94a3b8;border-bottom:3px solid transparent;transition:all 0.2s}
.sysTab.active{background:#16a34a;color:white;border-bottom:3px solid #4ade80}
.sysTab.active.shadow{background:#475569;color:white;border-bottom:3px solid #94a3b8}
.sysBadge{background:rgba(0,0,0,0.3);padding:2px 8px;border-radius:10px;font-size:11px;margin-left:6px}

/* PANEL — Sistem içeriği */
.sysPanel{display:none;padding:12px 0}
.sysPanel.active{display:block}

/* MOD SWITCH */
.modeBox{display:flex;align-items:center;justify-content:space-between;background:#1e293b;padding:10px 14px;border-radius:8px;margin:10px 0;border:1px solid #334155}
.modeLabel{font-size:13px;font-weight:600;color:#e5e7eb}
.modeBtn{padding:6px 14px;border:none;border-radius:5px;cursor:pointer;font-size:12px;font-weight:700;color:white}
.modeBtn.shadow{background:#475569}
.modeBtn.live{background:#16a34a}

/* Pause kutusu */
.pauseBox{padding:12px;border-radius:8px;margin:8px 0;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px}
.pauseBox.active{background:#7f1d1d;border:1px solid #dc2626}
.pauseBox.inactive{background:#064e3b;border:1px solid #059669}
.pauseTitle{font-weight:700;font-size:13px}

/* MAX POS */
.maxBox{display:flex;align-items:center;gap:8px;background:#1e293b;padding:8px 12px;border-radius:6px;margin:6px 0}
.maxNum{font-size:18px;font-weight:700;color:#fbbf24;min-width:30px;text-align:center}

/* Stat kutuları */
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:8px;margin:10px 0}
.stat{background:#1e293b;padding:10px;border-radius:6px;text-align:center}
.stat .v{font-size:18px;font-weight:700}
.stat .l{font-size:10px;color:#94a3b8;margin-top:2px}
.green{color:#4ade80}.red{color:#f87171}.yellow{color:#fbbf24}

/* Section */
.section{background:#1e293b;border-radius:8px;padding:10px;margin:10px 0}
.section h3{margin:0 0 8px;font-size:13px;color:#a5b4fc;display:flex;align-items:center;gap:8px}
.section h3 .ct{background:#334155;padding:2px 7px;border-radius:10px;font-size:11px}

/* Tablo */
table{width:100%;border-collapse:collapse;font-size:11px}
th,td{padding:5px 6px;text-align:left;border-bottom:1px solid #334155}
th{background:#0f172a;color:#94a3b8;font-weight:600;cursor:pointer;user-select:none;position:sticky;top:0;z-index:5}
th[data-sort]:hover{background:#1e293b}
th.sorted-asc::after{content:" ▲";color:#4ade80}
th.sorted-desc::after{content:" ▼";color:#4ade80}
td{font-size:11px}
tr:hover{background:#16213a}
.tableWrap{overflow-x:auto;max-height:400px;overflow-y:auto}

/* Toast */
#toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:#0f172a;border:1px solid #334155;padding:10px 16px;border-radius:6px;display:none;z-index:9999;font-size:12px;max-width:90vw}
#toast.error{border-color:#dc2626;color:#fca5a5}
#toast.success{border-color:#059669;color:#6ee7b7}

/* Mode rejim göstergesi */
.regimeChip{display:inline-block;padding:2px 7px;border-radius:8px;font-size:10px;font-weight:600;margin-left:4px}
.regimeChip.alt{background:#064e3b;color:#6ee7b7}
.regimeChip.btc{background:#7f1d1d;color:#fca5a5}
.regimeChip.risk{background:#7c2d12;color:#fdba74}
.regimeChip.neu{background:#1e293b;color:#94a3b8}

/* Modal */
.modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,0.8);z-index:1000;padding:20px;overflow-y:auto}
.modal.show{display:block}
.modalBox{background:#1e293b;border-radius:10px;padding:20px;max-width:900px;margin:20px auto;border:2px solid #475569}
.modalBox h2{margin:0 0 12px;color:#a5b4fc;font-size:18px}
.modalClose{float:right;background:#dc2626;color:white;border:none;padding:6px 12px;border-radius:5px;cursor:pointer}

/* Mobil */
@media(max-width:640px){
  body{padding:6px}
  h1{font-size:15px}
  th,td{font-size:10px;padding:4px}
  .stats{grid-template-columns:repeat(2,1fr)}
  .sysTab{padding:10px 6px;font-size:12px}
  .modeBox{flex-direction:column;align-items:stretch}
}
</style>
</head>
<body>

<h1>🤖 CAB Bot v6.7 — Dual System</h1>
<div class="muted">⟳ <span id="lastUpdate">—</span> | Veri 10sn'de yenilenir</div>

<!-- Mod genel bilgi (üst) -->
<div class="bar">
  <span style="background:""" + cab_mod_color + r""";color:#0f172a;padding:4px 10px;border-radius:6px;font-weight:700;font-size:12px">CAB: """ + cab_mod_text + r"""</span>
  <span style="background:""" + ram_mod_color + r""";color:#0f172a;padding:4px 10px;border-radius:6px;font-weight:700;font-size:12px">RAM: """ + ram_mod_text + r"""</span>
  <button class="btn btn-purple btn-sm" onclick="openArchive()">📚 Arşiv</button>
  <button class="btn btn-orange btn-sm" onclick="archiveAndReset()">🧹 Arşivle + Temizle</button>
  <button class="btn btn-grey btn-sm" onclick="migratePnl()">🔥 Binance PNL Çek</button>
</div>

<!-- Sistem Sekmeleri -->
<div id="systemTabs">
  <button id="tabCab" class="sysTab active" onclick="switchSys('cab')">
    🟢 CAB v14 — <span id="cabModeLabel">""" + cab_mod_text + r"""</span>
    <span id="cabBadge" class="sysBadge">0</span>
  </button>
  <button id="tabRam" class="sysTab" onclick="switchSys('ram')">
    🌓 RAM v15 — <span id="ramModeLabel">""" + ram_mod_text + r"""</span>
    <span id="ramBadge" class="sysBadge">0</span>
  </button>
</div>

<!-- CAB Panel -->
<div id="panelCab" class="sysPanel active">
  <!-- Mode Toggle -->
  <div class="modeBox">
    <div>
      <div class="modeLabel">⚡ Çalışma Modu</div>
      <div class="muted" style="margin-top:3px" id="cabModeDesc">—</div>
    </div>
    <button id="cabModeBtn" class="modeBtn shadow" onclick="toggleMode('cab')">SHADOW</button>
  </div>

  <!-- Pause Box -->
  <div id="cabPauseBox" class="pauseBox inactive">
    <div>
      <div class="pauseTitle" id="cabPauseTitle">✅ Aktif</div>
      <div class="muted" id="cabPauseDetail">Yeni sinyaller kabul ediliyor</div>
    </div>
    <button id="cabPauseBtn" class="btn btn-red btn-sm" onclick="togglePause('cab')">⏸ Durdur</button>
  </div>

  <!-- MAX POS -->
  <div class="maxBox">
    <span style="font-size:12px">📊 MAX Pozisyon:</span>
    <button class="btn btn-grey btn-sm" onclick="setMaxPos('cab',-1)">−</button>
    <span class="maxNum" id="cabMaxNum">7</span>
    <button class="btn btn-grey btn-sm" onclick="setMaxPos('cab',+1)">+</button>
    <span class="muted" style="margin-left:auto" id="cabMaxInfo">min:3 max:12</span>
  </div>

  <!-- Stats -->
  <div class="stats">
    <div class="stat"><div class="v" id="cabOpenN">0</div><div class="l">Açık</div></div>
    <div class="stat"><div class="v" id="cabClosedN">0</div><div class="l">Kapanan</div></div>
    <div class="stat"><div class="v" id="cabNetKar">$0</div><div class="l">Net Kar</div></div>
    <div class="stat"><div class="v" id="cabWR">0%</div><div class="l">Win Rate</div></div>
    <div class="stat"><div class="v" id="cabTodayKar">$0</div><div class="l">Bugün</div></div>
    <div class="stat"><div class="v" id="cabSkipN">0</div><div class="l">Kaçırılan</div></div>
  </div>

  <!-- Açık Pozlar -->
  <div class="section">
    <h3>📌 Açık Pozisyonlar <span class="ct" id="cabOpenCt">0</span>
      <button class="btn btn-grey btn-sm" style="margin-left:auto" onclick="exportCSV('cab','open')">CSV</button>
    </h3>
    <div class="tableWrap">
      <table>
        <thead><tr>
          <th data-sort="ticker">Coin</th>
          <th data-sort="marj">Marj</th>
          <th data-sort="lev">Lev</th>
          <th data-sort="giris">Giriş</th>
          <th>Stop</th><th>TP1</th><th>TP2</th>
          <th data-sort="hh_pct">HH%</th>
          <th data-sort="market_regime">Rejim</th>
          <th data-sort="zaman">Zaman</th>
        </tr></thead>
        <tbody id="cabOpenBody"><tr><td colspan="10" style="text-align:center;color:#94a3b8;padding:14px">Açık poz yok</td></tr></tbody>
      </table>
    </div>
  </div>

  <!-- Kapanan -->
  <div class="section">
    <h3>📜 Kapanan Pozisyonlar (son 50) <span class="ct" id="cabClosedCt">0</span>
      <button class="btn btn-grey btn-sm" style="margin-left:auto" onclick="exportCSV('cab','closed')">CSV</button>
    </h3>
    <div class="tableWrap">
      <table>
        <thead><tr>
          <th>Coin</th><th>Sonuç</th>
          <th data-sort="kar">Dashboard K/Z</th>
          <th data-sort="binance_pnl">Binance K/Z</th>
          <th>Rejim</th>
          <th>Süre</th>
          <th>Kapanış</th>
        </tr></thead>
        <tbody id="cabClosedBody"></tbody>
      </table>
    </div>
  </div>

  <!-- Kaçırılan -->
  <div class="section">
    <h3>🚫 Kaçırılan Sinyaller (bugün) <span class="ct" id="cabSkipCt">0</span></h3>
    <div class="tableWrap" style="max-height:200px">
      <table>
        <thead><tr><th>Coin</th><th>Sebep</th><th>Sinyal Fiyat</th><th>Zaman</th></tr></thead>
        <tbody id="cabSkipBody"></tbody>
      </table>
    </div>
  </div>
</div>

<!-- RAM Panel — TIPATIP AYNI yapı -->
<div id="panelRam" class="sysPanel">
  <!-- Mode Toggle -->
  <div class="modeBox">
    <div>
      <div class="modeLabel">⚡ Çalışma Modu</div>
      <div class="muted" style="margin-top:3px" id="ramModeDesc">—</div>
    </div>
    <button id="ramModeBtn" class="modeBtn shadow" onclick="toggleMode('ram')">SHADOW</button>
  </div>

  <!-- Pause Box -->
  <div id="ramPauseBox" class="pauseBox inactive">
    <div>
      <div class="pauseTitle" id="ramPauseTitle">✅ Aktif</div>
      <div class="muted" id="ramPauseDetail">Yeni sinyaller kabul ediliyor</div>
    </div>
    <button id="ramPauseBtn" class="btn btn-red btn-sm" onclick="togglePause('ram')">⏸ Durdur</button>
  </div>

  <!-- MAX POS -->
  <div class="maxBox">
    <span style="font-size:12px">📊 MAX Pozisyon:</span>
    <button class="btn btn-grey btn-sm" onclick="setMaxPos('ram',-1)">−</button>
    <span class="maxNum" id="ramMaxNum">7</span>
    <button class="btn btn-grey btn-sm" onclick="setMaxPos('ram',+1)">+</button>
    <span class="muted" style="margin-left:auto" id="ramMaxInfo">min:3 max:12</span>
  </div>

  <!-- Stats -->
  <div class="stats">
    <div class="stat"><div class="v" id="ramOpenN">0</div><div class="l">Açık</div></div>
    <div class="stat"><div class="v" id="ramClosedN">0</div><div class="l">Kapanan</div></div>
    <div class="stat"><div class="v" id="ramNetKar">$0</div><div class="l">Net Kar</div></div>
    <div class="stat"><div class="v" id="ramWR">0%</div><div class="l">Win Rate</div></div>
    <div class="stat"><div class="v" id="ramTodayKar">$0</div><div class="l">Bugün</div></div>
    <div class="stat"><div class="v" id="ramSkipN">0</div><div class="l">Kaçırılan</div></div>
  </div>

  <!-- Açık Pozlar -->
  <div class="section">
    <h3>📌 Açık Pozisyonlar <span class="ct" id="ramOpenCt">0</span>
      <button class="btn btn-grey btn-sm" style="margin-left:auto" onclick="exportCSV('ram','open')">CSV</button>
    </h3>
    <div class="tableWrap">
      <table>
        <thead><tr>
          <th>Coin</th><th>Marj</th><th>Lev</th><th>Giriş</th>
          <th>Stop</th><th>TP1</th><th>TP2</th>
          <th>HH%</th><th>Rejim</th><th>Zaman</th>
        </tr></thead>
        <tbody id="ramOpenBody"><tr><td colspan="10" style="text-align:center;color:#94a3b8;padding:14px">Açık poz yok</td></tr></tbody>
      </table>
    </div>
  </div>

  <!-- Kapanan -->
  <div class="section">
    <h3>📜 Kapanan Pozisyonlar (son 50) <span class="ct" id="ramClosedCt">0</span>
      <button class="btn btn-grey btn-sm" style="margin-left:auto" onclick="exportCSV('ram','closed')">CSV</button>
    </h3>
    <div class="tableWrap">
      <table>
        <thead><tr>
          <th>Coin</th><th>Sonuç</th>
          <th>Dashboard K/Z</th>
          <th>Binance K/Z</th>
          <th>Rejim</th><th>Süre</th><th>Kapanış</th>
        </tr></thead>
        <tbody id="ramClosedBody"></tbody>
      </table>
    </div>
  </div>

  <!-- Kaçırılan -->
  <div class="section">
    <h3>🚫 Kaçırılan Sinyaller (bugün) <span class="ct" id="ramSkipCt">0</span></h3>
    <div class="tableWrap" style="max-height:200px">
      <table>
        <thead><tr><th>Coin</th><th>Sebep</th><th>Sinyal Fiyat</th><th>Zaman</th></tr></thead>
        <tbody id="ramSkipBody"></tbody>
      </table>
    </div>
  </div>
</div>

<!-- Arşiv Modal -->
<div id="archiveModal" class="modal">
  <div class="modalBox">
    <button class="modalClose" onclick="closeArchive()">✕ Kapat</button>
    <h2>📚 Arşivler</h2>
    <div id="archiveContent" style="margin-top:14px">Yükleniyor...</div>
  </div>
</div>

<!-- Detay Arşiv Modal -->
<div id="archDetailModal" class="modal">
  <div class="modalBox" style="max-width:1000px">
    <button class="modalClose" onclick="closeArchDetail()">✕ Kapat</button>
    <h2 id="archDetailTitle">Arşiv Detay</h2>
    <div id="archDetailContent" style="margin-top:14px">Yükleniyor...</div>
  </div>
</div>

<div id="toast"></div>

<script>
let DATA={};
let CURRENT_SYS='cab';
let SORT={cab_open:{c:'zaman',d:'desc'}, cab_closed:{c:null,d:'desc'},
          ram_open:{c:'zaman',d:'desc'}, ram_closed:{c:null,d:'desc'}};

function toast(msg, kind){
  const el=document.getElementById('toast');
  el.textContent=msg;
  el.className=kind==='error'?'error':kind==='success'?'success':'';
  el.style.display='block';
  clearTimeout(window._tt);
  window._tt=setTimeout(()=>el.style.display='none', 3500);
}

function regimeChip(r){
  if(!r) return '';
  const cls = r==='ALT_SEASON'?'alt':r==='BTC_SEASON'?'btc':r==='RISK_OFF'?'risk':'neu';
  const label = r==='ALT_SEASON'?'ALT':r==='BTC_SEASON'?'BTC':r==='RISK_OFF'?'RISK':'NEU';
  return `<span class="regimeChip ${cls}" title="${r}">${label}</span>`;
}

function fmtMoney(v){
  if(v===null || v===undefined) return '—';
  v=Number(v);
  if(isNaN(v)) return '—';
  const cls = v>0?'green':v<0?'red':'';
  return `<span class="${cls}">${v>0?'+':''}${v.toFixed(2)}$</span>`;
}

function timeDiff(start, end){
  if(!start) return '—';
  try{
    const s = new Date(start.replace(' ','T')+'+03:00').getTime();
    const e = end ? new Date(end.replace(' ','T')+'+03:00').getTime() : Date.now();
    const diff = Math.floor((e-s)/60000);
    if(diff<60) return diff+'dk';
    const h = Math.floor(diff/60);
    const m = diff%60;
    if(h<24) return h+'s '+m+'dk';
    return Math.floor(h/24)+'g '+(h%24)+'s';
  }catch(e){return '—'}
}

function switchSys(sys){
  CURRENT_SYS=sys;
  document.getElementById('tabCab').classList.toggle('active', sys==='cab');
  document.getElementById('tabRam').classList.toggle('active', sys==='ram');
  document.getElementById('panelCab').classList.toggle('active', sys==='cab');
  document.getElementById('panelRam').classList.toggle('active', sys==='ram');
  // Mode'a göre tab rengi
  const cabT = document.getElementById('tabCab');
  const ramT = document.getElementById('tabRam');
  if(DATA.cab_mode==='shadow') cabT.classList.add('shadow'); else cabT.classList.remove('shadow');
  if(DATA.ram_mode==='shadow') ramT.classList.add('shadow'); else ramT.classList.remove('shadow');
  try{ localStorage.setItem('v67_sys', sys); }catch(e){}
}

async function loadData(){
  try{
    const r = await fetch('/api/data');
    if(!r.ok){toast('Veri yüklenemedi', 'error'); return}
    DATA = await r.json();
    render();
    document.getElementById('lastUpdate').textContent = new Date().toTimeString().slice(0,8);
  }catch(e){
    console.error(e);
    toast('Veri hatası: '+e.message, 'error');
  }
}

function render(){
  // CAB
  renderSystem('cab',
    DATA.open_positions || {},
    DATA.closed_positions || [],
    DATA.skipped_signals || [],
    DATA.pause_state || {},
    DATA.max_pos_state || {},
    DATA.cab_mode || 'shadow'
  );
  // RAM
  renderSystem('ram',
    DATA.ram_open_positions || {},
    DATA.ram_closed_positions || [],
    DATA.ram_skipped_signals || [],
    DATA.ram_pause_state || {},
    DATA.ram_max_pos_state || {},
    DATA.ram_mode || 'shadow'
  );
  // Üst banner mode'lar
  applyTopMode();
}

function applyTopMode(){
  // Tab class'ları için
  const cabT = document.getElementById('tabCab');
  const ramT = document.getElementById('tabRam');
  const cabActive = cabT.classList.contains('active');
  const ramActive = ramT.classList.contains('active');
  cabT.classList.remove('shadow');
  ramT.classList.remove('shadow');
  if(DATA.cab_mode==='shadow' && cabActive) cabT.classList.add('shadow');
  if(DATA.ram_mode==='shadow' && ramActive) ramT.classList.add('shadow');
  // Etiketler
  document.getElementById('cabModeLabel').textContent = (DATA.cab_mode||'shadow').toUpperCase();
  document.getElementById('ramModeLabel').textContent = (DATA.ram_mode||'shadow').toUpperCase();
}

function renderSystem(sys, open_pos, closed, skipped, pause, maxState, mode){
  const openCount = Object.keys(open_pos).length;

  // Mode buton
  const modeBtn = document.getElementById(sys+'ModeBtn');
  const modeDesc = document.getElementById(sys+'ModeDesc');
  modeBtn.textContent = mode.toUpperCase();
  modeBtn.className = 'modeBtn ' + (mode==='live'?'live':'shadow');
  modeDesc.textContent = mode==='live'
    ? '🟢 Bot gerçek Binance pozisyonu açar'
    : '👻 Bot sadece sanal kayıt tutar (gerçek poz açmaz)';

  // Pause
  const pBox = document.getElementById(sys+'PauseBox');
  const pTitle = document.getElementById(sys+'PauseTitle');
  const pDetail = document.getElementById(sys+'PauseDetail');
  const pBtn = document.getElementById(sys+'PauseBtn');
  if(pause.paused){
    pBox.className='pauseBox active';
    pTitle.textContent='🔴 BOT DURDURULDU';
    pDetail.textContent = (pause.reason_text||'Bilinmiyor') + ' | ' + (pause.paused_at||'');
    pBtn.textContent='▶ Başlat';
    pBtn.className='btn btn-green btn-sm';
  }else{
    pBox.className='pauseBox inactive';
    pTitle.textContent='✅ Aktif';
    pDetail.textContent='Yeni sinyaller kabul ediliyor';
    pBtn.textContent='⏸ Durdur';
    pBtn.className='btn btn-red btn-sm';
  }

  // MAX POS
  const maxNum = maxState.current || 7;
  document.getElementById(sys+'MaxNum').textContent = maxNum;
  const changes = (maxState.change_history||[]).slice(-5);
  const todayChanges = changes.filter(c => (c.ts||'').startsWith(now_tr_today())).length;
  document.getElementById(sys+'MaxInfo').textContent =
    `min:3 max:12${todayChanges>0?' | bugün '+todayChanges+' değişim':''}${maxState.auto_reduced?' (otomatik azalmış)':''}`;

  // Stats
  document.getElementById(sys+'OpenN').textContent = openCount;
  document.getElementById(sys+'ClosedN').textContent = closed.length;

  // Net kar (sadece kapanan pozlar — Binance varsa onu, yoksa dashboard kar)
  const today = now_tr_today();
  let netKar = 0, todayKar = 0, wins = 0, total = closed.length;
  for(const c of closed){
    const k = (c.binance_pnl !== null && c.binance_pnl !== undefined) ? c.binance_pnl : (c.kar||0);
    netKar += k;
    if((c.kapanis||'').startsWith(today)) todayKar += k;
    if(k > 0) wins++;
  }
  document.getElementById(sys+'NetKar').innerHTML = fmtMoney(netKar);
  document.getElementById(sys+'TodayKar').innerHTML = fmtMoney(todayKar);
  document.getElementById(sys+'WR').textContent = total>0 ? Math.round(wins/total*100)+'%' : '0%';

  // Skipped (bugün)
  const skipToday = (skipped||[]).filter(s => (s.zaman||'').startsWith(today));
  document.getElementById(sys+'SkipN').textContent = skipToday.length;
  document.getElementById(sys+'SkipCt').textContent = skipToday.length;

  // Tablo: Açık
  document.getElementById(sys+'OpenCt').textContent = openCount;
  renderOpenTable(sys, open_pos);

  // Tablo: Kapanan
  document.getElementById(sys+'ClosedCt').textContent = closed.length;
  renderClosedTable(sys, closed);

  // Tablo: Kaçırılan
  renderSkipTable(sys, skipToday);

  // Sekme badge
  document.getElementById(sys+'Badge').textContent = openCount;
}

function renderOpenTable(sys, open_pos){
  const body = document.getElementById(sys+'OpenBody');
  const arr = Object.entries(open_pos).map(([t,p]) => ({...p, ticker:t}));
  if(!arr.length){
    body.innerHTML = '<tr><td colspan="10" style="text-align:center;color:#94a3b8;padding:14px">Açık poz yok</td></tr>';
    return;
  }
  arr.sort((a,b) => (b.zaman||'').localeCompare(a.zaman||''));
  body.innerHTML = arr.map(p => `
    <tr>
      <td><b>${p.ticker}</b></td>
      <td>$${p.marj}</td>
      <td>${p.lev}x</td>
      <td>${fmtNum(p.giris)}</td>
      <td style="color:#f87171">${fmtNum(p.stop || p.original_stop)}</td>
      <td style="color:#4ade80">${fmtNum(p.tp1)}</td>
      <td style="color:#22d3ee">${fmtNum(p.tp2)}</td>
      <td>${(p.hh_pct||0).toFixed(1)}%</td>
      <td>${regimeChip(p.market_regime)}</td>
      <td>${(p.zaman||'').slice(11,16)}</td>
    </tr>
  `).join('');
}

function renderClosedTable(sys, closed){
  const body = document.getElementById(sys+'ClosedBody');
  if(!closed.length){
    body.innerHTML = '<tr><td colspan="7" style="text-align:center;color:#94a3b8;padding:14px">Kapanmış poz yok</td></tr>';
    return;
  }
  // Son 50, ters sıralı (yeni üstte)
  const arr = closed.slice(-50).reverse();
  body.innerHTML = arr.map(c => `
    <tr>
      <td><b>${c.ticker}</b></td>
      <td>${c.sonuc||'—'}</td>
      <td>${fmtMoney(c.kar)}</td>
      <td>${fmtMoney(c.binance_pnl)}</td>
      <td>${regimeChip(c.market_regime)}</td>
      <td>${timeDiff(c.acilis, c.kapanis)}</td>
      <td style="font-size:10px">${(c.kapanis||'').slice(5,16)}</td>
    </tr>
  `).join('');
}

function renderSkipTable(sys, skipped){
  const body = document.getElementById(sys+'SkipBody');
  if(!skipped.length){
    body.innerHTML = '<tr><td colspan="4" style="text-align:center;color:#94a3b8;padding:10px">Kaçırılan sinyal yok</td></tr>';
    return;
  }
  const arr = skipped.slice(-30).reverse();
  body.innerHTML = arr.map(s => `
    <tr>
      <td><b>${s.ticker||'?'}</b></td>
      <td style="font-size:10px">${(s.sebep||'').slice(0,80)}</td>
      <td>${fmtNum(s.giris)}</td>
      <td style="font-size:10px">${(s.zaman||'').slice(11,16)}</td>
    </tr>
  `).join('');
}

function fmtNum(v){
  if(v===null||v===undefined) return '—';
  v=Number(v);
  if(isNaN(v)) return '—';
  if(v>=1) return v.toFixed(4);
  return v.toFixed(6);
}

function now_tr_today(){
  const d = new Date();
  const trH = d.getUTCHours()+3;
  if(trH>=24){
    const tomorrow = new Date(d.getTime()+86400000);
    return tomorrow.toISOString().slice(0,10);
  }
  return d.toISOString().slice(0,10);
}

// ═══════════════ ACTIONS ═══════════════
async function togglePause(sys){
  const isPaused = sys==='cab' ? (DATA.pause_state||{}).paused : (DATA.ram_pause_state||{}).paused;
  if(isPaused){
    if(!confirm(sys.toUpperCase()+' botunu tekrar başlat?')) return;
  }else{
    const reason = prompt('Durdurma sebebi (opsiyonel):', 'Manuel durdurma');
    if(reason===null) return;
    var reason_text = reason || 'Manuel durdurma';
  }
  try{
    const body = {system:sys, action: isPaused?'resume':'pause'};
    if(!isPaused) body.reason_text = reason_text;
    const r = await fetch('/api/toggle_pause', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
    const j = await r.json();
    if(j.success){toast('✓ '+j.msg, 'success'); loadData();}
    else toast('Hata: '+(j.error||'?'), 'error');
  }catch(e){toast('Hata: '+e.message, 'error');}
}

async function setMaxPos(sys, delta){
  const cur = sys==='cab' ? (DATA.max_pos_state||{}).current||7 : (DATA.ram_max_pos_state||{}).current||7;
  const newVal = cur + delta;
  if(newVal<3 || newVal>12){toast('Sınır dışı (3-12)', 'error'); return;}
  try{
    const r = await fetch('/api/set_max_pos', {method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({system:sys, value:newVal, reason:'manual_dashboard'})});
    const j = await r.json();
    if(j.success){toast(`${sys.toUpperCase()} MAX: ${cur}→${newVal}`, 'success'); loadData();}
    else toast('Hata: '+(j.error||'?'), 'error');
  }catch(e){toast('Hata: '+e.message, 'error');}
}

async function toggleMode(sys){
  const cur = sys==='cab' ? (DATA.cab_mode||'shadow') : (DATA.ram_mode||'shadow');
  const target = cur==='live' ? 'shadow' : 'live';
  const openPos = sys==='cab' ? Object.keys(DATA.open_positions||{}).length : Object.keys(DATA.ram_open_positions||{}).length;

  let confirmMsg = `${sys.toUpperCase()} sistemi ${cur.toUpperCase()} → ${target.toUpperCase()} olarak değiştirilsin mi?\n\n`;
  if(target==='live'){
    confirmMsg += '⚠️ DİKKAT: CANLI moda geçiyorsun! Gerçek para ile işlem yapılacak.\n';
    if(sys==='ram'){
      confirmMsg += '\n⚠️ RAM için canlı handler henüz tam test edilmedi — shadow olarak çalışmaya devam eder!\n';
    }
  }else{
    confirmMsg += 'ℹ️ SHADOW moda geçince bot sadece sanal kayıt tutar.\n';
  }
  if(openPos>0){
    confirmMsg += `\n⚠️ ${openPos} açık poz var. Mode değişimi yeni gelen sinyalleri etkiler, mevcut pozları değil.\n\nYine de devam edilsin mi?`;
  }

  if(!confirm(confirmMsg)) return;

  try{
    const r = await fetch('/api/toggle_mode', {method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({system:sys, mode:target, force: openPos>0})});
    const j = await r.json();
    if(j.success){
      toast(`✓ ${sys.toUpperCase()} → ${target.toUpperCase()}`, 'success');
      loadData();
    }else{
      toast('Hata: '+(j.error||j.msg||'?'), 'error');
    }
  }catch(e){toast('Hata: '+e.message, 'error');}
}

async function archiveAndReset(){
  const desc = prompt('Arşiv açıklaması (örn: "v6.7 öncesi temiz başlangıç"):', 'Manuel arşiv '+new Date().toISOString().slice(0,10));
  if(desc===null) return;
  if(!confirm(`Bu işlem:\n• Mevcut tüm verileri arşive alır\n• Kapanan pozları, kaçırılan sinyalleri SİLER\n• AÇIK pozlar dokunulmaz (CAB live)\n• RAM açık sanal pozlar SİLİNİR\n\nDevam edilsin mi?`)) return;

  try{
    const r = await fetch('/api/archive_and_reset', {method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({description:desc, reset_cab:true, reset_ram:true})});
    const j = await r.json();
    if(j.success){
      const c = j.archived_counts;
      alert(`✅ Arşivlendi:\n• CAB: ${c.cab_closed} kapanan\n• RAM: ${c.ram_closed} kapanan\n• Legacy: ${c.legacy_closed} kapanan\n\nTemiz başlangıç tamamdır.`);
      loadData();
    }else{
      toast('Hata: '+(j.error||'?'), 'error');
    }
  }catch(e){toast('Hata: '+e.message, 'error');}
}

async function openArchive(){
  document.getElementById('archiveModal').classList.add('show');
  document.getElementById('archiveContent').innerHTML = 'Yükleniyor...';
  try{
    const r = await fetch('/api/archive_list');
    const j = await r.json();
    if(!j.archives || !j.archives.length){
      document.getElementById('archiveContent').innerHTML = '<div style="text-align:center;color:#94a3b8;padding:30px">Henüz arşiv yok.<br><br>Ana sayfada "🧹 Arşivle + Temizle" ile arşiv oluşturabilirsin.</div>';
      return;
    }
    document.getElementById('archiveContent').innerHTML = j.archives.reverse().map(a => `
      <div style="background:#0f172a;padding:12px;border-radius:6px;margin-bottom:8px;border-left:3px solid #7c3aed">
        <div style="font-weight:700;font-size:13px;color:#a5b4fc">${a.archived_at}</div>
        <div style="font-size:12px;margin:4px 0;color:#cbd5e1">${a.description||''}</div>
        <div style="font-size:11px;color:#94a3b8">CAB: ${a.cab_closed_count} | RAM: ${a.ram_closed_count} | Legacy: ${a.legacy_closed_count}</div>
        <button class="btn btn-sm" style="margin-top:6px" onclick="viewArchive(${a.index})">👁 Detay Gör</button>
      </div>
    `).join('');
  }catch(e){
    document.getElementById('archiveContent').innerHTML = '<div style="color:#f87171">Hata: '+e.message+'</div>';
  }
}
function closeArchive(){document.getElementById('archiveModal').classList.remove('show');}

async function viewArchive(index){
  document.getElementById('archDetailModal').classList.add('show');
  document.getElementById('archDetailTitle').textContent = `Arşiv #${index} Detay`;
  document.getElementById('archDetailContent').innerHTML = 'Yükleniyor...';
  try{
    const r = await fetch('/api/archive_get/'+index);
    const j = await r.json();
    if(!j) {document.getElementById('archDetailContent').innerHTML='Veri yok'; return;}

    const cabClosed = j.cab?.closed_positions || [];
    const ramClosed = j.ram?.closed_positions || [];

    let cabNet = 0, ramNet = 0;
    cabClosed.forEach(c => { cabNet += (c.binance_pnl!=null?c.binance_pnl:c.kar||0); });
    ramClosed.forEach(c => { ramNet += (c.kar||0); });

    document.getElementById('archDetailContent').innerHTML = `
      <div style="background:#0f172a;padding:12px;border-radius:6px;margin-bottom:14px">
        <div style="font-weight:700;color:#a5b4fc">${j.archived_at}</div>
        <div style="font-size:12px;margin-top:4px">${j.description||''}</div>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px">
        <div style="background:#0f172a;padding:14px;border-radius:6px">
          <h3 style="margin:0 0 8px;color:#4ade80">CAB</h3>
          <div>${cabClosed.length} kapanan poz</div>
          <div style="font-size:18px;margin-top:6px">${fmtMoney(cabNet)}</div>
        </div>
        <div style="background:#0f172a;padding:14px;border-radius:6px">
          <h3 style="margin:0 0 8px;color:#fbbf24">RAM</h3>
          <div>${ramClosed.length} kapanan poz</div>
          <div style="font-size:18px;margin-top:6px">${fmtMoney(ramNet)}</div>
        </div>
      </div>
      <h3 style="color:#a5b4fc;margin-top:18px">CAB son 20</h3>
      <div class="tableWrap" style="max-height:200px"><table>
        <thead><tr><th>Coin</th><th>Sonuç</th><th>Kar</th><th>Binance</th><th>Tarih</th></tr></thead>
        <tbody>${cabClosed.slice(-20).reverse().map(c=>`<tr><td>${c.ticker}</td><td>${c.sonuc||'—'}</td><td>${fmtMoney(c.kar)}</td><td>${fmtMoney(c.binance_pnl)}</td><td style="font-size:10px">${(c.kapanis||'').slice(5,16)}</td></tr>`).join('')}</tbody>
      </table></div>
      <h3 style="color:#a5b4fc;margin-top:18px">RAM son 20</h3>
      <div class="tableWrap" style="max-height:200px"><table>
        <thead><tr><th>Coin</th><th>Sonuç</th><th>Kar</th><th>Tarih</th></tr></thead>
        <tbody>${ramClosed.slice(-20).reverse().map(c=>`<tr><td>${c.ticker}</td><td>${c.sonuc||'—'}</td><td>${fmtMoney(c.kar)}</td><td style="font-size:10px">${(c.kapanis||'').slice(5,16)}</td></tr>`).join('')}</tbody>
      </table></div>
    `;
  }catch(e){
    document.getElementById('archDetailContent').innerHTML = '<div style="color:#f87171">Hata: '+e.message+'</div>';
  }
}
function closeArchDetail(){document.getElementById('archDetailModal').classList.remove('show');}

function exportCSV(sys, type){
  let rows;
  if(type==='open'){
    const op = sys==='cab'?DATA.open_positions:DATA.ram_open_positions;
    rows = Object.entries(op||{}).map(([t,p])=>({Coin:t, Marj:p.marj, Lev:p.lev, Giris:p.giris, Stop:p.stop, TP1:p.tp1, TP2:p.tp2, HH:(p.hh_pct||0).toFixed(2), Rejim:p.market_regime||'', Zaman:p.zaman||''}));
  }else{
    const cl = sys==='cab'?DATA.closed_positions:DATA.ram_closed_positions;
    rows = (cl||[]).map(c=>({Coin:c.ticker, Sonuc:c.sonuc, Kar:c.kar, BinancePNL:c.binance_pnl, Rejim:c.market_regime||'', Acilis:c.acilis, Kapanis:c.kapanis}));
  }
  if(!rows.length){toast('Veri yok','error'); return;}
  const h = Object.keys(rows[0]);
  const csv = [h.join(','), ...rows.map(r => h.map(k => r[k]||'').join(','))].join('\n');
  const blob = new Blob(['\ufeff'+csv], {type:'text/csv'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `${sys}_${type}_${new Date().toISOString().slice(0,10)}.csv`;
  a.click();
  toast('✓ CSV indirildi', 'success');
}

// ═══════════════ MIGRATE PNL (background, mevcut Patch 14.2 mantığı) ═══════════════
async function migratePnl(){
  const refreshExisting = confirm('Gerçek Binance PNL çek?\n\n• TAMAM: TÜM pozları yeniden hesapla\n• İPTAL: Sadece güncellenmemiş olanları çek');
  try{
    toast('Sayım yapılıyor...');
    const r1 = await fetch('/api/migrate_pnl', {method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({action:'dry_run', force_refresh:refreshExisting})});
    if(!r1.ok){toast('Sayım hatası', 'error'); return;}
    const dry = await r1.json();
    let msg = `📊 Migrate Hazır\n\nToplam: ${dry.total}\nİşlenecek: ${dry.to_process}\nZaten var: ${dry.already_have}\nTahmini süre: ~${dry.estimated_seconds}sn\n\nBaşlatılsın mı?`;
    if(dry.to_process===0){alert('İşlenecek poz yok'); return;}
    if(!confirm(msg)) return;

    window._migrateActive = true;
    const r2 = await fetch('/api/migrate_pnl', {method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({action:'start', force_refresh:refreshExisting})});
    if(!r2.ok && r2.status!==409){window._migrateActive=false; toast('Başlatma hatası', 'error'); return;}
    toast('✓ Arka planda başladı...');

    const pollInt = setInterval(async()=>{
      try{
        const rs = await fetch('/api/migrate_status');
        const st = await rs.json();
        if(st.status==='running'){
          const pct = st.total>0 ? Math.round(st.processed/st.total*100) : 0;
          toast(`Migrate: ${st.processed}/${st.total} (${pct}%) — ${st.current_ticker||'...'} | ✓${st.migrated} ✗${st.failed}`);
        }else if(st.status==='completed'){
          clearInterval(pollInt);
          window._migrateActive = false;
          let done = `✅ Migrate Tamamlandı\nİşlenen: ${st.processed}\nGüncellendi: ${st.migrated}\nAtlanan: ${st.skipped}\nBaşarısız: ${st.failed}`;
          if(st.summary) done += `\n\nDashboard: ${st.summary.dashboard_total}$\nBinance: ${st.summary.binance_total}$\nFARK: ${st.summary.fark}$`;
          alert(done);
          loadData();
        }else if(st.status==='error'){
          clearInterval(pollInt);
          window._migrateActive = false;
          alert('❌ Hata: '+(st.error||'?'));
        }
      }catch(e){console.error(e);}
    }, 2000);
    setTimeout(()=>{clearInterval(pollInt); window._migrateActive=false;}, 600000);
  }catch(e){
    window._migrateActive = false;
    alert('Hata: '+e.message);
  }
}

// ═══════════════ INIT ═══════════════
async function init(){
  // Bildirim izni iste (opsiyonel)
  if('Notification' in window && Notification.permission==='default'){
    try{Notification.requestPermission();}catch(e){}
  }
  // Önce data
  await loadData();
  // Sekme hatırla
  try{
    const saved = localStorage.getItem('v67_sys');
    if(saved==='ram') switchSys('ram');
  }catch(e){}
  // 10sn yenileme (migrate aktif değilse)
  setInterval(()=>{
    if(!window._migrateActive) loadData();
  }, 10000);
}
init();
</script>
</body></html>"""
    return html
