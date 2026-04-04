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

# Açık pozisyonlar
open_positions = {}

# Kapanan pozisyonlar (tarihçe)
closed_positions = []

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
            tp_type = "TP2" if "TP2 trailing" in message else "TP1"
            return {"type": "TRAIL", "ticker": ticker, "kalan": int(kalan), "tp_type": tp_type}
        elif "CAB v13 STOP |" in message:
            ticker = message.split("CAB v13 STOP | ")[1].split(" |")[0].strip()
            kalan  = "100"
            if "Kalan %" in message:
                kalan = message.split("Kalan %")[1].split()[0]
            stop_type = "BE" if "BE stop" in message else ("TP2" if "TP2 sonrasi" in message else "TAM")
            return {"type": "STOP", "ticker": ticker, "kalan": int(kalan), "stop_type": stop_type}
    except Exception as e:
        return {"type": "ERROR", "msg": str(e)}
    return {"type": "UNKNOWN"}

def get_symbol(ticker):
    return ticker + "USDT" if not ticker.endswith("USDT") else ticker

def close_position(ticker, sonuc, kar):
    """Pozisyonu kapat ve tarihçeye ekle"""
    if ticker in open_positions:
        pos = open_positions[ticker]
        closed_positions.append({
            "ticker": ticker,
            "marj": pos["marj"],
            "giris": pos["giris"],
            "tp1": pos["tp1"],
            "tp2": pos["tp2"],
            "stop": pos["stop"],
            "sonuc": sonuc,
            "kar": kar,
            "zaman_acilis": pos.get("zaman", ""),
            "zaman_kapanis": datetime.utcnow().strftime("%H:%M"),
        })
        del open_positions[ticker]

@app.get("/")
def health():
    mode = "TEST MODU" if TEST_MODE else "CANLI"
    return {"status": f"CAB Bot calisiyor — {mode}", "acik": len(open_positions), "kapanan": len(closed_positions)}

