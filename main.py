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
TIMEOUT_HOURS = 12  # pozisyon timeout süresi
TIMEOUT_CHECK_INTERVAL_SEC = 300  # her 5 dakika

client = UMFutures(
    key=os.environ.get("BINANCE_API_KEY"),
    secret=os.environ.get("BINANCE_SECRET_KEY"),
    base_url="https://fapi.binance.com"
)

INITIAL_DATA = {"open_positions": {}, "closed_positions": [], "skipped_signals": []}

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

# v6.1: Skipped_signals key'i eski verilerde olmayabilir, garanti et
if "skipped_signals" not in data:
    data["skipped_signals"] = []
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
    """v6.2: closePosition=true kullanarak lot rounding kalıntısı bırakmadan TAM kapatma"""
    qty = binance_get_position_qty(symbol)
    if qty <= 0:
        return {"success": True, "msg": "no position"}

    binance_cancel_all(symbol)

    try:
        # closePosition=true → quantity yoksayılır, Binance pozisyonun TAMAMINI kapatır
        # Lot rounding kalıntısı bırakmaz, "Partially Closed" anomalisi olmaz
        result = client.new_order(
            symbol=symbol, side="SELL", type="MARKET",
            closePosition="true"
        )
        filled_qty = float(result.get("executedQty", 0))
        avg_price = float(result.get("avgPrice", 0))
        if avg_price == 0 and filled_qty > 0:
            cum_quote = float(result.get("cumQuote", result.get("cumQty", 0)))
            if cum_quote > 0:
                avg_price = cum_quote / filled_qty
        print(f"[BINANCE] CLOSE_POSITION {symbol} qty:{filled_qty} avgPx:{avg_price} ✓ (full close, no remainder)")
        return {"success": True, "avg_price": avg_price, "filled_qty": filled_qty}
    except ClientError as e:
        # Fallback: closePosition başarısız olursa eski yöntemle dene
        print(f"[BINANCE WARN] closePosition fail {symbol}: {e}, fallback market sell")
        info = get_symbol_info(symbol)
        qty = round_qty(qty, info)
        if qty > 0:
            return binance_market_sell(symbol, qty)
        return {"success": False, "error": str(e)}

def binance_get_mark_price(symbol):
    """v6.1: Sembol için mark price çek (timeout kontrolü için)"""
    try:
        result = client.mark_price(symbol=symbol)
        return float(result.get("markPrice", 0))
    except Exception as e:
        print(f"[BINANCE ERR] Mark price {symbol}: {e}")
        return None

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
    return f"<h3>🤖 CAB Bot v6.2 çalışıyor</h3><p>{mode}</p><p>MAX_POSITIONS: {MAX_POSITIONS} | TIMEOUT: {TIMEOUT_HOURS}s</p><p><a href='/dashboard'>Dashboard</a> | <a href='/test_binance'>Binance Test</a> | <a href='/api/timeout_check'>Manuel Timeout Check</a></p>"

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
    results["max_positions"] = MAX_POSITIONS
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

@app.get("/api/timeout_check")
async def manual_timeout_check():
    """v6.1: Manuel timeout taraması — debugging için"""
    result = await timeout_scan_once()
    return JSONResponse(result)

