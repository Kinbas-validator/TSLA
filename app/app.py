#!/usr/bin/env python3
"""TSLA spread monitor: Lighter (Robinhood chain) perp short close  vs  Jupiter TSLAx sell.

Spread per share = (USDC received selling N TSLAx on Jupiter) / N  -  VWAP of buying N TSLA on Lighter asks.
Positive spread  => closing a chunk of the delta-neutral position right now locks in profit.

Stdlib only (Python 3.10+). Config comes from environment variables (see config.env.example).
"""
import hashlib
import hmac
import json
import os
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ----------------------------------------------------------------------------- config
E = os.environ.get
WEB_PASSWORD = E("WEB_PASSWORD", "")
SESSION_SECRET = E("SESSION_SECRET", "") or hashlib.sha256(("s|" + WEB_PASSWORD).encode()).hexdigest()
BIND = E("BIND", "0.0.0.0")
PORT = int(E("WEB_PORT", "8080"))
PUBLIC_URL = E("PUBLIC_URL", "")
DB_PATH = E("DB_PATH", "/var/lib/tsla-spread/data.db")
POLL_SEC = max(3.0, float(E("POLL_SEC", "10")))
SIZES = sorted({int(x) for x in E("SIZES", "1,2,3,5,10,24").split(",") if x.strip()})
RETENTION_DAYS = int(E("RETENTION_DAYS", "60"))

LIGHTER_API = E("LIGHTER_API", "https://api.rh.lighter.xyz").rstrip("/")
LIGHTER_MARKET_ID = int(E("LIGHTER_MARKET_ID", "16"))  # TSLA perp on robinhoodchain.lighter.xyz
LIGHTER_TAKER_FEE = float(E("LIGHTER_TAKER_FEE", "0"))  # fraction, e.g. 0.0002

JUP_API_KEY = E("JUP_API_KEY", "")
JUP_BASE = "https://api.jup.ag" if JUP_API_KEY else "https://lite-api.jup.ag"
TSLAX_MINT = E("TSLAX_MINT", "XsDoVfqeBukxuZHWhdvWHBhgEHjGNst4MLodqsJHzoB")
TSLAX_DECIMALS = int(E("TSLAX_DECIMALS", "8"))
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

TG_TOKEN = E("TG_BOT_TOKEN", "").strip()
HYSTERESIS = float(E("ALERT_HYSTERESIS", "0.10"))
STALE_ALERT_SEC = int(E("STALE_ALERT_SEC", "300"))
TUNNEL_METRICS = E("TUNNEL_METRICS", "")  # cloudflared quick-tunnel metrics addr, e.g. 127.0.0.1:20241

DEFAULT_SETTINGS = {
    "threshold": float(E("ALERT_SPREAD_USD", "0.30")),  # $ per share
    "alert_size": int(E("ALERT_SIZE", "5")),
    "cooldown_min": int(E("ALERT_COOLDOWN_MIN", "15")),
    "alerts_enabled": 1,
    "notify_close": 1,
    "chat_id": E("TG_CHAT_ID", "").strip(),
    "position": int(E("POSITION_SHARES", "24")),
}

UA = {"User-Agent": "Mozilla/5.0 tsla-spread-monitor/1.0", "Accept": "application/json"}


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


