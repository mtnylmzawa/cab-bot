from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from binance.um_futures import UMFutures
from binance.error import ClientError
import os, json, httpx, asyncio, math, time
from datetime import datetime, timezone, timedelta

app = FastAPI()

# ============ KONFIGÜRASYON ============
TEST_MODE = False  # 🟢 CANLI MOD
MAX_POSITIONS = 7  # v6.1: 5 → 7 (akıllı slot ile pratikte daha fazla açık olabilir)
DATA_FILE = os.environ.get("DATA_FILE", "/tmp/cab_data.json")
TIMEOUT_HOURS = 12
TIMEOUT_ABSOLUTE_HOURS = 24
TIMEOUT_PRESSURE_THRESHOLD = 5
TIMEOUT_CHECK_INTERVAL_SEC = 300
HIGH_LOW_CHECK_INTERVAL_SEC = 60

# v6.6 YENI: Watchdog + Reconciler aralıkları
STOP_CHECK_INTERVAL_SEC = 60  # Stop watchdog
STATE_RECONCILE_INTERVAL_SEC = 60  # Binance vs bot state senkron
STOP_RETRY_MAX = 3  # binance_stop_loss retry sayısı

client = UMFutures(
    key=os.environ.get("BINANCE_API_KEY"),
    secret=os.environ.get("BINANCE_SECRET_KEY"),
    base_url="https://fapi.binance.com"
)

INITIAL_DATA = {
    "open_positions": {}, "closed_positions": [], "skipped_signals": [],
    "shadow_positions": {}, "shadow_closed": [], "shadow_skipped": []
}

# v6.6 YENI: Global state
stopless_positions = set()   # Stop'u olmayan pozlar (UI alarm için)
reconcile_warnings = []      # State divergence tarihçesi (son 50 kayıt)

# ============ VERİ YÖNETİMİ ============
def load_data():
    try:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, "r") as f:
                return json.load(f)
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

# Eski verilere eksik key'leri ekle
for key in ["skipped_signals", "shadow_positions", "shadow_closed", "shadow_skipped"]:
    if key not in data:
        data[key] = {} if key == "shadow_positions" else []
save_data(data)

