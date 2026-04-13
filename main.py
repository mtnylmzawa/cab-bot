from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from binance.um_futures import UMFutures
import os, json, httpx, asyncio
from datetime import datetime, timezone, timedelta

app = FastAPI()

TEST_MODE = True

client = UMFutures(
    key=os.environ.get("BINANCE_API_KEY"),
    secret=os.environ.get("BINANCE_SECRET_KEY"),
    base_url="https://fapi.binance.com"
)

MAX_POSITIONS = int(os.environ.get("MAX_POSITIONS", "3"))
DATA_FILE = os.environ.get("DATA_FILE", "/tmp/cab_data.json")

INITIAL_DATA = {"open_positions": {}, "closed_positions": []}

def load_data():
    try:
        with open(DATA_FILE, 'r') as f:
            d = json.load(f)
            if d.get("closed_positions") is not None:
                return d
    except:
        pass
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
            kapat_oran = 60
            atr_skor   = 100
            if "Kapat:" in message:
                try: kapat_oran = int(message.split("Kapat:")[1].split()[0])
                except: pass
            if "ATR:" in message:
                try: atr_skor = round(int(message.split("ATR:")[1].split()[0]) / 100, 2)
                except: pass
            return {"type":"GIRIS","ticker":ticker,"giris":giris,"stop":stop,
                    "tp1":tp1,"tp2":tp2,"marj":marj,"lev":lev,"risk":risk,
                    "kapat_oran":kapat_oran,"atr_skor":atr_skor}
        elif "CAB v13 TP1 |" in message:
            ticker = message.split("CAB v13 TP1 | ")[1].split(" |")[0].strip()
            tp1    = float(message.split("TP1:")[1].split()[0])
            stop   = float(message.split("YeniStop:")[1].split()[0])
            # Adaptif kapatma oranı — v13.13'te alert mesajında geliyor
            kapat_oran = 60  # varsayılan
            if "% kapat" in message:
                try:
                    kapat_oran = int(message.split("| %")[1].split(" kapat")[0].strip())
                except:
                    pass
            # ATR skoru
            atr_skor   = 1.0
            trail_pct  = None
            trail_stop = None
            if "ATR:" in message:
                try:
                    raw = message.split("ATR:")[1].strip().split()[0]
                    val = float(raw)
                    atr_skor = round(val / 100, 2) if val > 10 else round(val, 2)
                except: pass
            if "Trail:" in message:
                try: trail_pct = float(message.split("Trail:")[1].split()[0])
                except: pass
            if "TrailStop:" in message:
                try: trail_stop = float(message.split("TrailStop:")[1].split()[0])
                except: pass
            return {"type":"TP1","ticker":ticker,"tp1":tp1,"stop":stop,
                    "kapat_oran":kapat_oran,"atr_skor":atr_skor,
                    "trail_pct":trail_pct,"trail_stop":trail_stop}
        elif "CAB v13 TP2 |" in message:
            ticker = message.split("CAB v13 TP2 | ")[1].split(" |")[0].strip()
            return {"type":"TP2","ticker":ticker}
        elif "CAB v13 TRAIL |" in message:
            ticker  = message.split("CAB v13 TRAIL | ")[1].split(" |")[0].strip()
            kalan   = message.split("Kalan %")[1].split()[0]
            tp_type = "TP2" if "TP2 trailing" in message else "TP1"
            return {"type":"TRAIL","ticker":ticker,"kalan":int(kalan),"tp_type":tp_type}
        elif "CAB v13 STOP |" in message:
            ticker    = message.split("CAB v13 STOP | ")[1].split(" |")[0].strip()
            kalan     = "100"
            if "Kalan %" in message:
                kalan = message.split("Kalan %")[1].split()[0]
            stop_type = "BE" if "BE stop" in message else ("TP2" if "TP2 sonrasi" in message else "TAM")
            return {"type":"STOP","ticker":ticker,"kalan":int(kalan),"stop_type":stop_type}
    except Exception as e:
        return {"type":"ERROR","msg":str(e)}
    return {"type":"UNKNOWN"}

def get_symbol(ticker):
    return ticker + "USDT" if not ticker.endswith("USDT") else ticker