@app.get("/ip")
async def get_ip():
    async with httpx.AsyncClient() as c:
        r = await c.get("https://api.ipify.org?format=json")
        return r.json()

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    mode_badge = "🟡 TEST MODU" if TEST_MODE else "🟢 CANLI"

    # Açık pozisyonlar tablosu
    acik_rows = ""
    for ticker, pos in sorted(open_positions.items()):
        symbol = get_symbol(ticker)
        giris = pos["giris"]
        stop  = pos["stop"]
        tp1   = pos["tp1"]
        tp2   = pos["tp2"]
        durum = pos.get("durum", "Aktif")

        acik_rows += f"""
        <tr>
            <td><b>{ticker}</b></td>
            <td>{pos['marj']:.0f}$</td>
            <td>{giris:.6f}</td>
            <td id="price-{symbol}" style="color:#94a3b8">...</td>
            <td>{stop:.6f}</td>
            <td style="color:#4ade80">{tp1:.6f}</td>
            <td style="color:#2dd4bf">{tp2:.6f}</td>
            <td id="status-{symbol}" style="color:#94a3b8">{durum}</td>
            <td style="color:#94a3b8;font-size:11px">{pos.get('zaman','')}</td>
        </tr>"""

    if not acik_rows:
        acik_rows = '<tr><td colspan="9" style="text-align:center;color:#94a3b8;padding:16px">Açık pozisyon yok</td></tr>'

    # Kapanan pozisyonlar tablosu
    kapanan_rows = ""
    toplam_kar = 0
    for pos in reversed(closed_positions):
        kar = pos.get("kar", 0)
        toplam_kar += kar
        kar_renk = "#4ade80" if kar > 0 else "#f87171" if kar < 0 else "#94a3b8"
        kar_str = f"+{kar:.1f}$" if kar > 0 else f"{kar:.1f}$" if kar < 0 else "±0$"

        sonuc_renk = {
            "≈ TP1+Trail": "#fb923c",
            "★ TP1+TP2+Trail": "#c084fc",
            "~ TP1+BE Stop": "#60a5fa",
            "✗ Stop": "#f87171",
            "↓ TP2+Stop": "#f472b6",
        }.get(pos["sonuc"], "#94a3b8")

        kapanan_rows += f"""
        <tr>
            <td><b>{pos['ticker']}</b></td>
            <td>{pos['marj']:.0f}$</td>
            <td>{pos['giris']:.6f}</td>
            <td style="color:{sonuc_renk};font-weight:bold">{pos['sonuc']}</td>
            <td style="color:{kar_renk};font-weight:bold">{kar_str}</td>
            <td style="color:#94a3b8;font-size:11px">{pos.get('zaman_acilis','')} → {pos.get('zaman_kapanis','')}</td>
        </tr>"""

    if not kapanan_rows:
        kapanan_rows = '<tr><td colspan="6" style="text-align:center;color:#94a3b8;padding:16px">Henüz kapanan pozisyon yok</td></tr>'

    net_renk = "#4ade80" if toplam_kar > 0 else "#f87171" if toplam_kar < 0 else "#94a3b8"
    net_str = f"+{toplam_kar:.1f}$" if toplam_kar > 0 else f"{toplam_kar:.1f}$"

    symbols_json = str([get_symbol(t) for t in open_positions.keys()])
    positions_json = "{"
    for t, p in open_positions.items():
        sym = get_symbol(t)
        positions_json += f'"{sym}":{{"giris":{p["giris"]},"stop":{p["stop"]},"tp1":{p["tp1"]}}},'
    positions_json = positions_json.rstrip(",") + "}"

    html = f"""<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CAB Bot Dashboard</title>
<style>
  body {{ background:#0f0f1a; color:#eee; font-family:monospace; padding:16px; margin:0; }}
  h2 {{ color:#a78bfa; margin-bottom:4px; font-size:16px; }}
  h3 {{ color:#60a5fa; font-size:13px; margin:16px 0 6px 0; }}
  .badge {{ display:inline-block; padding:3px 10px; border-radius:12px; font-size:12px; background:#1e1b4b; margin-bottom:8px; }}
  .info {{ color:#94a3b8; font-size:11px; margin-bottom:12px; }}
  table {{ border-collapse:collapse; width:100%; font-size:12px; margin-bottom:16px; }}
  th {{ background:#1e1b4b; color:#a78bfa; padding:8px 6px; text-align:left; border-bottom:1px solid #3730a3; white-space:nowrap; }}
  td {{ padding:7px 6px; border-bottom:1px solid #1e1e2e; white-space:nowrap; }}
  tr:hover {{ background:#1a1a2e; }}
  .stat {{ display:inline-block; background:#1e1b4b; border-radius:8px; padding:8px 14px; margin:4px; font-size:12px; }}
  .stat b {{ color:#a78bfa; font-size:16px; display:block; }}
</style>
</head>
<body>
<h2>🤖 CAB Bot Dashboard</h2>
<div class="badge">{mode_badge}</div>
<div class="info" id="timer">⟳ Fiyatlar yükleniyor...</div>

<div style="margin-bottom:14px">
  <div class="stat"><b>{len(open_positions)}</b>Açık</div>
  <div class="stat"><b>{len(closed_positions)}</b>Kapanan</div>
  <div class="stat"><b style="color:{net_renk}">{net_str}</b>Net Kar</div>
  <div class="stat"><b>{"TEST" if TEST_MODE else "CANLI"}</b>Mod</div>
</div>

<h3>📊 Açık Pozisyonlar</h3>
<table>
  <tr>
    <th>Coin</th><th>Marjin</th><th>Giriş</th><th>Şu An</th>
    <th>Stop</th><th>TP1</th><th>TP2</th><th>Durum</th><th>Zaman</th>
  </tr>
  {acik_rows}
</table>

<h3>📋 Kapanan Pozisyonlar</h3>
<table>
  <tr>
    <th>Coin</th><th>Marjin</th><th>Giriş</th><th>Sonuç</th><th>Kar/Zarar</th><th>Zaman</th>
  </tr>
  {kapanan_rows}
</table>

<script>
var symbols = {symbols_json};
var positions = {positions_json};

async function fetchPrice(symbol) {{
  try {{
    var r = await fetch("https://fapi.binance.com/fapi/v1/ticker/price?symbol=" + symbol);
    var d = await r.json();
    return parseFloat(d.price);
  }} catch(e) {{
    try {{
      var r2 = await fetch("https://api.binance.com/api/v3/ticker/price?symbol=" + symbol);
      var d2 = await r2.json();
      return parseFloat(d2.price);
    }} catch(e2) {{ return null; }}
  }}
}}

async function updatePrices() {{
  for (var i = 0; i < symbols.length; i++) {{
    var sym = symbols[i];
    var price = await fetchPrice(sym);
    var priceEl = document.getElementById("price-" + sym);
    var statusEl = document.getElementById("status-" + sym);
    if (!priceEl || !price) continue;

    var pos = positions[sym];
    var giris = pos.giris;
    var stop = pos.stop;
    var tp1 = pos.tp1;
    var pnl = ((price - giris) / giris * 100).toFixed(2);

    priceEl.textContent = price.toFixed(6);

    if (price >= tp1) {{
      priceEl.style.color = "#4ade80";
      if (statusEl.textContent === "Aktif") {{ statusEl.textContent = "✓ TP1 Üstünde"; statusEl.style.color = "#4ade80"; }}
    }} else if (price >= giris) {{
      priceEl.style.color = "#86efac";
      if (statusEl.textContent === "Aktif") {{ statusEl.textContent = "▲ +" + pnl + "%"; statusEl.style.color = "#86efac"; }}
    }} else if (price > stop) {{
      priceEl.style.color = "#fbbf24";
      if (statusEl.textContent === "Aktif") {{ statusEl.textContent = "▼ " + pnl + "%"; statusEl.style.color = "#fbbf24"; }}
    }} else {{
      priceEl.style.color = "#f87171";
      statusEl.textContent = "⚠ STOP ALTI";
      statusEl.style.color = "#f87171";
    }}
  }}
  var now = new Date();
  document.getElementById("timer").textContent = "⟳ Son güncelleme: " + now.toLocaleTimeString();
  setTimeout(updatePrices, 15000);
}}

updatePrices();
</script>
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
            return {"status": "ATLANDI"}

        ticker = parsed["ticker"]
        zaman  = datetime.utcnow().strftime("%H:%M")
        open_positions[ticker] = {
            "giris": parsed["giris"], "stop": parsed["stop"],
            "tp1": parsed["tp1"], "tp2": parsed["tp2"],
            "marj": parsed["marj"], "lev": parsed["lev"],
            "risk": parsed["risk"], "zaman": zaman, "durum": "Aktif",
            "tp1_hit": False, "tp2_hit": False
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
            return {"status": "TEST", "symbol": symbol}

        client.change_leverage(symbol=symbol, leverage=lev)
        try:
            client.change_margin_type(symbol=symbol, marginType="ISOLATED")
        except:
            pass
        order = client.new_order(symbol=symbol, side="BUY", type="MARKET", quantity=pos_size)
        client.new_order(symbol=symbol, side="SELL", type="STOP_MARKET", stopPrice=round(stop, 6), closePosition=True)
        client.new_order(symbol=symbol, side="SELL", type="TAKE_PROFIT_MARKET", stopPrice=round(tp1, 6), quantity=round(pos_size * 0.60, 3))
        client.new_order(symbol=symbol, side="SELL", type="TAKE_PROFIT_MARKET", stopPrice=round(tp2, 6), quantity=round(pos_size * 0.25, 3))
        return {"status": "OK", "symbol": symbol, "order": order["orderId"]}

    elif parsed["type"] == "TP1":
        ticker = parsed["ticker"]
        if ticker in open_positions:
            open_positions[ticker]["stop"]    = parsed["stop"]
            open_positions[ticker]["durum"]   = "✓ TP1 Alındı — BE'de"
            open_positions[ticker]["tp1_hit"] = True
        symbol = get_symbol(ticker)
        if TEST_MODE:
            print(f"[TEST] TP1: {symbol} | Stop BE: {parsed['stop']}")
            return {"status": "TEST"}
        orders = client.get_orders(symbol=symbol)
        for o in orders:
            if o["type"] == "STOP_MARKET":
                client.cancel_order(symbol=symbol, orderId=o["orderId"])
        client.new_order(symbol=symbol, side="SELL", type="STOP_MARKET", stopPrice=round(parsed["stop"], 6), closePosition=True)
        return {"status": "TP1 stop guncellendi"}

    elif parsed["type"] == "TP2":
        ticker = parsed["ticker"]
        if ticker in open_positions:
            open_positions[ticker]["durum"]   = "✓✓ TP2 Alındı"
            open_positions[ticker]["tp2_hit"] = True
        return {"status": "TP2 kaydedildi"}

    elif parsed["type"] == "TRAIL":
        ticker   = parsed["ticker"]
        tp_type  = parsed.get("tp_type", "TP1")
        tp1_hit  = open_positions.get(ticker, {}).get("tp1_hit", False)
        tp2_hit  = open_positions.get(ticker, {}).get("tp2_hit", False)
        marj     = open_positions.get(ticker, {}).get("marj", 0)
        giris    = open_positions.get(ticker, {}).get("giris", 0)
        tp1      = open_positions.get(ticker, {}).get("tp1", 0)
        tp2      = open_positions.get(ticker, {}).get("tp2", 0)

        if tp2_hit:
            sonuc = "★ TP1+TP2+Trail"
            pos_size = marj * 10 / giris if giris > 0 else 0
            kar = round(pos_size * giris * 0.60 * (tp1-giris)/giris + pos_size * giris * 0.25 * (tp2-giris)/giris, 1) if giris > 0 else 0
        else:
            sonuc = "≈ TP1+Trail"
            pos_size = marj * 10 / giris if giris > 0 else 0
            kar = round(pos_size * giris * 0.60 * (tp1-giris)/giris, 1) if giris > 0 else 0

        close_position(ticker, sonuc, kar)
        symbol = get_symbol(ticker)
        if TEST_MODE:
            print(f"[TEST] TRAIL KAPAT: {symbol} | {sonuc} | Kar: +{kar}$")
            return {"status": "TEST"}
        client.cancel_open_orders(symbol=symbol)
        pos = [p for p in get_open_positions_binance() if p["symbol"] == symbol]
        if pos:
            amt = abs(float(pos[0]["positionAmt"]))
            if amt > 0:
                client.new_order(symbol=symbol, side="SELL", type="MARKET", quantity=amt)
        return {"status": "Trail kapandi"}

    elif parsed["type"] == "STOP":
        ticker    = parsed["ticker"]
        stop_type = parsed.get("stop_type", "TAM")
        tp1_hit   = open_positions.get(ticker, {}).get("tp1_hit", False)
        tp2_hit   = open_positions.get(ticker, {}).get("tp2_hit", False)
        risk      = open_positions.get(ticker, {}).get("risk", 0)
        marj      = open_positions.get(ticker, {}).get("marj", 0)
        giris     = open_positions.get(ticker, {}).get("giris", 0)
        tp1       = open_positions.get(ticker, {}).get("tp1", 0)

        if stop_type == "BE":
            sonuc = "~ TP1+BE Stop"
            pos_size = marj * 10 / giris if giris > 0 else 0
            kar = round(pos_size * giris * 0.60 * (tp1-giris)/giris, 1) if giris > 0 else 0
        elif stop_type == "TP2":
            sonuc = "↓ TP2+Stop"
            kar = 0
        else:
            sonuc = "✗ Stop"
            kar = -risk

        close_position(ticker, sonuc, kar)
        symbol = get_symbol(ticker)
        if TEST_MODE:
            print(f"[TEST] STOP KAPAT: {symbol} | {sonuc} | Kar: {kar}$")
            return {"status": "TEST"}
        client.cancel_open_orders(symbol=symbol)
        pos = [p for p in get_open_positions_binance() if p["symbol"] == symbol]
        if pos:
            amt = abs(float(pos[0]["positionAmt"]))
            if amt > 0:
                client.new_order(symbol=symbol, side="SELL", type="MARKET", quantity=amt)
        return {"status": "Stop kapandi"}

    return {"status": "Islem yapilmadi", "parsed": parsed}