def now_tr():
    return (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M")

def now_tr_short():
    return (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%H:%M")

def now_tr_dt():
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=3)

def now_ms():
    return int(time.time() * 1000)

# ============ AKILLI SLOT SAYIMI ============
def count_active_risk():
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
invalid_symbols_cache = set()

def get_symbol_info(symbol):
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
            return True
        print(f"[BINANCE ERR] Leverage {symbol}: {e}")
        return False

def binance_set_margin_type(symbol, margin_type="ISOLATED"):
    try:
        result = client.change_margin_type(symbol=symbol, marginType=margin_type)
        return True
    except ClientError as e:
        if "No need to change" in str(e):
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

# v6.6 YENI: Stop Safety Net — closePosition=true + retry 3x
def binance_stop_loss(symbol, qty, stop_price, info, retry=STOP_RETRY_MAX):
    """
    v6.6: Stop order'ı KRİTİK güvenle koyar.
    - closePosition=true: qty verilmez, Binance pozisyon ne kadarsa hepsini kapatır (lot rounding → 0 bug'ı biter)
    - Retry 3x: geçici hata olursa tekrar dener
    - immediate trigger: stop_price mevcut fiyatın altında/üstündeyse Binance -2021 döner → bu durumu yakala, return ile işaretle
    """
    sp = round_price(stop_price, info)
    last_err = None
    for attempt in range(1, retry + 1):
        try:
            result = client.new_order(
                symbol=symbol,
                side="SELL",
                type="STOP_MARKET",
                closePosition="true",
                stopPrice=sp,
                workingType="MARK_PRICE",
                priceProtect="true",
                timeInForce="GTE_GTC"
            )
            order_id = result.get("orderId")
            print(f"[BINANCE] STOP_MARKET {symbol} closePos stop:{sp} orderId:{order_id} (attempt {attempt}) ✓")
            return {"success": True, "order_id": order_id, "stop_price": sp}
        except ClientError as e:
            last_err = str(e)
            err_code = getattr(e, "error_code", None)
            # -2021: Order would immediately trigger (stop çok yakın/yanlış tarafta)
            if "2021" in last_err or "immediately trigger" in last_err.lower():
                print(f"[BINANCE WARN] Stop {symbol} immediate trigger (stop:{sp} yanlış tarafta) → caller pozu kapatacak")
                return {"success": False, "error": last_err, "immediate_trigger": True}
            # -2022: ReduceOnly Order is rejected (pozisyon zaten yok)
            if "2022" in last_err or "ReduceOnly" in last_err:
                print(f"[BINANCE WARN] Stop {symbol} reduceOnly rejected (pozisyon yok)")
                return {"success": False, "error": last_err, "no_position": True}
            # Geçici hata → retry
            if attempt < retry:
                print(f"[BINANCE] Stop {symbol} attempt {attempt} fail: {last_err}, tekrar denenecek...")
                time.sleep(0.5 * attempt)
                continue
            print(f"[BINANCE ERR] Stop {symbol} tüm retry'lar başarısız: {last_err}")
            return {"success": False, "error": last_err}
        except Exception as e:
            last_err = str(e)
            if attempt < retry:
                time.sleep(0.5 * attempt)
                continue
            return {"success": False, "error": last_err}
    return {"success": False, "error": last_err or "unknown"}

def binance_cancel_all(symbol):
    try:
        result = client.cancel_open_orders(symbol=symbol)
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
    """Pozisyonu tamamen kapat (emergency close için)"""
    qty = binance_get_position_qty(symbol)
    if qty <= 0:
        return {"success": True, "msg": "no position"}
    info = get_symbol_info(symbol)
    binance_cancel_all(symbol)
    qty_rounded = round_qty(qty, info)
    if qty_rounded <= 0:
        return {"success": False, "error": "qty rounded to 0"}
    result = binance_market_sell(symbol, qty_rounded)
    if not result["success"]:
        return result
    time.sleep(0.5)
    remaining = binance_get_position_qty(symbol)
    if remaining > 0:
        precision = info["qty_precision"]
        remaining_str = f"{remaining:.{precision + 2}f}".rstrip('0').rstrip('.')
        try:
            remaining_qty = float(remaining_str)
            remaining_rounded = round_qty(remaining_qty, info)
            if remaining_rounded > 0:
                binance_market_sell(symbol, remaining_rounded)
        except Exception as e:
            print(f"[BINANCE WARN] {symbol} kalıntı temizleme: {e}")
    return result

def binance_get_mark_price(symbol):
    try:
        result = client.mark_price(symbol=symbol)
        return float(result.get("markPrice", 0))
    except Exception as e:
        print(f"[BINANCE ERR] Mark price {symbol}: {e}")
        return None

def binance_get_klines(symbol, interval="1m", limit=60):
    if symbol in invalid_symbols_cache:
        return None
    try:
        klines = client.klines(symbol=symbol, interval=interval, limit=limit)
        return klines
    except Exception as e:
        err_str = str(e)
        if "Invalid symbol" in err_str or "-1121" in err_str:
            invalid_symbols_cache.add(symbol)
        else:
            print(f"[KLINES ERR] {symbol} {interval}: {e}")
        return None

def get_high_low_since(symbol, since_ms, interval="1m"):
    now_ms_val = now_ms()
    minutes_passed = max(1, (now_ms_val - since_ms) // 60000 + 5)
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

# ============ v6.6 YENI: STOP & STATE HELPERS ============
def check_stop_exists_on_binance(symbol):
    """
    Binance'te sembol için AÇIK bir STOP_MARKET veya STOP emri var mı?
    True/False + mevcut stop fiyatı döner (varsa).
    """
    try:
        orders = client.get_orders(symbol=symbol)
        for o in orders:
            otype = o.get("type", "")
            ostatus = o.get("status", "")
            if ostatus != "NEW":
                continue
            if otype in ("STOP_MARKET", "STOP"):
                return {"exists": True, "stop_price": float(o.get("stopPrice", 0)), "order_id": o.get("orderId")}
        return {"exists": False, "stop_price": None, "order_id": None}
    except ClientError as e:
        print(f"[BINANCE ERR] check_stop {symbol}: {e}")
        return {"exists": None, "error": str(e)}  # None = bilinmiyor (API hatası)
    except Exception as e:
        return {"exists": None, "error": str(e)}

def get_binance_position_status(symbol):
    """
    Binance'te sembol pozisyonu durumu: açık mı kapalı mı?
    Açıksa qty ve entry fiyatı ile birlikte döner.
    """
    try:
        positions = client.get_position_risk(symbol=symbol)
        for p in positions:
            if p["symbol"] == symbol:
                amt = float(p.get("positionAmt", 0))
                if abs(amt) > 0:
                    return {
                        "open": True,
                        "qty": abs(amt),
                        "entry_price": float(p.get("entryPrice", 0)),
                        "side": "LONG" if amt > 0 else "SHORT"
                    }
        return {"open": False, "qty": 0, "entry_price": 0, "side": None}
    except Exception as e:
        print(f"[BINANCE ERR] position_status {symbol}: {e}")
        return {"open": None, "error": str(e)}  # None = bilinmiyor

def fetch_binance_realized_pnl(symbol, start_ms, end_ms=None):
    """
    Binance'ten gerçek realized PnL'i çek (fee dahil).
    start_ms: pozisyon açılış zamanı (ms)
    end_ms: kapanış zamanı (ms), None ise şimdi
    Geri dönüş: {"realized_pnl": float, "commission": float, "net_pnl": float, "trades": int}
    """
    if end_ms is None:
        end_ms = now_ms()
    try:
        # userTrades endpoint — tüm trade'leri çek
        trades = client.get_account_trades(symbol=symbol, startTime=start_ms, endTime=end_ms, limit=1000)
        total_pnl = 0.0
        total_commission = 0.0
        trade_count = 0
        for t in trades:
            realized = float(t.get("realizedPnl", 0))
            commission = float(t.get("commission", 0))
            total_pnl += realized
            total_commission += commission
            trade_count += 1
        net = total_pnl - total_commission
        return {
            "success": True,
            "realized_pnl": round(total_pnl, 4),
            "commission": round(total_commission, 4),
            "net_pnl": round(net, 2),
            "trades": trade_count
        }
    except Exception as e:
        print(f"[BINANCE ERR] realized_pnl {symbol}: {e}")
        return {"success": False, "error": str(e)}

# ============ KAR HESAPLAMA (fallback, Binance down ise) ============
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

# ============ TRADE EXECUTION (v6.6: Safety Net ile) ============
def execute_entry(ticker, parsed):
    """
    v6.6: Safety Net Entry
    - Pozisyon açılır
    - Stop koyulmaya çalışılır (retry 3x, closePosition=true)
    - Stop başarısız olursa → POZISYON ACİL KAPATILIR + skipped_signals'a eklenir
    Bu sayede "stop'suz açık poz" bug'ı yaşanmaz.
    """
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

    # avgPrice 0 ise position_risk'ten al
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

    # v6.6 SAFETY NET: stop koyma girişimi
    sl_result = binance_stop_loss(symbol, actual_qty, stop_px, info)

    if not sl_result.get("success"):
        # Stop konamadı → POZISYONU ACİL KAPAT
        error_msg = sl_result.get("error", "unknown")
        immediate = sl_result.get("immediate_trigger", False)
        print(f"[SAFETY NET] 🚨 {symbol} STOP KONAMADI ({error_msg}) — pozisyon acil kapatılıyor!")

        close_result = binance_close_position(symbol)

        # Skipped signals'a güvenlik kaydı ekle
        if "skipped_signals" not in data:
            data["skipped_signals"] = []
        data["skipped_signals"].append({
            "ticker": ticker,
            "giris": actual_price,
            "stop": stop_px,
            "tp1": parsed["tp1"],
            "tp2": parsed["tp2"],
            "marj": marj,
            "lev": lev,
            "risk": parsed.get("risk", 0),
            "kapat_oran": parsed.get("kapat_oran", 60),
            "atr_skor": parsed.get("atr_skor", 1.0),
            "zaman": now_tr(),
            "sebep": f"🚨 SAFETY NET: Stop konamadı ({error_msg[:60]}) → pozisyon acil kapandı",
            "safety_closed": True,
            "stop_error": error_msg[:200]
        })
        if len(data["skipped_signals"]) > 50:
            data["skipped_signals"] = data["skipped_signals"][-50:]
        save_data(data)

        return {"safety_closed": True, "error": error_msg}

    sl_order_id = sl_result.get("order_id")
    stop_verified_px = sl_result.get("stop_price")

    print(f"[TRADE] GIRIS OK: {symbol} | qty:{actual_qty} | px:{actual_price} | SL:{stop_verified_px} | order:{sl_order_id}")
    return {
        "qty": actual_qty,
        "avg_price": actual_price,
        "sl_order_id": sl_order_id,
        "binance_stop_price": stop_verified_px,
        "binance_stop_verified": True
    }

def execute_tp1_close(ticker, pos):
    """
    v6.6: TP1 kapama + BE stop VERIFY
    - TP1 kısmi kapama yapılır
    - BE stop koyulur (closePosition=true)
    - Stop konamazsa uyarı loglanır (pozisyon açık kalır, watchdog yakalar)
    Dönüş: {"success": bool, "be_stop_ok": bool, "tp1_fill_price": float}
    """
    symbol = ticker
    info = get_symbol_info(symbol)
    kapat_oran = pos.get("kapat_oran", 60)

    total_qty = binance_get_position_qty(symbol)
    if total_qty <= 0:
        print(f"[TRADE WARN] TP1 ama {symbol} pozisyon yok")
        return {"success": False, "be_stop_ok": False, "tp1_fill_price": 0}

    close_qty = round_qty(total_qty * (kapat_oran / 100.0), info)
    if close_qty <= 0:
        return {"success": False, "be_stop_ok": False, "tp1_fill_price": 0}

    binance_cancel_all(symbol)
    result = binance_market_sell(symbol, close_qty)
    if not result["success"]:
        return {"success": False, "be_stop_ok": False, "tp1_fill_price": 0, "error": result.get("error")}

    tp1_fill_price = result.get("avg_price", 0)
    remaining_qty = round_qty(total_qty - close_qty, info)

    be_stop_ok = False
    if remaining_qty > 0:
        be_price = pos["giris"]
        sl_result = binance_stop_loss(symbol, remaining_qty, be_price, info)
        be_stop_ok = sl_result.get("success", False)
        if not be_stop_ok:
            print(f"[TRADE WARN] 🚨 {symbol} TP1 sonrası BE stop konamadı ({sl_result.get('error')}) — watchdog yakalayacak")

    print(f"[TRADE] TP1 OK: {symbol} | Kapat:{close_qty}@{tp1_fill_price} | Kalan:{remaining_qty} | BE:{pos['giris']} ({'✓' if be_stop_ok else '🚨'})")
    return {"success": True, "be_stop_ok": be_stop_ok, "tp1_fill_price": tp1_fill_price}

def execute_tp2_close(ticker, pos):
    """v6.6: TP2 kapama + stop verify"""
    symbol = ticker
    info = get_symbol_info(symbol)

    total_qty = binance_get_position_qty(symbol)
    if total_qty <= 0:
        return {"success": False, "be_stop_ok": False, "tp2_fill_price": 0}

    close_qty = round_qty(total_qty * 0.625, info)
    if close_qty <= 0 or close_qty > total_qty:
        close_qty = round_qty(total_qty * 0.5, info)
    if close_qty <= 0:
        return {"success": False, "be_stop_ok": False, "tp2_fill_price": 0}

    binance_cancel_all(symbol)
    result = binance_market_sell(symbol, close_qty)
    if not result["success"]:
        return {"success": False, "be_stop_ok": False, "tp2_fill_price": 0, "error": result.get("error")}

    tp2_fill_price = result.get("avg_price", 0)
    remaining_qty = round_qty(total_qty - close_qty, info)

    be_stop_ok = False
    if remaining_qty > 0:
        be_price = pos["giris"]
        sl_result = binance_stop_loss(symbol, remaining_qty, be_price, info)
        be_stop_ok = sl_result.get("success", False)
        if not be_stop_ok:
            print(f"[TRADE WARN] 🚨 {symbol} TP2 sonrası BE stop konamadı — watchdog yakalayacak")

    print(f"[TRADE] TP2 OK: {symbol} | Kapat:{close_qty}@{tp2_fill_price} | Kalan:{remaining_qty} ({'✓' if be_stop_ok else '🚨'})")
    return {"success": True, "be_stop_ok": be_stop_ok, "tp2_fill_price": tp2_fill_price}

def execute_full_close(ticker, reason="TRAIL"):
    symbol = ticker
    binance_cancel_all(symbol)
    result = binance_close_position(symbol)
    print(f"[TRADE] {reason} CLOSE: {symbol} | {result}")
    return result

def execute_smart_timeout(ticker, pos):
    """
    Akıllı timeout — kârda → BE stop, zararda → market kapat
    Geri dönüş: ('be', kar) veya ('close', kar)
    """
    symbol = ticker
    giris = pos["giris"]
    pos_size = pos["marj"] * pos["lev"]

    current_px = binance_get_mark_price(symbol)
    if current_px is None or current_px <= 0:
        print(f"[TIMEOUT WARN] {symbol} mark price alınamadı, full close")
        if not TEST_MODE:
            execute_full_close(ticker, "TIMEOUT_FALLBACK")
        return ('close', 0)

    pct = (current_px - giris) / giris * 100.0
    unrealized = pos_size * (current_px - giris) / giris
    print(f"[TIMEOUT] {symbol} mark:{current_px} giris:{giris} pct:{pct:.2f}% unreal:{unrealized:+.1f}$")

    if unrealized > 0:
        if not TEST_MODE:
            info = get_symbol_info(symbol)
            qty = binance_get_position_qty(symbol)
            if qty > 0:
                qty = round_qty(qty, info)
                binance_cancel_all(symbol)
                sl_result = binance_stop_loss(symbol, qty, giris, info)
                if not sl_result["success"]:
                    print(f"[TIMEOUT ERR] {symbol} BE stop fail → full close")
                    execute_full_close(ticker, "TIMEOUT_BE_FAIL")
                    return ('close', round(unrealized, 2))
            else:
                return ('close', 0)
        return ('be', round(unrealized, 2))
    else:
        if not TEST_MODE:
            execute_full_close(ticker, "TIMEOUT_LOSS")
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
            "rs_spread": float(kv.get("RS", 0)),
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

# ============ SHADOW MODE HANDLERS (v6.5 — RAM v14 için) ============
SHADOW_FEE_PCT = 0.1
SHADOW_SLIPPAGE_PCT = 0.05

def shadow_calc_kar(marj, lev, giris, exit_px, oran_pct):
    if giris <= 0:
        return 0.0
    pos_size = marj * lev
    kapatilan = pos_size * (oran_pct / 100.0)
    ham_kar = kapatilan * (exit_px - giris) / giris
    fee_cost = kapatilan * (SHADOW_FEE_PCT / 100.0)
    slip_cost = kapatilan * (SHADOW_SLIPPAGE_PCT / 100.0)
    net_kar = ham_kar - fee_cost - slip_cost
    return round(net_kar, 2)

def shadow_handle_giris(msg, system_tag):
    parsed = parse_giris(msg)
    if not parsed:
        return {"status": "parse_error", "shadow": True}
    print(f"[SHADOW PARSE] {parsed}")
    ticker = parsed["ticker"]
    if ticker in data["shadow_positions"]:
        return {"status": "duplicate", "shadow": True}
    data["shadow_positions"][ticker] = {
        "ticker": ticker, "system": system_tag,
        "giris": parsed["giris"], "stop": parsed["stop"],
        "tp1": parsed["tp1"], "tp2": parsed["tp2"],
        "marj": parsed["marj"], "lev": parsed["lev"],
        "risk": parsed["risk"], "kapat_oran": parsed["kapat_oran"],
        "atr_skor": parsed["atr_skor"], "rs_spread": parsed.get("rs_spread", 0),
        "zaman": now_tr(), "zaman_full": now_tr(),
        "tp1_hit": False, "tp2_hit": False,
        "hh_pct": 0, "max_seen": 0, "min_seen": 0,
        "current_stop": parsed["stop"], "tp1_kar": 0, "tp2_kar": 0,
    }
    save_data(data)
    print(f"[SHADOW GIRIS] {system_tag} | {ticker} @ {parsed['giris']}")
    return {"status": "shadow_opened", "shadow": True, "ticker": ticker}

def shadow_handle_tp1(msg, system_tag):
    parsed = parse_tp1(msg)
    if not parsed:
        return {"status": "parse_error", "shadow": True}
    ticker = parsed["ticker"]
    if ticker not in data["shadow_positions"]:
        return {"status": "not_found", "shadow": True}
    pos = data["shadow_positions"][ticker]
    if pos.get("tp1_hit"):
        return {"status": "tp1_duplicate", "shadow": True}
    tp1_px = parsed.get("tp1") or pos["tp1"]
    yeni_stop = parsed.get("stop") or pos["giris"]
    kapat_oran = parsed.get("kapat_oran", pos.get("kapat_oran", 50))
    tp1_kar = shadow_calc_kar(pos["marj"], pos["lev"], pos["giris"], tp1_px, kapat_oran)
    pos["tp1_hit"] = True
    pos["tp1_kar"] = tp1_kar
    pos["current_stop"] = yeni_stop
    pos["tp1_time"] = now_tr()
    save_data(data)
    print(f"[SHADOW TP1] {system_tag} | {ticker} @ {tp1_px} → +{tp1_kar}$")
    return {"status": "shadow_tp1", "shadow": True, "kar": tp1_kar}

def shadow_handle_tp2(msg, system_tag):
    parsed = parse_tp2(msg)
    if not parsed:
        return {"status": "parse_error", "shadow": True}
    ticker = parsed["ticker"]
    if ticker not in data["shadow_positions"]:
        return {"status": "not_found", "shadow": True}
    pos = data["shadow_positions"][ticker]
    if pos.get("tp2_hit"):
        return {"status": "tp2_duplicate", "shadow": True}
    tp2_px = parsed.get("tp2") or pos["tp2"]
    kapat_oran = parsed.get("kapat_oran", 25)
    tp2_kar = shadow_calc_kar(pos["marj"], pos["lev"], pos["giris"], tp2_px, kapat_oran)
    pos["tp2_hit"] = True
    pos["tp2_kar"] = tp2_kar
    pos["current_stop"] = pos["tp1"]
    pos["tp2_time"] = now_tr()
    save_data(data)
    print(f"[SHADOW TP2] {system_tag} | {ticker} @ {tp2_px} → +{tp2_kar}$")
    return {"status": "shadow_tp2", "shadow": True, "kar": tp2_kar}

def shadow_handle_stop_or_trail(msg, system_tag, kind="STOP"):
    try:
        parts = [p.strip() for p in msg.split("|")]
        ticker = parts[1].strip()
    except Exception as e:
        return {"status": "parse_error", "shadow": True}
    if ticker not in data["shadow_positions"]:
        return {"status": "not_found", "shadow": True}
    pos = data["shadow_positions"][ticker]
    exit_px = pos.get("current_stop", pos["stop"])
    kapat_oran = 100
    if pos.get("tp2_hit"):
        kapat_oran = 100 - pos.get("kapat_oran", 50) - 25
    elif pos.get("tp1_hit"):
        kapat_oran = 100 - pos.get("kapat_oran", 50)
    remaining_kar = shadow_calc_kar(pos["marj"], pos["lev"], pos["giris"], exit_px, kapat_oran)
    total_kar = round(pos.get("tp1_kar", 0) + pos.get("tp2_kar", 0) + remaining_kar, 2)
    if pos.get("tp2_hit"):
        sonuc = "TP1+TP2+Trail"
        kind = "TRAIL"
    elif pos.get("tp1_hit"):
        sonuc = "TP1+" + ("Trail" if kind == "TRAIL" else "Stop")
    else:
        sonuc = kind
    closed_rec = {
        **pos, "sonuc": sonuc, "kar": total_kar,
        "trail_kar": remaining_kar, "exit_px": exit_px,
        "kapanis": now_tr(), "kind": kind,
    }
    data["shadow_closed"].append(closed_rec)
    del data["shadow_positions"][ticker]
    if len(data["shadow_closed"]) > 200:
        data["shadow_closed"] = data["shadow_closed"][-200:]
    save_data(data)
    print(f"[SHADOW {kind}] {system_tag} | {ticker} | {sonuc} | +{total_kar}$")
    return {"status": "shadow_closed", "shadow": True, "kar": total_kar, "sonuc": sonuc}

# ============ WEBHOOK (v6.6: Safety Net entegre) ============
@app.post("/webhook")
async def webhook(req: Request):
    msg = (await req.body()).decode()
    print(f"[ALERT] {msg}")
    mode_tag = "[CANLI]" if not TEST_MODE else "[TEST]"

    # === RAM v14 SHADOW MODE ===
    if msg.startswith("RAM v14 |"):
        return shadow_handle_giris(msg, "RAM v14")
    if msg.startswith("RAM v14 TP1 |"):
        return shadow_handle_tp1(msg, "RAM v14")
    if msg.startswith("RAM v14 TP2 |"):
        return shadow_handle_tp2(msg, "RAM v14")
    if msg.startswith("RAM v14 STOP |"):
        return shadow_handle_stop_or_trail(msg, "RAM v14", "STOP")

    # === GIRIS ===
    if msg.startswith("CAB v13 |"):
        parsed = parse_giris(msg)
        if not parsed:
            return {"status": "parse_error"}
        print(f"[PARSE] {parsed}")
        ticker = parsed["ticker"]

        aktif_risk, gar_tp1, gar_to = count_active_risk()
        garantili = gar_tp1 + gar_to

        if aktif_risk >= MAX_POSITIONS:
            if "skipped_signals" not in data:
                data["skipped_signals"] = []
            data["skipped_signals"].append({
                "ticker": ticker, "giris": parsed["giris"], "stop": parsed["stop"],
                "tp1": parsed["tp1"], "tp2": parsed["tp2"],
                "marj": parsed["marj"], "lev": parsed["lev"],
                "risk": parsed["risk"], "kapat_oran": parsed["kapat_oran"],
                "atr_skor": parsed["atr_skor"], "zaman": now_tr(),
                "sebep": f"Max {MAX_POSITIONS} aktif risk dolu (+{garantili} garantili)"
            })
            if len(data["skipped_signals"]) > 50:
                data["skipped_signals"] = data["skipped_signals"][-50:]
            save_data(data)
            print(f"[LIMIT] Aktif risk {aktif_risk}/{MAX_POSITIONS} — {ticker} atlandı")
            return {"status": "limit"}
        if ticker in data["open_positions"]:
            return {"status": "duplicate"}

        trade_result = None
        if not TEST_MODE:
            trade_result = execute_entry(ticker, parsed)
            if not trade_result:
                print(f"[TRADE FAIL] {ticker} giriş başarısız!")
                return {"status": "trade_failed"}
            # v6.6: Safety net'in kapattığı pozisyonu tanı
            if isinstance(trade_result, dict) and trade_result.get("safety_closed"):
                return {"status": "safety_closed", "error": trade_result.get("error")}

        data["open_positions"][ticker] = {
            "giris": trade_result["avg_price"] if (trade_result and trade_result["avg_price"] > 0) else parsed["giris"],
            "stop": parsed["stop"],
            "tp1": parsed["tp1"], "tp2": parsed["tp2"],
            "marj": parsed["marj"], "lev": parsed["lev"],
            "risk": parsed["risk"], "kapat_oran": parsed["kapat_oran"],
            "atr_skor": parsed["atr_skor"], "durum": "Açık",
            "hh_pct": 0.0, "tp1_hit": False, "tp2_hit": False,
            "timeout_be": False,
            "tp1_kar": 0.0, "tp2_kar": 0.0,
            "qty": trade_result["qty"] if trade_result else 0,
            "sl_order_id": trade_result.get("sl_order_id") if trade_result else None,
            # v6.6 YENI: Stop verify alanları
            "binance_stop_verified": trade_result.get("binance_stop_verified", False) if trade_result else False,
            "binance_stop_price": trade_result.get("binance_stop_price") if trade_result else None,
            "last_stop_check": now_ms(),
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
                    return {"status": "reconciled"}
            return {"status": "already_reconciled"}

        if ticker not in data["open_positions"]:
            return {"status": "not_found"}

        pos = data["open_positions"][ticker]
        tp1_close_result = None
        if not TEST_MODE:
            tp1_close_result = execute_tp1_close(ticker, pos)

        if parsed["tp1_kar"] == 0:
            parsed["tp1_kar"] = calc_tp1_kar(pos, parsed["tp1"])

        pos["tp1_hit"] = True
        pos["tp1_kar"] = parsed["tp1_kar"]
        pos["stop"] = parsed["stop"]
        pos["durum"] = "✓ TP1 Alındı"
        pos["kapat_oran"] = parsed["kapat_oran"]
        pos["tp1_zaman"] = now_tr()
        pos["timeout_be"] = False
        # v6.6: gerçek fill fiyat ve BE stop sonucu
        if tp1_close_result:
            pos["tp1_fill_price"] = tp1_close_result.get("tp1_fill_price", 0)
            pos["binance_stop_verified"] = tp1_close_result.get("be_stop_ok", False)
            pos["last_stop_check"] = now_ms()
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
                    return {"status": "reconciled"}
            return {"status": "already_reconciled"}

        if ticker not in data["open_positions"]:
            return {"status": "not_found"}

        pos = data["open_positions"][ticker]
        tp2_close_result = None
        if not TEST_MODE:
            tp2_close_result = execute_tp2_close(ticker, pos)

        if parsed["tp2_kar"] == 0:
            parsed["tp2_kar"] = calc_tp2_kar(pos, parsed["tp2"])

        pos["tp2_hit"] = True
        pos["tp2_kar"] = parsed["tp2_kar"]
        pos["durum"] = "✓✓ TP2 Alındı"
        if tp2_close_result:
            pos["tp2_fill_price"] = tp2_close_result.get("tp2_fill_price", 0)
            pos["binance_stop_verified"] = tp2_close_result.get("be_stop_ok", False)
            pos["last_stop_check"] = now_ms()
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

        # v6.6: Binance'ten gerçek PNL çekmeyi dene
        acilis_ms = parse_zaman_to_ms(pos.get("zaman_full", ""))
        binance_pnl = None
        if acilis_ms and not TEST_MODE:
            pnl_result = fetch_binance_realized_pnl(ticker, acilis_ms)
            if pnl_result.get("success"):
                binance_pnl = pnl_result["net_pnl"]

        closed = {
            "ticker": ticker, "giris": pos["giris"], "marj": pos["marj"], "lev": pos["lev"],
            "sonuc": sonuc, "kar": total_kar,
            "tp1_kar": tp1_kar, "tp2_kar": tp2_kar, "trail_kar": trail_kar,
            "tp1_kar_added": pos.get("tp1_hit", False), "tp2_kar_added": pos.get("tp2_hit", False),
            "hh_pct": pos.get("hh_pct", 0), "atr_skor": pos.get("atr_skor", 1.0),
            "kapat_oran": pos.get("kapat_oran", 60),
            "acilis": pos.get("zaman_full", ""), "kapanis": now_tr(),
            "binance_pnl": binance_pnl,  # v6.6: Binance'in raporladığı gerçek PNL
            "pine_pnl": total_kar,
        }
        data["closed_positions"].append(closed)
        del data["open_positions"][ticker]
        stopless_positions.discard(ticker)  # v6.6: clean up
        save_data(data)
        print(f"{mode_tag} TRAIL({parsed['tp_type']}): {ticker} | {sonuc} | Pine:+{total_kar}$ | Binance:{binance_pnl}$")
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
                print(f"[TRADE WARN] STOP geldi ama {ticker} hala açık, zorla kapatılıyor")
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
            stop_kar = pos_size * ((stop_px - giris) / giris)
            sonuc = "⏰ Timeout-BE Stop"
        else:
            stop_kar = pos_size * ((stop_px - giris) / giris)
            sonuc = "✗ Stop"

        total_kar = round(tp1_kar + tp2_kar + stop_kar, 2)

        # v6.6: Binance gerçek PNL
        acilis_ms = parse_zaman_to_ms(pos.get("zaman_full", ""))
        binance_pnl = None
        if acilis_ms and not TEST_MODE:
            pnl_result = fetch_binance_realized_pnl(ticker, acilis_ms)
            if pnl_result.get("success"):
                binance_pnl = pnl_result["net_pnl"]

        closed = {
            "ticker": ticker, "giris": giris, "marj": pos["marj"], "lev": pos["lev"],
            "sonuc": sonuc, "kar": total_kar,
            "tp1_kar": tp1_kar, "tp2_kar": tp2_kar, "trail_kar": round(stop_kar, 2),
            "tp1_kar_added": pos.get("tp1_hit", False), "tp2_kar_added": pos.get("tp2_hit", False),
            "hh_pct": pos.get("hh_pct", 0), "atr_skor": pos.get("atr_skor", 1.0),
            "kapat_oran": kapat_oran,
            "acilis": pos.get("zaman_full", ""), "kapanis": now_tr(),
            "binance_pnl": binance_pnl,
            "pine_pnl": total_kar,
        }
        data["closed_positions"].append(closed)
        del data["open_positions"][ticker]
        stopless_positions.discard(ticker)
        save_data(data)
        print(f"{mode_tag} STOP: {ticker} | {sonuc} | Pine:{total_kar}$ | Binance:{binance_pnl}$")
        return {"status": "stopped"}

    else:
        print(f"[UNKNOWN] {msg[:80]}")
        return {"status": "unknown"}

# ============ BACKGROUND TASKS ============
def parse_zaman_to_ms(zaman_str):
    """'2026-04-18 10:30' formatını TR saati varsayıp UTC ms'e çevir"""
    try:
        dt = datetime.strptime(zaman_str, "%Y-%m-%d %H:%M")
        dt_utc = dt - timedelta(hours=3)
        return int(dt_utc.replace(tzinfo=timezone.utc).timestamp() * 1000)
    except Exception:
        return None

async def timeout_scan_once():
    scanned = 0
    actioned = []
    errors = []
    try:
        now = now_tr_dt()
        for ticker in list(data["open_positions"].keys()):
            pos = data["open_positions"][ticker]
            if pos.get("tp1_hit") or pos.get("timeout_be"):
                continue
            scanned += 1
            try:
                zaman_str = pos.get("zaman_full", "")
                if not zaman_str:
                    continue
                acilis = datetime.strptime(zaman_str, "%Y-%m-%d %H:%M")
                age_hours = (now - acilis).total_seconds() / 3600
            except Exception as e:
                errors.append(f"{ticker}: zaman parse: {e}")
                continue
            if age_hours < TIMEOUT_HOURS:
                continue
            aktif_risk, gar_tp1, gar_to = count_active_risk()
            if age_hours < TIMEOUT_ABSOLUTE_HOURS:
                if aktif_risk < TIMEOUT_PRESSURE_THRESHOLD:
                    continue
                print(f"[TIMEOUT] {ticker} {age_hours:.1f}s, aktif_risk:{aktif_risk}/{MAX_POSITIONS} — akıllı kapama")
            else:
                print(f"[TIMEOUT] {ticker} {age_hours:.1f}s MUTLAK limit — zorla kapama")

            try:
                action, kar = execute_smart_timeout(ticker, pos)
            except Exception as e:
                errors.append(f"{ticker}: smart timeout: {e}")
                continue

            if action == 'be':
                pos["timeout_be"] = True
                pos["timeout_zaman"] = now_tr()
                pos["timeout_kar_initial"] = kar
                pos["stop"] = pos["giris"]
                pos["durum"] = f"⏰ Timeout-BE (+{kar:.1f}$)"
                pos["binance_stop_verified"] = True
                pos["last_stop_check"] = now_ms()
                save_data(data)
                actioned.append({"ticker": ticker, "action": "BE", "unrealized_kar": kar, "age_hours": round(age_hours, 1)})
            elif action == 'close':
                acilis_ms = parse_zaman_to_ms(pos.get("zaman_full", ""))
                binance_pnl = None
                if acilis_ms and not TEST_MODE:
                    pnl_result = fetch_binance_realized_pnl(ticker, acilis_ms)
                    if pnl_result.get("success"):
                        binance_pnl = pnl_result["net_pnl"]
                closed = {
                    "ticker": ticker, "giris": pos["giris"], "marj": pos["marj"], "lev": pos["lev"],
                    "sonuc": "⏰ Timeout", "kar": round(kar, 2),
                    "tp1_kar": 0, "tp2_kar": 0, "trail_kar": round(kar, 2),
                    "tp1_kar_added": False, "tp2_kar_added": False,
                    "hh_pct": pos.get("hh_pct", 0), "atr_skor": pos.get("atr_skor", 1.0),
                    "kapat_oran": pos.get("kapat_oran", 60),
                    "acilis": pos.get("zaman_full", ""), "kapanis": now_tr(),
                    "binance_pnl": binance_pnl,
                    "pine_pnl": round(kar, 2),
                }
                data["closed_positions"].append(closed)
                del data["open_positions"][ticker]
                stopless_positions.discard(ticker)
                save_data(data)
                actioned.append({"ticker": ticker, "action": "CLOSE", "kar": kar, "age_hours": round(age_hours, 1)})
    except Exception as e:
        errors.append(f"global: {e}")
    return {
        "scanned": scanned, "actioned": actioned, "actioned_count": len(actioned),
        "errors": errors, "now_tr": now_tr(), "timeout_hours": TIMEOUT_HOURS
    }

async def check_timeouts():
    while True:
        await asyncio.sleep(TIMEOUT_CHECK_INTERVAL_SEC)
        try:
            result = await timeout_scan_once()
            if result["actioned_count"] > 0 or result["errors"]:
                print(f"[TIMEOUT-SCAN] scanned:{result['scanned']} actioned:{result['actioned_count']}")
        except Exception as e:
            print(f"[TIMEOUT TASK ERR] {e}")

async def update_position_highs_lows():
    while True:
        await asyncio.sleep(HIGH_LOW_CHECK_INTERVAL_SEC)
        try:
            updated_open = 0
            updated_skipped = 0
            shadow_updated = 0
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
                hh_pct = (max_high - giris) / giris * 100.0
                old_hh = pos.get("hh_pct", 0)
                if hh_pct > old_hh:
                    pos["hh_pct"] = round(hh_pct, 2)
                    updated_open += 1
                pos["max_seen"] = max_high
                pos["min_seen"] = min_low
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
                    old_max = s.get("max_seen", 0)
                    old_min = s.get("min_seen", float('inf'))
                    if max_high > old_max:
                        s["max_seen"] = max_high
                        updated_skipped += 1
                    if min_low < old_min:
                        s["min_seen"] = min_low
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
                    hh_pct = (max_high - giris) / giris * 100.0
                    old_hh = sp.get("hh_pct", 0)
                    if hh_pct > old_hh:
                        sp["hh_pct"] = round(hh_pct, 2)
                        shadow_updated += 1
                    sp["max_seen"] = max_high
                    sp["min_seen"] = min_low
            if updated_open > 0 or updated_skipped > 0 or shadow_updated > 0:
                save_data(data)
        except Exception as e:
            print(f"[HIGH-LOW TASK ERR] {e}")

# ============ v6.6 YENI: STOP WATCHDOG ============
async def stop_watchdog_scan_once():
    """
    Tüm açık pozları tara, Binance'te stop emri var mı kontrol et.
    Stop YOK ise: yeni stop ekle (pos.stop değeriyle) — yani manuel stop'lara DOKUNMA,
    sadece eksik olanları tamamla.
    Stop VAR ise: binance_stop_price güncelle (kullanıcı manuel değiştirmiş olabilir).
    """
    scanned = 0
    fixed = []
    still_missing = []
    errors = []
    global stopless_positions

    for ticker in list(data["open_positions"].keys()):
        pos = data["open_positions"][ticker]
        scanned += 1
        try:
            # Önce Binance'te gerçekten açık bir pozisyon var mı kontrol et
            # (state_reconciler ayrı iş yapıyor ama watchdog da ihtiyatlı olmalı)
            pos_status = get_binance_position_status(ticker)
            if pos_status.get("open") is False:
                # Binance'te pozisyon yok → watchdog atlar, reconciler halleder
                continue
            if pos_status.get("open") is None:
                errors.append(f"{ticker}: position_status bilinmiyor")
                continue

            stop_check = check_stop_exists_on_binance(ticker)
            if stop_check.get("exists") is None:
                errors.append(f"{ticker}: stop check hata: {stop_check.get('error')}")
                continue

            if stop_check.get("exists"):
                # Stop var → verify + güncel fiyatı kaydet
                pos["binance_stop_verified"] = True
                pos["binance_stop_price"] = stop_check.get("stop_price")
                pos["last_stop_check"] = now_ms()
                stopless_positions.discard(ticker)
            else:
                # Stop YOK — eksik, tamamla
                stopless_positions.add(ticker)
                pos["binance_stop_verified"] = False
                pos["last_stop_check"] = now_ms()
                info = get_symbol_info(ticker)
                qty = binance_get_position_qty(ticker)
                if qty <= 0:
                    continue  # pozisyon yokmuş, reconciler halleder
                # Stop fiyatını pos'tan al (manuel veya webhook değeri)
                stop_price = pos.get("stop", 0)
                if stop_price <= 0:
                    errors.append(f"{ticker}: stop_price yok/sıfır, eklenemedi")
                    still_missing.append(ticker)
                    continue
                sl_result = binance_stop_loss(ticker, qty, stop_price, info)
                if sl_result.get("success"):
                    pos["binance_stop_verified"] = True
                    pos["binance_stop_price"] = sl_result.get("stop_price")
                    pos["sl_order_id"] = sl_result.get("order_id")
                    stopless_positions.discard(ticker)
                    fixed.append({"ticker": ticker, "stop": sl_result.get("stop_price")})
                    print(f"[WATCHDOG] {ticker} stop eksikti → eklendi: {sl_result.get('stop_price')}")
                else:
                    still_missing.append(ticker)
                    pos["stop_last_error"] = sl_result.get("error", "unknown")[:200]
                    print(f"[WATCHDOG] 🚨 {ticker} stop eklenemedi: {sl_result.get('error')}")
        except Exception as e:
            errors.append(f"{ticker}: {e}")

    save_data(data)
    return {
        "scanned": scanned,
        "fixed": fixed,
        "fixed_count": len(fixed),
        "still_missing": still_missing,
        "still_missing_count": len(still_missing),
        "errors": errors,
        "stopless_count": len(stopless_positions),
    }

async def stop_watchdog():
    """Background watchdog — her 60sn tüm pozları tara"""
    while True:
        await asyncio.sleep(STOP_CHECK_INTERVAL_SEC)
        try:
            result = await stop_watchdog_scan_once()
            if result["fixed_count"] > 0 or result["still_missing_count"] > 0:
                print(f"[WATCHDOG] scanned:{result['scanned']} fixed:{result['fixed_count']} missing:{result['still_missing_count']}")
        except Exception as e:
            print(f"[WATCHDOG TASK ERR] {e}")

# ============ v6.6 YENI: STATE RECONCILER ============
async def state_reconciler_scan_once():
    """
    Bot'un açık pozlar listesi vs Binance'in gerçek durumu karşılaştır.
    Bot'a göre açık ama Binance'te yoksa → "hayalet poz", closed'a aktar.
    Binance realized PNL'i çek, fee dahil gerçek sonucu kaydet.
    """
    scanned = 0
    reconciled = []
    errors = []

    for ticker in list(data["open_positions"].keys()):
        pos = data["open_positions"][ticker]
        scanned += 1
        try:
            pos_status = get_binance_position_status(ticker)
            if pos_status.get("open") is None:
                errors.append(f"{ticker}: position_status bilinmiyor")
                continue
            if pos_status.get("open"):
                continue  # Poz gerçekten açık — sorun yok

            # Binance'te kapalı ama bot'ta açık → hayalet poz
            print(f"[RECONCILE] 🔍 {ticker} Binance'te KAPALI ama bot'ta AÇIK → closed'a aktarılıyor")

            # Gerçek PNL çek
            acilis_ms = parse_zaman_to_ms(pos.get("zaman_full", ""))
            binance_pnl = None
            trades = 0
            if acilis_ms:
                pnl_result = fetch_binance_realized_pnl(ticker, acilis_ms)
                if pnl_result.get("success"):
                    binance_pnl = pnl_result["net_pnl"]
                    trades = pnl_result.get("trades", 0)

            # Pine hesabını fallback yap (binance_pnl None ise)
            if binance_pnl is None:
                # Unrealized hesap olmaz — close fiyatı yok, sadece stop/tp'den tahmin et
                # Eğer tp1_hit VE tp2_hit ise + trail stop, yoksa pine_fallback kullan
                giris = pos["giris"]
                pos_size = pos["marj"] * pos["lev"]
                stop = pos.get("stop", giris)  # ya mevcut stop ya da giris (BE)
                tp1_kar = pos.get("tp1_kar", 0)
                tp2_kar = pos.get("tp2_kar", 0)
                kapat_oran = pos.get("kapat_oran", 60)
                if pos.get("tp2_hit"):
                    kalan = 100 - kapat_oran - 25
                elif pos.get("tp1_hit"):
                    kalan = 100 - kapat_oran
                else:
                    kalan = 100
                tail_kar = pos_size * (kalan / 100.0) * ((stop - giris) / giris)
                binance_pnl = round(tp1_kar + tp2_kar + tail_kar, 2)

            sonuc = "🔄 Reconcile"
            if pos.get("tp2_hit"):
                sonuc = "🔄 TP2+Reconcile"
            elif pos.get("tp1_hit"):
                sonuc = "🔄 TP1+Reconcile"
            elif pos.get("timeout_be"):
                sonuc = "🔄 Timeout-BE Reconcile"

            closed = {
                "ticker": ticker, "giris": pos["giris"], "marj": pos["marj"], "lev": pos["lev"],
                "sonuc": sonuc, "kar": binance_pnl if binance_pnl is not None else 0,
                "tp1_kar": pos.get("tp1_kar", 0), "tp2_kar": pos.get("tp2_kar", 0),
                "trail_kar": 0, "tp1_kar_added": pos.get("tp1_hit", False),
                "tp2_kar_added": pos.get("tp2_hit", False),
                "hh_pct": pos.get("hh_pct", 0), "atr_skor": pos.get("atr_skor", 1.0),
                "kapat_oran": pos.get("kapat_oran", 60),
                "acilis": pos.get("zaman_full", ""), "kapanis": now_tr(),
                "binance_pnl": binance_pnl,
                "pine_pnl": 0,
                "reconciled": True,
                "binance_trades": trades,
            }
            data["closed_positions"].append(closed)
            del data["open_positions"][ticker]
            stopless_positions.discard(ticker)

            # Uyarı kaydı ekle
            warn = {
                "ticker": ticker, "zaman": now_tr(),
                "mesaj": f"Bot'ta açıktı, Binance'te kapalı — PNL:{binance_pnl}$ ({trades} trade)",
                "binance_pnl": binance_pnl,
                "sonuc": sonuc,
            }
            reconcile_warnings.append(warn)
            if len(reconcile_warnings) > 50:
                reconcile_warnings.pop(0)

            reconciled.append({"ticker": ticker, "pnl": binance_pnl, "trades": trades, "sonuc": sonuc})
        except Exception as e:
            errors.append(f"{ticker}: {e}")

    if reconciled:
        save_data(data)
    return {
        "scanned": scanned,
        "reconciled": reconciled,
        "reconciled_count": len(reconciled),
        "errors": errors,
        "warnings_total": len(reconcile_warnings),
    }

async def state_reconciler():
    """Background reconciler — her 60sn"""
    while True:
        await asyncio.sleep(STATE_RECONCILE_INTERVAL_SEC)
        try:
            result = await state_reconciler_scan_once()
            if result["reconciled_count"] > 0:
                print(f"[RECONCILE] scanned:{result['scanned']} reconciled:{result['reconciled_count']}")
        except Exception as e:
            print(f"[RECONCILE TASK ERR] {e}")

# ============ STARTUP ============
@app.on_event("startup")
async def startup():
    asyncio.create_task(check_timeouts())
    asyncio.create_task(update_position_highs_lows())
    asyncio.create_task(stop_watchdog())          # v6.6 YENI
    asyncio.create_task(state_reconciler())       # v6.6 YENI

    # v6.6: Boot-time ilk stop watchdog taraması (manuel stop'lara dokunmaz, sadece eksikleri tamamlar)
    try:
        initial_scan = await stop_watchdog_scan_once()
        print(f"[BOOT-SCAN] Stop watchdog ilk tarama: scanned:{initial_scan['scanned']} fixed:{initial_scan['fixed_count']} missing:{initial_scan['still_missing_count']}")
    except Exception as e:
        print(f"[BOOT-SCAN ERR] {e}")

    print(f"[BOOT] 🤖 CAB Bot v6.6 | Mode:{'CANLI' if not TEST_MODE else 'TEST'} | MaxPos:{MAX_POSITIONS} | Timeout:{TIMEOUT_HOURS}s→{TIMEOUT_ABSOLUTE_HOURS}s | HL:{HIGH_LOW_CHECK_INTERVAL_SEC}s | StopCheck:{STOP_CHECK_INTERVAL_SEC}s | Reconcile:{STATE_RECONCILE_INTERVAL_SEC}s | RAM Shadow:ON")

# ============ ROUTES ============
@app.get("/", response_class=HTMLResponse)
async def root():
    mode = "🟡 TEST MODU" if TEST_MODE else "🟢 CANLI MOD"
    return f"""<h3>🤖 CAB Bot v6.6 çalışıyor</h3>
<p>{mode}</p>
<p>MAX_POSITIONS: {MAX_POSITIONS} | TIMEOUT: {TIMEOUT_HOURS}s | STOP CHECK: {STOP_CHECK_INTERVAL_SEC}s | RECONCILE: {STATE_RECONCILE_INTERVAL_SEC}s</p>
<p>
  <a href='/dashboard'>Dashboard</a> |
  <a href='/test_binance'>Binance Test</a> |
  <a href='/api/health'>Health</a> |
  <a href='/api/timeout_check'>Timeout Check</a> |
  <a href='/api/stop_check'>Stop Check</a> |
  <a href='/api/reconcile_now'>Reconcile Now</a>
</p>"""

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
    results["SONUC"] = "🟢 TÜM TESTLER BAŞARILI" if all_ok else "🔴 BAZI TESTLER BAŞARISIZ"
    results["test_mode"] = TEST_MODE
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
    return {"status": "ok"}

@app.get("/api/data")
async def api_data():
    """v6.6: Dashboard için tüm veri + stopless_positions + reconcile_warnings"""
    d = load_data()
    d["stopless_positions"] = list(stopless_positions)
    d["reconcile_warnings"] = reconcile_warnings[-20:]  # son 20
    d["version"] = "v6.6"
    d["bot_mode"] = "TEST" if TEST_MODE else "CANLI"
    return JSONResponse(d)

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
    return {"status": "ok", "cleared": cleared}

@app.get("/api/timeout_check")
async def manual_timeout_check():
    result = await timeout_scan_once()
    return JSONResponse(result)

# ============ v6.6 YENI ENDPOINTS ============
@app.get("/api/stop_check")
async def api_stop_check():
    """Manuel watchdog tetikleme: tüm pozları tara, eksik stop varsa ekle"""
    result = await stop_watchdog_scan_once()
    return JSONResponse(result)

@app.get("/api/reconcile_now")
async def api_reconcile_now():
    """Manuel reconciler tetikleme: bot vs Binance state senkron kontrol"""
    result = await state_reconciler_scan_once()
    return JSONResponse(result)

@app.post("/api/resync_stops")
async def api_resync_stops(req: Request):
    """
    TEHLİKELİ: Tüm açık pozların stop'larını cancel edip Pine'dan gelen/pos.stop değeriyle
    YENİDEN koyar. Manuel stop seviyelerini sıfırlar. Sadece UI butonu ile çağrılmalı.
    Body: {"confirm": true} gerekir.
    """
    try:
        body = await req.json()
    except Exception:
        body = {}
    if not body.get("confirm"):
        return {"status": "confirm_required", "msg": "Bu işlem tüm manuel stop seviyelerini SİLER. {'confirm': true} gönder."}

    resynced = []
    errors = []
    for ticker in list(data["open_positions"].keys()):
        try:
            pos = data["open_positions"][ticker]
            pos_status = get_binance_position_status(ticker)
            if not pos_status.get("open"):
                continue
            binance_cancel_all(ticker)
            info = get_symbol_info(ticker)
            qty = pos_status["qty"]
            stop_price = pos.get("stop", 0)
            if stop_price <= 0:
                errors.append(f"{ticker}: stop_price yok")
                continue
            sl_result = binance_stop_loss(ticker, qty, stop_price, info)
            if sl_result.get("success"):
                pos["binance_stop_verified"] = True
                pos["binance_stop_price"] = sl_result.get("stop_price")
                pos["sl_order_id"] = sl_result.get("order_id")
                pos["last_stop_check"] = now_ms()
                stopless_positions.discard(ticker)
                resynced.append({"ticker": ticker, "stop": sl_result.get("stop_price")})
            else:
                errors.append(f"{ticker}: {sl_result.get('error')}")
        except Exception as e:
            errors.append(f"{ticker}: {e}")
    save_data(data)
    return {"status": "ok", "resynced": resynced, "resynced_count": len(resynced), "errors": errors}

@app.post("/api/migrate_pnl")
async def api_migrate_pnl(req: Request):
    """
    Geriye dönük: Son N kapanan pozisyonun Binance gerçek PNL'ini çek ve kaydet.
    Body: {"count": 20, "dry_run": true}
    dry_run=true → sadece raporla, değişiklik yapma
    dry_run=false → binance_pnl field'ını güncelle VE kar field'ını onunla değiştir
    """
    try:
        body = await req.json()
    except Exception:
        body = {}
    count = int(body.get("count", 20))
    dry_run = body.get("dry_run", True)  # DEFAULT güvenli

    to_migrate = data["closed_positions"][-count:]
    report = []
    total_pine = 0.0
    total_binance = 0.0
    missing = 0

    for c in to_migrate:
        ticker = c.get("ticker")
        acilis_ms = parse_zaman_to_ms(c.get("acilis", ""))
        kapanis_ms = parse_zaman_to_ms(c.get("kapanis", ""))
        if not acilis_ms:
            missing += 1
            report.append({"ticker": ticker, "error": "acilis zaman parse fail"})
            continue
        # Biraz buffer bırak (60sn sonra kapanış ms)
        end_ms = kapanis_ms + 60000 if kapanis_ms else None
        pnl_result = fetch_binance_realized_pnl(ticker, acilis_ms, end_ms)
        if not pnl_result.get("success"):
            missing += 1
            report.append({"ticker": ticker, "error": pnl_result.get("error", "fetch fail")})
            continue
        pine = c.get("pine_pnl", c.get("kar", 0))
        binance_pnl = pnl_result["net_pnl"]
        diff = round(binance_pnl - pine, 2)
        total_pine += pine
        total_binance += binance_pnl
        entry = {
            "ticker": ticker, "acilis": c.get("acilis"), "kapanis": c.get("kapanis"),
            "sonuc": c.get("sonuc"),
            "pine_pnl": pine, "binance_pnl": binance_pnl, "diff": diff,
            "trades": pnl_result.get("trades", 0),
        }
        report.append(entry)

        if not dry_run:
            c["binance_pnl"] = binance_pnl
            c["pine_pnl"] = pine
            c["kar"] = binance_pnl  # Gerçek rakamı göster
            c["migrated"] = True

    if not dry_run:
        save_data(data)

    return {
        "status": "ok",
        "dry_run": dry_run,
        "scanned": len(to_migrate),
        "missing": missing,
        "total_pine_pnl": round(total_pine, 2),
        "total_binance_pnl": round(total_binance, 2),
        "total_diff": round(total_binance - total_pine, 2),
        "report": report,
    }

@app.get("/api/binance_pnl/{ticker}")
async def api_binance_pnl_single(ticker: str, hours: int = 48):
    """Tek bir sembol için son N saatteki Binance PNL'i"""
    start_ms = now_ms() - hours * 3600 * 1000
    result = fetch_binance_realized_pnl(ticker, start_ms)
    return JSONResponse(result)

@app.get("/api/health")
async def api_health():
    """v6.6: Sistem sağlık özeti"""
    open_count = len(data["open_positions"])
    closed_count = len(data["closed_positions"])
    shadow_open = len(data.get("shadow_positions", {}))
    shadow_closed_count = len(data.get("shadow_closed", []))

    # Doğrulanmamış stop'ları say
    unverified = 0
    for pos in data["open_positions"].values():
        if not pos.get("binance_stop_verified"):
            unverified += 1

    return JSONResponse({
        "version": "v6.6",
        "mode": "TEST" if TEST_MODE else "CANLI",
        "timestamp": now_tr(),
        "open_positions": open_count,
        "closed_positions": closed_count,
        "shadow_open": shadow_open,
        "shadow_closed": shadow_closed_count,
        "stopless_count": len(stopless_positions),
        "stopless_tickers": list(stopless_positions),
        "unverified_stops": unverified,
        "recent_reconcile_warnings": reconcile_warnings[-5:],
        "intervals": {
            "timeout_check": TIMEOUT_CHECK_INTERVAL_SEC,
            "high_low": HIGH_LOW_CHECK_INTERVAL_SEC,
            "stop_check": STOP_CHECK_INTERVAL_SEC,
            "reconcile": STATE_RECONCILE_INTERVAL_SEC,
        },
        "alerts": {
            "has_stopless": len(stopless_positions) > 0,
            "has_reconcile_warning": len(reconcile_warnings) > 0,
        }
    })

# ============ DASHBOARD ============
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    mod_badge = "🟡 TEST MODU" if TEST_MODE else "🟢 CANLI MOD"
    mod_text = "TEST" if TEST_MODE else "CANLI"
    mod_color = "#fbbf24" if TEST_MODE else "#4ade80"

    html = f"""<!DOCTYPE html>
<html lang="tr"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CAB Bot v6.6 Dashboard</title>
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
.alarm-banner{{background:linear-gradient(90deg,#991b1b,#7f1d1d);color:#fff;padding:12px 14px;margin:8px 0 12px;border-radius:6px;border:2px solid #f87171;font-size:13px;display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;box-shadow:0 0 20px rgba(239,68,68,0.4)}}
.alarm-banner.hidden{{display:none}}
.alarm-banner strong{{font-size:14px}}
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

<h1>🤖 CAB Bot v6.6 Dashboard</h1>
<div>
  <span class="badge">{mod_badge}</span>
  <small style="color:#9ca3af">MAX:{MAX_POSITIONS} | Timeout:{TIMEOUT_HOURS}→{TIMEOUT_ABSOLUTE_HOURS}s | StopCheck:{STOP_CHECK_INTERVAL_SEC}s | Reconcile:{STATE_RECONCILE_INTERVAL_SEC}s</small>
  <button class="btn btn-warn" style="margin-left:8px;padding:3px 8px;font-size:10px" onclick="requestNotif()">🔔 Bildirim</button>
  <button class="btn" style="padding:3px 8px;font-size:10px" onclick="manualStopCheck()">🛡️ Stop Kontrol</button>
  <button class="btn" style="padding:3px 8px;font-size:10px" onclick="manualTimeoutCheck()">⏰ Timeout</button>
</div>
<div class="subtitle">⟳ Son güncelleme: <span id="lastUpdate">—</span> <small>(5sn fiyat / 20sn sayfa)</small></div>

<!-- v6.6: KIRMIZI ALARM BANNER (UI Aşama 2'de tam entegre olacak; mevcut durumda kritik bilgi gösteriliyor) -->
<div class="alarm-banner hidden" id="alarmBanner">
  <div>
    <strong>🚨 <span id="alarmText"></span></strong>
    <div style="font-size:11px;margin-top:4px;opacity:0.9">Bu pozisyonlarda Binance'te STOP emri eksik olabilir!</div>
  </div>
  <div style="display:flex;gap:6px">
    <button class="btn" onclick="manualStopCheck()">🛡️ Watchdog Çalıştır</button>
    <button class="btn btn-danger" onclick="confirmResyncStops()">🔨 Resync Stops</button>
  </div>
</div>

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
      <th data-sort="px">Şu An</th><th data-sort="tp1kalan">TP1'e Kalan</th><th data-sort="pct">Durum</th>
      <th data-sort="hh_pct">HH%</th><th data-sort="atr_skor">ATR/Kapat</th><th data-sort="trail">Trailing</th>
      <th data-sort="stop">Stop</th><th data-sort="tp1">TP1</th><th data-sort="tp2">TP2</th><th data-sort="zaman_full">Zaman</th>
    </tr></thead><tbody id="openBody"><tr><td colspan="13" style="text-align:center;color:#9ca3af">Yükleniyor...</td></tr></tbody></table>
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
        <option value="reconcile">🔄 Reconcile</option>
      </select>
      <input type="text" id="searchClosed" placeholder="Coin ara..." oninput="renderClosed()">
      <button class="btn" onclick="showAnalysis()">📊 Analiz</button>
      <button class="btn btn-alt" onclick="exportCSV('closed')">📥 CSV</button>
      <button class="btn btn-danger" onclick="clearOld()">🗑 30g+ Sil</button>
    </div>
  </div>
  <div class="ozet" id="closedOzet"></div>
  <div style="overflow-x:auto;">
    <table id="closedTable"><thead><tr>
      <th data-sort="ticker">Coin</th><th data-sort="marj">Marjin</th><th data-sort="giris">Giriş</th>
      <th data-sort="sonuc">Sonuç</th><th data-sort="kar">Kar/Zarar</th><th data-sort="hh_pct">HH%</th>
      <th data-sort="atr_skor">ATR/Kapat</th><th data-sort="sure_dk">Süre</th><th data-sort="kapanis">Zaman</th>
    </tr></thead><tbody id="closedBody"><tr><td colspan="9" style="text-align:center;color:#9ca3af">Yükleniyor...</td></tr></tbody></table>
  </div>
</div>

<div class="section" id="skippedSection" style="display:none">
  <div class="section-head">
    <h2>⏭ Kaçırılan Sinyaller <small id="skippedCount"></small></h2>
    <div class="toolbar">
      <label>Durum:</label>
      <select id="filterSkipped" onchange="renderSkipped()">
        <option value="all">Hepsi</option>
        <option value="tp">✓ TP Vurmuş</option>
        <option value="stop">✗ Stop Olmuş</option>
        <option value="active">⏳ Hala Aktif</option>
        <option value="safety">🚨 Safety Closed</option>
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

<div class="section" id="shadowSection">
  <div class="section-head">
    <h2>🌓 RAM v14 Shadow <small id="shadowCount" style="color:#fb923c"></small></h2>
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
let stoplessPositions=[],reconcileWarnings=[];
let sortState={{open:{{col:null,dir:'asc'}},closed:{{col:'kapanis',dir:'desc'}}}};

async function loadData(){{try{{const r=await fetch('/api/data');const d=await r.json();openPositions=d.open_positions||{{}};closedPositions=d.closed_positions||[];skippedSignals=d.skipped_signals||[];shadowPositions=d.shadow_positions||{{}};shadowClosed=d.shadow_closed||[];stoplessPositions=d.stopless_positions||[];reconcileWarnings=d.reconcile_warnings||[];renderAll();detectChanges()}}catch(e){{console.error(e)}}}}

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
function resultMatches(s,f){{if(f==='all')return true;if(f==='stop')return s==='✗ Stop';if(f==='tp1trail')return s.includes('TP1+Trail')&&!s.includes('TP2');if(f==='tp2trail')return s.includes('TP1+TP2');if(f==='be')return s.includes('BE');if(f==='timeout')return s.includes('Timeout');if(f==='reconcile')return s.includes('Reconcile');return true}}
function openTV(t){{window.open('https://www.tradingview.com/chart/?symbol=BINANCE:'+t+'.P','_blank')}}
function toast(msg,err){{const t=document.getElementById('toast');t.textContent=msg;t.className='toast show'+(err?' err':'');setTimeout(()=>t.classList.remove('show'),2500)}}

async function manualStopCheck(){{
toast('Stop watchdog çalıştırılıyor...');
try{{const r=await fetch('/api/stop_check');const j=await r.json();
const msg=`Tarandı:${{j.scanned}} | Düzeltildi:${{j.fixed_count}} | Hala eksik:${{j.still_missing_count}}`;
toast(j.fixed_count>0?`✓ ${{msg}}`:`ℹ ${{msg}}`);loadData()}}catch(e){{toast('Hata:'+e.message,true)}}}}

async function manualTimeoutCheck(){{
toast('Timeout taraması...');
try{{const r=await fetch('/api/timeout_check');const j=await r.json();
toast(j.actioned_count>0?`✓ ${{j.actioned_count}} aksiyon`:`✓ Tarandı:${{j.scanned}}, aksiyon yok`);loadData()}}catch(e){{toast('Hata',true)}}}}

async function confirmResyncStops(){{
if(!confirm('⚠️ TEHLİKELİ: Bu işlem TÜM açık pozların stop\\'larını cancel edip Pine\\'dan gelen değerle YENİDEN koyar.\\n\\nManuel stop seviyelerin SİLİNECEK!\\n\\nDevam edilsin mi?'))return;
toast('Resync başladı...');
try{{const r=await fetch('/api/resync_stops',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{confirm:true}})}});const j=await r.json();
toast(`✓ Resync: ${{j.resynced_count}} poz`);loadData()}}catch(e){{toast('Hata:'+e.message,true)}}}}

function renderAll(){{renderStats();renderOpen();renderClosed();renderSkipped();renderShadow();checkWarnings();renderAlarmBanner()}}

function renderAlarmBanner(){{
const banner=document.getElementById('alarmBanner');
const txt=document.getElementById('alarmText');
if(stoplessPositions.length>0){{
  txt.textContent=`${{stoplessPositions.length}} pozisyonda stop eksik: ${{stoplessPositions.join(', ')}}`;
  banner.classList.remove('hidden');
}}else{{banner.classList.add('hidden')}}
}}

let skippedPrices={{}};
async function updateSkippedPrices(){{
const promises=skippedSignals.map(async s=>{{const px=await fp(s.ticker);if(px!==null)skippedPrices[s.ticker]=px;return{{sym:s.ticker,px}}}});
try{{await Promise.all(promises)}}catch(e){{console.error(e)}}
renderSkipped()}}

function renderSkipped(){{
const sec=document.getElementById('skippedSection');
if(!skippedSignals.length){{sec.style.display='none';return}}
sec.style.display='block';
const filter=document.getElementById('filterSkipped').value;
let rows=skippedSignals.slice().reverse().map(s=>{{
const px=skippedPrices[s.ticker]||null;
const giris=s.giris,stop=s.stop,tp1=s.tp1,tp2=s.tp2;
const isSafetyClosed=s.safety_closed===true;
let status='active',statusTxt='⏳ Aktif',statusColor='#fbbf24';
let sanalKar=0;
const posSize=s.marj*s.lev;
if(isSafetyClosed){{status='safety';statusTxt='🚨 Safety Closed';statusColor='#f87171';sanalKar=0}}
else{{
const tp2HitSeen=s.tp2_hit_seen===true;
const tp1HitSeen=s.tp1_hit_seen===true;
const stopHitSeen=s.stop_hit_seen===true;
const maxSeen=s.max_seen||null;
if(tp2HitSeen){{status='tp2';statusTxt='★ TP2 Vurmuş!';statusColor='#14b8a6';const cur=px||maxSeen||tp2;sanalKar=posSize*0.5*((tp1-giris)/giris)+posSize*0.25*((tp2-giris)/giris)+posSize*0.25*((cur-giris)/giris);
}}else if(tp1HitSeen){{status='tp1';statusTxt='✓ TP1 Vurmuş';statusColor='#84cc16';const cur=px||maxSeen||tp1;sanalKar=posSize*0.5*((tp1-giris)/giris)+posSize*0.5*((cur-giris)/giris);
}}else if(stopHitSeen){{status='stop';statusTxt='✗ Stop Olmuş';statusColor='#f87171';sanalKar=posSize*((stop-giris)/giris);
}}else if(px){{
if(px<=stop){{status='stop';statusTxt='✗ Stop';statusColor='#f87171';sanalKar=posSize*((stop-giris)/giris)}}
else if(px>=tp2){{status='tp2';statusTxt='★ TP2!';statusColor='#14b8a6';sanalKar=posSize*0.5*((tp1-giris)/giris)+posSize*0.25*((tp2-giris)/giris)+posSize*0.25*((px-giris)/giris)}}
else if(px>=tp1){{status='tp1';statusTxt='✓ TP1';statusColor='#84cc16';sanalKar=posSize*0.5*((tp1-giris)/giris)+posSize*0.5*((px-giris)/giris)}}
else{{sanalKar=posSize*((px-giris)/giris);if(sanalKar>0)statusTxt='📈 Kârda';else statusTxt='📉 Zararda'}}
}}}}
const rr=stop>0?((tp1-giris)/(giris-stop)).toFixed(1):'—';
const tp1kalan=px?((tp1-px)/px*100):null;
return{{...s,px,status,statusTxt,statusColor,sanalKar:Math.round(sanalKar*100)/100,rr,tp1kalan}}}});

if(filter==='tp')rows=rows.filter(r=>r.status==='tp1'||r.status==='tp2');
else if(filter==='stop')rows=rows.filter(r=>r.status==='stop');
else if(filter==='active')rows=rows.filter(r=>r.status==='active');
else if(filter==='safety')rows=rows.filter(r=>r.status==='safety');

const tpCount=rows.filter(r=>r.status==='tp1'||r.status==='tp2').length;
const stopCount=rows.filter(r=>r.status==='stop').length;
const activeCount=rows.filter(r=>r.status==='active').length;
const safetyCount=rows.filter(r=>r.status==='safety').length;
const missedKar=rows.filter(r=>r.status==='tp1'||r.status==='tp2').reduce((s,r)=>s+r.sanalKar,0);
const dodgedZarar=rows.filter(r=>r.status==='stop').reduce((s,r)=>s+Math.abs(r.sanalKar),0);
document.getElementById('skippedOzet').innerHTML=`✓TP:${{tpCount}} <span style="color:#f87171">(-${{missedKar.toFixed(0)}}$)</span> | ✗Stop:${{stopCount}} <span style="color:#4ade80">(+${{dodgedZarar.toFixed(0)}}$)</span> | ⏳Aktif:${{activeCount}} | 🚨Safety:${{safetyCount}}`;
document.getElementById('skippedCount').textContent=`(${{skippedSignals.length}})`;

const body=document.getElementById('skippedBody');
if(!rows.length){{body.innerHTML='<tr><td colspan="10" style="text-align:center;color:#9ca3af">Filtre sonucu boş</td></tr>';return}}
body.innerHTML=rows.map(s=>{{
const pxStr=s.px?s.px.toFixed(6):'—';
const pxColor=s.status==='tp2'?'#14b8a6':s.status==='tp1'?'#84cc16':s.status==='stop'?'#f87171':'#e5e7eb';
const tp1Str=s.tp1kalan!=null?(s.tp1kalan<=0?'<span style="color:#4ade80">✓</span>':`%${{s.tp1kalan.toFixed(2)}}`):'—';
const karStr=s.sanalKar!==0?`<span style="color:${{s.sanalKar>0?'#4ade80':'#f87171'}}">${{s.sanalKar>=0?'+':''}}${{s.sanalKar.toFixed(1)}}$</span>`:'—';
let rowBg='';
if(s.status==='tp2')rowBg='background:rgba(20,184,166,0.12);border-left:3px solid #14b8a6;';
else if(s.status==='tp1')rowBg='background:rgba(132,204,22,0.08);border-left:3px solid #84cc16;';
else if(s.status==='stop')rowBg='background:rgba(239,68,68,0.08);border-left:3px solid #f87171;';
else if(s.status==='safety')rowBg='background:rgba(239,68,68,0.2);border-left:3px solid #dc2626;';
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

async function clearSkipped(){{if(!confirm('Kaçırılan sinyal kayıtlarını temizle?'))return;try{{await fetch('/api/clear_skipped',{{method:'POST'}});skippedSignals=[];skippedPrices={{}};renderSkipped();toast('✓')}}catch(e){{toast('Hata',true)}}}}

function switchShadowTab(tab){{shadowTab=tab;document.getElementById('shadowTabOpen').style.background=tab==='open'?'#ea580c':'#1e40af';document.getElementById('shadowTabClosed').style.background=tab==='closed'?'#ea580c':'#1e40af';document.getElementById('shadowOpenView').style.display=tab==='open'?'block':'none';document.getElementById('shadowClosedView').style.display=tab==='closed'?'block':'none';renderShadow()}}

async function clearShadow(){{if(!confirm('TÜM Shadow kayıtları silinsin mi?'))return;try{{await fetch('/api/clear_shadow',{{method:'POST'}});shadowPositions={{}};shadowClosed=[];shadowPrices={{}};renderShadow();toast('✓')}}catch(e){{toast('Hata',true)}}}}

async function updateShadowPrices(){{const tickers=Object.keys(shadowPositions);if(tickers.length===0)return;const promises=tickers.map(async sym=>{{const px=await fp(sym);if(px!==null)shadowPrices[sym]=px}});await Promise.all(promises);renderShadow()}}

function renderShadow(){{
const openCount=Object.keys(shadowPositions).length;
const closedCount=shadowClosed.length;
document.getElementById('shadowCount').textContent=`(${{openCount}} açık / ${{closedCount}} kapalı)`;
let totalSanalKar=0;let tp1Count=0;let tp2Count=0;let stopCount=0;let trailCount=0;
shadowClosed.forEach(c=>{{totalSanalKar+=parseFloat(c.kar||0);if(c.sonuc&&c.sonuc.includes('TP2'))tp2Count++;else if(c.sonuc&&c.sonuc.includes('TP1'))tp1Count++;else if(c.sonuc==='STOP')stopCount++;if(c.kind==='TRAIL')trailCount++}});
let openSanalKar=0;
Object.entries(shadowPositions).forEach(([sym,p])=>{{const px=shadowPrices[sym];if(px&&p.giris){{const posSize=p.marj*p.lev;openSanalKar+=posSize*((px-p.giris)/p.giris)}}}});
const karColor=totalSanalKar>=0?'#4ade80':'#f87171';
const openColor=openSanalKar>=0?'#4ade80':'#f87171';
document.getElementById('shadowOzet').innerHTML=`<b>Açık:</b> <span style="color:${{openColor}}">${{openSanalKar>=0?'+':''}}${{openSanalKar.toFixed(1)}}$</span> | <b>Kapanan:</b> <span style="color:${{karColor}}">${{totalSanalKar>=0?'+':''}}${{totalSanalKar.toFixed(1)}}$</span> | TP1:${{tp1Count}} TP2:${{tp2Count}} Stop:${{stopCount}}`;

if(shadowTab==='open'){{
const tb=document.getElementById('shadowOpenBody');
if(openCount===0){{tb.innerHTML='<tr><td colspan="10" style="text-align:center;color:#9ca3af">RAM sinyali yok</td></tr>';return}}
const rows=Object.entries(shadowPositions).map(([sym,p])=>{{
const px=shadowPrices[sym]||p.max_seen||p.giris;
const giris=p.giris;
const posSize=p.marj*p.lev;
let durum='⏳ Aktif';let durumColor='#fbbf24';
if(p.tp2_hit){{durum='★ TP2';durumColor='#14b8a6'}}else if(p.tp1_hit){{durum='✓ TP1';durumColor='#84cc16'}}
const sanalKar=p.tp1_hit||p.tp2_hit?(p.tp1_kar||0)+(p.tp2_kar||0)+posSize*(1-((p.kapat_oran||50)/100)-(p.tp2_hit?0.25:0))*((px-giris)/giris):posSize*((px-giris)/giris);
return{{sym,giris,px,hh:p.hh_pct||0,stop:p.current_stop||p.stop,tp1:p.tp1,tp2:p.tp2,durum,durumColor,sanalKar:Math.round(sanalKar*100)/100,rs:p.rs_spread||0,zaman:p.zaman||'-'}};
}});
tb.innerHTML=rows.map(r=>`<tr><td><b>${{r.sym}}</b> <a href="https://www.tradingview.com/chart/?symbol=BINANCE:${{r.sym}}.P" target="_blank" style="color:#60a5fa">🔗</a></td><td>${{r.giris}}</td><td>${{r.px}}</td><td style="color:${{r.hh>0?'#4ade80':'#9ca3af'}}">${{r.hh.toFixed(2)}}%</td><td style="color:#f87171">${{r.stop}}</td><td>${{r.tp1}} / ${{r.tp2}}</td><td style="color:${{r.durumColor}}">${{r.durum}}</td><td style="color:${{r.sanalKar>=0?'#4ade80':'#f87171'}}"><b>${{r.sanalKar>=0?'+':''}}${{r.sanalKar.toFixed(1)}}$</b></td><td style="color:#fb923c">${{r.rs.toFixed(1)}}%</td><td style="color:#9ca3af">${{r.zaman}}</td></tr>`).join('')}}else{{
const tb=document.getElementById('shadowClosedBody');
if(closedCount===0){{tb.innerHTML='<tr><td colspan="9" style="text-align:center;color:#9ca3af">Yok</td></tr>';return}}
const sorted=[...shadowClosed].reverse();
tb.innerHTML=sorted.map(c=>{{
const karColor=c.kar>=0?'#4ade80':'#f87171';
let sonucColor='#fbbf24';
if(c.sonuc&&c.sonuc.includes('TP2'))sonucColor='#14b8a6';else if(c.sonuc&&c.sonuc.includes('TP1'))sonucColor='#84cc16';else if(c.sonuc==='STOP')sonucColor='#f87171';
return`<tr><td><b>${{c.ticker}}</b></td><td style="color:${{sonucColor}}">${{c.sonuc||'-'}}</td><td>${{c.giris}}</td><td>${{c.exit_px||'-'}}</td><td style="color:${{karColor}}"><b>${{c.kar>=0?'+':''}}${{c.kar}}$</b></td><td>${{c.tp1_kar||0}}/${{c.tp2_kar||0}}/${{c.trail_kar||0}}</td><td style="color:#fb923c">${{(c.rs_spread||0).toFixed(1)}}%</td><td style="color:#9ca3af">${{c.zaman||'-'}}</td><td style="color:#9ca3af">${{c.kapanis||'-'}}</td></tr>`;
}}).join('')}}
}}

function renderStats(){{
const tk=closedPositions.reduce((s,c)=>s+c.kar,0),ts=closedPositions.length,ws=closedPositions.filter(c=>c.kar>0).length,wr=ts>0?(ws/ts*100).toFixed(1):0;
const today=now_tr().slice(0,10),bg=closedPositions.filter(c=>c.kapanis.startsWith(today)),bk=bg.reduce((s,c)=>s+c.kar,0),bt=bg.filter(c=>c.kar>0).length,bs=bg.filter(c=>c.kar<=0).length,bw=bg.length>0?(bt/bg.length*100).toFixed(1):0;
const su=closedPositions.map(sureDk),os=su.length>0?su.reduce((a,b)=>a+b,0)/su.length:0,oss=os===0?'—':sureFmt(Math.round(os));
const nr=tk>0?'#4ade80':(tk<0?'#f87171':'#e5e7eb'),wrr=wr>=50?'#4ade80':(ts>0?'#f87171':'#e5e7eb'),bkr=bk>0?'#4ade80':(bk<0?'#f87171':'#e5e7eb'),bwr=bw>=50?'#4ade80':(bg.length>0?'#f87171':'#e5e7eb');
let aktifRisk=0,gar_tp1=0,gar_to=0;
for(const p of Object.values(openPositions)){{if(p.tp1_hit)gar_tp1++;else if(p.timeout_be)gar_to++;else aktifRisk++}}
const garantili=gar_tp1+gar_to;
const openLbl=garantili>0?`${{aktifRisk}}+${{gar_tp1}}★${{gar_to>0?'+'+gar_to+'⏰':''}}`:`${{aktifRisk}}`;
const openSubLbl=garantili>0?`Aktif+TP1${{gar_to>0?'+TO':''}}`:'Açık';
document.getElementById('statsBar').innerHTML=
`<div class="stat"><div class="stat-val">${{openLbl}}</div><div class="stat-lbl">${{openSubLbl}}</div></div>`+
`<div class="stat"><div class="stat-val">${{ts}}</div><div class="stat-lbl">Kapanan</div></div>`+
`<div class="stat"><div class="stat-val" style="color:${{nr}}">${{tk>=0?'+':''}}${{tk.toFixed(1)}}$</div><div class="stat-lbl">Net Kar</div></div>`+
`<div class="stat"><div class="stat-val" style="color:${{wrr}}">${{wr}}%</div><div class="stat-lbl">Win Rate</div></div>`+
`<div class="stat"><div class="stat-val" style="color:${{bwr}}">${{bw}}%</div><div class="stat-lbl">Bugün WR</div></div>`+
`<div class="stat"><div class="stat-val">${{oss}}</div><div class="stat-lbl">Ort. Süre</div></div>`+
`<div class="stat"><div class="stat-val" style="color:${{MC}}">${{MODE}}</div><div class="stat-lbl">Mod</div></div>`;
document.getElementById('bugunBox').innerHTML=`📅 <b>Bugün (${{today}}):</b> ${{bg.length}} kapanan | ${{bt}} TP | ${{bs}} Stop | <span style="color:${{bkr}}">${{bk>=0?'+':''}}${{bk.toFixed(1)}}$</span>`}}

function renderOpen(){{
const search=document.getElementById('searchOpen').value.toLowerCase();
let rows=Object.entries(openPositions).filter(([t])=>t.toLowerCase().includes(search)).map(([t,p])=>{{
const px=openPrices[t]||null,pct=px?(px-p.giris)/p.giris*100:null,tp1k=px?(p.tp1-px)/px*100:null;
return{{ticker:t,pos:p,px,pct,tp1kalan:tp1k,marj:p.marj,giris:p.giris,stop:p.stop,tp1:p.tp1,tp2:p.tp2,hh_pct:p.hh_pct||0,atr_skor:p.atr_skor||1.0,trail:p.tp1_hit?(p.tp1_kar||0):-999,zaman_full:p.zaman_full||''}}}});
const s=sortState.open;if(s.col)rows.sort((a,b)=>{{let av=a[s.col],bv=b[s.col];if(av==null)av=-Infinity;if(bv==null)bv=-Infinity;return typeof av==='string'?s.dir==='asc'?av.localeCompare(bv):bv.localeCompare(av):s.dir==='asc'?av-bv:bv-av}});
const body=document.getElementById('openBody');
if(!rows.length){{body.innerHTML='<tr><td colspan="13" style="text-align:center;color:#9ca3af">Açık pozisyon yok</td></tr>'}}
else{{body.innerHTML=rows.map(r=>{{const p=r.pos,t=r.ticker,ps=p.marj*p.lev,sz=ps*(p.stop-p.giris)/p.giris,k=p.kapat_oran||60,t1k=ps*(k/100)*(p.tp1-p.giris)/p.giris,t2k=ps*0.25*(p.tp2-p.giris)/p.giris;
let pxS='—',pxC='#e5e7eb',pcS='—';if(r.px){{const pr=ps*r.pct/100;pxS=r.px.toFixed(6);if(r.pct>0){{pxC='#4ade80';pcS=`<span style="color:#4ade80">▲ +${{pr.toFixed(1)}}$ (+${{r.pct.toFixed(2)}}%)</span>`}}else{{pxC='#f87171';pcS=`<span style="color:#f87171">▼ ${{pr.toFixed(1)}}$ (${{r.pct.toFixed(2)}}%)</span>`}}}}
const tkS=r.tp1kalan!=null?(r.tp1kalan<=0?'<span style="color:#4ade80">✓</span>':`%${{r.tp1kalan.toFixed(2)}}`):'—';
let trS='—';if(p.tp2_hit)trS=`✓TP2+ (+${{((p.tp1_kar||0)+(p.tp2_kar||0)).toFixed(1)}}$)`;else if(p.tp1_hit)trS=`✓TP1+ (+${{(p.tp1_kar||0).toFixed(1)}}$)`;else if(p.timeout_be)trS=`⏰TO-BE (+${{(p.timeout_kar_initial||0).toFixed(1)}}$)`;
let rb='';if(p.tp2_hit)rb='background:rgba(20,184,166,0.15);border-left:3px solid #14b8a6;';else if(p.tp1_hit)rb='background:rgba(132,204,22,0.12);border-left:3px solid #84cc16;';else if(p.timeout_be)rb='background:rgba(249,115,22,0.12);border-left:3px solid #f97316;';
// v6.6: stop eksik göstergesi
if(stoplessPositions.includes(t))rb='background:rgba(239,68,68,0.15);border-left:3px solid #dc2626;';
let wc='';try{{const ac=new Date(p.zaman_full.replace(' ','T')+':00+03:00');if((Date.now()-ac.getTime())/3600000>6&&!p.tp1_hit&&!p.timeout_be)wc='warn-row'}}catch(e){{}}
const hd=(p.hh_pct||0)>0?`%${{p.hh_pct.toFixed(2)}}`:'—',ad=`${{(p.atr_skor||1.0).toFixed(2)}}x (${{k}}%)`;
// v6.6: stop badge
const stopBadge=p.binance_stop_verified?'🟢':'🔴';
return`<tr class="${{wc}}" style="${{rb}}"><td>${{stopBadge}} <a href="javascript:void(0)" onclick="openTV('${{t}}')" style="color:#60a5fa;text-decoration:none;">${{t}}</a> 🔗</td><td>${{p.marj.toFixed(0)}}$ <small>(${{p.lev}}x)</small></td><td>${{p.giris.toFixed(6)}}</td><td style="color:${{pxC}}">${{pxS}}</td><td>${{tkS}}</td><td>${{p.durum&&(p.durum.includes('TP')||p.durum.includes('Timeout'))?p.durum:pcS}}</td><td>${{hd}}</td><td>${{ad}}</td><td>${{trS}}</td><td>${{p.stop.toFixed(6)}} <small style="color:#f87171">(${{sz>=0?'+':''}}${{sz.toFixed(1)}}$)</small></td><td>${{p.tp1.toFixed(6)}} <small style="color:#4ade80">(+${{t1k.toFixed(1)}}$)</small></td><td>${{p.tp2.toFixed(6)}} <small style="color:#4ade80">(+${{t2k.toFixed(1)}}$)</small></td><td>${{p.zaman||''}}</td></tr>`}}).join('')}}
document.getElementById('openCount').textContent=`(${{rows.length}})`;updateSortArrows('open')}}

function renderClosed(){{
const df=document.getElementById('filterDate').value,rf=document.getElementById('filterResult').value,search=document.getElementById('searchClosed').value.toLowerCase();
let rows=closedPositions.filter(c=>dateMatches(c.kapanis,df)&&resultMatches(c.sonuc,rf)&&c.ticker.toLowerCase().includes(search)).map(c=>({{...c,sure_dk:sureDk(c)}}));
const s=sortState.closed;if(s.col)rows.sort((a,b)=>{{let av=a[s.col],bv=b[s.col];if(av==null)av=-Infinity;if(bv==null)bv=-Infinity;return typeof av==='string'?s.dir==='asc'?av.localeCompare(bv):bv.localeCompare(av):s.dir==='asc'?av-bv:bv-av}});
const body=document.getElementById('closedBody');
if(!rows.length){{body.innerHTML='<tr><td colspan="9" style="text-align:center;color:#9ca3af">Veri yok</td></tr>'}}
else{{body.innerHTML=rows.map(c=>{{const rk=c.kar>0?'#4ade80':'#f87171',ks=(c.kar>=0?'+':'')+c.kar.toFixed(1)+'$',hd=(c.hh_pct||0)>0?`%${{c.hh_pct.toFixed(2)}}`:'—',ad=`${{(c.atr_skor||1.0).toFixed(2)}}x/${{c.kapat_oran||60}}%`,su=sureFmt(c.sure_dk),zm=c.acilis.slice(5)+'→'+c.kapanis.slice(11);
let wc='';if(c.sonuc==='✗ Stop'&&(c.hh_pct||0)>=5)wc='warn-row';
let scolor=rk;if(c.sonuc.includes('Timeout'))scolor='#f97316';if(c.sonuc.includes('Reconcile'))scolor='#a855f7';
// v6.6: Binance PNL varsa işareti göster
const bpLabel=(c.binance_pnl!=null)?` <small style="color:#60a5fa">🅱️</small>`:'';
return`<tr class="${{wc}}"><td><a href="javascript:void(0)" onclick="openTV('${{c.ticker}}')" style="color:#60a5fa;text-decoration:none;">${{c.ticker}}</a> 🔗</td><td>${{c.marj.toFixed(0)}}$</td><td>${{c.giris.toFixed(6)}}</td><td style="color:${{scolor}}">${{c.sonuc}}</td><td style="color:${{rk}};font-weight:bold">${{ks}}${{bpLabel}}</td><td>${{hd}}</td><td>${{ad}}</td><td>${{su}}</td><td>${{zm}}</td></tr>`}}).join('')}}
const tk=rows.filter(c=>c.kar>0).reduce((s,c)=>s+c.kar,0),tz=rows.filter(c=>c.kar<0).reduce((s,c)=>s+c.kar,0),nt=tk+tz,nc=nt>0?'#4ade80':(nt<0?'#f87171':'#e5e7eb');
document.getElementById('closedOzet').innerHTML=rows.length>0?`<b>${{rows.length}}</b> poz | <span style="color:#4ade80">+${{tk.toFixed(1)}}$</span> | <span style="color:#f87171">${{tz.toFixed(1)}}$</span> | <span style="color:${{nc}}">NET:${{nt>=0?'+':''}}${{nt.toFixed(1)}}$</span>`:'Veri yok';
document.getElementById('closedCount').textContent=`(${{rows.length}}/${{closedPositions.length}})`;updateSortArrows('closed')}}

function checkWarnings(){{
const warns=[];
for(const[t,p]of Object.entries(openPositions)){{if(p.tp1_hit||p.timeout_be)continue;try{{const ac=new Date(p.zaman_full.replace(' ','T')+':00+03:00');const h=((Date.now()-ac.getTime())/3600000).toFixed(1);if(h>6)warns.push(`⏰ <b>${{t}}</b> ${{sureFmt(Math.round(h*60))}}'dir açık`)}}catch(e){{}}}}
const son5=closedPositions.slice(-5);if(son5.length===5&&son5.every(c=>c.sonuc==='✗ Stop'))warns.push('🚨 <b>Son 5 üst üste STOP!</b>');
const hh_stop=closedPositions.slice(-15).filter(c=>c.sonuc==='✗ Stop'&&(c.hh_pct||0)>=5);
if(hh_stop.length>=2)warns.push(`⚠️ <b>${{hh_stop.length}}</b> TP1'e yaklaşıp stop yedi`);
// v6.6: reconcile uyarıları
if(reconcileWarnings.length>0){{warns.push(`🔄 <b>${{reconcileWarnings.length}}</b> reconcile uyarısı (Binance-bot state uyuşmazlığı)`)}}
document.getElementById('warnBox').innerHTML=warns.length>0?warns.map(w=>`<div class="warn-box">${{w}}</div>`).join(''):''}}

function showAnalysis(){{
const filtered=closedPositions.filter(c=>dateMatches(c.kapanis,document.getElementById('filterDate').value)&&resultMatches(c.sonuc,document.getElementById('filterResult').value));
if(!filtered.length){{toast('Veri yok',true);return}}
const wins=filtered.filter(c=>c.kar>0),losses=filtered.filter(c=>c.kar<0),tk=filtered.reduce((s,c)=>s+c.kar,0);
const wr=(wins.length/filtered.length*100).toFixed(1),aw=wins.length?wins.reduce((s,c)=>s+c.kar,0)/wins.length:0,al=losses.length?losses.reduce((s,c)=>s+c.kar,0)/losses.length:0;
const mw=wins.length?Math.max(...wins.map(c=>c.kar)):0,ml=losses.length?Math.min(...losses.map(c=>c.kar)):0;
const pf=losses.length&&Math.abs(al)>0?(wins.reduce((s,c)=>s+c.kar,0)/Math.abs(losses.reduce((s,c)=>s+c.kar,0))):0;
const ev=tk/filtered.length;
let peak=0,dd=0,mdd=0,cum=0;for(const c of filtered){{cum+=c.kar;if(cum>peak)peak=cum;dd=cum-peak;if(dd<mdd)mdd=dd}}
const dist={{}};for(const c of filtered)dist[c.sonuc]=(dist[c.sonuc]||0)+1;
document.getElementById('analysisBody').innerHTML=`<div class="stats-grid">
<div class="stat-card"><div class="stat-card-lbl">Toplam</div><div class="stat-card-val">${{filtered.length}}</div></div>
<div class="stat-card"><div class="stat-card-lbl">Win Rate</div><div class="stat-card-val" style="color:${{wr>=50?'#4ade80':'#f87171'}}">${{wr}}%</div></div>
<div class="stat-card"><div class="stat-card-lbl">Net</div><div class="stat-card-val" style="color:${{tk>0?'#4ade80':'#f87171'}}">${{tk>=0?'+':''}}${{tk.toFixed(1)}}$</div></div>
<div class="stat-card"><div class="stat-card-lbl">Avg Win</div><div class="stat-card-val" style="color:#4ade80">+${{aw.toFixed(1)}}$</div></div>
<div class="stat-card"><div class="stat-card-lbl">Avg Loss</div><div class="stat-card-val" style="color:#f87171">${{al.toFixed(1)}}$</div></div>
<div class="stat-card"><div class="stat-card-lbl">PF</div><div class="stat-card-val" style="color:${{pf>=1?'#4ade80':'#f87171'}}">${{pf.toFixed(2)}}</div></div>
<div class="stat-card"><div class="stat-card-lbl">EV</div><div class="stat-card-val" style="color:${{ev>0?'#4ade80':'#f87171'}}">${{ev>=0?'+':''}}${{ev.toFixed(2)}}$</div></div>
<div class="stat-card"><div class="stat-card-lbl">Max DD</div><div class="stat-card-val" style="color:#f87171">${{mdd.toFixed(1)}}$</div></div>
</div>
<div class="chart-wrap"><h3>Sonuç Dağılımı</h3><div style="max-width:280px;margin:0 auto"><canvas id="distChart"></canvas></div></div>
<div class="chart-wrap"><h3>Kümülatif Kar</h3><canvas id="cumChart" style="max-height:200px"></canvas></div>`;
document.getElementById('analysisModal').classList.add('show');
setTimeout(()=>{{
const dLabels=Object.keys(dist),dData=Object.values(dist),dColors=dLabels.map(k=>k.includes('TP2')?'#14b8a6':k.includes('TP1+Trail')?'#84cc16':k.includes('BE')?'#fbbf24':k.includes('Timeout')?'#f97316':k.includes('Reconcile')?'#a855f7':'#f87171');
new Chart(document.getElementById('distChart'),{{type:'doughnut',data:{{labels:dLabels,datasets:[{{data:dData,backgroundColor:dColors}}]}},options:{{plugins:{{legend:{{labels:{{color:'#e5e7eb',font:{{size:11}}}}}}}},responsive:true}}}});
let c2=0;const cD=filtered.slice().sort((a,b)=>a.kapanis.localeCompare(b.kapanis)).map(c=>{{c2+=c.kar;return c2}});
const cColor=cD[cD.length-1]>0?'#4ade80':'#f87171';
new Chart(document.getElementById('cumChart'),{{type:'line',data:{{labels:cD.map((_,i)=>i+1),datasets:[{{label:'Net ($)',data:cD,borderColor:cColor,backgroundColor:cColor+'1a',fill:true,tension:0.2}}]}},options:{{plugins:{{legend:{{labels:{{color:'#e5e7eb'}}}}}},scales:{{x:{{ticks:{{color:'#9ca3af'}},grid:{{color:'#334155'}}}},y:{{ticks:{{color:'#9ca3af'}},grid:{{color:'#334155'}}}}}}}}}});
}},100)}}
function closeModal(){{document.getElementById('analysisModal').classList.remove('show')}}

async function clearOld(){{if(!confirm('30g+ pozlar silinsin mi?'))return;try{{const r=await fetch('/api/clear_old',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{days:30}})}});const j=await r.json();toast(`✓ ${{j.removed}} silindi`);loadData()}}catch(e){{toast('Hata',true)}}}}

function requestNotif(){{if(!('Notification' in window)){{toast('Desteklenmiyor',true);return}}Notification.requestPermission().then(p=>{{if(p==='granted'){{toast('✓ Açık');new Notification('CAB Bot v6.6',{{body:'Bildirimler aktif!'}})}}}})}}

function detectChanges(){{
for(const[t,p]of Object.entries(openPositions)){{const prev=lastOpenState[t];if(prev){{if(p.tp1_hit&&!prev.tp1_hit)notify('🎯 TP1',t+' +'+((p.tp1_kar||0).toFixed(1))+'$');if(p.tp2_hit&&!prev.tp2_hit)notify('🎯🎯 TP2',t);if(p.timeout_be&&!prev.timeout_be)notify('⏰ TO-BE',t)}}else if(Object.keys(lastOpenState).length>0)notify('🆕 Yeni',t+' açıldı')}}
for(const t of Object.keys(lastOpenState)){{if(!openPositions[t]){{const c=closedPositions.find(x=>x.ticker===t);if(c)notify(c.kar>0?'✓ KAR':'✗ ZARAR',t+': '+c.sonuc+' '+c.kar.toFixed(1)+'$')}}}}
lastOpenState=JSON.parse(JSON.stringify(openPositions))}}

function notify(title,body){{if('Notification'in window&&Notification.permission==='granted')try{{new Notification(title,{{body}})}}catch(e){{}}}}

function setupSort(){{document.querySelectorAll('#openTable th[data-sort]').forEach(th=>{{th.onclick=()=>{{const c=th.dataset.sort;if(sortState.open.col===c)sortState.open.dir=sortState.open.dir==='asc'?'desc':'asc';else{{sortState.open.col=c;sortState.open.dir='desc'}}renderOpen()}}}});document.querySelectorAll('#closedTable th[data-sort]').forEach(th=>{{th.onclick=()=>{{const c=th.dataset.sort;if(sortState.closed.col===c)sortState.closed.dir=sortState.closed.dir==='asc'?'desc':'asc';else{{sortState.closed.col=c;sortState.closed.dir='desc'}}renderClosed()}}}})}}
function updateSortArrows(w){{const tid=w==='open'?'openTable':'closedTable',s=sortState[w];document.querySelectorAll('#'+tid+' th').forEach(th=>{{th.classList.remove('sorted-asc','sorted-desc');if(th.dataset.sort===s.col)th.classList.add('sorted-'+s.dir)}})}}

function exportCSV(type){{let rows,fn;if(type==='open'){{rows=Object.entries(openPositions).map(([t,p])=>({{Coin:t,Marjin:p.marj,Lev:p.lev,Giris:p.giris,Stop:p.stop,TP1:p.tp1,TP2:p.tp2,HH:p.hh_pct||0,ATR:p.atr_skor||1.0,StopVerified:p.binance_stop_verified?'YES':'NO',Zaman:p.zaman_full||''}}));fn='acik_'+now_tr().slice(0,10)+'.csv'}}else{{rows=closedPositions.map(c=>({{Coin:c.ticker,Marjin:c.marj,Giris:c.giris,Sonuc:c.sonuc,Kar:c.kar,BinancePnL:c.binance_pnl||'',HH:c.hh_pct||0,Acilis:c.acilis,Kapanis:c.kapanis}}));fn='kapanan_'+now_tr().slice(0,10)+'.csv'}}if(!rows.length){{toast('Veri yok',true);return}}const h=Object.keys(rows[0]),csv=[h.join(','),...rows.map(r=>h.map(k=>r[k]).join(','))].join('\\n');const b=new Blob(['\\ufeff'+csv],{{type:'text/csv'}});const a=document.createElement('a');a.href=URL.createObjectURL(b);a.download=fn;a.click();toast('✓ '+fn)}}

async function init(){{setupSort();await loadData();lastOpenState=JSON.parse(JSON.stringify(openPositions));
if(Object.keys(openPositions).length>0||skippedSignals.length>0){{updatePrices()}}else{{setInterval(async()=>{{await loadData();document.getElementById('lastUpdate').textContent=new Date().toTimeString().slice(0,8)}},10000)}}
setTimeout(()=>location.reload(),20000)}}
init();
</script>
</body></html>"""
    return html
