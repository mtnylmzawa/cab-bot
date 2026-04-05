from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from binance.um_futures import UMFutures
import os, json, httpx
from datetime import datetime, timezone

app = FastAPI()

TEST_MODE = True

client = UMFutures(
    key=os.environ.get("BINANCE_API_KEY"),
    secret=os.environ.get("BINANCE_SECRET_KEY"),
    base_url="https://fapi.binance.com"
)

MAX_POSITIONS = int(os.environ.get("MAX_POSITIONS", "3"))

# Kalıcı veri dosyası — Railway volume'da
DATA_FILE = os.environ.get("DATA_FILE", "/data/cab_data.json")

INITIAL_DATA = {"open_positions": {}, "closed_positions": [
    {"ticker":"GRASSUSDT","marj":77,"giris":0.283,"sonuc":"✗ Stop","kar":-22.7,"tarih":"2026-04-04","zaman_acilis":"16:00","zaman_kapanis":"06:18","max_yukselis":0},
    {"ticker":"1INCHUSDT","marj":72,"giris":0.0887,"sonuc":"✗ Stop","kar":-14.7,"tarih":"2026-04-05","zaman_acilis":"00:15","zaman_kapanis":"06:17","max_yukselis":0},
    {"ticker":"ADAUSDT","marj":191,"giris":0.2462,"sonuc":"✗ Stop","kar":-30.1,"tarih":"2026-04-04","zaman_acilis":"15:15","zaman_kapanis":"06:13","max_yukselis":0},
    {"ticker":"SUSHIUSDT","marj":135,"giris":0.1945,"sonuc":"✗ Stop","kar":-27.2,"tarih":"2026-04-04","zaman_acilis":"15:00","zaman_kapanis":"06:10","max_yukselis":0},
    {"ticker":"VANAUSDT","marj":151,"giris":1.222,"sonuc":"✗ Stop","kar":-24.1,"tarih":"2026-04-05","zaman_acilis":"00:30","zaman_kapanis":"06:10","max_yukselis":0},
    {"ticker":"SPXUSDT","marj":129,"giris":0.2652,"sonuc":"✗ Stop","kar":-27.2,"tarih":"2026-04-04","zaman_acilis":"15:15","zaman_kapanis":"06:07","max_yukselis":0},
    {"ticker":"AXSUSDT","marj":128,"giris":1.124,"sonuc":"✗ Stop","kar":-24.4,"tarih":"2026-04-04","zaman_acilis":"21:15","zaman_kapanis":"06:02","max_yukselis":0},
    {"ticker":"HYPERUSDT","marj":140,"giris":0.0866,"sonuc":"✗ Stop","kar":-27.0,"tarih":"2026-04-05","zaman_acilis":"01:15","zaman_kapanis":"05:53","max_yukselis":0},
    {"ticker":"RESOLVUSDT","marj":84,"giris":0.04047,"sonuc":"✗ Stop","kar":-25.5,"tarih":"2026-04-05","zaman_acilis":"02:30","zaman_kapanis":"05:52","max_yukselis":0},
    {"ticker":"BATUSDT","marj":130,"giris":0.0959,"sonuc":"✗ Stop","kar":-24.9,"tarih":"2026-04-05","zaman_acilis":"00:00","zaman_kapanis":"05:52","max_yukselis":0},
    {"ticker":"PNUTUSDT","marj":107,"giris":0.04049,"sonuc":"✗ Stop","kar":-24.2,"tarih":"2026-04-04","zaman_acilis":"15:15","zaman_kapanis":"05:52","max_yukselis":0},
    {"ticker":"ARBUSDT","marj":144,"giris":0.0929,"sonuc":"✗ Stop","kar":-26.1,"tarih":"2026-04-04","zaman_acilis":"15:15","zaman_kapanis":"05:51","max_yukselis":0},
    {"ticker":"SONICUSDT","marj":48,"giris":0.03757,"sonuc":"✗ Stop","kar":-14.6,"tarih":"2026-04-05","zaman_acilis":"00:15","zaman_kapanis":"05:39","max_yukselis":0},
    {"ticker":"KAVAUSDT","marj":113,"giris":0.0525,"sonuc":"✗ Stop","kar":-22.0,"tarih":"2026-04-05","zaman_acilis":"00:15","zaman_kapanis":"05:37","max_yukselis":0},
    {"ticker":"1000XECUSDT","marj":85,"giris":0.00697,"sonuc":"✗ Stop","kar":-20.2,"tarih":"2026-04-04","zaman_acilis":"19:15","zaman_kapanis":"05:28","max_yukselis":0},
    {"ticker":"ATHUSDT","marj":96,"giris":0.00685,"sonuc":"✗ Stop","kar":-25.5,"tarih":"2026-04-04","zaman_acilis":"23:00","zaman_kapanis":"05:15","max_yukselis":0},
    {"ticker":"ZBTUSDT","marj":66,"giris":0.09695,"sonuc":"≈ TP1+Trail","kar":25.3,"tarih":"2026-04-05","zaman_acilis":"04:45","zaman_kapanis":"05:13","max_yukselis":0},
    {"ticker":"INJUSDT","marj":144,"giris":2.835,"sonuc":"✗ Stop","kar":-23.4,"tarih":"2026-04-04","zaman_acilis":"19:15","zaman_kapanis":"04:49","max_yukselis":0},
    {"ticker":"COWUSDT","marj":111,"giris":0.204,"sonuc":"✗ Stop","kar":-22.8,"tarih":"2026-04-04","zaman_acilis":"19:15","zaman_kapanis":"04:11","max_yukselis":0},
    {"ticker":"SAHARAUSDT","marj":65,"giris":0.02365,"sonuc":"✗ Stop","kar":-21.7,"tarih":"2026-04-05","zaman_acilis":"00:15","zaman_kapanis":"02:40","max_yukselis":0},
    {"ticker":"DIAUSDT","marj":64,"giris":0.1846,"sonuc":"✗ Stop","kar":-17.8,"tarih":"2026-04-04","zaman_acilis":"17:00","zaman_kapanis":"02:30","max_yukselis":0},
    {"ticker":"CVCUSDT","marj":119,"giris":0.03012,"sonuc":"✗ Stop","kar":-26.0,"tarih":"2026-04-05","zaman_acilis":"00:15","zaman_kapanis":"01:53","max_yukselis":0},
    {"ticker":"KASUSDT","marj":125,"giris":0.03166,"sonuc":"✗ Stop","kar":-24.4,"tarih":"2026-04-04","zaman_acilis":"14:00","zaman_kapanis":"00:46","max_yukselis":0},
    {"ticker":"AEROUSDT","marj":136,"giris":0.317,"sonuc":"✗ Stop","kar":-22.9,"tarih":"2026-04-04","zaman_acilis":"19:15","zaman_kapanis":"00:34","max_yukselis":0},
    {"ticker":"COSUSDT","marj":39,"giris":0.00128,"sonuc":"≈ TP1+Trail","kar":18.3,"tarih":"2026-04-04","zaman_acilis":"21:15","zaman_kapanis":"23:00","max_yukselis":0},
    {"ticker":"EDENUSDT","marj":76,"giris":0.03301,"sonuc":"✗ Stop","kar":-34.5,"tarih":"2026-04-04","zaman_acilis":"12:45","zaman_kapanis":"22:06","max_yukselis":0},
    {"ticker":"FFUSDT","marj":203,"giris":0.0696,"sonuc":"≈ TP1+Trail","kar":19.8,"tarih":"2026-04-04","zaman_acilis":"19:45","zaman_kapanis":"20:15","max_yukselis":0},
    {"ticker":"DOGEUSDT","marj":249,"giris":0.09146,"sonuc":"≈ TP1+Trail","kar":24.8,"tarih":"2026-04-04","zaman_acilis":"15:15","zaman_kapanis":"19:14","max_yukselis":0},
    {"ticker":"AIXBTUSDT","marj":77,"giris":0.02296,"sonuc":"✗ Stop","kar":-26.2,"tarih":"2026-04-04","zaman_acilis":"15:00","zaman_kapanis":"18:43","max_yukselis":0},
    {"ticker":"TRUMPUSDT","marj":102,"giris":2.882,"sonuc":"≈ TP1+Trail","kar":19.2,"tarih":"2026-04-04","zaman_acilis":"17:15","zaman_kapanis":"17:43","max_yukselis":0},
]}