# ============ WEBHOOK ============
@app.post("/webhook")
async def webhook(req: Request):
    msg = (await req.body()).decode()
    print(f"[ALERT] {msg}")
    mode_tag = "[CANLI]" if not TEST_MODE else "[TEST]"

    # === GIRIS ===
    if msg.startswith("CAB v13 |"):
        parsed = parse_giris(msg)
        if not parsed:
            return {"status": "parse_error"}
        print(f"[PARSE] {parsed}")
        ticker = parsed["ticker"]

        # v6.1: Akıllı slot — TP1 vurmuş VE timeout-BE'liler exempt
        aktif_risk, gar_tp1, gar_to = count_active_risk()
        garantili = gar_tp1 + gar_to

        if aktif_risk >= MAX_POSITIONS:
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
                "sebep": f"Max {MAX_POSITIONS} aktif risk dolu (+{garantili} garantili)"
            })
            if len(data["skipped_signals"]) > 50:
                data["skipped_signals"] = data["skipped_signals"][-50:]
            save_data(data)
            print(f"[LIMIT] Aktif risk {aktif_risk}/{MAX_POSITIONS} (+{gar_tp1} TP1 +{gar_to} TO-BE) — {ticker} atlandı (kaydedildi)")
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

        closed = {
            "ticker": ticker, "giris": pos["giris"], "marj": pos["marj"], "lev": pos["lev"],
            "sonuc": sonuc, "kar": total_kar,
            "tp1_kar": tp1_kar, "tp2_kar": tp2_kar, "trail_kar": trail_kar,
            "tp1_kar_added": pos.get("tp1_hit", False), "tp2_kar_added": pos.get("tp2_hit", False),
            "hh_pct": pos.get("hh_pct", 0), "atr_skor": pos.get("atr_skor", 1.0),
            "kapat_oran": pos.get("kapat_oran", 60),
            "acilis": pos.get("zaman_full", ""), "kapanis": now_tr(),
        }
        data["closed_positions"].append(closed)
        del data["open_positions"][ticker]
        save_data(data)
        print(f"{mode_tag} TRAIL({parsed['tp_type']}): {ticker} | {sonuc} | +{total_kar}$")
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

        closed = {
            "ticker": ticker, "giris": giris, "marj": pos["marj"], "lev": pos["lev"],
            "sonuc": sonuc, "kar": total_kar,
            "tp1_kar": tp1_kar, "tp2_kar": tp2_kar, "trail_kar": round(stop_kar, 2),
            "tp1_kar_added": pos.get("tp1_hit", False), "tp2_kar_added": pos.get("tp2_hit", False),
            "hh_pct": pos.get("hh_pct", 0), "atr_skor": pos.get("atr_skor", 1.0),
            "kapat_oran": kapat_oran,
            "acilis": pos.get("zaman_full", ""), "kapanis": now_tr(),
        }
        data["closed_positions"].append(closed)
        del data["open_positions"][ticker]
        save_data(data)
        print(f"{mode_tag} STOP: {ticker} | {sonuc} | {total_kar}$")
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

            print(f"[TIMEOUT] {ticker} {age_hours:.1f}s açık (limit:{TIMEOUT_HOURS}s) — akıllı kapama")

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
                closed = {
                    "ticker": ticker, "giris": pos["giris"], "marj": pos["marj"], "lev": pos["lev"],
                    "sonuc": "⏰ Timeout", "kar": round(kar, 2),
                    "tp1_kar": 0, "tp2_kar": 0, "trail_kar": round(kar, 2),
                    "tp1_kar_added": False, "tp2_kar_added": False,
                    "hh_pct": pos.get("hh_pct", 0), "atr_skor": pos.get("atr_skor", 1.0),
                    "kapat_oran": pos.get("kapat_oran", 60),
                    "acilis": pos.get("zaman_full", ""), "kapanis": now_tr(),
                }
                data["closed_positions"].append(closed)
                del data["open_positions"][ticker]
                save_data(data)
                actioned.append({"ticker": ticker, "action": "CLOSE", "kar": kar, "age_hours": round(age_hours, 1)})
                print(f"[TIMEOUT-CLOSE] {ticker} → kapatıldı ({kar:+.1f}$)")

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


@app.on_event("startup")
async def startup():
    asyncio.create_task(check_timeouts())
    print(f"[BOOT] CAB Bot v6.2 | Mode:{'CANLI' if not TEST_MODE else 'TEST'} | MaxPos:{MAX_POSITIONS} | Timeout:{TIMEOUT_HOURS}s | Check:{TIMEOUT_CHECK_INTERVAL_SEC}s")


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
<title>CAB Bot v6.2 Dashboard</title>
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

<h1>🤖 CAB Bot v6.2 Dashboard</h1>
<div>
  <span class="badge">{mod_badge}</span>
  <small style="color:#9ca3af">MAX:{MAX_POSITIONS} aktif | Timeout:{TIMEOUT_HOURS}s (akıllı: kâr→BE, zarar→kapat)</small>
  <button class="btn btn-warn" style="margin-left:8px;padding:3px 8px;font-size:10px" onclick="requestNotif()">🔔 Bildirim İzni</button>
  <button class="btn" style="padding:3px 8px;font-size:10px" onclick="manualTimeoutCheck()">⏰ Timeout Kontrol</button>
</div>
<div class="subtitle">⟳ Son güncelleme: <span id="lastUpdate">—</span> <small>(5sn fiyat / 20sn sayfa)</small></div>

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

<!-- KAÇIRILAN SİNYALLER -->
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
let sortState={{open:{{col:null,dir:'asc'}},closed:{{col:'kapanis',dir:'desc'}}}};

async function loadData(){{try{{const r=await fetch('/api/data');const d=await r.json();openPositions=d.open_positions||{{}};closedPositions=d.closed_positions||[];skippedSignals=d.skipped_signals||[];renderAll();detectChanges()}}catch(e){{console.error(e)}}}}