def now_str():
    # UTC+3 (Türkiye saati)
    return (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%H:%M")

def today_str():
    # UTC+3
    return (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%d")

def calc_sure(zaman_acilis, zaman_kapanis):
    try:
        fmt = "%H:%M"
        a = datetime.strptime(zaman_acilis, fmt)
        k = datetime.strptime(zaman_kapanis, fmt)
        dk = int((k - a).seconds / 60)
        if dk < 0: dk += 1440
        return dk
    except:
        return 0

def sure_str(dk):
    if dk <= 0: return "—"
    if dk < 60: return f"{dk}dk"
    return f"{dk//60}s {dk%60}dk"

def close_position(data, ticker, sonuc, kar):
    if ticker in data["open_positions"]:
        pos = data["open_positions"][ticker]
        sure_dk = calc_sure(pos.get("zaman","00:00"), now_str())
        data["closed_positions"].append({
            "ticker":        ticker,
            "marj":          pos["marj"],
            "giris":         pos["giris"],
            "stop":          pos["stop"],
            "tp1":           pos["tp1"],
            "tp2":           pos["tp2"],
            "lev":           pos.get("lev", 10),
            "risk":          pos.get("risk", 0),
            "sonuc":         sonuc,
            "kar":           round(kar, 1),
            "max_yukselis":  pos.get("max_yukselis", 0),
            "kapat_oran":    pos.get("kapat_oran", 60),
            "atr_skor":      pos.get("atr_skor", 1.0),
            "sure_dk":       sure_dk,
            "tarih":         today_str(),
            "zaman_acilis":  pos.get("zaman",""),
            "zaman_kapanis": now_str(),
        })
        del data["open_positions"][ticker]

async def fetch_price(symbol):
    urls = [
        f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={symbol}",
        f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}",
    ]
    for url in urls:
        try:
            async with httpx.AsyncClient(timeout=4) as c:
                r = await c.get(url)
                if not r.ok: continue
                d = r.json()
                if "price" in d:
                    return float(d["price"])
        except:
            pass
    return None

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

    toplam_kar   = sum(p.get("kar",0) for p in closed_positions)
    kapanan_n    = len(closed_positions)
    karli_n      = sum(1 for p in closed_positions if p.get("kar",0) > 0)
    win_rate     = round(karli_n / kapanan_n * 100, 1) if kapanan_n > 0 else 0
    sure_list    = [p.get("sure_dk",0) for p in closed_positions if p.get("sure_dk",0) > 0]
    ort_sure     = round(sum(sure_list)/len(sure_list)) if sure_list else 0

    # Günlük istatistikler
    bugun        = today_str()
    bugun_pozlar = [p for p in closed_positions if p.get("tarih","") == bugun]
    bugun_kar    = sum(p.get("kar",0) for p in bugun_pozlar)
    bugun_giris  = len(bugun_pozlar)
    bugun_stop   = sum(1 for p in bugun_pozlar if "Stop" in p.get("sonuc","") and "BE" not in p.get("sonuc",""))
    bugun_tp     = sum(1 for p in bugun_pozlar if p.get("kar",0) > 0)
    # Günlük win rate
    bugun_karli  = sum(1 for p in bugun_pozlar if p.get("kar",0) > 0)
    bugun_wr     = round(bugun_karli / len(bugun_pozlar) * 100, 1) if bugun_pozlar else 0
    bugun_wr_renk = "#4ade80" if bugun_wr > 50 else "#f87171" if bugun_pozlar else "#94a3b8"

    net_renk = "#4ade80" if toplam_kar > 0 else "#f87171" if toplam_kar < 0 else "#94a3b8"
    net_str  = f"+{toplam_kar:.1f}$" if toplam_kar > 0 else f"{toplam_kar:.1f}$"
    bugun_renk = "#4ade80" if bugun_kar > 0 else "#f87171"
    bugun_str  = f"+{bugun_kar:.1f}$" if bugun_kar > 0 else f"{bugun_kar:.1f}$"
    wr_renk = "#4ade80" if win_rate > 50 else "#94a3b8" if kapanan_n == 0 else "#f87171"

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
        max_y   = pos.get("max_yukselis", 0)
        atr_s   = pos.get("atr_skor", None)
        kap_o   = pos.get("kapat_oran", None)
        pos_sz  = pos["marj"] * lev
        tp1_kar = round(pos_sz * (kap_o/100 if kap_o else 0.60) * (tp1-giris)/giris, 1) if giris > 0 else 0
        tp2_kar = round(pos_sz * 0.25 * (tp2-giris)/giris, 1) if giris > 0 else 0
        tv_link = f"https://www.tradingview.com/chart/?symbol=BINANCE:{ticker}.P"
        hh_str  = f'<span style="color:#f59e0b">%{max_y:.2f}</span>' if max_y > 0 else '<span style="color:#555">—</span>'
        # ATR skoru göster
        if atr_s is not None:
            atr_renk = "#f87171" if atr_s >= 1.5 else "#f59e0b" if atr_s >= 1.0 else "#4ade80"
            atr_str = f'<span style="color:{atr_renk}">{atr_s:.2f}x ({kap_o}%)</span>'
        else:
            atr_str = '<span style="color:#555">—</span>'

        trail_aktif  = pos.get("trail_aktif", False)
        tp1_kar_pos  = pos.get("tp1_kar", 0)
        trail_pct    = pos.get("trail_pct")
        trail_stop_v = pos.get("trail_stop")
        row_bg = "background:#1a2a1a;" if trail_aktif else ""
        durum_str = pos.get("durum", "Aktif")
        acik_rows += f"""<tr style="{row_bg}">
            <td><a href="{tv_link}" target="_blank" style="color:#a78bfa;text-decoration:none"><b>{ticker}</b> 🔗</a></td>
            <td>{pos['marj']:.0f}$ <small style="color:#666">({lev}x)</small></td>
            <td>{giris:.6f}</td>
            <td id="price-{symbol}" style="color:#94a3b8">...</td>
            <td id="tp1dist-{symbol}" style="color:#94a3b8">—</td>
            <td id="status-{symbol}" style="color:#94a3b8">Yükleniyor...</td>
            <td>{hh_str}</td>
            <td>{atr_str}</td>
            <td id="trail-{symbol}" style="color:#fb923c">{'✓ +' + str(tp1_kar_pos) + '$ | Stop:' + str(round(trail_stop_v,6) if trail_stop_v else '—') + ' | %' + str(trail_pct if trail_pct else '—') if trail_aktif else '—'}</td>
            <td>{stop:.6f} <small style="color:#f87171">(-{risk:.1f}$)</small></td>
            <td style="color:#4ade80">{tp1:.6f} <small style="color:#4ade80">(+{tp1_kar:.1f}$)</small></td>
            <td style="color:#2dd4bf">{tp2:.6f} <small style="color:#2dd4bf">(+{tp2_kar:.1f}$)</small></td>
            <td style="color:#94a3b8;font-size:10px">{pos.get('zaman','')}</td>
        </tr>"""

    if not acik_rows:
        acik_rows = '<tr><td colspan="13" style="text-align:center;color:#555;padding:16px">Açık pozisyon yok</td></tr>'

    # Kapanan pozisyonlar
    kapanan_rows = ""
    for pos in reversed(closed_positions):
        kar      = pos.get("kar", 0)
        kar_renk = "#4ade80" if kar > 0 else "#f87171" if kar < 0 else "#94a3b8"
        kar_str  = f"+{kar:.1f}$" if kar > 0 else f"{kar:.1f}$"
        max_y    = pos.get("max_yukselis", 0)
        max_str  = f'%{max_y:.2f}' if max_y > 0 else "—"
        sure_dk  = pos.get("sure_dk", 0)
        atr_s    = pos.get("atr_skor", None)
        kap_o    = pos.get("kapat_oran", None)
        sonuc_renk = {"≈ TP1+Trail":"#fb923c","★ TP1+TP2+Trail":"#c084fc",
                      "~ TP1+BE Stop":"#60a5fa","✗ Stop":"#f87171","↓ TP2+Stop":"#f472b6"}.get(pos["sonuc"],"#94a3b8")
        tv_link  = f"https://www.tradingview.com/chart/?symbol=BINANCE:{pos['ticker']}.P"
        tarih    = pos.get("tarih","")
        if atr_s is not None:
            atr_renk = "#f87171" if atr_s >= 1.5 else "#f59e0b" if atr_s >= 1.0 else "#4ade80"
            atr_str = f'<span style="color:{atr_renk}">{atr_s:.2f}x/{kap_o}%</span>'
        else:
            atr_str = "—"

        kapanan_rows += f"""<tr data-tarih="{tarih}" data-kar="{kar}" data-sonuc="{pos['sonuc']}">
            <td><a href="{tv_link}" target="_blank" style="color:#a78bfa;text-decoration:none"><b>{pos['ticker']}</b> 🔗</a></td>
            <td>{pos['marj']:.0f}$</td>
            <td>{pos['giris']:.6f}</td>
            <td style="color:{sonuc_renk};font-weight:bold">{pos['sonuc']}</td>
            <td style="color:{kar_renk};font-weight:bold">{kar_str}</td>
            <td style="color:#f59e0b">{max_str}</td>
            <td style="color:#94a3b8">{atr_str}</td>
            <td style="color:#94a3b8">{sure_str(sure_dk)}</td>
            <td style="color:#94a3b8;font-size:10px">{tarih} {pos.get('zaman_acilis','')}→{pos.get('zaman_kapanis','')}</td>
        </tr>"""

    if not kapanan_rows:
        kapanan_rows = '<tr><td colspan="9" style="text-align:center;color:#555;padding:16px">Henüz kapanan yok</td></tr>'

    tarihler = sorted(set(p.get("tarih","") for p in closed_positions if p.get("tarih","")), reverse=True)
    tarih_options = '<option value="hepsi">Tüm Zamanlar</option><option value="bugun">Bugün</option>'
    for t in tarihler:
        tarih_options += f'<option value="{t}">{t}</option>'

    symbols_json   = str([get_symbol(t) for t in open_positions.keys()])
    positions_json = "{"
    for t, p in open_positions.items():
        sym = get_symbol(t)
        positions_json += f'"{sym}":{{"giris":{p["giris"]},"stop":{p["stop"]},"tp1":{p["tp1"]},"marj":{p.get("marj",0)},"lev":{p.get("lev",10)}}},'
    positions_json = positions_json.rstrip(",") + "}"

    html = f"""<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CAB Bot</title>
<style>
* {{ box-sizing:border-box; }}
body {{ background:#0a0a14; color:#ddd; font-family:monospace; padding:10px; margin:0; font-size:11px; }}
h2 {{ color:#a78bfa; margin:0 0 4px 0; font-size:14px; }}
h3 {{ color:#60a5fa; font-size:11px; margin:12px 0 5px 0; border-bottom:1px solid #1e1e2e; padding-bottom:3px; }}
.badge {{ display:inline-block; padding:2px 8px; border-radius:10px; font-size:10px; background:#1e1b4b; margin-bottom:6px; }}
.info {{ color:#555; font-size:10px; margin-bottom:8px; }}
.stats {{ display:flex; flex-wrap:wrap; gap:5px; margin-bottom:10px; }}
.stat {{ background:#1e1b4b; border-radius:6px; padding:5px 10px; text-align:center; }}
.stat b {{ color:#a78bfa; font-size:13px; display:block; }}
.stat small {{ color:#666; font-size:9px; }}
.daily {{ background:#1a1a2e; border:1px solid #3730a3; border-radius:6px; padding:8px 12px; margin-bottom:10px; font-size:11px; }}
.filters {{ display:flex; flex-wrap:wrap; gap:6px; margin-bottom:6px; align-items:center; }}
select {{ background:#1e1b4b; color:#eee; border:1px solid #3730a3; border-radius:4px; padding:3px 6px; font-size:10px; }}
.ozet {{ background:#1a1a2e; border-radius:6px; padding:6px 10px; margin-bottom:8px; font-size:10px; color:#94a3b8; }}
table {{ border-collapse:collapse; width:100%; margin-bottom:10px; }}
th {{ background:#1e1b4b; color:#a78bfa; padding:5px 4px; text-align:left; border-bottom:1px solid #3730a3; cursor:pointer; white-space:nowrap; font-size:10px; }}
th:hover {{ background:#2d2a5e; }}
td {{ padding:4px 4px; border-bottom:1px solid #161624; white-space:nowrap; }}
tr:hover {{ background:#111120; }}
</style>
</head>
<body>
<h2>🤖 CAB Bot Dashboard</h2>
<div class="badge">{mode_badge}</div>
<div class="info" id="timer">⟳ Yükleniyor...</div>

<div class="stats">
  <div class="stat"><b>{len(open_positions)}</b><small>Açık</small></div>
  <div class="stat"><b>{kapanan_n}</b><small>Kapanan</small></div>
  <div class="stat"><b id="netkar" style="color:{net_renk}">{net_str}</b><small>Net Kar</small></div>
  <div class="stat"><b style="color:{wr_renk}">{win_rate}%</b><small>Win Rate</small></div>
  <div class="stat"><b style="color:{bugun_wr_renk}">{bugun_wr}%</b><small>Bugün WR</small></div>
  <div class="stat"><b>{sure_str(ort_sure)}</b><small>Ort. Süre</small></div>
  <div class="stat"><b>{"TEST" if TEST_MODE else "CANLI"}</b><small>Mod</small></div>
</div>

<div class="daily">
  📅 <b>Bugün ({bugun}):</b> &nbsp;
  {bugun_giris} kapanan &nbsp;|&nbsp;
  <span style="color:#4ade80">{bugun_tp} TP</span> &nbsp;|&nbsp;
  <span style="color:#f87171">{bugun_stop} Stop</span> &nbsp;|&nbsp;
  <span style="color:{bugun_renk}">{bugun_str}</span>
</div>

<h3>📊 Açık Pozisyonlar</h3>
<table id="acikTable">
  <tr>
    <th onclick="sortT('acikTable',0)">Coin ↕</th>
    <th onclick="sortT('acikTable',1)">Marjin ↕</th>
    <th onclick="sortT('acikTable',2)">Giriş ↕</th>
    <th onclick="sortT('acikTable',3)">Şu An ↕</th>
    <th onclick="sortT('acikTable',4)">TP1'e Kalan ↕</th>
    <th onclick="sortT('acikTable',5)">Durum ↕</th>
    <th onclick="sortT('acikTable',6)">HH% ↕</th>
    <th onclick="sortT('acikTable',7)">ATR/Kapat ↕</th>
    <th onclick="sortT('acikTable',8)">Trailing ↕</th>
    <th onclick="sortT('acikTable',9)">Stop</th>
    <th onclick="sortT('acikTable',9)">TP1</th>
    <th onclick="sortT('acikTable',10)">TP2</th>
    <th onclick="sortT('acikTable',11)">Zaman ↕</th>
  </tr>
  {acik_rows}
</table>

<h3>📋 Kapanan Pozisyonlar</h3>
<div class="filters">
  <label>Tarih: <select id="tarihF" onchange="filterT()"> {tarih_options} </select></label>
  <label>Sonuç: <select id="sonucF" onchange="filterT()">
    <option value="hepsi">Hepsi</option>
    <option value="✗ Stop">✗ Stop</option>
    <option value="≈ TP1+Trail">≈ TP1+Trail</option>
    <option value="★ TP1+TP2+Trail">★ TP1+TP2+Trail</option>
    <option value="~ TP1+BE Stop">~ TP1+BE Stop</option>
  </select></label>
</div>
<div class="ozet" id="ozet" style="margin-top:6px">—</div>
<table id="kapananTable">
  <tr>
    <th onclick="sortT('kapananTable',0)">Coin ↕</th>
    <th onclick="sortT('kapananTable',1)">Marjin ↕</th>
    <th onclick="sortT('kapananTable',2)">Giriş ↕</th>
    <th onclick="sortT('kapananTable',3)">Sonuç ↕</th>
    <th onclick="sortT('kapananTable',4)">Kar/Zarar ↕</th>
    <th onclick="sortT('kapananTable',5)">HH% ↕</th>
    <th onclick="sortT('kapananTable',6)">ATR/Kapat ↕</th>
    <th onclick="sortT('kapananTable',7)">Süre ↕</th>
    <th onclick="sortT('kapananTable',8)">Zaman ↕</th>
  </tr>
  {kapanan_rows}
</table>

<script>
var symbols   = {symbols_json};
var positions = {positions_json};
var sortDirs  = {{}};

async function fp(sym) {{
  try {{
    var r = await fetch("https://fapi.binance.com/fapi/v1/ticker/price?symbol="+sym);
    if (!r.ok) return null;
    var d = await r.json();
    return d.price ? parseFloat(d.price) : null;
  }} catch(e) {{
    try {{
      var r2 = await fetch("https://api.binance.com/api/v3/ticker/price?symbol="+sym);
      if (!r2.ok) return null;
      var d2 = await r2.json();
      return d2.price ? parseFloat(d2.price) : null;
    }} catch(e2) {{ return null; }}
  }}
}}

async function updatePrices() {{
  for (var i=0; i<symbols.length; i++) {{
    var sym   = symbols[i];
    var price = await fp(sym);
    var pEl   = document.getElementById("price-"+sym);
    var sEl   = document.getElementById("status-"+sym);
    var dEl   = document.getElementById("tp1dist-"+sym);
    if (!pEl || !price) continue;
    var pos  = positions[sym];
    var pnl  = ((price-pos.giris)/pos.giris*100).toFixed(2);
    var tp1d = ((pos.tp1-price)/price*100).toFixed(2);
    pEl.textContent = price.toFixed(6);
    dEl.textContent = (price >= pos.tp1) ? "✓ Geçildi" : ("%"+tp1d+" uzak");
    dEl.style.color = (price >= pos.tp1) ? "#4ade80" : "#f59e0b";
    var marj = pos.marj || 0;
    var lev  = pos.lev  || 10;
    var posSz = marj * lev;
    var pnlUsdt = (posSz * (price - pos.giris) / pos.giris).toFixed(1);
    var pnlStr = (pnlUsdt >= 0 ? "+" : "") + pnlUsdt + "$ (" + (pnl >= 0 ? "+" : "") + pnl + "%)";
    if (price >= pos.tp1) {{
      pEl.style.color = "#4ade80";
      sEl.textContent = "✓ TP1+ " + pnlStr;
      sEl.style.color = "#4ade80";
    }} else if (price >= pos.giris) {{
      pEl.style.color = "#86efac";
      sEl.textContent = "▲ " + pnlStr;
      sEl.style.color = "#86efac";
    }} else if (price > pos.stop) {{
      pEl.style.color = "#f87171";
      sEl.textContent = "▼ " + pnlStr;
      sEl.style.color = "#f87171";
    }} else {{
      pEl.style.color = "#f87171";
      sEl.textContent = "⚠ STOP " + pnlStr;
      sEl.style.color = "#f87171";
    }}
    fetch("/update_hh", {{
      method: "POST",
      headers: {{"Content-Type": "application/json"}},
      body: JSON.stringify({{ticker: sym, price: price}})
    }}).catch(function(){{}});
  }}
  document.getElementById("timer").textContent = "⟳ Son güncelleme: "+new Date().toLocaleTimeString();
  setTimeout(updatePrices, 15000);
}}

setTimeout(function() {{ location.reload(); }}, 30000);

function sortT(tid, col) {{
  var tbl  = document.getElementById(tid);
  var rows = Array.from(tbl.rows).slice(1);
  var key  = tid+col;
  sortDirs[key] = !sortDirs[key];
  rows.sort(function(a,b) {{
    var av = a.cells[col] ? a.cells[col].textContent.trim() : "";
    var bv = b.cells[col] ? b.cells[col].textContent.trim() : "";
    var an = parseFloat(av.replace(/[^0-9.-]/g,""));
    var bn = parseFloat(bv.replace(/[^0-9.-]/g,""));
    if (!isNaN(an)&&!isNaN(bn)) return sortDirs[key] ? an-bn : bn-an;
    return sortDirs[key] ? av.localeCompare(bv) : bv.localeCompare(av);
  }});
  rows.forEach(function(r) {{ tbl.tBodies[0].appendChild(r); }});
}}

function filterT() {{
  var tarih = document.getElementById("tarihF").value;
  var sonuc = document.getElementById("sonucF").value;
  var today = new Date().toISOString().slice(0,10);
  var rows  = Array.from(document.getElementById("kapananTable").rows).slice(1);
  var vK=0, vZ=0, vN=0;
  rows.forEach(function(r) {{
    if (!r.getAttribute("data-tarih")) return;
    var rT = r.getAttribute("data-tarih")||"";
    var rK = parseFloat(r.getAttribute("data-kar")||"0");
    var rS = r.getAttribute("data-sonuc")||"";
    var tOk = tarih==="hepsi"||(tarih==="bugun"&&rT===today)||rT===tarih;
    var sOk = sonuc==="hepsi"||rS===sonuc;
    if (tOk&&sOk) {{
      r.style.display=""; vN++;
      if (rK>0) vK+=rK; else vZ+=rK;
    }} else {{ r.style.display="none"; }}
  }});
  var net=vK+vZ;
  var nc=net>=0?"#4ade80":"#f87171";
  var ns=net>=0?"+"+net.toFixed(1)+"$":net.toFixed(1)+"$";
  var ozetEl = document.getElementById("ozet");
  if (!ozetEl) return;
  ozetEl.innerHTML = vN===0 ? "Henüz kapanan pozisyon yok" :
    "<b>"+vN+"</b> pozisyon &nbsp;|&nbsp; "+
    "<span style='color:#4ade80'>Kar: +"+vK.toFixed(1)+"$</span> &nbsp;|&nbsp; "+
    "<span style='color:#f87171'>Zarar: "+vZ.toFixed(1)+"$</span> &nbsp;|&nbsp; "+
    "<span style='color:"+nc+"'><b>NET: "+ns+"</b></span>";
}}

updatePrices();
filterT();
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
            return {"status":"ATLANDI"}
        ticker = parsed["ticker"]
        data["open_positions"][ticker] = {
            "giris":parsed["giris"],"stop":parsed["stop"],
            "tp1":parsed["tp1"],"tp2":parsed["tp2"],
            "marj":parsed["marj"],"lev":parsed["lev"],
            "risk":parsed["risk"],"zaman":now_str(),
            "tarih":today_str(),"durum":"Aktif",
            "tp1_hit":False,"tp2_hit":False,"max_yukselis":0.0,
            "kapat_oran":parsed.get("kapat_oran", 60),
            "atr_skor":parsed.get("atr_skor", 1.0),
            "tp1_kar":0.0,"trail_aktif":False,"trail_px":None
        }
        save_data(data)
        symbol = get_symbol(ticker)
        marj,lev,giris = parsed["marj"],parsed["lev"],parsed["giris"]
        stop,tp1,tp2   = parsed["stop"],parsed["tp1"],parsed["tp2"]
        pos_size = round((marj*lev)/giris, 3)
        if TEST_MODE:
            print(f"[TEST] GIRIS: {symbol} | {giris} | Stop:{stop} | TP1:{tp1} | Lot:{pos_size} | {lev}x")
            return {"status":"TEST","symbol":symbol}
        client.change_leverage(symbol=symbol, leverage=lev)
        try: client.change_margin_type(symbol=symbol, marginType="ISOLATED")
        except: pass
        order = client.new_order(symbol=symbol,side="BUY",type="MARKET",quantity=pos_size)
        client.new_order(symbol=symbol,side="SELL",type="STOP_MARKET",stopPrice=round(stop,6),closePosition=True)
        client.new_order(symbol=symbol,side="SELL",type="TAKE_PROFIT_MARKET",stopPrice=round(tp1,6),quantity=round(pos_size*0.60,3))
        client.new_order(symbol=symbol,side="SELL",type="TAKE_PROFIT_MARKET",stopPrice=round(tp2,6),quantity=round(pos_size*0.25,3))
        return {"status":"OK","order":order["orderId"]}

    elif parsed["type"] == "TP1":
        ticker     = parsed["ticker"]
        kapat_oran = parsed.get("kapat_oran", 60)
        atr_skor   = parsed.get("atr_skor", 100)
        tp1_kar    = 0
        if ticker in data["open_positions"]:
            pos        = data["open_positions"][ticker]
            marj       = pos.get("marj", 0)
            lev        = pos.get("lev", 10)
            giris      = pos.get("giris", 0)
            tp1        = pos.get("tp1", 0)
            pos_sz     = marj * lev
            kap_r      = kapat_oran / 100
            tp1_kar    = round(pos_sz * kap_r * (tp1 - giris) / giris, 1) if giris > 0 else 0
            data["open_positions"][ticker]["stop"]        = parsed["stop"]
            data["open_positions"][ticker]["durum"]       = f"✓ TP1 (+{tp1_kar}$)"
            data["open_positions"][ticker]["tp1_hit"]     = True
            data["open_positions"][ticker]["kapat_oran"]  = kapat_oran
            data["open_positions"][ticker]["atr_skor"]    = atr_skor
            data["open_positions"][ticker]["tp1_kar"]     = tp1_kar
            data["open_positions"][ticker]["trail_aktif"] = True
            data["open_positions"][ticker]["trail_pct"]   = parsed.get("trail_pct")
            data["open_positions"][ticker]["trail_stop"]  = parsed.get("trail_stop")
        save_data(data)
        symbol = get_symbol(ticker)
        if TEST_MODE:
            print(f"[TEST] TP1: {symbol} | BE:{parsed['stop']} | Kapat:%{kapat_oran} | TP1Kar:{tp1_kar}$")
            return {"status":"TEST"}
        orders = client.get_orders(symbol=symbol)
        for o in orders:
            if o["type"]=="STOP_MARKET": client.cancel_order(symbol=symbol,orderId=o["orderId"])
        client.new_order(symbol=symbol,side="SELL",type="STOP_MARKET",stopPrice=round(parsed["stop"],6),closePosition=True)
        return {"status":"TP1 ok"}

    elif parsed["type"] == "TP2":
        ticker = parsed["ticker"]
        if ticker in data["open_positions"]:
            data["open_positions"][ticker]["durum"]   = "✓✓ TP2 Alındı"
            data["open_positions"][ticker]["tp2_hit"] = True
        save_data(data)
        return {"status":"TP2 ok"}

    elif parsed["type"] == "TRAIL":
        ticker    = parsed["ticker"]
        pos       = data["open_positions"].get(ticker, {})
        tp2_hit   = pos.get("tp2_hit", False)
        marj      = pos.get("marj", 0)
        lev       = pos.get("lev", 10)
        giris     = pos.get("giris", 0)
        tp1       = pos.get("tp1", 0)
        tp2       = pos.get("tp2", 0)
        kap_o     = pos.get("kapat_oran", 60)
        tp1_kar   = pos.get("tp1_kar", 0)
        pos_sz    = marj * lev
        kap_r     = (kap_o or 60) / 100
        kalan_r   = 1.0 - kap_r
        # Trail fiyatı alert'ten al
        trail_px  = None
        if "Trailing:" in message:
            try: trail_px = float(message.split("Trailing:")[1].strip())
            except: pass
        if tp2_hit:
            sonuc    = "★ TP1+TP2+Trail"
            trail_kar = round(pos_sz * 0.25 * (tp2 - giris) / giris, 1) if giris > 0 else 0
            kar      = round(tp1_kar + trail_kar, 1)
        else:
            sonuc    = "≈ TP1+Trail"
            trail_kar = round(pos_sz * kalan_r * (trail_px - giris) / giris, 1) if trail_px and giris > 0 else 0
            kar      = round(tp1_kar + trail_kar, 1)
        close_position(data, ticker, sonuc, kar)
        save_data(data)
        symbol = get_symbol(ticker)
        if TEST_MODE:
            print(f"[TEST] TRAIL: {symbol} | {sonuc} | +{kar}$")
            return {"status":"TEST"}
        client.cancel_open_orders(symbol=symbol)
        pos = [p for p in get_open_positions_binance() if p["symbol"]==symbol]
        if pos:
            amt = abs(float(pos[0]["positionAmt"]))
            if amt>0: client.new_order(symbol=symbol,side="SELL",type="MARKET",quantity=amt)
        return {"status":"Trail ok"}

    elif parsed["type"] == "STOP":
        ticker    = parsed["ticker"]
        stop_type = parsed.get("stop_type","TAM")
        risk      = data["open_positions"].get(ticker,{}).get("risk",0)
        marj      = data["open_positions"].get(ticker,{}).get("marj",0)
        lev       = data["open_positions"].get(ticker,{}).get("lev",10)
        giris     = data["open_positions"].get(ticker,{}).get("giris",0)
        tp1       = data["open_positions"].get(ticker,{}).get("tp1",0)
        kap_o     = data["open_positions"].get(ticker,{}).get("kapat_oran", 60)
        pos_sz    = marj * lev
        kap_r     = (kap_o or 60) / 100
        if stop_type == "BE":
            sonuc = "~ TP1+BE Stop"
            kar   = round(pos_sz*kap_r*(tp1-giris)/giris, 1) if giris>0 else 0
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
            return {"status":"TEST"}
        client.cancel_open_orders(symbol=symbol)
        pos = [p for p in get_open_positions_binance() if p["symbol"]==symbol]
        if pos:
            amt = abs(float(pos[0]["positionAmt"]))
            if amt>0: client.new_order(symbol=symbol,side="SELL",type="MARKET",quantity=amt)
        return {"status":"Stop ok"}

    return {"status":"Islem yapilmadi"}

@app.post("/update_hh")
async def update_hh(request: Request):
    try:
        body = await request.body()
        req  = json.loads(body)
        data = load_data()
        ticker = req.get("ticker", "")
        price  = float(req.get("price", 0))
        if ticker not in data["open_positions"]:
            alt = ticker[:-4] if ticker.endswith("USDT") else ticker + "USDT"
            ticker = alt if alt in data["open_positions"] else None
        if ticker and price > 0:
            giris    = data["open_positions"][ticker]["giris"]
            pct      = (price - giris) / giris * 100 if giris > 0 else 0
            curr_hh  = data["open_positions"][ticker].get("max_yukselis", 0.0)
            if pct > curr_hh:
                data["open_positions"][ticker]["max_yukselis"] = round(pct, 2)
                save_data(data)
                print(f"[HH] {ticker}: {curr_hh:.2f}% -> {pct:.2f}%")
    except Exception as e:
        print(f"[HH ERR] {e}")
    return {"status": "ok"}