def load_data():
    try:
        with open(DATA_FILE, 'r') as f:
            d = json.load(f)
            # Eski veri varsa koru, yoksa başlangıç verisini kullan
            if d.get("closed_positions"):
                return d
    except:
        pass
    # İlk çalışma — başlangıç verisini kaydet
    save_data(INITIAL_DATA)
    return dict(INITIAL_DATA)

def save_data(data):
    try:
        os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
        with open(DATA_FILE, 'w') as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        print(f"[SAVE ERR] {e}")

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
            ticker  = message.split("CAB v13 TRAIL | ")[1].split(" |")[0].strip()
            kalan   = message.split("Kalan %")[1].split()[0]
            tp_type = "TP2" if "TP2 trailing" in message else "TP1"
            return {"type": "TRAIL", "ticker": ticker, "kalan": int(kalan), "tp_type": tp_type}
        elif "CAB v13 STOP |" in message:
            ticker    = message.split("CAB v13 STOP | ")[1].split(" |")[0].strip()
            kalan     = "100"
            if "Kalan %" in message:
                kalan = message.split("Kalan %")[1].split()[0]
            stop_type = "BE" if "BE stop" in message else ("TP2" if "TP2 sonrasi" in message else "TAM")
            return {"type": "STOP", "ticker": ticker, "kalan": int(kalan), "stop_type": stop_type}
    except Exception as e:
        return {"type": "ERROR", "msg": str(e)}
    return {"type": "UNKNOWN"}