async function fp(sym){{try{{const r=await fetch('https://fapi.binance.com/fapi/v1/ticker/price?symbol='+sym);if(!r.ok)return null;return parseFloat((await r.json()).price)}}catch(e){{return null}}}}

let skippedUpdateCounter=0;
async function updatePrices(){{for(const sym of Object.keys(openPositions)){{const px=await fp(sym);if(px===null)continue;openPrices[sym]=px;try{{await fetch('/update_hh',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{ticker:sym,price:px}})}})}}catch(e){{}}}}renderOpen();
if(skippedUpdateCounter===0||skippedUpdateCounter%6===0){{if(skippedSignals.length>0)updateSkippedPrices()}}
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

function renderAll(){{renderStats();renderOpen();renderClosed();renderSkipped();checkWarnings()}}

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

if(px){{
if(px<=stop){{status='stop';statusTxt='✗ Stop Olmuş';statusColor='#f87171';sanalKar=posSize*((stop-giris)/giris)}}
else if(px>=tp2){{status='tp2';statusTxt='★ TP2 Vurmuş!';statusColor='#14b8a6';sanalKar=posSize*0.5*((tp1-giris)/giris)+posSize*0.25*((tp2-giris)/giris)+posSize*0.25*((px-giris)/giris)}}
else if(px>=tp1){{status='tp1';statusTxt='✓ TP1 Vurmuş';statusColor='#84cc16';sanalKar=posSize*0.5*((tp1-giris)/giris)+posSize*0.5*((px-giris)/giris)}}
else{{sanalKar=posSize*((px-giris)/giris);if(sanalKar>0)statusTxt='📈 Kârda';else statusTxt='📉 Zararda'}}
}}
const rr=stop>0?((tp1-giris)/(giris-stop)).toFixed(1):'—';
const tp1kalan=px?((tp1-px)/px*100):null;
return{{...s,px,status,statusTxt,statusColor,sanalKar:Math.round(sanalKar*100)/100,rr,tp1kalan}}}});

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

function renderStats(){{
const tk=closedPositions.reduce((s,c)=>s+c.kar,0),ts=closedPositions.length,ws=closedPositions.filter(c=>c.kar>0).length,wr=ts>0?(ws/ts*100).toFixed(1):0;
const today=now_tr().slice(0,10),bg=closedPositions.filter(c=>c.kapanis.startsWith(today)),bk=bg.reduce((s,c)=>s+c.kar,0),bt=bg.filter(c=>c.kar>0).length,bs=bg.filter(c=>c.kar<=0).length,bw=bg.length>0?(bt/bg.length*100).toFixed(1):0;
const su=closedPositions.map(sureDk),os=su.length>0?su.reduce((a,b)=>a+b,0)/su.length:0,oss=os===0?'—':sureFmt(Math.round(os));
const nr=tk>0?'#4ade80':(tk<0?'#f87171':'#e5e7eb'),wrr=wr>=50?'#4ade80':(ts>0?'#f87171':'#e5e7eb'),bkr=bk>0?'#4ade80':(bk<0?'#f87171':'#e5e7eb'),bwr=bw>=50?'#4ade80':(bg.length>0?'#f87171':'#e5e7eb');
// v6.1: Akıllı slot — TP1 vurmuş VE timeout-BE'liler exempt
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
const tkS=r.tp1kalan!=null?(r.tp1kalan<=0?'<span style="color:#4ade80">✓ Geçildi</span>':`%${{r.tp1kalan.toFixed(2)}} uzak`):'—';
let trS='—';if(p.tp2_hit)trS=`✓ TP2+ (+${{((p.tp1_kar||0)+(p.tp2_kar||0)).toFixed(1)}}$)`;else if(p.tp1_hit)trS=`✓ TP1+ (+${{(p.tp1_kar||0).toFixed(1)}}$)`;else if(p.timeout_be)trS=`⏰ TO-BE (+${{(p.timeout_kar_initial||0).toFixed(1)}}$)`;
let rb='';if(p.tp2_hit)rb='background:rgba(20,184,166,0.15);border-left:3px solid #14b8a6;';else if(p.tp1_hit)rb='background:rgba(132,204,22,0.12);border-left:3px solid #84cc16;';else if(p.timeout_be)rb='background:rgba(249,115,22,0.12);border-left:3px solid #f97316;';
let wc='';try{{const ac=new Date(p.zaman_full.replace(' ','T')+':00+03:00');if((Date.now()-ac.getTime())/3600000>6&&!p.tp1_hit&&!p.timeout_be)wc='warn-row'}}catch(e){{}}
const hd=(p.hh_pct||0)>0?`%${{p.hh_pct.toFixed(2)}}`:'—',ad=`${{(p.atr_skor||1.0).toFixed(2)}}x (${{k}}%)`;
return`<tr class="${{wc}}" style="${{rb}}"><td><a href="javascript:void(0)" onclick="openTV('${{t}}')" style="color:#60a5fa;text-decoration:none;">${{t}}</a> 🔗</td><td>${{p.marj.toFixed(0)}}$ <small>(${{p.lev}}x)</small></td><td>${{p.giris.toFixed(6)}}</td><td style="color:${{pxC}}">${{pxS}}</td><td>${{tkS}}</td><td>${{p.durum&&(p.durum.includes('TP')||p.durum.includes('Timeout'))?p.durum:pcS}}</td><td>${{hd}}</td><td>${{ad}}</td><td>${{trS}}</td><td>${{p.stop.toFixed(6)}} <small style="color:#f87171">(${{sz>=0?'+':''}}${{sz.toFixed(1)}}$)</small></td><td>${{p.tp1.toFixed(6)}} <small style="color:#4ade80">(+${{t1k.toFixed(1)}}$)</small></td><td>${{p.tp2.toFixed(6)}} <small style="color:#4ade80">(+${{t2k.toFixed(1)}}$)</small></td><td>${{p.zaman||''}}</td></tr>`}}).join('')}}
document.getElementById('openCount').textContent=`(${{rows.length}})`;updateSortArrows('open')}}

