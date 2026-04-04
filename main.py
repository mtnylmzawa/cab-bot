from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from binance.um_futures import UMFutures
import os
import httpx
from datetime import datetime

app = FastAPI()

# ================================================================
# TEST MODU
# True  = Gerçek emir gönderilmez, sadece log'a yazar
# False = Gerçek işlem açar
# ================================================================
TEST_MODE = True

client = UMFutures(
    key=os.environ.get("BINANCE_API_KEY"),
    secret=os.environ.get("BINANCE_SECRET_KEY"),
    base_url="https://fapi.binance.com"
)

MAX_POSITIONS = int(os.environ.get("MAX_POSITIONS", "3"))

# Açık pozisyonları hafızada tut
open_positions = {}  # ticker -> {giris, stop, tp1, tp2, marj, lev, risk, zaman, durum}

def get_open_positions_binance():
    if TEST_MODE:
        return []
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
            risk   = float(message.split("Risk:")[1].split("$")[0])
            return {"type": "GIRIS", "ticker": ticker, "giris": giris,
                    "stop": stop, "tp1": tp1, "tp2": tp2, "marj": marj, "lev": lev, "risk": risk}
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

async def get_price(symbol):
    try:
        async with httpx.AsyncClient() as c:
            r = await c.get(f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={symbol}", timeout=3)
            return float(r.json()["price"])
    except:
        return None

@app.get("/")
def health():
    mode = "TEST MODU" if TEST_MODE else "CANLI"
    return {"status": f"CAB Bot calisiyor — {mode}", "acik_poz": len(open_positions)}

@app.get("/ip")
async def get_ip():
    async with httpx.AsyncClient() as c:
        r = await c.get("https://api.ipify.org?format=json")
        return r.json()

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    mode_badge = "🟡 TEST MODU" if TEST_MODE else "🟢 CANLI"
    
    rows = ""
    for ticker, pos in sorted(open_positions.items()):
        symbol = get_symbol(ticker)
        price = await get_price(symbol)
        
        giris = pos["giris"]
        stop  = pos["stop"]
        tp1   = pos["tp1"]
        tp2   = pos["tp2"]
        durum = pos.get("durum", "Aktif")
        
        if price:
            pnl_pct = (price - giris) / giris * 100
            tp1_uzak = (tp1 - price) / price * 100
            stop_uzak = (price - stop) / price * 100
            
            if price >= tp1:
                renk = "#4ade80"
                durum_txt = "✓ TP1 Üstünde"
            elif price >= giris:
                renk = "#86efac"
                durum_txt = f"▲ +{pnl_pct:.2f}%"
            elif price > stop:
                renk = "#fbbf24"
                durum_txt = f"▼ {pnl_pct:.2f}%"
            else:
                renk = "#f87171"
                durum_txt = "⚠ STOP ALTI"
            
            price_str = f"{price:.6f}"
            tp1_str = f"%{tp1_uzak:.2f} uzak"
            stop_str = f"%{stop_uzak:.2f} tampon"
        else:
            renk = "#94a3b8"
            durum_txt = "?"
            price_str = "—"
            tp1_str = "—"
            stop_str = "—"

        rows += f"""
        <tr>
            <td><b>{ticker}</b></td>
            <td>{pos['marj']:.0f}$</td>
            <td>{giris:.6f}</td>
            <td style="color:{renk};font-weight:bold">{price_str}</td>
            <td>{stop:.6f}</td>
            <td style="color:#4ade80">{tp1:.6f}</td>
            <td style="color:#2dd4bf">{tp2:.6f}</td>
            <td style="color:{renk};font-weight:bold">{durum_txt}</td>
            <td style="color:#94a3b8;font-size:11px">{pos.get('zaman','')}</td>
        </tr>"""

    if not rows:
        rows = '<tr><td colspan="9" style="text-align:center;color:#94a3b8;padding:20px">Açık pozisyon yok</td></tr>'

    html = f"""<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="30">
<title>CAB Bot Dashboard</title>
<style>
  body {{ background:#0f0f1a; color:#eee; font-family:monospace; padding:16px; margin:0; }}
  h2 {{ color:#a78bfa; margin-bottom:4px; font-size:16px; }}
  .badge {{ display:inline-block; padding:3px 10px; border-radius:12px; font-size:12px; 
            background:#1e1b4b; margin-bottom:12px; }}
  .info {{ color:#94a3b8; font-size:11px; margin-bottom:12px; }}
  table {{ border-collapse:collapse; width:100%; font-size:12px; }}
  th {{ background:#1e1b4b; color:#a78bfa; padding:8px 6px; text-align:left; 
        border-bottom:1px solid #3730a3; white-space:nowrap; }}
  td {{ padding:7px 6px; border-bottom:1px solid #1e1e2e; white-space:nowrap; }}
  tr:hover {{ background:#1a1a2e; }}
  .stat {{ display:inline-block; background:#1e1b4b; border-radius:8px; 
           padding:8px 14px; margin:4px; font-size:12px; }}
  .stat b {{ color:#a78bfa; font-size:16px; display:block; }}
</style>
</head>
<body>
<h2>🤖 CAB Bot Dashboard</h2>
<div class="badge">{mode_badge}</div>
<div class="info">⟳ 30 saniyede bir otomatik yenilenir | {datetime.utcnow().strftime('%H:%M:%S')} UTC</div>

<div style="margin-bottom:14px">
  <div class="stat"><b>{len(open_positions)}</b>Açık Pozisyon</div>
  <div class="stat"><b>{MAX_POSITIONS}</b>Max Pozisyon</div>
  <div class="stat"><b>{'TEST' if TEST_MODE else 'CANLI'}</b>Mod</div>
</div>

<table>
  <tr>
    <th>Coin</th>
    <th>Marjin</th>
    <th>Giriş</th>
    <th>Şu An</th>
    <th>Stop</th>
    <th>TP1</th>
    <th>TP2</th>
    <th>Durum</th>
    <th>Zaman</th>
  </tr>
  {rows}
</table>
</body>
</html>"""
    return html

@app.post("/webhook")
async def webhook(request: Request):
    body = await request.body()
    message = body.decode("utf-8")
    print(f"[ALERT] {message}")

    parsed = parse_alert(message)
    print(f"[PARSE] {parsed}")

    if parsed["type"] == "GIRIS":
        if len(open_positions) >= MAX_POSITIONS:
            print(f"[ATLA] Max pozisyon doldu ({MAX_POSITIONS})")
            return {"status": "ATLANDI", "sebep": f"Max {MAX_POSITIONS} pozisyon doldu"}

        ticker = parsed["ticker"]
        zaman  = datetime.utcnow().strftime("%H:%M")
        open_positions[ticker] = {
            "giris": parsed["giris"], "stop": parsed["stop"],
            "tp1": parsed["tp1"], "tp2": parsed["tp2"],
            "marj": parsed["marj"], "lev": parsed["lev"],
            "risk": parsed["risk"], "zaman": zaman, "durum": "Aktif"
        }

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
        ticker = parsed["ticker"]
        if ticker in open_positions:
            open_positions[ticker]["stop"]  = parsed["stop"]
            open_positions[ticker]["durum"] = "TP1 Alındı"
        symbol = get_symbol(ticker)

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

    elif parsed["type"] == "TP2":
        ticker = parsed["ticker"]
        if ticker in open_positions:
            open_positions[ticker]["durum"] = "TP2 Alındı"
        return {"status": "TP2 kaydedildi"}

    elif parsed["type"] in ["TRAIL", "STOP"]:
        ticker = parsed["ticker"]
        symbol = get_symbol(ticker)
        if ticker in open_positions:
            del open_positions[ticker]

        if TEST_MODE:
            print(f"[TEST] KAPAT: {symbol} | Tip: {parsed['type']}")
            return {"status": "TEST", "islem": "Pozisyon kapatildi", "symbol": symbol}

        client.cancel_open_orders(symbol=symbol)
        pos = [p for p in get_open_positions_binance() if p["symbol"] == symbol]
        if pos:
            amt = abs(float(pos[0]["positionAmt"]))
            if amt > 0:
                client.new_order(symbol=symbol, side="SELL", type="MARKET", quantity=amt)
        return {"status": "Pozisyon kapatildi"}

    return {"status": "Islem yapilmadi", "parsed": parsed}
