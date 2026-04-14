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

MAX_POSITIONS = int(os.environ.get("MAX_POSITIONS", "999"))
DATA_FILE = os.environ.get("DATA_FILE", "/tmp/cab_data.json")

# ============ BAŞLANGIÇ VERİSİ v4 — TEMİZ ============
INITIAL_DATA = {
    "open_positions": {},
    "closed_positions": []
}
# =====================================================

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

@app.get("/", response_class=HTMLResponse)
async def root():
    return f"<h3>🤖 CAB Bot v4 çalışıyor</h3><p>{'🟡 TEST MODU' if TEST_MODE else '🟢 CANLI MOD'}</p><p><a href='/dashboard'>Dashboard</a></p>"

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
            "type": "GIRIS",
            "ticker": ticker,
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
            "type": "TP1",
            "ticker": ticker,
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
            "type": "TP2",
            "ticker": ticker,
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
            "type": "TRAIL",
            "ticker": ticker,
            "kalan": kalan,
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
            "type": "STOP",
            "ticker": ticker,
            "aciklama": aciklama,
            "stop": float(kv.get("Stop", 0)),
        }
    except Exception as e:
        print(f"[PARSE ERR STOP] {e}")
        return None


@app.post("/webhook")
async def webhook(req: Request):
    msg = (await req.body()).decode()
    print(f"[ALERT] {msg}")

    # === GIRIS ===
    if msg.startswith("CAB v13 |"):
        parsed = parse_giris(msg)
        if not parsed:
            return {"status": "parse_error"}
        print(f"[PARSE] {parsed}")
        ticker = parsed["ticker"]

        if len(data["open_positions"]) >= MAX_POSITIONS:
            print(f"[LIMIT] Max {MAX_POSITIONS} pozisyon — {ticker} atlandı")
            return {"status": "limit"}

        if ticker in data["open_positions"]:
            print(f"[DUP] {ticker} zaten açık — atlandı")
            return {"status": "duplicate"}

        data["open_positions"][ticker] = {
            "giris": parsed["giris"],
            "stop": parsed["stop"],
            "tp1": parsed["tp1"],
            "tp2": parsed["tp2"],
            "marj": parsed["marj"],
            "lev": parsed["lev"],
            "risk": parsed["risk"],
            "kapat_oran": parsed["kapat_oran"],
            "atr_skor": parsed["atr_skor"],
            "durum": "Açık",
            "hh_pct": 0.0,
            "tp1_hit": False,
            "tp2_hit": False,
            "tp1_kar": 0.0,
            "tp2_kar": 0.0,
            "zaman": now_tr_short(),
            "zaman_full": now_tr(),
        }
        save_data(data)
        print(f"[TEST] GIRIS: {ticker} | {parsed['giris']} | Stop:{parsed['stop']} | TP1:{parsed['tp1']} | Marj:{parsed['marj']}$ | {parsed['lev']}x | Kapat:%{parsed['kapat_oran']}")
        return {"status": "opened"}

    # === TP1 ===
    elif msg.startswith("CAB v13 TP1"):
        parsed = parse_tp1(msg)
        if not parsed:
            return {"status": "parse_error"}
        print(f"[PARSE] {parsed}")
        ticker = parsed["ticker"]

        # TRAIL önce gelmiş olabilir → closed_positions'da karı güncelle
        if is_recently_closed(ticker) and ticker not in data["open_positions"]:
            for c in reversed(data["closed_positions"][-20:]):
                if c["ticker"] == ticker and not c.get("tp1_kar_added"):
                    pos_size = c["marj"] * c.get("lev", 10)
                    tp1_kar = round(pos_size * (parsed["kapat_oran"] / 100.0) * (parsed["tp1"] - c["giris"]) / c["giris"], 2)
                    c["kar"] = round(c["kar"] + tp1_kar, 2)
                    c["tp1_kar_added"] = True
                    c["tp1_kar"] = tp1_kar
                    save_data(data)
                    print(f"[RECONCILE] TP1 geç geldi, {ticker} kapanan kayda +{tp1_kar}$ eklendi (toplam: {c['kar']}$)")
                    return {"status": "reconciled"}
            return {"status": "already_reconciled"}

        if ticker not in data["open_positions"]:
            print(f"[WARN] TP1 geldi ama {ticker} ne açık ne kapalı — yoksayıldı")
            return {"status": "not_found"}

        pos = data["open_positions"][ticker]

        # Pine TP1Kar göndermediyse kendimiz hesapla
        if parsed["tp1_kar"] == 0:
            parsed["tp1_kar"] = calc_tp1_kar(pos, parsed["tp1"])

        pos["tp1_hit"] = True
        pos["tp1_kar"] = parsed["tp1_kar"]
        pos["stop"] = parsed["stop"]
        pos["durum"] = "✓ TP1 Alındı"
        pos["kapat_oran"] = parsed["kapat_oran"]
        save_data(data)
        print(f"[TEST] TP1: {ticker} | BE:{parsed['stop']} | Kapat:%{parsed['kapat_oran']} | TP1Kar:+{parsed['tp1_kar']}$")
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
                    print(f"[RECONCILE] TP2 geç geldi, {ticker} kapanan kayda +{tp2_kar}$ eklendi (toplam: {c['kar']}$)")
                    return {"status": "reconciled"}
            return {"status": "already_reconciled"}

        if ticker not in data["open_positions"]:
            print(f"[WARN] TP2 geldi ama {ticker} ne açık ne kapalı — yoksayıldı")
            return {"status": "not_found"}

        pos = data["open_positions"][ticker]

        if parsed["tp2_kar"] == 0:
            parsed["tp2_kar"] = calc_tp2_kar(pos, parsed["tp2"])

        pos["tp2_hit"] = True
        pos["tp2_kar"] = parsed["tp2_kar"]
        pos["durum"] = "✓✓ TP2 Alındı"
        save_data(data)
        print(f"[TEST] TP2: {ticker} | TP2Kar:+{parsed['tp2_kar']}$")
        return {"status": "tp2"}

    # === TRAIL ===
    elif msg.startswith("CAB v13 TRAIL"):
        parsed = parse_trail(msg)
        if not parsed:
            return {"status": "parse_error"}
        print(f"[PARSE] {parsed}")
        ticker = parsed["ticker"]

        if is_recently_closed(ticker):
            print(f"[WARN] TRAIL geldi ama {ticker} zaten kapalı — yoksayıldı")
            return {"status": "already_closed"}

        if ticker not in data["open_positions"]:
            print(f"[WARN] TRAIL geldi ama {ticker} açık değil — atlandı")
            return {"status": "not_found"}

        pos = data["open_positions"][ticker]

        trail_kar = parsed["trail_kar"]
        if trail_kar == 0:
            trail_kar = calc_trail_kar(pos, parsed["trail_px"], parsed["tp_type"])

        tp1_kar = pos.get("tp1_kar", 0)
        tp2_kar = pos.get("tp2_kar", 0)

        # Yarış durumu uyarısı
        if parsed["tp_type"] == "TP1" and not pos.get("tp1_hit"):
            print(f"[RACE] TRAIL-TP1 yarışı: {ticker} TP1 henüz gelmedi, geç gelirse reconcile")
        if parsed["tp_type"] == "TP2" and not pos.get("tp2_hit"):
            print(f"[RACE] TRAIL-TP2 yarışı: {ticker} TP2 henüz gelmedi, geç gelirse reconcile")

        total_kar = round(tp1_kar + tp2_kar + trail_kar, 2)

        if parsed["tp_type"] == "TP2" or pos.get("tp2_hit"):
            sonuc = "★ TP1+TP2+Trail"
        else:
            sonuc = "≈ TP1+Trail"

        closed = {
            "ticker": ticker,
            "giris": pos["giris"],
            "marj": pos["marj"],
            "lev": pos["lev"],
            "sonuc": sonuc,
            "kar": total_kar,
            "tp1_kar": tp1_kar,
            "tp2_kar": tp2_kar,
            "trail_kar": trail_kar,
            "trail_px": parsed["trail_px"],
            "tp_type": parsed["tp_type"],
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
        print(f"[TEST] TRAIL({parsed['tp_type']}): {ticker} | {sonuc} | TP1:+{tp1_kar}$ TP2:+{tp2_kar}$ Trail:+{trail_kar}$ = TOPLAM:+{total_kar}$")
        return {"status": "trail_closed"}

    # === STOP ===
    elif msg.startswith("CAB v13 STOP"):
        parsed = parse_stop(msg)
        if not parsed:
            return {"status": "parse_error"}
        print(f"[PARSE] {parsed}")
        ticker = parsed["ticker"]

        if is_recently_closed(ticker):
            print(f"[WARN] STOP geldi ama {ticker} zaten kapalı — yoksayıldı")
            return {"status": "already_closed"}

        if ticker not in data["open_positions"]:
            print(f"[WARN] STOP geldi ama {ticker} açık değil")
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
            "ticker": ticker,
            "giris": giris,
            "marj": marj,
            "lev": lev,
            "sonuc": sonuc,
            "kar": total_kar,
            "tp1_kar": tp1_kar,
            "tp2_kar": tp2_kar,
            "trail_kar": round(stop_kar, 2),
            "trail_px": stop_px,
            "tp_type": "STOP",
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


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    d = load_data()
    op = d["open_positions"]
    cp = d["closed_positions"]

    toplam_kar = sum(c["kar"] for c in cp)
    toplam_say = len(cp)
    karli_say = sum(1 for c in cp if c["kar"] > 0)
    win_rate = round(karli_say / toplam_say * 100, 1) if toplam_say > 0 else 0

    bugun = (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%d")
    bugunku = [c for c in cp if c["kapanis"].startswith(bugun)]
    bugun_kar = sum(c["kar"] for c in bugunku)
    bugun_tp = sum(1 for c in bugunku if c["kar"] > 0)
    bugun_stop = sum(1 for c in bugunku if c["kar"] <= 0)
    bugun_wr = round(bugun_tp / len(bugunku) * 100, 1) if bugunku else 0

    def sure_dk(c):
        try:
            a = datetime.strptime(c["acilis"], "%Y-%m-%d %H:%M")
            k = datetime.strptime(c["kapanis"], "%Y-%m-%d %H:%M")
            return int((k - a).total_seconds() / 60)
        except:
            return 0
    sureler = [sure_dk(c) for c in cp]
    ort_sure = sum(sureler) / len(sureler) if sureler else 0
    ort_sure_str = f"{int(ort_sure // 1440)}g {int((ort_sure % 1440) // 60)}s {int(ort_sure % 60)}dk" if ort_sure >= 60 else f"{int(ort_sure)}dk"
    if ort_sure == 0:
        ort_sure_str = "—"

    net_kar_renk = "#4ade80" if toplam_kar > 0 else ("#f87171" if toplam_kar < 0 else "#e5e7eb")
    bugun_kar_renk = "#4ade80" if bugun_kar > 0 else ("#f87171" if bugun_kar < 0 else "#e5e7eb")
    wr_renk = "#4ade80" if win_rate >= 50 else ("#f87171" if win_rate < 50 and toplam_say > 0 else "#e5e7eb")
    bugun_wr_renk = "#4ade80" if bugun_wr >= 50 else ("#f87171" if bugun_wr < 50 and bugunku else "#e5e7eb")

    mod_badge = "🟡 TEST MODU" if TEST_MODE else "🟢 CANLI MOD"
    mod_text = "TEST" if TEST_MODE else "CANLI"

    op_rows = ""
    op_symbols = []
    for ticker, pos in op.items():
        op_symbols.append(ticker)
        giris = pos["giris"]
        stop = pos["stop"]
        tp1 = pos["tp1"]
        tp2 = pos["tp2"]
        marj = pos["marj"]
        lev = pos["lev"]
        hh_pct = pos.get("hh_pct", 0)
        atr_skor = pos.get("atr_skor", 1.0)
        kapat_oran = pos.get("kapat_oran", 60)
        durum = pos.get("durum", "Açık")
        tp1_hit = pos.get("tp1_hit", False)
        tp2_hit = pos.get("tp2_hit", False)
        tp1_kar = pos.get("tp1_kar", 0)
        tp2_kar = pos.get("tp2_kar", 0)

        pos_size = marj * lev
        stop_zarar = round(pos_size * (stop - giris) / giris, 1)
        tp1_kar_hesap = round(pos_size * (kapat_oran / 100.0) * (tp1 - giris) / giris, 1)
        tp2_kar_hesap = round(pos_size * 0.25 * (tp2 - giris) / giris, 1)

        hh_disp = f"%{hh_pct:.2f}" if hh_pct > 0 else "—"
        atr_disp = f"{atr_skor:.2f}x ({kapat_oran}%)"

        if tp2_hit:
            trail_disp = f"✓ TP2+ (+{tp1_kar+tp2_kar:.1f}$)"
        elif tp1_hit:
            trail_disp = f"✓ TP1+ (+{tp1_kar:.1f}$)"
        else:
            trail_disp = "—"

        row_bg = ""
        if tp2_hit:
            row_bg = "background:rgba(20,184,166,0.15);border-left:3px solid #14b8a6;"
        elif tp1_hit:
            row_bg = "background:rgba(132,204,22,0.12);border-left:3px solid #84cc16;"

        op_rows += f"""
        <tr style="{row_bg}">
            <td><a href="javascript:void(0)" onclick="openTV('{ticker}')" style="color:#60a5fa;text-decoration:none;">{ticker}</a> 🔗</td>
            <td>{marj:.0f}$ <small style="color:#9ca3af">({lev}x)</small></td>
            <td>{giris:.6f}</td>
            <td class="px-{ticker}" data-giris="{giris}" data-tp1="{tp1}" data-stop="{stop}">—</td>
            <td class="tp1kalan-{ticker}">—</td>
            <td class="durum-{ticker}">{durum}</td>
            <td class="hh-{ticker}">{hh_disp}</td>
            <td>{atr_disp}</td>
            <td class="trail-{ticker}">{trail_disp}</td>
            <td>{stop:.6f} <small style="color:#f87171">({stop_zarar:+.1f}$)</small></td>
            <td>{tp1:.6f} <small style="color:#4ade80">({tp1_kar_hesap:+.1f}$)</small></td>
            <td>{tp2:.6f} <small style="color:#4ade80">({tp2_kar_hesap:+.1f}$)</small></td>
            <td>{pos.get('zaman', '')}</td>
        </tr>
        """

    if not op_rows:
        op_rows = "<tr><td colspan='13' style='text-align:center;color:#9ca3af'>Açık pozisyon yok</td></tr>"

    cp_rows = ""
    cp_sorted = sorted(cp, key=lambda x: x["kapanis"], reverse=True)
    for c in cp_sorted:
        sonuc_renk = "#4ade80" if c["kar"] > 0 else "#f87171"
        kar_str = f"{c['kar']:+.1f}$"
        hh_disp = f"%{c['hh_pct']:.2f}" if c.get("hh_pct", 0) > 0 else "—"
        atr_disp = f"{c.get('atr_skor', 1.0):.2f}x/{c.get('kapat_oran', 60)}%"

        try:
            a = datetime.strptime(c["acilis"], "%Y-%m-%d %H:%M")
            k = datetime.strptime(c["kapanis"], "%Y-%m-%d %H:%M")
            dk = int((k - a).total_seconds() / 60)
            if dk >= 1440:
                sure = f"{dk // 1440}g {(dk % 1440) // 60}s"
            elif dk >= 60:
                sure = f"{dk // 60}s {dk % 60}dk"
            else:
                sure = f"{dk}dk"
        except:
            sure = "—"

        zaman_disp = c["acilis"][5:] + "→" + c["kapanis"][11:]

        cp_rows += f"""
        <tr>
            <td><a href="javascript:void(0)" onclick="openTV('{c['ticker']}')" style="color:#60a5fa;text-decoration:none;">{c['ticker']}</a> 🔗</td>
            <td>{c['marj']:.0f}$</td>
            <td>{c['giris']:.6f}</td>
            <td style="color:{sonuc_renk}">{c['sonuc']}</td>
            <td style="color:{sonuc_renk};font-weight:bold">{kar_str}</td>
            <td>{hh_disp}</td>
            <td>{atr_disp}</td>
            <td>{sure}</td>
            <td>{zaman_disp}</td>
        </tr>
        """

    if not cp_rows:
        cp_rows = "<tr><td colspan='9' style='text-align:center;color:#9ca3af'>Henüz kapanan yok</td></tr>"

    if cp:
        toplam_k = sum(c["kar"] for c in cp if c["kar"] > 0)
        toplam_z = sum(c["kar"] for c in cp if c["kar"] < 0)
        ozet_txt = f'<b>{len(cp)}</b> pozisyon &nbsp;|&nbsp; <span style="color:#4ade80">Kar: +{toplam_k:.1f}$</span> &nbsp;|&nbsp; <span style="color:#f87171">Zarar: {toplam_z:.1f}$</span> &nbsp;|&nbsp; <span style="color:{net_kar_renk}">NET: {toplam_kar:+.1f}$</span>'
    else:
        ozet_txt = "Henüz kapanan pozisyon yok"

    symbols_json = json.dumps(op_symbols)

    # JS — 5sn fiyat / 20sn sayfa
    js = """
<script>
const symbols = __SYMBOLS__;

function openTV(ticker) {
    const appUrl = 'tradingview://x-callback-url/chart?symbol=BINANCE:' + ticker + '.P';
    const webUrl = 'https://tr.tradingview.com/chart/?symbol=BINANCE:' + ticker + '.P';
    const t = Date.now();
    window.open(appUrl, '_blank');
    setTimeout(function() {
        if (Date.now() - t < 1500) {
            window.open(webUrl, '_blank');
        }
    }, 500);
}

async function fp(sym) {
    try {
        const r = await fetch('https://fapi.binance.com/fapi/v1/ticker/price?symbol=' + sym);
        if (!r.ok) return null;
        const j = await r.json();
        return parseFloat(j.price);
    } catch(e) { return null; }
}

async function updatePrices() {
    for (const sym of symbols) {
        const px = await fp(sym);
        if (px === null) continue;

        const cell = document.querySelector('.px-' + sym);
        if (!cell) continue;

        const giris = parseFloat(cell.dataset.giris);
        const tp1 = parseFloat(cell.dataset.tp1);
        const stop = parseFloat(cell.dataset.stop);

        cell.textContent = px.toFixed(6);

        const pct = (px - giris) / giris * 100;
        const tp1Kalan = (tp1 - px) / px * 100;

        const tp1KalanCell = document.querySelector('.tp1kalan-' + sym);
        if (tp1KalanCell) {
            if (px >= tp1) {
                tp1KalanCell.innerHTML = '<span style="color:#4ade80">✓ Geçildi</span>';
            } else {
                tp1KalanCell.textContent = '%' + tp1Kalan.toFixed(2) + ' uzak';
            }
        }

        const durumCell = document.querySelector('.durum-' + sym);
        if (durumCell && !durumCell.textContent.includes('TP')) {
            if (px < stop) {
                durumCell.innerHTML = '<span style="color:#f87171">⚠ STOP ALTI</span>';
            } else if (pct > 0) {
                durumCell.innerHTML = '<span style="color:#4ade80">▲ +' + pct.toFixed(2) + '%</span>';
            } else {
                durumCell.innerHTML = '<span style="color:#f87171">▼ ' + pct.toFixed(2) + '%</span>';
            }
        }

        try {
            await fetch('/update_hh', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ticker: sym, price: px})
            });
        } catch(e) {}
    }
    setTimeout(updatePrices, 5000);
}

if (symbols.length > 0) {
    setTimeout(updatePrices, 500);
}

setTimeout(() => location.reload(), 20000);
</script>
"""
    js = js.replace("__SYMBOLS__", symbols_json)

    html = f"""<!DOCTYPE html>
<html><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CAB Bot v4 Dashboard</title>
<style>
body {{ font-family: -apple-system, sans-serif; background:#0f172a; color:#e5e7eb; margin:0; padding:10px; }}
h1 {{ font-size:18px; margin:0 0 8px; }}
.badge {{ display:inline-block; padding:4px 10px; background:#1e293b; border-radius:4px; font-size:12px; }}
.update {{ color:#9ca3af; font-size:11px; margin-top:5px; }}
.stats {{ display:flex; flex-wrap:wrap; gap:10px; margin:10px 0; }}
.stat {{ padding:10px 14px; background:#1e293b; border-radius:6px; min-width:80px; }}
.stat-val {{ font-size:16px; font-weight:bold; }}
.stat-lbl {{ font-size:10px; color:#9ca3af; margin-top:2px; }}
.section {{ background:#1e293b; border-radius:6px; padding:8px; margin:10px 0; }}
.section h2 {{ font-size:14px; margin:0 0 8px; }}
table {{ width:100%; border-collapse:collapse; font-size:11px; }}
th {{ background:#334155; padding:6px 4px; text-align:left; position:sticky; top:0; }}
td {{ padding:5px 4px; border-bottom:1px solid #334155; }}
tr:hover {{ background:#334155; }}
.ozet {{ padding:6px 10px; background:#0f172a; border-radius:4px; margin:6px 0; font-size:12px; }}
small {{ color:#9ca3af; }}
.bugun-box {{ background:#312e81; padding:6px 10px; border-radius:4px; margin:8px 0; font-size:12px; }}
</style>
</head>
<body>
<h1>🤖 CAB Bot v4 Dashboard</h1>
<div class="badge">{mod_badge}</div>
<div class="update">⟳ Son güncelleme: {now_tr_short()} <small>(5sn fiyat / 20sn sayfa yenileme)</small></div>

<div class="stats">
    <div class="stat"><div class="stat-val">{len(op)}</div><div class="stat-lbl">Açık</div></div>
    <div class="stat"><div class="stat-val">{len(cp)}</div><div class="stat-lbl">Kapanan</div></div>
    <div class="stat"><div class="stat-val" style="color:{net_kar_renk}">{toplam_kar:+.1f}$</div><div class="stat-lbl">Net Kar</div></div>
    <div class="stat"><div class="stat-val" style="color:{wr_renk}">{win_rate}%</div><div class="stat-lbl">Win Rate</div></div>
    <div class="stat"><div class="stat-val" style="color:{bugun_wr_renk}">{bugun_wr}%</div><div class="stat-lbl">Bugün WR</div></div>
    <div class="stat"><div class="stat-val">{ort_sure_str}</div><div class="stat-lbl">Ort. Süre</div></div>
    <div class="stat"><div class="stat-val">{mod_text}</div><div class="stat-lbl">Mod</div></div>
</div>

<div class="bugun-box">📅 <b>Bugün ({bugun}):</b> &nbsp; {len(bugunku)} kapanan &nbsp;|&nbsp; {bugun_tp} TP &nbsp;|&nbsp; {bugun_stop} Stop &nbsp;|&nbsp; <span style="color:{bugun_kar_renk}">{bugun_kar:+.1f}$</span></div>

<div class="section">
    <h2>📊 Açık Pozisyonlar</h2>
    <table>
        <thead><tr>
            <th>Coin</th><th>Marjin</th><th>Giriş</th><th>Şu An</th><th>TP1'e Kalan</th>
            <th>Durum</th><th>HH%</th><th>ATR/Kapat</th><th>Trailing</th>
            <th>Stop</th><th>TP1</th><th>TP2</th><th>Zaman</th>
        </tr></thead>
        <tbody>{op_rows}</tbody>
    </table>
</div>

<div class="section">
    <h2>📋 Kapanan Pozisyonlar</h2>
    <div class="ozet">{ozet_txt}</div>
    <table>
        <thead><tr>
            <th>Coin</th><th>Marjin</th><th>Giriş</th><th>Sonuç</th><th>Kar/Zarar</th>
            <th>HH%</th><th>ATR/Kapat</th><th>Süre</th><th>Zaman</th>
        </tr></thead>
        <tbody>{cp_rows}</tbody>
    </table>
</div>

{js}
</body></html>"""
    return html