function renderClosed(){{
const df=document.getElementById('filterDate').value,rf=document.getElementById('filterResult').value,search=document.getElementById('searchClosed').value.toLowerCase();
let rows=closedPositions.filter(c=>dateMatches(c.kapanis,df)&&resultMatches(c.sonuc,rf)&&c.ticker.toLowerCase().includes(search)).map(c=>({{...c,sure_dk:sureDk(c)}}));
const s=sortState.closed;if(s.col)rows.sort((a,b)=>{{let av=a[s.col],bv=b[s.col];if(av==null)av=-Infinity;if(bv==null)bv=-Infinity;return typeof av==='string'?s.dir==='asc'?av.localeCompare(bv):bv.localeCompare(av):s.dir==='asc'?av-bv:bv-av}});
const body=document.getElementById('closedBody');
if(!rows.length){{body.innerHTML='<tr><td colspan="9" style="text-align:center;color:#9ca3af">Veri yok</td></tr>'}}
else{{body.innerHTML=rows.map(c=>{{const rk=c.kar>0?'#4ade80':'#f87171',ks=(c.kar>=0?'+':'')+c.kar.toFixed(1)+'$',hd=(c.hh_pct||0)>0?`%${{c.hh_pct.toFixed(2)}}`:'—',ad=`${{(c.atr_skor||1.0).toFixed(2)}}x/${{c.kapat_oran||60}}%`,su=sureFmt(c.sure_dk),zm=c.acilis.slice(5)+'→'+c.kapanis.slice(11);
let wc='';if(c.sonuc==='✗ Stop'&&(c.hh_pct||0)>=5)wc='warn-row';
let scolor=rk;if(c.sonuc.includes('Timeout'))scolor='#f97316';
return`<tr class="${{wc}}"><td><a href="javascript:void(0)" onclick="openTV('${{c.ticker}}')" style="color:#60a5fa;text-decoration:none;">${{c.ticker}}</a> 🔗</td><td>${{c.marj.toFixed(0)}}$</td><td>${{c.giris.toFixed(6)}}</td><td style="color:${{scolor}}">${{c.sonuc}}</td><td style="color:${{rk}};font-weight:bold">${{ks}}</td><td>${{hd}}</td><td>${{ad}}</td><td>${{su}}</td><td>${{zm}}</td></tr>`}}).join('')}}
const tk=rows.filter(c=>c.kar>0).reduce((s,c)=>s+c.kar,0),tz=rows.filter(c=>c.kar<0).reduce((s,c)=>s+c.kar,0),nt=tk+tz,nc=nt>0?'#4ade80':(nt<0?'#f87171':'#e5e7eb');
document.getElementById('closedOzet').innerHTML=rows.length>0?`<b>${{rows.length}}</b> poz | <span style="color:#4ade80">Kar:+${{tk.toFixed(1)}}$</span> | <span style="color:#f87171">Zarar:${{tz.toFixed(1)}}$</span> | <span style="color:${{nc}}">NET:${{nt>=0?'+':''}}${{nt.toFixed(1)}}$</span>`:'Veri yok';
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

function requestNotif(){{if(!('Notification' in window)){{toast('Bildirim desteklenmiyor',true);return}}Notification.requestPermission().then(p=>{{if(p==='granted'){{toast('✓ Bildirimler açık');new Notification('CAB Bot v6.2',{{body:'TP1/TP2/STOP bildirimlerini alacaksın!'}})}}}})}}

function detectChanges(){{
for(const[t,p]of Object.entries(openPositions)){{const prev=lastOpenState[t];if(prev){{if(p.tp1_hit&&!prev.tp1_hit)notify('🎯 TP1 Vurdu!',t+' TP1 alındı +'+((p.tp1_kar||0).toFixed(1))+'$');if(p.tp2_hit&&!prev.tp2_hit)notify('🎯🎯 TP2!',t+' TP2 alındı!');if(p.timeout_be&&!prev.timeout_be)notify('⏰ Timeout-BE',t+' BE\\'ye çekildi (+'+(p.timeout_kar_initial||0).toFixed(1)+'$)')}}else if(Object.keys(lastOpenState).length>0)notify('🆕 Yeni Pozisyon',t+' açıldı')}}
for(const t of Object.keys(lastOpenState)){{if(!openPositions[t]){{const c=closedPositions.find(x=>x.ticker===t);if(c){{if(c.kar>0)notify('✓ KAR',t+': '+c.sonuc+' +'+c.kar.toFixed(1)+'$');else notify('✗ ZARAR',t+': '+c.sonuc+' '+c.kar.toFixed(1)+'$')}}}}}}
lastOpenState=JSON.parse(JSON.stringify(openPositions))}}

function notify(title,body){{if('Notification'in window&&Notification.permission==='granted')try{{new Notification(title,{{body,icon:'https://www.tradingview.com/favicon.ico'}})}}catch(e){{}}}}

function setupSort(){{document.querySelectorAll('#openTable th[data-sort]').forEach(th=>{{th.onclick=()=>{{const c=th.dataset.sort;if(sortState.open.col===c)sortState.open.dir=sortState.open.dir==='asc'?'desc':'asc';else{{sortState.open.col=c;sortState.open.dir='desc'}}renderOpen()}}}});document.querySelectorAll('#closedTable th[data-sort]').forEach(th=>{{th.onclick=()=>{{const c=th.dataset.sort;if(sortState.closed.col===c)sortState.closed.dir=sortState.closed.dir==='asc'?'desc':'asc';else{{sortState.closed.col=c;sortState.closed.dir='desc'}}renderClosed()}}}})}}
function updateSortArrows(w){{const tid=w==='open'?'openTable':'closedTable',s=sortState[w];document.querySelectorAll('#'+tid+' th').forEach(th=>{{th.classList.remove('sorted-asc','sorted-desc');if(th.dataset.sort===s.col)th.classList.add('sorted-'+s.dir)}})}}

function exportCSV(type){{let rows,fn;if(type==='open'){{rows=Object.entries(openPositions).map(([t,p])=>({{Coin:t,Marjin:p.marj,Lev:p.lev,Giris:p.giris,Stop:p.stop,TP1:p.tp1,TP2:p.tp2,HH:p.hh_pct||0,ATR:p.atr_skor||1.0,Kapat:p.kapat_oran||60,Durum:p.durum||'',Zaman:p.zaman_full||''}}));fn='acik_'+now_tr().slice(0,10)+'.csv'}}else{{rows=closedPositions.map(c=>({{Coin:c.ticker,Marjin:c.marj,Lev:c.lev||10,Giris:c.giris,Sonuc:c.sonuc,Kar:c.kar,HH:c.hh_pct||0,Acilis:c.acilis,Kapanis:c.kapanis}}));fn='kapanan_'+now_tr().slice(0,10)+'.csv'}}if(!rows.length){{toast('Veri yok',true);return}}const h=Object.keys(rows[0]),csv=[h.join(','),...rows.map(r=>h.map(k=>r[k]).join(','))].join('\\n');const b=new Blob(['\\ufeff'+csv],{{type:'text/csv'}});const a=document.createElement('a');a.href=URL.createObjectURL(b);a.download=fn;a.click();toast('✓ '+fn)}}

async function init(){{setupSort();await loadData();lastOpenState=JSON.parse(JSON.stringify(openPositions));
if(Object.keys(openPositions).length>0||skippedSignals.length>0){{updatePrices()}}else{{setInterval(async()=>{{await loadData();document.getElementById('lastUpdate').textContent=new Date().toTimeString().slice(0,8)}},10000)}}
setTimeout(()=>location.reload(),20000)}}
init();
</script>
</body></html>"""
    return html
