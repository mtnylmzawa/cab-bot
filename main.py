from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
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

MAX_POSITIONS = int(os.environ.get("MAX_POSITIONS", "999"))
DATA_FILE = os.environ.get("DATA_FILE", "/tmp/cab_data.json")

INITIAL_DATA = {"open_positions": {}, "closed_positions": []}

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

def now_tr():
    return (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M")

def now_tr_short():
    return (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%H:%M")

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
    if tp_type == "TP2":
        kalan_oran = 100 - kapat_oran - 25
    else:
        kalan_oran = 100 - kapat_oran
    return round(pos_size * (kalan_oran / 100.0) * (trail_px - giris) / giris, 2)

def is_recently_closed(ticker, n=20):
    return ticker in [c["ticker"] for c in data["closed_positions"][-n:]]

# ============ ROUTES ============
@app.get("/", response_class=HTMLResponse)
async def root():
    return f"<h3>🤖 CAB Bot v5 çalışıyor</h3><p>{'🟡 TEST MODU' if TEST_MODE else '🟢 CANLI MOD'}</p><p><a href='/dashboard'>Dashboard</a></p>"

@app.get("/ip")
async def get_ip():
    try:
        async with httpx.AsyncClient() as c:
            r = await c.get("https://api.ipify.org?format=json")
            return r.json()
    except Exception as e:
        return {"error": str(e)}

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
    """Dashboard JS için veri endpoint'i"""
    return JSONResponse(load_data())

@app.post("/api/clear_old")
async def api_clear_old(req: Request):
    """30+ günden eski kapanan pozisyonları temizle"""
    body = await req.json()
    days = int(body.get("days", 30))
    cutoff = (datetime.now(timezone.utc) + timedelta(hours=3)) - timedelta(days=days)
    cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M")
    before = len(data["closed_positions"])
    data["closed_positions"] = [c for c in data["closed_positions"] if c.get("kapanis", "") >= cutoff_str]
    after = len(data["closed_positions"])
    save_data(data)
    return {"removed": before - after, "remaining": after}

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
            "giris": float(kv.get("Giris", 0)),
            "stop": float(kv.get("Stop", 0)),
            "tp1": float(kv.get("TP1", 0)),
            "tp2": float(kv.get("TP2", 0)),
            "marj": float(kv.get("Marj", 0)),
            "lev": int(float(kv.get("Lev", 10))),
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
            "tp1": float(kv.get("TP1", 0)),
            "stop": float(kv.get("YeniStop", 0)),
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
            "tp2": float(kv.get("TP2", 0)),
            "tp2_kar": float(kv.get("TP2Kar", 0)),
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
                try:
                    kalan = int(tok.replace("%", ""))
                except:
                    pass
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

@app.post("/webhook")
async def webhook(req: Request):
    msg = (await req.body()).decode()
    print(f"[ALERT] {msg}")

    if msg.startswith("CAB v13 |"):
        parsed = parse_giris(msg)
        if not parsed:
            return {"status": "parse_error"}
        print(f"[PARSE] {parsed}")
        ticker = parsed["ticker"]

        if len(data["open_positions"]) >= MAX_POSITIONS:
            print(f"[LIMIT] Max {MAX_POSITIONS} — {ticker} atlandı")
            return {"status": "limit"}
        if ticker in data["open_positions"]:
            print(f"[DUP] {ticker} zaten açık")
            return {"status": "duplicate"}

        data["open_positions"][ticker] = {
            "giris": parsed["giris"], "stop": parsed["stop"],
            "tp1": parsed["tp1"], "tp2": parsed["tp2"],
            "marj": parsed["marj"], "lev": parsed["lev"],
            "risk": parsed["risk"], "kapat_oran": parsed["kapat_oran"],
            "atr_skor": parsed["atr_skor"], "durum": "Açık",
            "hh_pct": 0.0, "tp1_hit": False, "tp2_hit": False,
            "tp1_kar": 0.0, "tp2_kar": 0.0,
            "zaman": now_tr_short(), "zaman_full": now_tr(),
        }
        save_data(data)
        print(f"[TEST] GIRIS: {ticker} | {parsed['giris']} | Marj:{parsed['marj']}$ | {parsed['lev']}x")
        return {"status": "opened"}

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
                    print(f"[RECONCILE] TP1 geç: {ticker} +{tp1_kar}$ (toplam:{c['kar']}$)")
                    return {"status": "reconciled"}
            return {"status": "already_reconciled"}

        if ticker not in data["open_positions"]:
            print(f"[WARN] TP1: {ticker} yok")
            return {"status": "not_found"}

        pos = data["open_positions"][ticker]
        if parsed["tp1_kar"] == 0:
            parsed["tp1_kar"] = calc_tp1_kar(pos, parsed["tp1"])

        pos["tp1_hit"] = True
        pos["tp1_kar"] = parsed["tp1_kar"]
        pos["stop"] = parsed["stop"]
        pos["durum"] = "✓ TP1 Alındı"
        pos["kapat_oran"] = parsed["kapat_oran"]
        pos["tp1_zaman"] = now_tr()
        save_data(data)
        print(f"[TEST] TP1: {ticker} | BE:{parsed['stop']} | +{parsed['tp1_kar']}$")
        return {"status": "tp1"}

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
                    print(f"[RECONCILE] TP2 geç: {ticker} +{tp2_kar}$ (toplam:{c['kar']}$)")
                    return {"status": "reconciled"}
            return {"status": "already_reconciled"}

        if ticker not in data["open_positions"]:
            print(f"[WARN] TP2: {ticker} yok")
            return {"status": "not_found"}

        pos = data["open_positions"][ticker]
        if parsed["tp2_kar"] == 0:
            parsed["tp2_kar"] = calc_tp2_kar(pos, parsed["tp2"])

        pos["tp2_hit"] = True
        pos["tp2_kar"] = parsed["tp2_kar"]
        pos["durum"] = "✓✓ TP2 Alındı"
        save_data(data)
        print(f"[TEST] TP2: {ticker} | +{parsed['tp2_kar']}$")
        return {"status": "tp2"}

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
            print(f"[WARN] TRAIL: {ticker} açık değil")
            return {"status": "not_found"}

        pos = data["open_positions"][ticker]
        trail_kar = parsed["trail_kar"]
        if trail_kar == 0:
            trail_kar = calc_trail_kar(pos, parsed["trail_px"], parsed["tp_type"])

        tp1_kar = pos.get("tp1_kar", 0)
        tp2_kar = pos.get("tp2_kar", 0)

        if parsed["tp_type"] == "TP1" and not pos.get("tp1_hit"):
            print(f"[RACE] TRAIL-TP1 yarışı: {ticker}")
        if parsed["tp_type"] == "TP2" and not pos.get("tp2_hit"):
            print(f"[RACE] TRAIL-TP2 yarışı: {ticker}")

        total_kar = round(tp1_kar + tp2_kar + trail_kar, 2)
        sonuc = "★ TP1+TP2+Trail" if (parsed["tp_type"] == "TP2" or pos.get("tp2_hit")) else "≈ TP1+Trail"

        closed = {
            "ticker": ticker, "giris": pos["giris"], "marj": pos["marj"], "lev": pos["lev"],
            "sonuc": sonuc, "kar": total_kar,
            "tp1_kar": tp1_kar, "tp2_kar": tp2_kar, "trail_kar": trail_kar,
            "trail_px": parsed["trail_px"], "tp_type": parsed["tp_type"],
            "tp1_kar_added": pos.get("tp1_hit", False),
            "tp2_kar_added": pos.get("tp2_hit", False),
            "hh_pct": pos.get("hh_pct", 0),
            "atr_skor": pos.get("atr_skor", 1.0),
            "kapat_oran": pos.get("kapat_oran", 60),
            "acilis": pos.get("zaman_full", ""),
            "kapanis": now_tr(),
        }
        data["closed_positions"].append(closed)
        del data["open_positions"][ticker]
        save_data(data)
        print(f"[TEST] TRAIL({parsed['tp_type']}): {ticker} | {sonuc} | Toplam:+{total_kar}$")
        return {"status": "trail_closed"}

    elif msg.startswith("CAB v13 STOP"):
        parsed = parse_stop(msg)
        if not parsed:
            return {"status": "parse_error"}
        print(f"[PARSE] {parsed}")
        ticker = parsed["ticker"]

        if is_recently_closed(ticker):
            print(f"[WARN] STOP: {ticker} zaten kapalı")
            return {"status": "already_closed"}
        if ticker not in data["open_positions"]:
            print(f"[WARN] STOP: {ticker} açık değil")
            return {"status": "not_found"}

        pos = data["open_positions"][ticker]
        stop_px = parsed["stop"]
        giris = pos["giris"]
        lev = pos["lev"]
        marj = pos["marj"]
        pos_size = marj * lev

        tp1_kar = pos.get("tp1_kar", 0)
        tp2_kar = pos.get("tp2_kar", 0)
        kapat_oran = pos.get("kapat_oran", 60)

        if pos.get("tp2_hit"):
            kalan_oran = 100 - kapat_oran - 25
            stop_kar = pos_size * (kalan_oran / 100.0) * ((stop_px - giris) / giris)
            sonuc = "↓ TP2+Stop"
        elif pos.get("tp1_hit"):
            kalan_oran = 100 - kapat_oran
            stop_kar = pos_size * (kalan_oran / 100.0) * ((stop_px - giris) / giris)
            sonuc = "~ TP1+BE Stop"
        else:
            stop_kar = pos_size * ((stop_px - giris) / giris)
            sonuc = "✗ Stop"

        total_kar = round(tp1_kar + tp2_kar + stop_kar, 2)

        closed = {
            "ticker": ticker, "giris": giris, "marj": marj, "lev": lev,
            "sonuc": sonuc, "kar": total_kar,
            "tp1_kar": tp1_kar, "tp2_kar": tp2_kar,
            "trail_kar": round(stop_kar, 2), "trail_px": stop_px, "tp_type": "STOP",
            "tp1_kar_added": pos.get("tp1_hit", False),
            "tp2_kar_added": pos.get("tp2_hit", False),
            "hh_pct": pos.get("hh_pct", 0),
            "atr_skor": pos.get("atr_skor", 1.0),
            "kapat_oran": kapat_oran,
            "acilis": pos.get("zaman_full", ""),
            "kapanis": now_tr(),
        }
        data["closed_positions"].append(closed)
        del data["open_positions"][ticker]
        save_data(data)
        print(f"[TEST] STOP: {ticker} | {sonuc} | Toplam:{total_kar}$")
        return {"status": "stopped"}
    else:
        print(f"[UNKNOWN] {msg[:80]}")
        return {"status": "unknown"}


# ============ DASHBOARD ============
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    mod_badge = "🟡 TEST MODU" if TEST_MODE else "🟢 CANLI MOD"
    mod_text = "TEST" if TEST_MODE else "CANLI"

    html = f"""<!DOCTYPE html>
<html lang="tr"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CAB Bot v5 Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
* {{ box-sizing: border-box; }}
body {{ font-family: -apple-system,system-ui,sans-serif; background:#0f172a; color:#e5e7eb; margin:0; padding:10px; }}
h1 {{ font-size:18px; margin:0 0 4px; }}
.badge {{ display:inline-block; padding:4px 10px; background:#1e293b; border-radius:4px; font-size:12px; }}
.subtitle {{ color:#9ca3af; font-size:11px; margin:4px 0 10px; }}
.stats {{ display:flex; flex-wrap:wrap; gap:8px; margin:10px 0; }}
.stat {{ padding:10px 14px; background:#1e293b; border-radius:6px; min-width:85px; }}
.stat-val {{ font-size:16px; font-weight:bold; }}
.stat-lbl {{ font-size:10px; color:#9ca3af; margin-top:2px; }}
.bugun-box {{ background:#312e81; padding:6px 10px; border-radius:4px; margin:8px 0; font-size:12px; }}
.section {{ background:#1e293b; border-radius:6px; padding:10px; margin:10px 0; }}
.section-head {{ display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:8px; margin-bottom:8px; }}
.section-head h2 {{ font-size:14px; margin:0; }}
.toolbar {{ display:flex; gap:6px; flex-wrap:wrap; align-items:center; font-size:11px; }}
.toolbar label {{ color:#9ca3af; }}
.toolbar input, .toolbar select {{ background:#0f172a; color:#e5e7eb; border:1px solid #334155; padding:4px 8px; border-radius:4px; font-size:11px; }}
.toolbar input {{ width:120px; }}
.btn {{ background:#1e40af; color:#fff; border:none; padding:5px 10px; border-radius:4px; cursor:pointer; font-size:11px; font-weight:600; }}
.btn:hover {{ background:#2563eb; }}
.btn-alt {{ background:#059669; }}
.btn-alt:hover {{ background:#047857; }}
.btn-danger {{ background:#7c2d12; }}
.btn-danger:hover {{ background:#991b1b; }}
.btn-small {{ padding:3px 8px; font-size:10px; }}
table {{ width:100%; border-collapse:collapse; font-size:11px; }}
th {{ background:#334155; padding:6px 4px; text-align:left; position:sticky; top:0; cursor:pointer; user-select:none; white-space:nowrap; }}
th:hover {{ background:#475569; }}
th.sorted-asc::after {{ content:" ↑"; color:#fbbf24; }}
th.sorted-desc::after {{ content:" ↓"; color:#fbbf24; }}
td {{ padding:5px 4px; border-bottom:1px solid #334155; white-space:nowrap; }}
tr:hover {{ background:#334155; }}
.warn-row {{ background:rgba(239,68,68,0.08) !important; }}
.ozet {{ padding:6px 10px; background:#0f172a; border-radius:4px; margin:6px 0; font-size:12px; }}
small {{ color:#9ca3af; }}

/* Modal */
.modal-overlay {{ display:none; position:fixed; inset:0; background:rgba(0,0,0,0.7); z-index:1000; justify-content:center; align-items:center; padding:20px; }}
.modal-overlay.show {{ display:flex; }}
.modal {{ background:#1e293b; border-radius:8px; padding:20px; max-width:720px; width:100%; max-height:90vh; overflow-y:auto; }}
.modal-head {{ display:flex; justify-content:space-between; align-items:center; margin-bottom:14px; }}
.modal-head h2 {{ font-size:16px; margin:0; }}
.modal-close {{ background:#475569; color:#fff; border:none; border-radius:50%; width:28px; height:28px; cursor:pointer; font-size:16px; }}
.stats-grid {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(130px, 1fr)); gap:8px; margin-bottom:14px; }}
.stat-card {{ background:#0f172a; padding:10px; border-radius:6px; }}
.stat-card-lbl {{ font-size:10px; color:#9ca3af; text-transform:uppercase; }}
.stat-card-val {{ font-size:18px; font-weight:bold; margin-top:3px; }}
.chart-wrap {{ background:#0f172a; padding:10px; border-radius:6px; margin-bottom:10px; }}
.chart-wrap h3 {{ font-size:12px; margin:0 0 8px; color:#9ca3af; }}

/* Toast */
.toast {{ position:fixed; bottom:20px; right:20px; background:#059669; color:#fff; padding:10px 16px; border-radius:6px; font-weight:600; font-size:12px; opacity:0; transition:opacity 0.3s; z-index:2000; box-shadow:0 4px 12px rgba(0,0,0,0.4); }}
.toast.show {{ opacity:1; }}
.toast.err {{ background:#991b1b; }}

/* Mobile */
@media (max-width: 640px) {{
  .stat {{ min-width:70px; padding:8px 10px; }}
  .stat-val {{ font-size:14px; }}
  table {{ font-size:10px; }}
  td, th {{ padding:4px 3px; }}
  .toolbar input {{ width:100px; }}
}}
</style>
</head>
<body>

<h1>🤖 CAB Bot v5 Dashboard</h1>
<div>
  <span class="badge">{mod_badge}</span>
  <button class="btn btn-alt btn-small" onclick="requestNotif()">🔔 Bildirim İzni</button>
</div>
<div class="subtitle">⟳ Son güncelleme: <span id="lastUpdate">—</span> <small>(5sn fiyat / 20sn sayfa)</small></div>

<div class="stats" id="statsBar"></div>
<div class="bugun-box" id="bugunBox"></div>

<!-- AÇIK POZİSYONLAR -->
<div class="section">
  <div class="section-head">
    <h2>📊 Açık Pozisyonlar <small id="openCount"></small></h2>
    <div class="toolbar">
      <input type="text" id="searchOpen" placeholder="Coin ara..." oninput="renderOpen()">
      <button class="btn btn-small" onclick="exportCSV('open')">📥 CSV</button>
    </div>
  </div>
  <div id="warnBox" style="margin-bottom:8px;"></div>
  <div style="overflow-x:auto;">
    <table id="openTable">
      <thead><tr>
        <th data-sort="ticker">Coin</th>
        <th data-sort="marj">Marjin</th>
        <th data-sort="giris">Giriş</th>
        <th data-sort="px">Şu An</th>
        <th data-sort="tp1kalan">TP1'e Kalan</th>
        <th data-sort="pct">Durum</th>
        <th data-sort="hh_pct">HH%</th>
        <th data-sort="atr_skor">ATR/Kapat</th>
        <th data-sort="trail">Trailing</th>
        <th data-sort="stop">Stop</th>
        <th data-sort="tp1">TP1</th>
        <th data-sort="tp2">TP2</th>
        <th data-sort="zaman_full">Zaman</th>
      </tr></thead>
      <tbody id="openBody"><tr><td colspan="13" style="text-align:center;color:#9ca3af">Yükleniyor...</td></tr></tbody>
    </table>
  </div>
</div>

<!-- KAPANAN POZİSYONLAR -->
<div class="section">
  <div class="section-head">
    <h2>📋 Kapanan Pozisyonlar <small id="closedCount"></small></h2>
    <div class="toolbar">
      <label>Tarih:</label>
      <select id="filterDate" onchange="renderClosed()">
        <option value="all">Tüm Zamanlar</option>
        <option value="today" selected>Bugün</option>
        <option value="yesterday">Dün</option>
        <option value="7d">Son 7 Gün</option>
        <option value="30d">Son 30 Gün</option>
      </select>
      <label>Sonuç:</label>
      <select id="filterResult" onchange="renderClosed()">
        <option value="all">Hepsi</option>
        <option value="stop">✗ Stop</option>
        <option value="tp1trail">≈ TP1+Trail</option>
        <option value="tp2trail">★ TP1+TP2+Trail</option>
        <option value="be">~ TP1+BE Stop</option>
        <option value="tp2stop">↓ TP2+Stop</option>
      </select>
      <input type="text" id="searchClosed" placeholder="Coin ara..." oninput="renderClosed()">
      <button class="btn btn-small" onclick="showAnalysis()">📊 Analiz</button>
      <button class="btn btn-alt btn-small" onclick="exportCSV('closed')">📥 CSV</button>
      <button class="btn btn-danger btn-small" onclick="clearOld()">🗑 30g+ Sil</button>
    </div>
  </div>
  <div class="ozet" id="closedOzet"></div>
  <div style="overflow-x:auto;">
    <table id="closedTable">
      <thead><tr>
        <th data-sort="ticker">Coin</th>
        <th data-sort="marj">Marjin</th>
        <th data-sort="giris">Giriş</th>
        <th data-sort="sonuc">Sonuç</th>
        <th data-sort="kar">Kar/Zarar</th>
        <th data-sort="hh_pct">HH%</th>
        <th data-sort="atr_skor">ATR/Kapat</th>
        <th data-sort="sure_dk">Süre</th>
        <th data-sort="kapanis">Zaman</th>
      </tr></thead>
      <tbody id="closedBody"><tr><td colspan="9" style="text-align:center;color:#9ca3af">Yükleniyor...</td></tr></tbody>
    </table>
  </div>
</div>

<!-- ANALIZ MODAL -->
<div class="modal-overlay" id="analysisModal" onclick="if(event.target.id=='analysisModal')closeModal()">
  <div class="modal">
    <div class="modal-head">
      <h2>📊 Performans Analizi</h2>
      <button class="modal-close" onclick="closeModal()">×</button>
    </div>
    <div id="analysisBody"></div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
const MODE_TEXT = "{mod_text}";
let openPositions = {{}};
let closedPositions = [];
let openPrices = {{}};  // canlı fiyatlar
let sortState = {{ open: {{col:null, dir:'asc'}}, closed: {{col:'kapanis', dir:'desc'}} }};

// ============ API ============
async function loadData() {{
  try {{
    const r = await fetch('/api/data');
    const d = await r.json();
    openPositions = d.open_positions || {{}};
    closedPositions = d.closed_positions || [];
    renderAll();
  }} catch(e) {{
    console.error(e);
  }}
}}

async function fp(sym) {{
  try {{
    const r = await fetch('https://fapi.binance.com/fapi/v1/ticker/price?symbol=' + sym);
    if (!r.ok) return null;
    const j = await r.json();
    return parseFloat(j.price);
  }} catch(e) {{ return null; }}
}}

async function updatePrices() {{
  const syms = Object.keys(openPositions);
  const prevTpHit = {{}};
  for (const s of syms) prevTpHit[s] = openPositions[s].tp1_hit;

  for (const sym of syms) {{
    const px = await fp(sym);
    if (px === null) continue;
    openPrices[sym] = px;

    // HH güncelle
    try {{
      await fetch('/update_hh', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{ticker: sym, price: px}})
      }});
    }} catch(e) {{}}
  }}
  renderOpen();
  document.getElementById('lastUpdate').textContent = new Date().toTimeString().slice(0,8);
  setTimeout(updatePrices, 5000);
}}

// ============ HELPERS ============
function now_tr() {{
  const d = new Date();
  return d.toISOString().replace('T',' ').slice(0,16);
}}

function sureDk(c) {{
  try {{
    const a = new Date(c.acilis.replace(' ', 'T') + ':00+03:00');
    const k = new Date(c.kapanis.replace(' ', 'T') + ':00+03:00');
    return Math.round((k - a) / 60000);
  }} catch(e) {{ return 0; }}
}}

function sureFmt(dk) {{
  if (dk >= 1440) return `${{Math.floor(dk/1440)}}g ${{Math.floor((dk%1440)/60)}}s`;
  if (dk >= 60) return `${{Math.floor(dk/60)}}s ${{dk%60}}dk`;
  return `${{dk}}dk`;
}}

function fmt6(v) {{ return v != null ? v.toFixed(6) : '—'; }}
function fmt2(v) {{ return v != null ? v.toFixed(2) : '—'; }}
function fmt1(v) {{ return v != null ? v.toFixed(1) : '—'; }}

function dateMatches(kapanis, filter) {{
  if (filter === 'all') return true;
  const today = now_tr().slice(0,10);
  if (filter === 'today') return kapanis.startsWith(today);
  const d = new Date();
  if (filter === 'yesterday') {{
    d.setDate(d.getDate() - 1);
    const y = d.toISOString().slice(0,10);
    return kapanis.startsWith(y);
  }}
  if (filter === '7d') {{
    d.setDate(d.getDate() - 7);
    return kapanis >= d.toISOString().slice(0,16).replace('T',' ');
  }}
  if (filter === '30d') {{
    d.setDate(d.getDate() - 30);
    return kapanis >= d.toISOString().slice(0,16).replace('T',' ');
  }}
  return true;
}}

function resultMatches(sonuc, filter) {{
  if (filter === 'all') return true;
  if (filter === 'stop') return sonuc === '✗ Stop';
  if (filter === 'tp1trail') return sonuc.includes('TP1+Trail') && !sonuc.includes('TP2');
  if (filter === 'tp2trail') return sonuc.includes('TP1+TP2+Trail');
  if (filter === 'be') return sonuc.includes('BE Stop');
  if (filter === 'tp2stop') return sonuc.includes('TP2+Stop');
  return true;
}}

function openTV(ticker) {{
  const appUrl = 'tradingview://x-callback-url/chart?symbol=BINANCE:' + ticker + '.P';
  const webUrl = 'https://tr.tradingview.com/chart/?symbol=BINANCE:' + ticker + '.P';
  const t = Date.now();
  window.open(appUrl, '_blank');
  setTimeout(() => {{
    if (Date.now() - t < 1500) window.open(webUrl, '_blank');
  }}, 500);
}}

function toast(msg, err=false) {{
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'toast show' + (err ? ' err' : '');
  setTimeout(() => t.classList.remove('show'), 2500);
}}

// ============ RENDER ============
function renderAll() {{
  renderStats();
  renderOpen();
  renderClosed();
  checkWarnings();
}}

function renderStats() {{
  const toplamKar = closedPositions.reduce((s,c) => s + c.kar, 0);
  const toplamSay = closedPositions.length;
  const karliSay = closedPositions.filter(c => c.kar > 0).length;
  const winRate = toplamSay > 0 ? (karliSay / toplamSay * 100).toFixed(1) : 0;

  const today = now_tr().slice(0,10);
  const bugunku = closedPositions.filter(c => c.kapanis.startsWith(today));
  const bugunKar = bugunku.reduce((s,c) => s + c.kar, 0);
  const bugunTP = bugunku.filter(c => c.kar > 0).length;
  const bugunStop = bugunku.filter(c => c.kar <= 0).length;
  const bugunWR = bugunku.length > 0 ? (bugunTP / bugunku.length * 100).toFixed(1) : 0;

  const sureler = closedPositions.map(sureDk);
  const ortSure = sureler.length > 0 ? sureler.reduce((a,b) => a+b, 0) / sureler.length : 0;
  const ortSureStr = ortSure === 0 ? '—' : (ortSure >= 60 ? sureFmt(Math.round(ortSure)) : `${{Math.round(ortSure)}}dk`);

  const netRenk = toplamKar > 0 ? '#4ade80' : (toplamKar < 0 ? '#f87171' : '#e5e7eb');
  const wrRenk = winRate >= 50 ? '#4ade80' : (toplamSay > 0 ? '#f87171' : '#e5e7eb');
  const bugunKarRenk = bugunKar > 0 ? '#4ade80' : (bugunKar < 0 ? '#f87171' : '#e5e7eb');
  const bugunWrRenk = bugunWR >= 50 ? '#4ade80' : (bugunku.length > 0 ? '#f87171' : '#e5e7eb');

  document.getElementById('statsBar').innerHTML = `
    <div class="stat"><div class="stat-val">${{Object.keys(openPositions).length}}</div><div class="stat-lbl">Açık</div></div>
    <div class="stat"><div class="stat-val">${{toplamSay}}</div><div class="stat-lbl">Kapanan</div></div>
    <div class="stat"><div class="stat-val" style="color:${{netRenk}}">${{toplamKar >= 0 ? '+' : ''}}${{toplamKar.toFixed(1)}}$</div><div class="stat-lbl">Net Kar</div></div>
    <div class="stat"><div class="stat-val" style="color:${{wrRenk}}">${{winRate}}%</div><div class="stat-lbl">Win Rate</div></div>
    <div class="stat"><div class="stat-val" style="color:${{bugunWrRenk}}">${{bugunWR}}%</div><div class="stat-lbl">Bugün WR</div></div>
    <div class="stat"><div class="stat-val">${{ortSureStr}}</div><div class="stat-lbl">Ort. Süre</div></div>
    <div class="stat"><div class="stat-val">${{MODE_TEXT}}</div><div class="stat-lbl">Mod</div></div>
  `;

  document.getElementById('bugunBox').innerHTML = `📅 <b>Bugün (${{today}}):</b> &nbsp; ${{bugunku.length}} kapanan &nbsp;|&nbsp; ${{bugunTP}} TP &nbsp;|&nbsp; ${{bugunStop}} Stop &nbsp;|&nbsp; <span style="color:${{bugunKarRenk}}">${{bugunKar >= 0 ? '+' : ''}}${{bugunKar.toFixed(1)}}$</span>`;
}}

function renderOpen() {{
  const search = document.getElementById('searchOpen').value.toLowerCase();
  let rows = Object.entries(openPositions).filter(([t,p]) => t.toLowerCase().includes(search));

  // Her satıra hesaplanmış değerler ekle
  rows = rows.map(([t,p]) => {{
    const px = openPrices[t] || null;
    const giris = p.giris;
    const pct = px ? (px - giris) / giris * 100 : null;
    const tp1kalan = px ? (p.tp1 - px) / px * 100 : null;
    const posSize = p.marj * p.lev;
    return {{
      ticker: t, pos: p, px,
      pct, tp1kalan,
      marj: p.marj, giris, stop: p.stop, tp1: p.tp1, tp2: p.tp2,
      hh_pct: p.hh_pct || 0, atr_skor: p.atr_skor || 1.0,
      trail: p.tp1_hit ? (p.tp1_kar || 0) : -999,
      zaman_full: p.zaman_full || ''
    }};
  }});

  // Sıralama
  const s = sortState.open;
  if (s.col) {{
    rows.sort((a,b) => {{
      let av = a[s.col], bv = b[s.col];
      if (av == null) av = -Infinity;
      if (bv == null) bv = -Infinity;
      if (typeof av === 'string') return s.dir === 'asc' ? av.localeCompare(bv) : bv.localeCompare(av);
      return s.dir === 'asc' ? av - bv : bv - av;
    }});
  }}

  // HTML üret
  const body = document.getElementById('openBody');
  if (rows.length === 0) {{
    body.innerHTML = '<tr><td colspan="13" style="text-align:center;color:#9ca3af">Açık pozisyon yok</td></tr>';
  }} else {{
    body.innerHTML = rows.map(r => {{
      const p = r.pos;
      const t = r.ticker;
      const posSize = p.marj * p.lev;
      const stopZarar = posSize * (p.stop - p.giris) / p.giris;
      const kapat = p.kapat_oran || 60;
      const tp1KarH = posSize * (kapat/100) * (p.tp1 - p.giris) / p.giris;
      const tp2KarH = posSize * 0.25 * (p.tp2 - p.giris) / p.giris;

      const pxStr = r.px ? r.px.toFixed(6) : '—';
      let pxColor = '#e5e7eb';
      let pctStr = '—';
      if (r.px) {{
        const profit = posSize * r.pct / 100;
        if (r.pct > 0) {{
          pxColor = '#4ade80';
          pctStr = `<span style="color:#4ade80">▲ +${{profit.toFixed(1)}}$ (+${{r.pct.toFixed(2)}}%)</span>`;
        }} else {{
          pxColor = '#f87171';
          pctStr = `<span style="color:#f87171">▼ ${{profit.toFixed(1)}}$ (${{r.pct.toFixed(2)}}%)</span>`;
        }}
      }}

      const tp1KalanStr = r.tp1kalan != null ? (r.tp1kalan <= 0 ? '<span style="color:#4ade80">✓ Geçildi</span>' : `%${{r.tp1kalan.toFixed(2)}} uzak`) : '—';

      let trailStr = '—';
      if (p.tp2_hit) trailStr = `✓ TP2+ (+${{((p.tp1_kar||0)+(p.tp2_kar||0)).toFixed(1)}}$)`;
      else if (p.tp1_hit) trailStr = `✓ TP1+ (+${{(p.tp1_kar||0).toFixed(1)}}$)`;

      let rowBg = '';
      if (p.tp2_hit) rowBg = 'background:rgba(20,184,166,0.15);border-left:3px solid #14b8a6;';
      else if (p.tp1_hit) rowBg = 'background:rgba(132,204,22,0.12);border-left:3px solid #84cc16;';

      // Süre uyarısı (6+ saat açık)
      let warnCls = '';
      try {{
        const ac = new Date(p.zaman_full.replace(' ','T') + ':00+03:00');
        const openDk = (Date.now() - ac.getTime()) / 60000;
        if (openDk > 360) warnCls = 'warn-row'; // 6 saat
      }} catch(e) {{}}

      const hh_disp = (p.hh_pct || 0) > 0 ? `%${{p.hh_pct.toFixed(2)}}` : '—';
      const atr_disp = `${{(p.atr_skor||1.0).toFixed(2)}}x (${{kapat}}%)`;

      return `<tr class="${{warnCls}}" style="${{rowBg}}">
        <td><a href="javascript:void(0)" onclick="openTV('${{t}}')" style="color:#60a5fa;text-decoration:none;">${{t}}</a> 🔗</td>
        <td>${{p.marj.toFixed(0)}}$ <small>(${{p.lev}}x)</small></td>
        <td>${{p.giris.toFixed(6)}}</td>
        <td style="color:${{pxColor}}">${{pxStr}}</td>
        <td>${{tp1KalanStr}}</td>
        <td>${{p.durum && p.durum.includes('TP') ? p.durum : pctStr}}</td>
        <td>${{hh_disp}}</td>
        <td>${{atr_disp}}</td>
        <td>${{trailStr}}</td>
        <td>${{p.stop.toFixed(6)}} <small style="color:#f87171">(${{stopZarar >= 0 ? '+' : ''}}${{stopZarar.toFixed(1)}}$)</small></td>
        <td>${{p.tp1.toFixed(6)}} <small style="color:#4ade80">(+${{tp1KarH.toFixed(1)}}$)</small></td>
        <td>${{p.tp2.toFixed(6)}} <small style="color:#4ade80">(+${{tp2KarH.toFixed(1)}}$)</small></td>
        <td>${{p.zaman || ''}}</td>
      </tr>`;
    }}).join('');
  }}

  document.getElementById('openCount').textContent = `(${{rows.length}})`;
  updateSortArrows('open');
}}

function renderClosed() {{
  const dateF = document.getElementById('filterDate').value;
  const resF = document.getElementById('filterResult').value;
  const search = document.getElementById('searchClosed').value.toLowerCase();

  let rows = closedPositions.filter(c =>
    dateMatches(c.kapanis, dateF) &&
    resultMatches(c.sonuc, resF) &&
    c.ticker.toLowerCase().includes(search)
  );
  rows = rows.map(c => ({{...c, sure_dk: sureDk(c)}}));

  const s = sortState.closed;
  if (s.col) {{
    rows.sort((a,b) => {{
      let av = a[s.col], bv = b[s.col];
      if (av == null) av = -Infinity;
      if (bv == null) bv = -Infinity;
      if (typeof av === 'string') return s.dir === 'asc' ? av.localeCompare(bv) : bv.localeCompare(av);
      return s.dir === 'asc' ? av - bv : bv - av;
    }});
  }}

  const body = document.getElementById('closedBody');
  if (rows.length === 0) {{
    body.innerHTML = '<tr><td colspan="9" style="text-align:center;color:#9ca3af">Filtre sonucu boş</td></tr>';
  }} else {{
    body.innerHTML = rows.map(c => {{
      const renk = c.kar > 0 ? '#4ade80' : '#f87171';
      const karStr = (c.kar >= 0 ? '+' : '') + c.kar.toFixed(1) + '$';
      const hh = (c.hh_pct || 0) > 0 ? `%${{c.hh_pct.toFixed(2)}}` : '—';
      const atr = `${{(c.atr_skor||1.0).toFixed(2)}}x/${{c.kapat_oran||60}}%`;
      const sure = sureFmt(c.sure_dk);
      const zaman = (c.acilis.slice(5) + '→' + c.kapanis.slice(11));

      // HH uyarı
      let warnCls = '';
      if (c.sonuc === '✗ Stop' && (c.hh_pct || 0) >= 5) warnCls = 'warn-row';

      return `<tr class="${{warnCls}}">
        <td><a href="javascript:void(0)" onclick="openTV('${{c.ticker}}')" style="color:#60a5fa;text-decoration:none;">${{c.ticker}}</a> 🔗</td>
        <td>${{c.marj.toFixed(0)}}$</td>
        <td>${{c.giris.toFixed(6)}}</td>
        <td style="color:${{renk}}">${{c.sonuc}}</td>
        <td style="color:${{renk}};font-weight:bold">${{karStr}}</td>
        <td>${{hh}}</td>
        <td>${{atr}}</td>
        <td>${{sure}}</td>
        <td>${{zaman}}</td>
      </tr>`;
    }}).join('');
  }}

  // Özet
  const toplamK = rows.filter(c => c.kar > 0).reduce((s,c) => s + c.kar, 0);
  const toplamZ = rows.filter(c => c.kar < 0).reduce((s,c) => s + c.kar, 0);
  const net = toplamK + toplamZ;
  const netRenk = net > 0 ? '#4ade80' : (net < 0 ? '#f87171' : '#e5e7eb');
  document.getElementById('closedOzet').innerHTML = rows.length > 0
    ? `<b>${{rows.length}}</b> pozisyon &nbsp;|&nbsp; <span style="color:#4ade80">Kar: +${{toplamK.toFixed(1)}}$</span> &nbsp;|&nbsp; <span style="color:#f87171">Zarar: ${{toplamZ.toFixed(1)}}$</span> &nbsp;|&nbsp; <span style="color:${{netRenk}}">NET: ${{net >= 0 ? '+' : ''}}${{net.toFixed(1)}}$</span>`
    : 'Filtreyle eşleşen pozisyon yok';

  document.getElementById('closedCount').textContent = `(${{rows.length}}/${{closedPositions.length}})`;
  updateSortArrows('closed');
}}

function checkWarnings() {{
  const warns = [];
  // 6+ saat açık olan pozisyonlar
  for (const [t, p] of Object.entries(openPositions)) {{
    try {{
      const ac = new Date(p.zaman_full.replace(' ','T') + ':00+03:00');
      const openDk = (Date.now() - ac.getTime()) / 60000;
      if (openDk > 360) warns.push(`⏰ <b>${{t}}</b> ${{sureFmt(Math.round(openDk))}}'dir açık`);
    }} catch(e) {{}}
  }}

  // Son 5 pozisyon üst üste stop
  const son5 = closedPositions.slice(-5);
  if (son5.length === 5 && son5.every(c => c.sonuc === '✗ Stop')) {{
    warns.push('🚨 <b>Son 5 pozisyon üst üste STOP!</b> Piyasa riskli, dikkatli ol');
  }}

  // HH%5+ olduğu halde stop yiyen son 3 pozisyon
  const tehlike = closedPositions.slice(-10).filter(c => c.sonuc === '✗ Stop' && (c.hh_pct || 0) >= 5);
  if (tehlike.length >= 3) {{
    warns.push(`⚠️ <b>${{tehlike.length}} pozisyon</b> TP1'e yaklaştıktan sonra stop yedi — trailing/stop ayarı incelenebilir`);
  }}

  const box = document.getElementById('warnBox');
  if (warns.length > 0) {{
    box.innerHTML = warns.map(w => `<div style="background:rgba(239,68,68,0.12);border-left:3px solid #f87171;padding:6px 10px;margin-bottom:4px;border-radius:3px;font-size:11px;">${{w}}</div>`).join('');
  }} else {{
    box.innerHTML = '';
  }}
}}

// ============ SORTING ============
function setupSort() {{
  document.querySelectorAll('#openTable th[data-sort]').forEach(th => {{
    th.onclick = () => {{
      const col = th.dataset.sort;
      if (sortState.open.col === col) sortState.open.dir = sortState.open.dir === 'asc' ? 'desc' : 'asc';
      else {{ sortState.open.col = col; sortState.open.dir = 'desc'; }}
      renderOpen();
    }};
  }});
  document.querySelectorAll('#closedTable th[data-sort]').forEach(th => {{
    th.onclick = () => {{
      const col = th.dataset.sort;
      if (sortState.closed.col === col) sortState.closed.dir = sortState.closed.dir === 'asc' ? 'desc' : 'asc';
      else {{ sortState.closed.col = col; sortState.closed.dir = 'desc'; }}
      renderClosed();
    }};
  }});
}}

function updateSortArrows(which) {{
  const tableId = which === 'open' ? 'openTable' : 'closedTable';
  const s = sortState[which];
  document.querySelectorAll(`#${{tableId}} th`).forEach(th => {{
    th.classList.remove('sorted-asc','sorted-desc');
    if (th.dataset.sort === s.col) th.classList.add('sorted-' + s.dir);
  }});
}}

// ============ ANALIZ ============
function showAnalysis() {{
  const filtered = closedPositions.filter(c => {{
    return dateMatches(c.kapanis, document.getElementById('filterDate').value) &&
           resultMatches(c.sonuc, document.getElementById('filterResult').value);
  }});

  if (filtered.length === 0) {{
    toast('Analiz için veri yok', true);
    return;
  }}

  const wins = filtered.filter(c => c.kar > 0);
  const losses = filtered.filter(c => c.kar < 0);
  const toplamKar = filtered.reduce((s,c) => s + c.kar, 0);
  const wr = (wins.length / filtered.length * 100).toFixed(1);
  const avgWin = wins.length > 0 ? wins.reduce((s,c) => s + c.kar, 0) / wins.length : 0;
  const avgLoss = losses.length > 0 ? losses.reduce((s,c) => s + c.kar, 0) / losses.length : 0;
  const maxWin = wins.length > 0 ? Math.max(...wins.map(c => c.kar)) : 0;
  const maxLoss = losses.length > 0 ? Math.min(...losses.map(c => c.kar)) : 0;
  const pf = losses.length > 0 && Math.abs(avgLoss) > 0 ? (wins.reduce((s,c)=>s+c.kar,0) / Math.abs(losses.reduce((s,c)=>s+c.kar,0))) : 0;
  const ev = toplamKar / filtered.length;

  // Max Drawdown
  let peak = 0, dd = 0, maxDD = 0, cum = 0;
  for (const c of filtered) {{
    cum += c.kar;
    if (cum > peak) peak = cum;
    dd = cum - peak;
    if (dd < maxDD) maxDD = dd;
  }}

  // Saat bazlı win rate
  const byHour = {{}};
  for (const c of filtered) {{
    const h = c.acilis.slice(11,13);
    if (!byHour[h]) byHour[h] = {{w:0, total:0}};
    byHour[h].total++;
    if (c.kar > 0) byHour[h].w++;
  }}
  const hourWR = Object.entries(byHour).filter(([h,v]) => v.total >= 2).map(([h,v]) => ({{h, wr: v.w/v.total*100, total:v.total}})).sort((a,b) => b.wr - a.wr);
  const bestHour = hourWR.length > 0 ? `${{hourWR[0].h}}:00 (%${{hourWR[0].wr.toFixed(0)}} - ${{hourWR[0].total}} işlem)` : '—';

  // Sonuç dağılımı
  const dist = {{}};
  for (const c of filtered) dist[c.sonuc] = (dist[c.sonuc] || 0) + 1;

  const body = document.getElementById('analysisBody');
  body.innerHTML = `
    <div class="stats-grid">
      <div class="stat-card"><div class="stat-card-lbl">Toplam</div><div class="stat-card-val">${{filtered.length}}</div></div>
      <div class="stat-card"><div class="stat-card-lbl">Win Rate</div><div class="stat-card-val" style="color:${{wr>=50?'#4ade80':'#f87171'}}">${{wr}}%</div></div>
      <div class="stat-card"><div class="stat-card-lbl">Net Kar</div><div class="stat-card-val" style="color:${{toplamKar>0?'#4ade80':'#f87171'}}">${{toplamKar>=0?'+':''}}${{toplamKar.toFixed(1)}}$</div></div>
      <div class="stat-card"><div class="stat-card-lbl">Avg Win</div><div class="stat-card-val" style="color:#4ade80">+${{avgWin.toFixed(1)}}$</div></div>
      <div class="stat-card"><div class="stat-card-lbl">Avg Loss</div><div class="stat-card-val" style="color:#f87171">${{avgLoss.toFixed(1)}}$</div></div>
      <div class="stat-card"><div class="stat-card-lbl">Max Win</div><div class="stat-card-val" style="color:#4ade80">+${{maxWin.toFixed(1)}}$</div></div>
      <div class="stat-card"><div class="stat-card-lbl">Max Loss</div><div class="stat-card-val" style="color:#f87171">${{maxLoss.toFixed(1)}}$</div></div>
      <div class="stat-card"><div class="stat-card-lbl">Profit Factor</div><div class="stat-card-val" style="color:${{pf>=1?'#4ade80':'#f87171'}}">${{pf.toFixed(2)}}</div></div>
      <div class="stat-card"><div class="stat-card-lbl">Beklenen Değer</div><div class="stat-card-val" style="color:${{ev>0?'#4ade80':'#f87171'}}">${{ev>=0?'+':''}}${{ev.toFixed(2)}}$/işlem</div></div>
      <div class="stat-card"><div class="stat-card-lbl">Max Drawdown</div><div class="stat-card-val" style="color:#f87171">${{maxDD.toFixed(1)}}$</div></div>
      <div class="stat-card"><div class="stat-card-lbl">En İyi Saat</div><div class="stat-card-val" style="font-size:12px">${{bestHour}}</div></div>
    </div>

    <div class="chart-wrap">
      <h3>Sonuç Dağılımı</h3>
      <div style="max-width:280px;margin:0 auto;"><canvas id="distChart"></canvas></div>
    </div>

    <div class="chart-wrap">
      <h3>Kümülatif Kar/Zarar Eğrisi</h3>
      <canvas id="cumChart" style="max-height:200px;"></canvas>
    </div>

    <div class="chart-wrap">
      <h3>Saat Bazlı Win Rate (2+ işlem)</h3>
      <canvas id="hourChart" style="max-height:200px;"></canvas>
    </div>
  `;

  document.getElementById('analysisModal').classList.add('show');

  // Chartları sonra çiz
  setTimeout(() => {{
    // Pie
    new Chart(document.getElementById('distChart'), {{
      type: 'doughnut',
      data: {{
        labels: Object.keys(dist),
        datasets: [{{
          data: Object.values(dist),
          backgroundColor: Object.keys(dist).map(k => {{
            if (k.includes('TP2')) return '#14b8a6';
            if (k.includes('TP1+Trail')) return '#84cc16';
            if (k.includes('BE')) return '#fbbf24';
            return '#f87171';
          }})
        }}]
      }},
      options: {{
        plugins: {{ legend: {{ labels: {{ color: '#e5e7eb', font: {{size:11}} }} }} }},
        responsive: true
      }}
    }});

    // Cumulative
    let cum2 = 0;
    const cumData = filtered.slice().sort((a,b) => a.kapanis.localeCompare(b.kapanis)).map(c => {{
      cum2 += c.kar;
      return cum2;
    }});
    new Chart(document.getElementById('cumChart'), {{
      type: 'line',
      data: {{
        labels: cumData.map((_,i) => i+1),
        datasets: [{{
          label: 'Net Kar ($)',
          data: cumData,
          borderColor: cumData[cumData.length-1] > 0 ? '#4ade80' : '#f87171',
          backgroundColor: cumData[cumData.length-1] > 0 ? 'rgba(74,222,128,0.1)' : 'rgba(248,113,113,0.1)',
          fill: true,
          tension: 0.2
        }}]
      }},
      options: {{
        plugins: {{ legend: {{ labels: {{ color: '#e5e7eb' }} }} }},
        scales: {{
          x: {{ ticks: {{ color: '#9ca3af' }}, grid: {{ color: '#334155' }} }},
          y: {{ ticks: {{ color: '#9ca3af' }}, grid: {{ color: '#334155' }} }}
        }}
      }}
    }});

    // Hour
    if (hourWR.length > 0) {{
      const sortedByHour = hourWR.slice().sort((a,b) => a.h.localeCompare(b.h));
      new Chart(document.getElementById('hourChart'), {{
        type: 'bar',
        data: {{
          labels: sortedByHour.map(h => h.h + ':00'),
          datasets: [{{
            label: 'Win Rate %',
            data: sortedByHour.map(h => h.wr.toFixed(0)),
            backgroundColor: sortedByHour.map(h => h.wr >= 50 ? '#4ade80' : '#f87171')
          }}]
        }},
        options: {{
          plugins: {{ legend: {{ labels: {{ color: '#e5e7eb' }} }} }},
          scales: {{
            x: {{ ticks: {{ color: '#9ca3af' }}, grid: {{ color: '#334155' }} }},
            y: {{ ticks: {{ color: '#9ca3af' }}, grid: {{ color: '#334155' }}, max: 100 }}
          }}
        }}
      }});
    }}
  }}, 100);
}}

function closeModal() {{
  document.getElementById('analysisModal').classList.remove('show');
}}

// ============ CSV EXPORT ============
function exportCSV(type) {{
  let rows, filename;
  if (type === 'open') {{
    rows = Object.entries(openPositions).map(([t,p]) => ({{
      Coin:t, Marjin:p.marj, Kaldirac:p.lev, Giris:p.giris, Stop:p.stop, TP1:p.tp1, TP2:p.tp2,
      HH:p.hh_pct||0, ATR:p.atr_skor||1.0, Kapat:p.kapat_oran||60, Durum:p.durum||'',
      TP1Hit:p.tp1_hit?'Evet':'Hayir', TP2Hit:p.tp2_hit?'Evet':'Hayir', Zaman:p.zaman_full||''
    }}));
    filename = 'cab_acik_' + now_tr().slice(0,10) + '.csv';
  }} else {{
    rows = closedPositions.map(c => ({{
      Coin:c.ticker, Marjin:c.marj, Kaldirac:c.lev||10, Giris:c.giris, Sonuc:c.sonuc, Kar:c.kar,
      TP1Kar:c.tp1_kar||0, TP2Kar:c.tp2_kar||0, TrailKar:c.trail_kar||0,
      HH:c.hh_pct||0, ATR:c.atr_skor||1.0, Kapat:c.kapat_oran||60,
      Acilis:c.acilis, Kapanis:c.kapanis, SureDk:sureDk(c)
    }}));
    filename = 'cab_kapanan_' + now_tr().slice(0,10) + '.csv';
  }}
  if (rows.length === 0) {{ toast('Veri yok', true); return; }}

  const headers = Object.keys(rows[0]);
  const csv = [headers.join(',')].concat(rows.map(r => headers.map(h => {{
    const v = r[h];
    return typeof v === 'string' && v.includes(',') ? `"${{v}}"` : v;
  }}).join(','))).join('\\n');

  const blob = new Blob(['\\ufeff' + csv], {{ type: 'text/csv;charset=utf-8' }});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = filename; a.click();
  URL.revokeObjectURL(url);
  toast('✓ ' + filename + ' indirildi');
}}

// ============ CLEAR OLD ============
async function clearOld() {{
  if (!confirm('30 günden eski kapanan pozisyonları silmek istiyor musun?')) return;
  try {{
    const r = await fetch('/api/clear_old', {{
      method:'POST', headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify({{days:30}})
    }});
    const j = await r.json();
    toast(`✓ ${{j.removed}} kayıt silindi, ${{j.remaining}} kaldı`);
    loadData();
  }} catch(e) {{
    toast('Silme hatası', true);
  }}
}}

// ============ NOTIFICATIONS ============
function requestNotif() {{
  if (!('Notification' in window)) {{ toast('Tarayıcı bildirim desteklemiyor', true); return; }}
  Notification.requestPermission().then(p => {{
    if (p === 'granted') {{
      toast('✓ Bildirimler açık');
      new Notification('CAB Bot', {{ body:'Bildirimler aktif! TP1/TP2/STOP olaylarında haberdar olacaksın.', icon:'https://tr.tradingview.com/favicon.ico' }});
    }} else toast('Bildirim izni reddedildi', true);
  }});
}}

// ============ CHANGE DETECTION ============
let lastOpenState = {{}};
function detectChanges() {{
  for (const [t, p] of Object.entries(openPositions)) {{
    const prev = lastOpenState[t];
    if (prev) {{
      if (p.tp1_hit && !prev.tp1_hit) notify('🎯 TP1 Vurdu!', `${{t}} TP1'e ulaştı. Stop BE'ye çekildi. +${{(p.tp1_kar||0).toFixed(1)}}$`);
      if (p.tp2_hit && !prev.tp2_hit) notify('🎯🎯 TP2 Vurdu!', `${{t}} TP2'ye ulaştı! +${{(p.tp2_kar||0).toFixed(1)}}$`);
    }} else if (!prev && Object.keys(lastOpenState).length > 0) {{
      notify('🆕 Yeni Pozisyon', `${{t}} açıldı. Giriş: ${{p.giris.toFixed(6)}}`);
    }}
  }}
  // Kapananlar
  const prevOpenTickers = Object.keys(lastOpenState);
  const nowOpenTickers = Object.keys(openPositions);
  for (const t of prevOpenTickers) {{
    if (!nowOpenTickers.includes(t)) {{
      const closed = closedPositions.find(c => c.ticker === t && c.kapanis.startsWith(now_tr().slice(0,10)));
      if (closed) {{
        if (closed.kar > 0) notify('✓ Kapandı (KAR)', `${{t}}: ${{closed.sonuc}} | +${{closed.kar.toFixed(1)}}$`);
        else notify('✗ Kapandı (ZARAR)', `${{t}}: ${{closed.sonuc}} | ${{closed.kar.toFixed(1)}}$`);
      }}
    }}
  }}
  lastOpenState = JSON.parse(JSON.stringify(openPositions));
}}

function notify(title, body) {{
  if ('Notification' in window && Notification.permission === 'granted') {{
    try {{ new Notification(title, {{ body, icon: 'https://tr.tradingview.com/favicon.ico' }}); }} catch(e) {{}}
  }}
}}

// ============ INIT ============
async function init() {{
  setupSort();
  await loadData();
  lastOpenState = JSON.parse(JSON.stringify(openPositions));
  if (Object.keys(openPositions).length > 0) {{
    updatePrices();
  }} else {{
    // pozisyon yoksa sadece veriyi yenile
    setInterval(async () => {{
      await loadData();
      detectChanges();
      document.getElementById('lastUpdate').textContent = new Date().toTimeString().slice(0,8);
    }}, 10000);
  }}
  // Full reload 20 sn
  setTimeout(() => location.reload(), 20000);
}}

// Her veri yüklemesinde değişiklik kontrolü
const origLoadData = loadData;
loadData = async function() {{
  await origLoadData();
  detectChanges();
}};

init();
</script>
</body></html>"""
    return html