# ----------------------------------------------------------------------------- storage
class DB:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.c = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.lock = threading.Lock()
        with self.lock:
            self.c.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=NORMAL;
                CREATE TABLE IF NOT EXISTS samples(
                    ts INTEGER NOT NULL, size INTEGER NOT NULL,
                    lighter_px REAL, jup_px REAL, spread REAL);
                CREATE INDEX IF NOT EXISTS ix_samples ON samples(size, ts);
                CREATE TABLE IF NOT EXISTS top(
                    ts INTEGER PRIMARY KEY, bid REAL, ask REAL, mark REAL, jup_ref REAL);
                CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
                """
            )
            self.c.commit()

    def q(self, sql, args=()):
        with self.lock:
            return self.c.execute(sql, args).fetchall()

    def x(self, sql, args=(), many=False):
        with self.lock:
            (self.c.executemany if many else self.c.execute)(sql, args)
            self.c.commit()

    def settings(self):
        s = dict(DEFAULT_SETTINGS)
        for k, v in self.q("SELECT k, v FROM settings"):
            if k in s:
                try:
                    s[k] = type(DEFAULT_SETTINGS[k])(json.loads(v))
                except Exception:
                    pass
        return s

    def set_setting(self, k, v):
        self.x("INSERT OR REPLACE INTO settings(k, v) VALUES(?, ?)", (k, json.dumps(v)))


db = DB(DB_PATH)

# ----------------------------------------------------------------------------- HTTP helpers


def http_json(url, headers=None, data=None, timeout=12):
    h = dict(UA)
    if headers:
        h.update(headers)
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


# ----------------------------------------------------------------------------- market data


def lighter_book():
    d = http_json(f"{LIGHTER_API}/api/v1/orderBookOrders?market_id={LIGHTER_MARKET_ID}&limit=250")
    asks = sorted(((float(o["price"]), float(o["remaining_base_amount"])) for o in d.get("asks", [])), key=lambda t: t[0])
    bids = sorted(((float(o["price"]), float(o["remaining_base_amount"])) for o in d.get("bids", [])), key=lambda t: -t[0])
    asks = [a for a in asks if a[1] > 0]
    bids = [b for b in bids if b[1] > 0]
    if not asks:
        raise RuntimeError("Lighter: empty asks")
    return asks, bids


def lighter_mark():
    try:
        d = http_json(f"{LIGHTER_API}/api/v1/orderBookDetails?market_id={LIGHTER_MARKET_ID}")
        return float(d["order_book_details"][0]["mark_price"])
    except Exception:
        return None


def vwap_buy(asks, qty):
    """Average price paid to market-buy qty from the ask side (closing the short)."""
    left, cost = qty, 0.0
    for px, sz in asks:
        take = min(left, sz)
        cost += take * px
        left -= take
        if left <= 1e-12:
            return cost / qty * (1 + LIGHTER_TAKER_FEE)
    return None  # not enough depth in fetched book


def jup_sell(qty):
    """USDC per share received for selling qty TSLAx on Jupiter (Ultra quote, fallback to Metis quote)."""
    amount = int(round(qty * 10 ** TSLAX_DECIMALS))
    hdr = {"x-api-key": JUP_API_KEY} if JUP_API_KEY else None
    params = urllib.parse.urlencode({"inputMint": TSLAX_MINT, "outputMint": USDC_MINT, "amount": amount})
    errs = []
    try:
        d = http_json(f"{JUP_BASE}/ultra/v1/order?{params}", headers=hdr)
        if d.get("outAmount"):
            return int(d["outAmount"]) / 1e6 / qty, "ultra"
        errs.append(str(d)[:200])
    except Exception as e:
        errs.append(f"ultra: {e}")
    try:
        d = http_json(f"{JUP_BASE}/swap/v1/quote?{params}&slippageBps=50", headers=hdr)
        if d.get("outAmount"):
            return int(d["outAmount"]) / 1e6 / qty, "metis"
        errs.append(str(d)[:200])
    except Exception as e:
        errs.append(f"metis: {e}")
    raise RuntimeError("Jupiter: " + " | ".join(errs))


# ----------------------------------------------------------------------------- state
STATE = {
    "last_ok": 0,
    "last_err": "",
    "last_err_ts": 0,
    "latest": {},  # size -> dict
    "top": {},
    "alert_active": False,
    "last_alert_ts": 0,
    "stale_notified": False,
    "started": time.time(),
}
state_lock = threading.Lock()
pool = ThreadPoolExecutor(max_workers=max(4, len(SIZES) + 2))


def poll_once():
    ts = int(time.time())
    f_book = pool.submit(lighter_book)
    f_mark = pool.submit(lighter_mark)
    f_jup = {n: pool.submit(jup_sell, n) for n in SIZES}
    asks, bids = f_book.result()
    mark = f_mark.result()
    rows, latest, errs = [], {}, []
    for n in SIZES:
        try:
            jpx, route = f_jup[n].result()
        except Exception as e:
            errs.append(str(e))
            continue
        lpx = vwap_buy(asks, n)
        spread = (jpx - lpx) if lpx is not None else None
        rows.append((ts, n, lpx, jpx, spread))
        latest[n] = {"size": n, "lighter_px": lpx, "jup_px": jpx, "spread": spread,
                     "profit": spread * n if spread is not None else None, "route": route, "ts": ts}
    if not rows:
        raise RuntimeError("; ".join(errs) or "no data")
    top = {"ts": ts, "bid": bids[0][0] if bids else None, "ask": asks[0][0], "mark": mark,
           "jup_ref": latest[min(latest)]["jup_px"]}
    db.x("INSERT INTO samples VALUES(?,?,?,?,?)", rows, many=True)
    db.x("INSERT OR REPLACE INTO top VALUES(?,?,?,?,?)", (ts, top["bid"], top["ask"], top["mark"], top["jup_ref"]))
    with state_lock:
        STATE["latest"] = latest
        STATE["top"] = top
        STATE["last_ok"] = ts
        if errs:
            STATE["last_err"], STATE["last_err_ts"] = errs[0][:300], ts
    return latest


def collector():
    last_cleanup = 0
    backoff = 0
    while True:
        t0 = time.time()
        try:
            latest = poll_once()
            backoff = 0
            check_alerts(latest)
            if STATE["stale_notified"]:
                STATE["stale_notified"] = False
                tg_send("✅ Данные снова поступают.")
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            log("poll error:", msg)
            with state_lock:
                STATE["last_err"], STATE["last_err_ts"] = msg[:300], int(time.time())
            backoff = min(60, backoff * 2 + 5) if "429" in msg else 0
            last_ok = STATE["last_ok"] or STATE["started"]
            if not STATE["stale_notified"] and time.time() - last_ok > STALE_ALERT_SEC:
                STATE["stale_notified"] = True
                tg_send(f"⚠️ Нет свежих данных более {STALE_ALERT_SEC // 60} мин.\nОшибка: {msg[:300]}")
        if time.time() - last_cleanup > 3600:
            last_cleanup = time.time()
            cut = int(time.time()) - RETENTION_DAYS * 86400
            try:
                db.x("DELETE FROM samples WHERE ts < ?", (cut,))
                db.x("DELETE FROM top WHERE ts < ?", (cut,))
            except Exception as e:
                log("cleanup error:", e)
        time.sleep(max(0.5, POLL_SEC + backoff - (time.time() - t0)))


# ----------------------------------------------------------------------------- telegram


def tg_api(method, payload, timeout=15):
    if not TG_TOKEN:
        return None
    return http_json(f"https://api.telegram.org/bot{TG_TOKEN}/{method}", data=payload, timeout=timeout)


def tg_send(text, chat_id=None):
    chat_id = chat_id or db.settings()["chat_id"]
    if not TG_TOKEN or not chat_id:
        return False
    try:
        tg_api("sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                               "disable_web_page_preview": True})
        return True
    except Exception as e:
        log("telegram send error:", e)
        return False


def fmt_table(latest, highlight=None):
    lines = ["<pre>шт   Lighter  Jupiter   спред   итого"]
    for n in sorted(latest):
        r = latest[n]
        if r["spread"] is None:
            lines.append(f"{n:<3}  нет глубины")
            continue
        mark = "◀" if n == highlight else " "
        lines.append(f"{n:<3} {r['lighter_px']:8.2f} {r['jup_px']:8.2f} {r['spread']:+7.2f} {r['profit']:+7.2f}{mark}")
    lines.append("</pre>")
    return "\n".join(lines)


def status_text():
    with state_lock:
        latest = dict(STATE["latest"])
        top = dict(STATE["top"])
        last_ok = STATE["last_ok"]
    s = db.settings()
    if not latest:
        return "Данных пока нет. Последняя ошибка: " + (STATE["last_err"] or "—")
    age = int(time.time() - last_ok)
    t = [f"<b>TSLA спред</b> (данные {age} с назад)",
         f"Lighter bid/ask: {top.get('bid') or 0:.2f} / {top.get('ask') or 0:.2f}",
         fmt_table(latest, s["alert_size"]),
         f"Порог: {s['threshold']:+.2f} $/акц для {s['alert_size']} шт · кулдаун {s['cooldown_min']} мин · "
         f"оповещения {'вкл' if s['alerts_enabled'] else 'выкл'}"]
    if PUBLIC_URL:
        t.append(PUBLIC_URL)
    return "\n".join(t)


def check_alerts(latest):
    s = db.settings()
    n = s["alert_size"] if s["alert_size"] in latest else min(latest, key=lambda k: abs(k - s["alert_size"]))
    r = latest.get(n)
    if not r or r["spread"] is None:
        return
    sp, now = r["spread"], time.time()
    thr = s["threshold"]
    if sp >= thr:
        due = (not STATE["alert_active"]) or (now - STATE["last_alert_ts"] >= s["cooldown_min"] * 60)
        STATE["alert_active"] = True
        if due and s["alerts_enabled"]:
            STATE["last_alert_ts"] = now
            head = "🟢 <b>Выгодный спред!</b>" if sp > 0 else "🔔 <b>Спред достиг порога</b>"
            msg = (f"{head}\nЗакрыть {n} шт: шорт Lighter ≈ {r['lighter_px']:.2f}, "
                   f"продажа TSLAx Jupiter ≈ {r['jup_px']:.2f}\n"
                   f"Спред <b>{sp:+.2f} $/акц</b> → <b>{r['profit']:+.2f} $</b> за {n} шт\n"
                   + fmt_table(latest, n) + (f"\n{PUBLIC_URL}" if PUBLIC_URL else ""))
            tg_send(msg)
    elif sp < thr - HYSTERESIS and STATE["alert_active"]:
        STATE["alert_active"] = False
        if s["alerts_enabled"] and s["notify_close"]:
            tg_send(f"🔻 Спред ушёл ниже порога: {sp:+.2f} $/акц ({n} шт).")


TG_HELP = ("Команды:\n/status — текущий спред\n/set 0.5 — порог $/акц\n/size 5 — объём порции для оповещений\n"
           "/cooldown 15 — повтор оповещения, мин\n/on, /off — включить/выключить оповещения")


def tg_bot():
    if not TG_TOKEN:
        log("Telegram disabled (no TG_BOT_TOKEN)")
        return
    offset = None
    while True:
        try:
            payload = {"timeout": 50, "allowed_updates": ["message"]}
            if offset:
                payload["offset"] = offset
            res = tg_api("getUpdates", payload, timeout=60) or {}
            for u in res.get("result", []):
                offset = u["update_id"] + 1
                m = u.get("message") or {}
                chat = str((m.get("chat") or {}).get("id", ""))
                text = (m.get("text") or "").strip()
                if not chat or not text:
                    continue
                s = db.settings()
                if not s["chat_id"]:
                    if text.startswith("/start"):
                        db.set_setting("chat_id", chat)
                        tg_send("✅ Этот чат привязан для оповещений.\n" + TG_HELP
                                + (f"\n\nВеб: {PUBLIC_URL}" if PUBLIC_URL else ""), chat)
                        log("Telegram chat bound:", chat)
                    continue
                if chat != s["chat_id"]:
                    continue  # ignore strangers
                handle_tg_command(text, chat)
        except Exception as e:
            log("telegram poll error:", e)
            time.sleep(10)


def handle_tg_command(text, chat):
    parts = text.split()
    cmd = parts[0].split("@")[0].lower()
    arg = parts[1].replace(",", ".") if len(parts) > 1 else None
    try:
        if cmd in ("/status", "/s", "/start"):
            tg_send(status_text(), chat)
        elif cmd == "/set" and arg:
            db.set_setting("threshold", float(arg)); STATE["alert_active"] = False
            tg_send(f"Порог: {float(arg):+.2f} $/акц", chat)
        elif cmd == "/size" and arg:
            db.set_setting("alert_size", int(float(arg))); STATE["alert_active"] = False
            tg_send(f"Порция для оповещений: {int(float(arg))} шт", chat)
        elif cmd == "/cooldown" and arg:
            db.set_setting("cooldown_min", int(float(arg)))
            tg_send(f"Кулдаун: {int(float(arg))} мин", chat)
        elif cmd in ("/on", "/off"):
            db.set_setting("alerts_enabled", 1 if cmd == "/on" else 0)
            tg_send("Оповещения " + ("включены" if cmd == "/on" else "выключены"), chat)
        else:
            tg_send(TG_HELP, chat)
    except ValueError:
        tg_send("Неверное число. " + TG_HELP, chat)


def tunnel_watch():
    """Quick-tunnel mode: the public trycloudflare.com URL changes on restart -> pick it up and send to Telegram."""
    global PUBLIC_URL
    while True:
        try:
            d = http_json(f"http://{TUNNEL_METRICS}/quicktunnel", timeout=5)
            host = (d or {}).get("hostname")
            if host and f"https://{host}" != PUBLIC_URL:
                PUBLIC_URL = f"https://{host}"
                log("public url:", PUBLIC_URL)
                tg_send(f"🌐 Адрес веб-интерфейса: {PUBLIC_URL}")
        except Exception:
            pass
        time.sleep(20)


# ----------------------------------------------------------------------------- web
FAILS = {}  # ip -> [count, first_ts]
HERE = os.path.dirname(os.path.abspath(__file__))
RANGES = {"1h": 3600, "6h": 21600, "24h": 86400, "3d": 3 * 86400, "7d": 7 * 86400, "30d": 30 * 86400}


def make_token(days=30):
    exp = str(int(time.time()) + days * 86400)
    sig = hmac.new(SESSION_SECRET.encode(), exp.encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def check_token(tok):
    try:
        exp, sig = tok.split(".", 1)
        good = hmac.new(SESSION_SECRET.encode(), exp.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, good) and int(exp) > time.time()
    except Exception:
        return False


def series(size, rng):
    span = RANGES.get(rng, 21600)
    since = int(time.time()) - span
    bucket = max(int(POLL_SEC), span // 1500)
    rows = db.q(
        "SELECT (ts/?)*? AS t, AVG(spread), MIN(spread), MAX(spread), AVG(lighter_px), AVG(jup_px) "
        "FROM samples WHERE size=? AND ts>=? AND spread IS NOT NULL GROUP BY t ORDER BY t",
        (bucket, bucket, size, since))
    st = db.q("SELECT MAX(spread), MIN(spread), AVG(spread), COUNT(*), "
              "SUM(CASE WHEN spread >= ? THEN 1 ELSE 0 END), SUM(CASE WHEN spread > 0 THEN 1 ELSE 0 END) "
              "FROM samples WHERE size=? AND ts>=? AND spread IS NOT NULL",
              (db.settings()["threshold"], size, since))[0]
    best = db.q("SELECT ts, spread FROM samples WHERE size=? AND ts>=? AND spread IS NOT NULL "
                "ORDER BY spread DESC LIMIT 1", (size, since))
    cols = list(zip(*rows)) if rows else [[]] * 6
    return {
        "t": list(cols[0]), "avg": list(cols[1]), "min": list(cols[2]), "max": list(cols[3]),
        "lighter": list(cols[4]), "jup": list(cols[5]), "bucket": bucket,
        "stats": {"max": st[0], "min": st[1], "avg": st[2], "n": st[3] or 0,
                  "above": st[4] or 0, "positive": st[5] or 0,
                  "best_ts": best[0][0] if best else None},
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "tsla-spread"
    sys_version = ""

    def log_message(self, fmt, *a):
        pass

    def ip(self):
        # app listens on 127.0.0.1 behind a reverse proxy; the proxy appends the real client IP last
        xff = [x.strip() for x in (self.headers.get("X-Forwarded-For") or "").split(",") if x.strip()]
        return xff[-1] if xff else self.client_address[0]

    def authed(self):
        c = SimpleCookie(self.headers.get("Cookie", ""))
        return "sess" in c and check_token(c["sess"].value)

    def send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, to, extra=None):
        self.send_response(303)
        self.send_header("Location", to)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def page(self, name):
        with open(os.path.join(HERE, name), "rb") as f:
            return f.read()

    def body_params(self):
        n = min(int(self.headers.get("Content-Length") or 0), 10000)
        raw = self.rfile.read(n).decode() if n else ""
        if "json" in (self.headers.get("Content-Type") or ""):
            return json.loads(raw or "{}")
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        qs = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        if u.path == "/login":
            return self.send(200, self.page("login.html"), "text/html; charset=utf-8")
        if u.path == "/health":
            return self.send(200, {"ok": True, "last_ok": STATE["last_ok"]})
        if not self.authed():
            if u.path.startswith("/api/"):
                return self.send(401, {"error": "auth"})
            return self.redirect("/login")
        if u.path in ("/", "/index.html"):
            return self.send(200, self.page("index.html"), "text/html; charset=utf-8")
        if u.path == "/logout":
            return self.redirect("/login", {"Set-Cookie": "sess=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"})
        if u.path == "/api/latest":
            with state_lock:
                d = {"latest": [STATE["latest"][k] for k in sorted(STATE["latest"])], "top": STATE["top"],
                     "last_ok": STATE["last_ok"], "last_err": STATE["last_err"], "last_err_ts": STATE["last_err_ts"],
                     "alert_active": STATE["alert_active"]}
            s = db.settings()
            d["settings"] = {k: v for k, v in s.items() if k != "chat_id"}
            d["tg"] = {"token": bool(TG_TOKEN), "chat": bool(s["chat_id"])}
            d["sizes"], d["now"], d["poll"] = SIZES, int(time.time()), POLL_SEC
            return self.send(200, d)
        if u.path == "/api/series":
            try:
                size = int(qs.get("size", SIZES[0]))
            except ValueError:
                size = SIZES[0]
            return self.send(200, series(size, qs.get("range", "6h")))
        if u.path == "/api/export.csv":
            size = int(qs.get("size", SIZES[0]))
            since = int(time.time()) - RANGES.get(qs.get("range", "24h"), 86400)
            rows = db.q("SELECT ts, size, lighter_px, jup_px, spread FROM samples WHERE size=? AND ts>=? ORDER BY ts",
                        (size, since))
            out = "time_utc,size,lighter_buy_vwap,jupiter_sell_px,spread\n" + "".join(
                f"{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(r[0]))},{r[1]},{r[2]},{r[3]},{r[4]}\n" for r in rows)
            return self.send(200, out, "text/csv; charset=utf-8",
                             {"Content-Disposition": f"attachment; filename=tsla_spread_{size}.csv"})
        return self.send(404, {"error": "not found"})

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/login":
            ip = self.ip()
            f = FAILS.get(ip)
            if f and f[0] >= 10 and time.time() - f[1] < 900:
                return self.send(429, "Слишком много попыток, подождите 15 минут.", "text/plain; charset=utf-8")
            p = self.body_params()
            if WEB_PASSWORD and hmac.compare_digest(str(p.get("password", "")).encode(), WEB_PASSWORD.encode()):
                FAILS.pop(ip, None)
                secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
                return self.redirect("/", {"Set-Cookie": f"sess={make_token()}; Path=/; Max-Age={30 * 86400}; HttpOnly; SameSite=Lax{secure}"})
            if not f or time.time() - f[1] >= 900:
                FAILS[ip] = [1, time.time()]
            else:
                f[0] += 1
            time.sleep(1)
            return self.redirect("/login?e=1")
        if not self.authed():
            return self.send(401, {"error": "auth"})
        if u.path == "/api/settings":
            p = self.body_params()
            try:
                if "threshold" in p:
                    db.set_setting("threshold", float(p["threshold"]))
                if "alert_size" in p:
                    db.set_setting("alert_size", int(p["alert_size"]))
                if "cooldown_min" in p:
                    db.set_setting("cooldown_min", max(1, int(p["cooldown_min"])))
                if "alerts_enabled" in p:
                    db.set_setting("alerts_enabled", 1 if p["alerts_enabled"] else 0)
                if "notify_close" in p:
                    db.set_setting("notify_close", 1 if p["notify_close"] else 0)
                if "position" in p:
                    db.set_setting("position", max(1, int(p["position"])))
            except (ValueError, TypeError):
                return self.send(400, {"error": "bad value"})
            STATE["alert_active"] = False
            return self.send(200, {"ok": True, "settings": db.settings() | {"chat_id": None}})
        if u.path == "/api/test-telegram":
            if not TG_TOKEN:
                return self.send(200, {"ok": False, "msg": "TG_BOT_TOKEN не задан в config.env"})
            if not db.settings()["chat_id"]:
                return self.send(200, {"ok": False, "msg": "Чат не привязан: отправьте боту /start"})
            ok = tg_send("🧪 Тест оповещения\n" + status_text())
            return self.send(200, {"ok": ok, "msg": "Отправлено" if ok else "Ошибка отправки, см. логи"})
        return self.send(404, {"error": "not found"})


def main():
    if not WEB_PASSWORD:
        raise SystemExit("WEB_PASSWORD is not set")
    log(f"start: lighter={LIGHTER_API} market={LIGHTER_MARKET_ID} jup={JUP_BASE} sizes={SIZES} poll={POLL_SEC}s")
    threading.Thread(target=collector, daemon=True).start()
    threading.Thread(target=tg_bot, daemon=True).start()
    if TUNNEL_METRICS:
        threading.Thread(target=tunnel_watch, daemon=True).start()
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    srv.daemon_threads = True
    log(f"web on http://{BIND}:{PORT}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