def get_symbol(ticker):
    return ticker + "USDT" if not ticker.endswith("USDT") else ticker

def now_str():
    return datetime.now(timezone.utc).strftime("%H:%M")

def today_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def close_position(data, ticker, sonuc, kar):
    if ticker in data["open_positions"]:
        pos = data["open_positions"][ticker]
        data["closed_positions"].append({
            "ticker":         ticker,
            "marj":           pos["marj"],
            "giris":          pos["giris"],
            "stop":           pos["stop"],
            "tp1":            pos["tp1"],
            "tp2":            pos["tp2"],
            "lev":            pos.get("lev", 10),
            "risk":           pos.get("risk", 0),
            "sonuc":          sonuc,
            "kar":            round(kar, 1),
            "max_yukselis":   pos.get("max_yukselis", 0),
            "tarih":          today_str(),
            "zaman_acilis":   pos.get("zaman", ""),
            "zaman_kapanis":  now_str(),
        })
        del data["open_positions"][ticker]

@app.get("/")
def health():
    data = load_data()
    mode = "TEST MODU" if TEST_MODE else "CANLI"
    return {"status": f"CAB Bot calisiyor — {mode}",
            "acik": len(data["open_positions"]),
            "kapanan": len(data["closed_positions"])}

@app.get("/ip")
async def get_ip():
    async with httpx.AsyncClient() as c:
        r = await c.get("https://api.ipify.org?format=json")
        return r.json()

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    data = load_data()
    open_positions   = data["open_positions"]
    closed_positions = data["closed_positions"]

    mode_badge = "🟡 TEST MODU" if TEST_MODE else "🟢 CANLI"

    # Açık pozisyonlar
    acik_rows = ""
    for ticker, pos in sorted(open_positions.items()):
        symbol  = get_symbol(ticker)
        giris   = pos["giris"]
        stop    = pos["stop"]
        tp1     = pos["tp1"]
        tp2     = pos["tp2"]
        lev     = pos.get("lev", 10)
        risk    = pos.get("risk", 0)
        durum   = pos.get("durum", "Aktif")
        max_y   = pos.get("max_yukselis", 0)
        pos_sz  = pos["marj"] * lev
        tp1_kar = round(pos_sz * 0.60 * (tp1 - giris) / giris, 1) if giris > 0 else 0
        tp2_kar = round(pos_sz * 0.25 * (tp2 - giris) / giris, 1) if giris > 0 else 0
        tv_link = f"https://www.tradingview.com/chart/?symbol=BINANCE:{ticker}.P"
        max_str = f'<span style="color:#f59e0b;font-size:10px">HH:%{max_y:.2f}</span>' if max_y > 0 else ''

        acik_rows += f"""
        <tr>
            <td><a href="{tv_link}" target="_blank" style="color:#a78bfa;text-decoration:none"><b>{ticker}</b> 🔗</a></td>
            <td>{pos['marj']:.0f}$ <span style="color:#94a3b8;font-size:10px">({lev}x)</span></td>
            <td>{giris:.6f}</td>
            <td id="price-{symbol}" style="color:#94a3b8">...</td>
            <td>{stop:.6f} <span style="color:#f87171;font-size:10px">(-{risk:.1f}$)</span></td>
            <td style="color:#4ade80">{tp1:.6f} <span style="color:#4ade80;font-size:10px">(+{tp1_kar:.1f}$)</span></td>
            <td style="color:#2dd4bf">{tp2:.6f} <span style="color:#2dd4bf;font-size:10px">(+{tp2_kar:.1f}$)</span></td>
            <td id="status-{symbol}" style="color:#94a3b8">{durum}</td>
            <td>{max_str}</td>
            <td style="color:#94a3b8;font-size:11px">{pos.get('zaman','')}</td>
        </tr>"""

    if not acik_rows:
        acik_rows = '<tr><td colspan="10" style="text-align:center;color:#94a3b8;padding:16px">Açık pozisyon yok</td></tr>'

    # Kapanan pozisyonlar
    kapanan_rows = ""
    toplam_kar = sum(p.get("kar", 0) for p in closed_positions)
    for pos in reversed(closed_positions):
        kar = pos.get("kar", 0)
        kar_renk = "#4ade80" if kar > 0 else "#f87171" if kar < 0 else "#94a3b8"
        kar_str  = f"+{kar:.1f}$" if kar > 0 else f"{kar:.1f}$"
        max_y    = pos.get("max_yukselis", 0)
        max_str  = f'%{max_y:.2f}' if max_y > 0 else "—"
        sonuc_renk = {
            "≈ TP1+Trail": "#fb923c", "★ TP1+TP2+Trail": "#c084fc",
            "~ TP1+BE Stop": "#60a5fa", "✗ Stop": "#f87171", "↓ TP2+Stop": "#f472b6",
        }.get(pos["sonuc"], "#94a3b8")
        tv_link = f"https://www.tradingview.com/chart/?symbol=BINANCE:{pos['ticker']}.P"
        tarih = pos.get("tarih", "")

        kapanan_rows += f"""
        <tr data-tarih="{tarih}" data-kar="{kar}" data-sonuc="{pos['sonuc']}">
            <td><a href="{tv_link}" target="_blank" style="color:#a78bfa;text-decoration:none"><b>{pos['ticker']}</b> 🔗</a></td>
            <td>{pos['marj']:.0f}$</td>
            <td>{pos['giris']:.6f}</td>
            <td style="color:{sonuc_renk};font-weight:bold">{pos['sonuc']}</td>
            <td style="color:{kar_renk};font-weight:bold">{kar_str}</td>
            <td style="color:#f59e0b">{max_str}</td>
            <td style="color:#94a3b8;font-size:11px">{tarih} {pos.get('zaman_acilis','')}→{pos.get('zaman_kapanis','')}</td>
        </tr>"""

    if not kapanan_rows:
        kapanan_rows = '<tr><td colspan="7" style="text-align:center;color:#94a3b8;padding:16px">Henüz kapanan yok</td></tr>'

    net_renk = "#4ade80" if toplam_kar > 0 else "#f87171"
    net_str  = f"+{toplam_kar:.1f}$" if toplam_kar > 0 else f"{toplam_kar:.1f}$"

    symbols_json   = str([get_symbol(t) for t in open_positions.keys()])
    positions_json = "{"
    for t, p in open_positions.items():
        sym = get_symbol(t)
        positions_json += f'"{sym}":{{"giris":{p["giris"]},"stop":{p["stop"]},"tp1":{p["tp1"]}}},'
    positions_json = positions_json.rstrip(",") + "}"

    tarihler = sorted(set(p.get("tarih","") for p in closed_positions if p.get("tarih","")), reverse=True)
    tarih_options = '<option value="hepsi">Tüm Zamanlar</option>'
    tarih_options += '<option value="bugun">Bugün</option>'
    for t in tarihler:
        tarih_options += f'<option value="{t}">{t}</option>'

    html = f"""<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="30">
<title>CAB Bot Dashboard</title>
<style>
  * {{ box-sizing:border-box; }}
  body {{ background:#0f0f1a; color:#eee; font-family:monospace; padding:12px; margin:0; font-size:12px; }}
  h2 {{ color:#a78bfa; margin:0 0 4px 0; font-size:15px; }}
  h3 {{ color:#60a5fa; font-size:12px; margin:14px 0 6px 0; }}
  .badge {{ display:inline-block; padding:2px 8px; border-radius:10px; font-size:11px; background:#1e1b4b; margin-bottom:6px; }}
  .info {{ color:#94a3b8; font-size:10px; margin-bottom:10px; }}
  .stats {{ display:flex; flex-wrap:wrap; gap:6px; margin-bottom:12px; }}
  .stat {{ background:#1e1b4b; border-radius:8px; padding:6px 12px; font-size:11px; }}
  .stat b {{ color:#a78bfa; font-size:14px; display:block; }}
  table {{ border-collapse:collapse; width:100%; margin-bottom:12px; }}
  th {{ background:#1e1b4b; color:#a78bfa; padding:6px 5px; text-align:left; border-bottom:1px solid #3730a3; cursor:pointer; white-space:nowrap; }}
  th:hover {{ background:#2d2a5e; }}
  td {{ padding:5px 5px; border-bottom:1px solid #1e1e2e; white-space:nowrap; }}
  tr:hover {{ background:#1a1a2e; }}
  .filters {{ display:flex; flex-wrap:wrap; gap:8px; margin-bottom:10px; align-items:center; }}
  select, input {{ background:#1e1b4b; color:#eee; border:1px solid #3730a3; border-radius:6px; padding:4px 8px; font-size:11px; }}
  .ozet {{ background:#1e1b4b; border-radius:8px; padding:8px 12px; margin-bottom:10px; font-size:11px; line-height:1.8; }}
</style>
</head>
<body>
<h2>🤖 CAB Bot Dashboard</h2>
<div class="badge">{mode_badge}</div>
<div class="info" id="timer">⟳ Yükleniyor...</div>

<div class="stats">
  <div class="stat"><b>{len(open_positions)}</b>Açık</div>
  <div class="stat"><b>{len(closed_positions)}</b>Kapanan</div>
  <div class="stat"><b style="color:{net_renk}">{net_str}</b>Net Kar</div>
  <div class="stat"><b>{"TEST" if TEST_MODE else "CANLI"}</b>Mod</div>
</div>

<h3>📊 Açık Pozisyonlar</h3>
<table id="acikTable">
  <tr>
    <th onclick="sortTable('acikTable',0)">Coin ↕</th>
    <th onclick="sortTable('acikTable',1)">Marjin ↕</th>
    <th onclick="sortTable('acikTable',2)">Giriş ↕</th>
    <th onclick="sortTable('acikTable',3)">Şu An ↕</th>
    <th onclick="sortTable('acikTable',4)">Stop</th>
    <th onclick="sortTable('acikTable',5)">TP1</th>
    <th onclick="sortTable('acikTable',6)">TP2</th>
    <th onclick="sortTable('acikTable',7)">Durum ↕</th>
    <th onclick="sortTable('acikTable',8)">HH% ↕</th>
    <th onclick="sortTable('acikTable',9)">Zaman ↕</th>
  </tr>
  {acik_rows}
</table>

<h3>📋 Kapanan Pozisyonlar</h3>
<div class="filters">
  <label>Tarih:
    <select id="tarihFilter" onchange="filterTable()">
      {tarih_options}
    </select>
  </label>
  <label>Sonuç:
    <select id="sonucFilter" onchange="filterTable()">
      <option value="hepsi">Hepsi</option>
      <option value="✗ Stop">✗ Stop</option>
      <option value="≈ TP1+Trail">≈ TP1+Trail</option>
      <option value="★ TP1+TP2+Trail">★ TP1+TP2+Trail</option>
      <option value="~ TP1+BE Stop">~ TP1+BE Stop</option>
    </select>
  </label>
</div>
<div class="ozet" id="ozet">Yükleniyor...</div>
<table id="kapananTable">
  <tr>
    <th onclick="sortTable('kapananTable',0)">Coin ↕</th>
    <th onclick="sortTable('kapananTable',1)">Marjin ↕</th>
    <th onclick="sortTable('kapananTable',2)">Giriş ↕</th>
    <th onclick="sortTable('kapananTable',3)">Sonuç ↕</th>
    <th onclick="sortTable('kapananTable',4)">Kar/Zarar ↕</th>
    <th onclick="sortTable('kapananTable',5)">HH% ↕</th>
    <th onclick="sortTable('kapananTable',6)">Zaman ↕</th>
  </tr>
  {kapanan_rows}
</table>

<script>
var symbols   = {symbols_json};
var positions = {positions_json};
var sortDirs  = {{}};

// Fiyat güncelle
async function fetchPrice(symbol) {{
  try {{
    var r = await fetch("https://fapi.binance.com/fapi/v1/ticker/price?symbol=" + symbol);
    return parseFloat((await r.json()).price);
  }} catch(e) {{
    try {{
      var r2 = await fetch("https://api.binance.com/api/v3/ticker/price?symbol=" + symbol);
      return parseFloat((await r2.json()).price);
    }} catch(e2) {{ return null; }}
  }}
}}

async function updatePrices() {{
  for (var i = 0; i < symbols.length; i++) {{
    var sym = symbols[i];
    var price = await fetchPrice(sym);
    var priceEl  = document.getElementById("price-"  + sym);
    var statusEl = document.getElementById("status-" + sym);
    if (!priceEl || !price) continue;
    var pos   = positions[sym];
    var pnl   = ((price - pos.giris) / pos.giris * 100).toFixed(2);
    priceEl.textContent = price.toFixed(6);
    if (price >= pos.tp1) {{
      priceEl.style.color = "#4ade80";
      if (statusEl.textContent === "Aktif") {{ statusEl.textContent = "✓ TP1 Üstünde"; statusEl.style.color = "#4ade80"; }}
    }} else if (price >= pos.giris) {{
      priceEl.style.color = "#86efac";
      if (statusEl.textContent === "Aktif") {{ statusEl.textContent = "▲ +" + pnl + "%"; statusEl.style.color = "#86efac"; }}
    }} else if (price > pos.stop) {{
      priceEl.style.color = "#fbbf24";
      if (statusEl.textContent === "Aktif") {{ statusEl.textContent = "▼ " + pnl + "%"; statusEl.style.color = "#fbbf24"; }}
    }} else {{
      priceEl.style.color = "#f87171";
      statusEl.textContent = "⚠ STOP ALTI"; statusEl.style.color = "#f87171";
    }}
  }}
  document.getElementById("timer").textContent = "⟳ Son güncelleme: " + new Date().toLocaleTimeString();
  setTimeout(updatePrices, 15000);
}}

// Tablo sıralama
function sortTable(tableId, col) {{
  var table = document.getElementById(tableId);
  var rows  = Array.from(table.rows).slice(1);
  var key   = tableId + col;
  sortDirs[key] = !sortDirs[key];
  rows.sort(function(a, b) {{
    var av = a.cells[col] ? a.cells[col].textContent.trim() : "";
    var bv = b.cells[col] ? b.cells[col].textContent.trim() : "";
    var an = parseFloat(av.replace(/[^0-9.-]/g,""));
    var bn = parseFloat(bv.replace(/[^0-9.-]/g,""));
    if (!isNaN(an) && !isNaN(bn)) return sortDirs[key] ? an-bn : bn-an;
    return sortDirs[key] ? av.localeCompare(bv) : bv.localeCompare(av);
  }});
  rows.forEach(function(r) {{ table.tBodies[0].appendChild(r); }});
}}

// Tarih & sonuç filtresi
function filterTable() {{
  var tarih  = document.getElementById("tarihFilter").value;
  var sonuc  = document.getElementById("sonucFilter").value;
  var today  = new Date().toISOString().slice(0,10);
  var table  = document.getElementById("kapananTable");
  var rows   = Array.from(table.rows).slice(1);
  var visKar = 0, visZarar = 0, visCount = 0;

  rows.forEach(function(r) {{
    var rTarih = r.getAttribute("data-tarih") || "";
    var rKar   = parseFloat(r.getAttribute("data-kar") || "0");
    var rSonuc = r.getAttribute("data-sonuc") || "";
    var tarihOk = tarih === "hepsi" || (tarih === "bugun" && rTarih === today) || rTarih === tarih;
    var sonucOk = sonuc === "hepsi" || rSonuc === sonuc;
    if (tarihOk && sonucOk) {{
      r.style.display = "";
      visCount++;
      if (rKar > 0) visKar += rKar; else visZarar += rKar;
    }} else {{
      r.style.display = "none";
    }}
  }});

  var net = visKar + visZarar;
  var nc  = net >= 0 ? "#4ade80" : "#f87171";
  var ns  = net >= 0 ? "+" + net.toFixed(1) : net.toFixed(1);
  document.getElementById("ozet").innerHTML =
    "<b>Gösterilen:</b> " + visCount + " poz &nbsp;|&nbsp; " +
    "<b style='color:#4ade80'>Kar: +" + visKar.toFixed(1) + "$</b> &nbsp;|&nbsp; " +
    "<b style='color:#f87171'>Zarar: " + visZarar.toFixed(1) + "$</b> &nbsp;|&nbsp; " +
    "<b style='color:" + nc + "'>NET: " + ns + "$</b>";
}}

updatePrices();
filterTable();
</script>
</body>
</html>"""
    return html

