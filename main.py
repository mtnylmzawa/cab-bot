from fastapi import FastAPI, Request
from binance.um_futures import UMFutures
import os
import httpx

app = FastAPI()

# ================================================================
# TEST MODU
# True  = Gerçek emir gönderilmez, sadece log'a yazar
# False = Gerçek işlem açar (bot hazır olduğunda False yap)
# ================================================================
TEST_MODE = True

# Binance API bağlantısı
client = UMFutures(
    key=os.environ.get("BINANCE_API_KEY"),
    secret=os.environ.get("BINANCE_SECRET_KEY"),
    base_url="https://fapi.binance.com"
)

MAX_POSITIONS = int(os.environ.get("MAX_POSITIONS", "3"))

def get_open_positions():
    if TEST_MODE:
        return []  # Test modunda Binance'e bağlanma
    positions = client.get_position_risk()
    return [p for p in positions if float(p['positionAmt']) != 0]

def parse_alert(message: str):
    try:
        if "CAB v13 |" in message and "Yon:LONG" in message:
            ticker = message.split("CAB v13 | ")[1].split(" |")[0].strip()
            giris  = float(message.split("Giris:")[1].split()[0])
            stop   = float(message.split("Stop:")[1].split()[0])
            tp1    = float(message.split("TP1:")[1].split()[0])
            tp2    = float(message.split("TP2:")[1].split()[0])
            marj   = float(message.split("Marj:")[1].split("$")[0])
            lev    = int(message.split("Lev:")[1].split("x")[0])
            return {"type": "GIRIS", "ticker": ticker, "giris": giris,
                    "stop": stop, "tp1": tp1, "tp2": tp2, "marj": marj, "lev": lev}

        elif "CAB v13 TP1 |" in message:
            ticker = message.split("CAB v13 TP1 | ")[1].split(" |")[0].strip()
            tp1    = float(message.split("TP1:")[1].split()[0])
            stop   = float(message.split("YeniStop:")[1].strip())
            return {"type": "TP1", "ticker": ticker, "tp1": tp1, "stop": stop}

        elif "CAB v13 TP2 |" in message:
            ticker = message.split("CAB v13 TP2 | ")[1].split(" |")[0].strip()
            return {"type": "TP2", "ticker": ticker}

        elif "CAB v13 TRAIL |" in message:
            ticker = message.split("CAB v13 TRAIL | ")[1].split(" |")[0].strip()
            kalan  = message.split("Kalan %")[1].split()[0]
            return {"type": "TRAIL", "ticker": ticker, "kalan": int(kalan)}

        elif "CAB v13 STOP |" in message:
            ticker = message.split("CAB v13 STOP | ")[1].split(" |")[0].strip()
            kalan  = "100"
            if "Kalan %" in message:
                kalan = message.split("Kalan %")[1].split()[0]
            return {"type": "STOP", "ticker": ticker, "kalan": int(kalan)}

    except Exception as e:
        return {"type": "ERROR", "msg": str(e)}

    return {"type": "UNKNOWN"}

def get_symbol(ticker):
    return ticker + "USDT" if not ticker.endswith("USDT") else ticker

@app.get("/")
def health():
    mode = "TEST MODU" if TEST_MODE else "CANLI"
    return {"status": f"CAB Bot calisiyor — {mode}"}

@app.get("/ip")
async def get_ip():
    async with httpx.AsyncClient() as c:
        r = await c.get("https://api.ipify.org?format=json")
        return r.json()

@app.post("/webhook")
async def webhook(request: Request):
    body = await request.body()
    message = body.decode("utf-8")
    print(f"[ALERT] {message}")

    parsed = parse_alert(message)
    print(f"[PARSE] {parsed}")

    if parsed["type"] == "GIRIS":
        open_pos = get_open_positions()
        if len(open_pos) >= MAX_POSITIONS:
            print(f"[ATLA] Max pozisyon doldu ({MAX_POSITIONS})")
            return {"status": "ATLANDI", "sebep": f"Max {MAX_POSITIONS} pozisyon doldu"}

        ticker   = parsed["ticker"]
        symbol   = get_symbol(ticker)
        marj     = parsed["marj"]
        lev      = parsed["lev"]
        giris    = parsed["giris"]
        stop     = parsed["stop"]
        tp1      = parsed["tp1"]
        tp2      = parsed["tp2"]
        pos_size = round((marj * lev) / giris, 3)

        if TEST_MODE:
            print(f"[TEST] GIRIS: {symbol} | Fiyat:{giris} | Stop:{stop} | TP1:{tp1} | TP2:{tp2} | Lot:{pos_size} | Kaldırac:{lev}x")
            return {"status": "TEST", "symbol": symbol, "lot": pos_size,
                    "giris": giris, "stop": stop, "tp1": tp1, "tp2": tp2}

        # CANLI MOD
        client.change_leverage(symbol=symbol, leverage=lev)
        try:
            client.change_margin_type(symbol=symbol, marginType="ISOLATED")
        except:
            pass

        order = client.new_order(symbol=symbol, side="BUY", type="MARKET", quantity=pos_size)
        client.new_order(symbol=symbol, side="SELL", type="STOP_MARKET",
                        stopPrice=round(stop, 6), closePosition=True)
        tp1_qty = round(pos_size * 0.60, 3)
        client.new_order(symbol=symbol, side="SELL", type="TAKE_PROFIT_MARKET",
                        stopPrice=round(tp1, 6), quantity=tp1_qty)
        tp2_qty = round(pos_size * 0.25, 3)
        client.new_order(symbol=symbol, side="SELL", type="TAKE_PROFIT_MARKET",
                        stopPrice=round(tp2, 6), quantity=tp2_qty)

        return {"status": "OK", "symbol": symbol, "order": order["orderId"]}

    elif parsed["type"] == "TP1":
        symbol = get_symbol(parsed["ticker"])

        if TEST_MODE:
            print(f"[TEST] TP1: {symbol} | Stop BE ye cekiliyor: {parsed['stop']}")
            return {"status": "TEST", "islem": "TP1 stop BE guncellendi", "symbol": symbol}

        orders = client.get_orders(symbol=symbol)
        for o in orders:
            if o["type"] == "STOP_MARKET":
                client.cancel_order(symbol=symbol, orderId=o["orderId"])
        client.new_order(symbol=symbol, side="SELL", type="STOP_MARKET",
                        stopPrice=round(parsed["stop"], 6), closePosition=True)
        return {"status": "TP1 stop guncellendi"}

    elif parsed["type"] in ["TRAIL", "STOP"]:
        symbol = get_symbol(parsed["ticker"])

        if TEST_MODE:
            print(f"[TEST] KAPAT: {symbol} | Tip: {parsed['type']}")
            return {"status": "TEST", "islem": "Pozisyon kapatildi", "symbol": symbol}

        client.cancel_open_orders(symbol=symbol)
        pos = [p for p in get_open_positions() if p["symbol"] == symbol]
        if pos:
            amt = abs(float(pos[0]["positionAmt"]))
            if amt > 0:
                client.new_order(symbol=symbol, side="SELL", type="MARKET", quantity=amt)
        return {"status": "Pozisyon kapatildi"}

    return {"status": "Islem yapilmadi", "parsed": parsed}