@app.post("/webhook")
async def webhook(request: Request):
    body    = await request.body()
    message = body.decode("utf-8")
    print(f"[ALERT] {message}")
    parsed = parse_alert(message)
    print(f"[PARSE] {parsed}")
    data = load_data()

    if parsed["type"] == "GIRIS":
        if len(data["open_positions"]) >= MAX_POSITIONS:
            return {"status": "ATLANDI"}
        ticker = parsed["ticker"]
        data["open_positions"][ticker] = {
            "giris": parsed["giris"], "stop": parsed["stop"],
            "tp1": parsed["tp1"],   "tp2": parsed["tp2"],
            "marj": parsed["marj"], "lev": parsed["lev"],
            "risk": parsed["risk"], "zaman": now_str(),
            "tarih": today_str(),   "durum": "Aktif",
            "tp1_hit": False,       "tp2_hit": False,
            "max_yukselis": 0.0
        }
        save_data(data)
        symbol   = get_symbol(ticker)
        marj, lev, giris = parsed["marj"], parsed["lev"], parsed["giris"]
        stop, tp1, tp2   = parsed["stop"], parsed["tp1"], parsed["tp2"]
        pos_size = round((marj * lev) / giris, 3)
        if TEST_MODE:
            print(f"[TEST] GIRIS: {symbol} | {giris} | Stop:{stop} | TP1:{tp1} | Lot:{pos_size} | {lev}x")
            return {"status": "TEST", "symbol": symbol}
        client.change_leverage(symbol=symbol, leverage=lev)
        try: client.change_margin_type(symbol=symbol, marginType="ISOLATED")
        except: pass
        order = client.new_order(symbol=symbol, side="BUY", type="MARKET", quantity=pos_size)
        client.new_order(symbol=symbol, side="SELL", type="STOP_MARKET", stopPrice=round(stop,6), closePosition=True)
        client.new_order(symbol=symbol, side="SELL", type="TAKE_PROFIT_MARKET", stopPrice=round(tp1,6), quantity=round(pos_size*0.60,3))
        client.new_order(symbol=symbol, side="SELL", type="TAKE_PROFIT_MARKET", stopPrice=round(tp2,6), quantity=round(pos_size*0.25,3))
        return {"status": "OK", "order": order["orderId"]}

    elif parsed["type"] == "TP1":
        ticker = parsed["ticker"]
        if ticker in data["open_positions"]:
            data["open_positions"][ticker]["stop"]    = parsed["stop"]
            data["open_positions"][ticker]["durum"]   = "✓ TP1 Alındı"
            data["open_positions"][ticker]["tp1_hit"] = True
        save_data(data)
        symbol = get_symbol(ticker)
        if TEST_MODE:
            print(f"[TEST] TP1: {symbol} | BE:{parsed['stop']}")
            return {"status": "TEST"}
        orders = client.get_orders(symbol=symbol)
        for o in orders:
            if o["type"] == "STOP_MARKET":
                client.cancel_order(symbol=symbol, orderId=o["orderId"])
        client.new_order(symbol=symbol, side="SELL", type="STOP_MARKET", stopPrice=round(parsed["stop"],6), closePosition=True)
        return {"status": "TP1 ok"}

    elif parsed["type"] == "TP2":
        ticker = parsed["ticker"]
        if ticker in data["open_positions"]:
            data["open_positions"][ticker]["durum"]   = "✓✓ TP2 Alındı"
            data["open_positions"][ticker]["tp2_hit"] = True
        save_data(data)
        return {"status": "TP2 ok"}

    elif parsed["type"] == "TRAIL":
        ticker  = parsed["ticker"]
        tp2_hit = data["open_positions"].get(ticker, {}).get("tp2_hit", False)
        tp1_hit = data["open_positions"].get(ticker, {}).get("tp1_hit", False)
        marj    = data["open_positions"].get(ticker, {}).get("marj", 0)
        lev     = data["open_positions"].get(ticker, {}).get("lev", 10)
        giris   = data["open_positions"].get(ticker, {}).get("giris", 0)
        tp1     = data["open_positions"].get(ticker, {}).get("tp1", 0)
        tp2     = data["open_positions"].get(ticker, {}).get("tp2", 0)
        pos_sz  = marj * lev
        if tp2_hit:
            sonuc = "★ TP1+TP2+Trail"
            kar = round(pos_sz*0.60*(tp1-giris)/giris + pos_sz*0.25*(tp2-giris)/giris, 1) if giris > 0 else 0
        else:
            sonuc = "≈ TP1+Trail"
            kar = round(pos_sz*0.60*(tp1-giris)/giris, 1) if giris > 0 else 0
        close_position(data, ticker, sonuc, kar)
        save_data(data)
        symbol = get_symbol(ticker)
        if TEST_MODE:
            print(f"[TEST] TRAIL: {symbol} | {sonuc} | +{kar}$")
            return {"status": "TEST"}
        client.cancel_open_orders(symbol=symbol)
        pos = [p for p in get_open_positions_binance() if p["symbol"] == symbol]
        if pos:
            amt = abs(float(pos[0]["positionAmt"]))
            if amt > 0: client.new_order(symbol=symbol, side="SELL", type="MARKET", quantity=amt)
        return {"status": "Trail ok"}

    elif parsed["type"] == "STOP":
        ticker    = parsed["ticker"]
        stop_type = parsed.get("stop_type", "TAM")
        risk      = data["open_positions"].get(ticker, {}).get("risk", 0)
        marj      = data["open_positions"].get(ticker, {}).get("marj", 0)
        lev       = data["open_positions"].get(ticker, {}).get("lev", 10)
        giris     = data["open_positions"].get(ticker, {}).get("giris", 0)
        tp1       = data["open_positions"].get(ticker, {}).get("tp1", 0)
        pos_sz    = marj * lev
        if stop_type == "BE":
            sonuc = "~ TP1+BE Stop"
            kar   = round(pos_sz*0.60*(tp1-giris)/giris, 1) if giris > 0 else 0
        elif stop_type == "TP2":
            sonuc = "↓ TP2+Stop"
            kar   = 0
        else:
            sonuc = "✗ Stop"
            kar   = -risk
        close_position(data, ticker, sonuc, kar)
        save_data(data)
        symbol = get_symbol(ticker)
        if TEST_MODE:
            print(f"[TEST] STOP: {symbol} | {sonuc} | {kar}$")
            return {"status": "TEST"}
        client.cancel_open_orders(symbol=symbol)
        pos = [p for p in get_open_positions_binance() if p["symbol"] == symbol]
        if pos:
            amt = abs(float(pos[0]["positionAmt"]))
            if amt > 0: client.new_order(symbol=symbol, side="SELL", type="MARKET", quantity=amt)
        return {"status": "Stop ok"}

    # HH güncelleme endpoint (ileride kullanmak için)
    return {"status": "Islem yapilmadi"}

@app.post("/update_hh")
async def update_hh(request: Request):
    """Açık pozisyonların HH değerini güncelle"""
    body = await request.body()
    req  = json.loads(body)
    data = load_data()
    ticker = req.get("ticker")
    price  = req.get("price", 0)
    if ticker and ticker in data["open_positions"]:
        giris    = data["open_positions"][ticker]["giris"]
        pct      = (price - giris) / giris * 100 if giris > 0 else 0
        curr_max = data["open_positions"][ticker].get("max_yukselis", 0)
        if pct > curr_max:
            data["open_positions"][ticker]["max_yukselis"] = round(pct, 2)
            save_data(data)
    return {"status": "ok"}
