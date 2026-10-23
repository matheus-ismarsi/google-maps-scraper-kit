#!/usr/bin/env python3
"""Local lead CRM: serves crm/index.html plus a small JSON API over SQLite.

    python crm/server.py                    # imports ../leads.csv on first run, opens the browser
    python crm/server.py --import outro.csv # merge another scrape (keeps statuses, notes, messages)
    python crm/server.py --port 8766 --no-browser

Uses only the Python standard library. Binds to 127.0.0.1 only: the database holds personal
data (phones, emails), so never expose it publicly. Data lives in crm/data/crm.db (gitignored).
The one exception is /webhook, which Meta calls (through a tunnel) to deliver WhatsApp replies:
every other path refuses requests that did not come straight from this machine.

WhatsApp goes out one of two ways:
- WhatsApp Web (unofficial): crm/wa-bridge/bridge.js, started here when installed
  (instalar-whatsapp.bat), logs in with a QR code and pushes replies to /api/bridge/event.
- The official Cloud API (whatsapp.py), when a token is configured and the bridge is not connected.
"""
import argparse, atexit, csv, datetime, io, json, os, re, secrets, shutil, sqlite3, subprocess, sys, threading, time
import urllib.error, urllib.request, webbrowser
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

import presence
import whatsapp as wa

HERE = os.path.dirname(os.path.abspath(__file__))
KIT = os.path.dirname(HERE)
DB = os.environ.get("CRM_DB") or os.path.join(HERE, "data", "crm.db")  # CRM_DB: point tests elsewhere
BRIDGE_DIR = os.path.join(HERE, "wa-bridge")
# BRIDGE_TOKEN in the environment means "a bridge is already running elsewhere" (tests); otherwise
# a fresh token per run is handed to the bridge process this server starts.
BRIDGE = {"proc": None, "port": int(os.environ.get("BRIDGE_PORT") or 8766),
          "token": os.environ.get("BRIDGE_TOKEN") or secrets.token_urlsafe(24),
          "external": bool(os.environ.get("BRIDGE_TOKEN"))}

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
  id INTEGER PRIMARY KEY,
  place_id TEXT UNIQUE,
  title TEXT, phone TEXT, emails TEXT, website TEXT, instagram TEXT, facebook TEXT, linkedin TEXT,
  category TEXT, address TEXT, city TEXT, rating REAL, reviews INTEGER, maps_link TEXT,
  site_type TEXT, platform TEXT,
  status TEXT NOT NULL DEFAULT 'novo', temperature TEXT NOT NULL DEFAULT '',
  notes TEXT NOT NULL DEFAULT '', follow_up TEXT NOT NULL DEFAULT '', draft TEXT NOT NULL DEFAULT '',
  created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY,
  lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
  direction TEXT NOT NULL, channel TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_lead ON messages(lead_id);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS changes (seq INTEGER PRIMARY KEY AUTOINCREMENT, lead_id INTEGER, kind TEXT);
"""
# Columns added after the first release; created on startup when missing.
MIGRATIONS = {
    "leads": {"wa_id": "TEXT", "web_links": "TEXT", "web_checked_at": "TEXT", "presence_override": "TEXT"},
    "messages": {"wa_msg_id": "TEXT", "wa_status": "TEXT", "wa_error": "TEXT", "template": "TEXT"},
}

STATUSES = {"novo", "contatado", "respondeu", "interessado", "reuniao", "fechado", "perdido", "nao_contatar"}
EDITABLE = {"status", "temperature", "notes", "follow_up", "draft", "presence_override"}
# CSV column -> leads column. CRM fields (status, notes...) are never overwritten by an import.
CSV_MAP = {"place_id": "place_id", "title": "title", "phone": "phone", "emails": "emails",
           "website": "website", "instagram": "instagram", "facebook": "facebook",
           "linkedin": "linkedin", "category": "category", "address": "address",
           "cidade_busca": "city", "review_rating": "rating", "review_count": "reviews",
           "link": "maps_link", "tipo_site": "site_type", "plataforma": "platform"}
# Webhook statuses can arrive out of order: never move a message back from "read" to "sent".
STATUS_RANK = {"accepted": 0, "sent": 1, "delivered": 2, "read": 3, "failed": 4}


def now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def connect():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def migrate(con):
    con.executescript(SCHEMA)
    for table, cols in MIGRATIONS.items():
        have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        for col, typ in cols.items():
            if col not in have:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS messages_wa ON messages(wa_msg_id) WHERE wa_msg_id IS NOT NULL")


def num(v, cast):
    try:
        return cast(float(str(v).replace(",", ".")))
    except (TypeError, ValueError):
        return None


def site_kind(url):
    """Stored at import time only; the CRM always shows presence.effective(), which re-reads the URL."""
    kind, platform, _ = presence.classify(url)
    if kind in ("none", "directory"):
        return "sem_site", ""
    if kind in ("site", "short"):
        return "com_site", ""
    return "so_rede_social", platform


def city_from_address(addr):
    """'R. X, 69 - Vila Y, Americana - SP, 13465-390' -> 'Americana'."""
    m = re.findall(r",\s*([^,]+?)\s*-\s*[A-Z]{2}\b", addr or "")
    return m[-1].strip() if m else ""


def import_rows(rows):
    """Upsert leads from any kit CSV: the merged leads.csv or a plain scrape.py export.
    Key is place_id (else link, else name+address). Scraped fields refresh only when the new
    value is non-empty; CRM work (stage, notes, messages) is never touched. -> (new, total)"""
    cols = list(CSV_MAP.values())
    updates = ", ".join(f"{c} = COALESCE(NULLIF(excluded.{c}, ''), {c})" for c in cols if c != "place_id")
    sql = (f"INSERT INTO leads ({', '.join(cols)}, created_at, updated_at) "
           f"VALUES ({', '.join('?' * len(cols))}, ?, ?) "
           f"ON CONFLICT(place_id) DO UPDATE SET {updates}")
    ts = now()
    with connect() as con:
        before = con.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
        for r in rows:
            vals = {col: (r.get(k) or "").strip() for k, col in CSV_MAP.items()}
            if not vals["title"]:
                continue
            vals["place_id"] = vals["place_id"] or vals["maps_link"] or f"t:{vals['title']}|{vals['address']}"
            if not vals["site_type"]:
                vals["site_type"], vals["platform"] = site_kind(vals["website"])
            if vals["platform"] == "instagram" and not vals["instagram"]:
                vals["instagram"] = vals["website"]
            vals["city"] = vals["city"] or city_from_address(vals["address"])
            vals["rating"] = num(vals["rating"], float)
            vals["reviews"] = num(vals["reviews"], int)
            con.execute(sql, [vals[c] for c in cols] + [ts, ts])
        after = con.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    return after - before, after


def import_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    new, total = import_rows(rows)
    print(f"✓ Importado {path}: {len(rows)} linhas, {new} leads novos, {total} no total")


LEAD_SQL = """
SELECT l.*, COUNT(m.id) AS msg_count,
       SUM(m.direction = 'in') AS msg_in, MAX(m.created_at) AS last_msg,
       MAX(CASE WHEN m.direction = 'in' AND m.channel = 'whatsapp' THEN m.created_at END) AS wa_last_in
FROM leads l LEFT JOIN messages m ON m.lead_id = l.id
{where} GROUP BY l.id
"""


def lead(con, lead_id):
    row = con.execute(LEAD_SQL.format(where="WHERE l.id = ?"), (lead_id,)).fetchone()
    return presence.effective(dict(row)) if row else None


def all_leads(con):
    return [presence.effective(dict(r)) for r in con.execute(LEAD_SQL.format(where=""))]


# ── online presence check (presence.py), one background job at a time ──
JOB = {"running": False, "done": 0, "total": 0, "changed": 0, "error": "", "current": "", "stop": False}


def stored_links(row):
    try:
        return json.loads(row["web_links"] or "[]")
    except (ValueError, TypeError):
        return []


def merge_links(old, new):
    """Keep what the user decided (manual links, confirmations, rejections); refresh the rest."""
    kept = [l for l in old if l.get("source") == "manual" or l.get("reviewed")]
    blocked = {l["url"] for l in kept}
    return kept + [l for l in new if l["url"] not in blocked]


def check_lead(lid, retries=3):
    """Verify one lead's presence and store it. Raises presence.Throttled after the retries."""
    with connect() as con:
        row = con.execute("SELECT id, title, city, website, web_links FROM leads WHERE id = ?", (lid,)).fetchone()
    if not row:
        return None
    for attempt in range(retries):
        try:
            found = presence.verify(dict(row))
            break
        except presence.Throttled:
            if attempt == retries - 1:
                raise
            wait = 60 * (attempt + 1)
            JOB["error"] = f"O buscador pediu uma pausa; tentando de novo em {wait}s…"
            for _ in range(wait):
                if JOB["stop"]:
                    return None
                time.sleep(1)
    JOB["error"] = ""
    with connect() as con:
        before = lead(con, lid)
        con.execute("UPDATE leads SET web_links = ?, web_checked_at = ? WHERE id = ?",
                    (json.dumps(merge_links(stored_links(row), found), ensure_ascii=False), now(), lid))
        con.execute("INSERT INTO changes (lead_id, kind) VALUES (?, 'web')", (lid,))
        after = lead(con, lid)
    return before["site_type"] != after["site_type"] or before["platform_name"] != after["platform_name"]


def run_job(ids):
    JOB.update(running=True, done=0, total=len(ids), changed=0, error="", stop=False)
    try:
        for lid in ids:
            if JOB["stop"]:
                break
            with connect() as con:
                JOB["current"] = (con.execute("SELECT title FROM leads WHERE id = ?", (lid,)).fetchone() or [""])[0]
            if check_lead(lid):
                JOB["changed"] += 1
            JOB["done"] += 1
    except presence.Throttled:
        JOB["error"] = ("Os buscadores bloquearam as consultas por enquanto. O que já foi verificado ficou salvo; "
                        "clique em Continuar daqui a uns 30 minutos.")
    except Exception as e:  # keep the server alive whatever happens in a lead
        JOB["error"] = f"Erro na verificação: {e}"
    finally:
        JOB.update(running=False, current="")
    print(f"✓ Validação de presença: {JOB['done']} verificados, {JOB['changed']} reclassificados.")


def leads_to_check(con, everything):
    """Leads whose Maps entry shows no own website, not yet checked (or all of them again)."""
    out = []
    for r in con.execute("SELECT id, website, web_checked_at FROM leads ORDER BY COALESCE(reviews, 0) DESC"):  # best leads first
        if presence.classify(r["website"])[0] == "site":
            continue
        if everything or not r["web_checked_at"]:
            out.append(r["id"])
    return out


def record_message(con, lid, direction, channel, body, created_at=None, wa_msg_id=None, wa_status=None, template=None):
    """Store a message and move the pipeline forward on first contact / first reply, never backwards."""
    ts = now()
    con.execute("INSERT INTO messages (lead_id, direction, channel, body, created_at, wa_msg_id, wa_status, template) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (lid, direction, channel, body, created_at or ts, wa_msg_id, wa_status, template))
    if direction == "out":
        con.execute("UPDATE leads SET draft='', updated_at=? WHERE id=?", (ts, lid))
        con.execute("UPDATE leads SET status='contatado' WHERE id=? AND status='novo'", (lid,))
    else:
        con.execute("UPDATE leads SET status='respondeu', updated_at=? "
                    "WHERE id=? AND status IN ('novo','contatado')", (ts, lid))


def lead_for_sender(con, wa_id, name):
    """Find the lead behind an incoming WhatsApp number, or create one for an unknown sender."""
    row = con.execute("SELECT id FROM leads WHERE wa_id = ?", (wa_id,)).fetchone()
    if row:
        return row[0]
    for r in con.execute("SELECT id, phone FROM leads WHERE phone IS NOT NULL AND phone != ''"):
        if wa_id in wa.candidates(r["phone"]):
            return r["id"]
    ts = now()
    cur = con.execute("INSERT INTO leads (place_id, title, phone, category, city, site_type, platform, wa_id, "
                      "created_at, updated_at) VALUES (?, ?, ?, 'Contato pelo WhatsApp', '', '', '', ?, ?, ?)",
                      (f"wa:{wa_id}", name or f"+{wa_id}", f"+{wa_id}", wa_id, ts, ts))
    return cur.lastrowid


def handle_incoming(con, number, name, msg_id, body, ts):
    """One incoming WhatsApp message, from the Cloud API webhook or the WhatsApp Web bridge."""
    if not msg_id or con.execute("SELECT 1 FROM messages WHERE wa_msg_id = ?", (msg_id,)).fetchone():
        return  # deliveries get retried; store each message once
    lid = lead_for_sender(con, number, name)
    record_message(con, lid, "in", "whatsapp", body or "(mensagem vazia)", created_at=ts, wa_msg_id=msg_id)
    con.execute("UPDATE leads SET wa_id = ? WHERE id = ? AND COALESCE(wa_id, '') = ''", (number, lid))
    if wa.is_opt_out(body):
        con.execute("UPDATE leads SET status = 'nao_contatar' WHERE id = ?", (lid,))
    con.execute("INSERT INTO changes (lead_id, kind) VALUES (?, 'in')", (lid,))
    title = con.execute("SELECT title FROM leads WHERE id = ?", (lid,)).fetchone()[0]
    print(f"💬 WhatsApp de {title}: {(body or '')[:60]}")


def update_status(con, msg_id, status, error=""):
    row = con.execute("SELECT id, lead_id, wa_status FROM messages WHERE wa_msg_id = ?", (msg_id,)).fetchone()
    if not row or STATUS_RANK.get(status, 0) < STATUS_RANK.get(row["wa_status"] or "", 0):
        return
    con.execute("UPDATE messages SET wa_status = ?, wa_error = ? WHERE id = ?", (status, error, row["id"]))
    con.execute("INSERT INTO changes (lead_id, kind) VALUES (?, 'status')", (row["lead_id"],))


def handle_webhook(con, payload):
    msgs, statuses = wa.parse_webhook(payload)
    for m in msgs:
        handle_incoming(con, m["from"], m["name"], m["id"], m["body"], m["ts"])
    for s in statuses:
        update_status(con, s["id"], s["status"], s["error"])


# ── WhatsApp Web bridge (crm/wa-bridge) ──
class BridgeError(Exception):
    pass


def bridge_installed():
    return BRIDGE["external"] or os.path.isdir(os.path.join(BRIDGE_DIR, "node_modules", "whatsapp-web.js"))


def bridge_call(method, path, body=None, timeout=5):
    req = urllib.request.Request(f"http://127.0.0.1:{BRIDGE['port']}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", "X-Bridge-Token": BRIDGE["token"]})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error")
        except ValueError:
            msg = None
        raise BridgeError(msg or f"Erro {e.code} na ponte do WhatsApp.")
    except (urllib.error.URLError, OSError):
        raise BridgeError("A ponte do WhatsApp não está respondendo. Reabra o CRM.")


def web_status():
    if not bridge_installed():
        return {"installed": False, "status": "not_installed"}
    p = BRIDGE["proc"]
    if p is not None and p.poll() is not None:
        return {"installed": True, "status": "stopped",
                "error": "A ponte do WhatsApp fechou. Veja a mensagem na janela preta do CRM e reabra o CRM."}
    try:
        s = bridge_call("GET", "/status", timeout=2)
    except BridgeError:
        return {"installed": True, "status": "starting"}
    return {**s, "installed": True}


def bridge_data_dir():
    """Where the WhatsApp Web session lives. On Windows it must be a short path: WhatsApp Web's
    IndexedDB files nest deep, and past 260 characters it fails with a "browser database" error."""
    if os.environ.get("BRIDGE_DATA"):
        return os.environ["BRIDGE_DATA"]
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return os.path.join(os.environ["LOCALAPPDATA"], "crm-leads", "wa-session")
    return os.path.join(os.path.dirname(DB), "wa-session")


def start_bridge(crm_port):
    if BRIDGE["external"] or not bridge_installed():
        return
    node = shutil.which("node")
    if not node:
        print("ℹ Node.js não encontrado: o WhatsApp Web do CRM fica desligado.")
        return
    env = dict(os.environ, BRIDGE_PORT=str(BRIDGE["port"]), BRIDGE_TOKEN=BRIDGE["token"],
               CRM_URL=f"http://127.0.0.1:{crm_port}", BRIDGE_DATA=bridge_data_dir())
    BRIDGE["proc"] = subprocess.Popen([node, "bridge.js"], cwd=BRIDGE_DIR, env=env)
    atexit.register(stop_bridge)


def stop_bridge():
    p = BRIDGE["proc"]
    if not p or p.poll() is not None:
        return
    try:
        bridge_call("POST", "/shutdown", timeout=8)  # lets it close its Chrome cleanly
        p.wait(8)
    except (BridgeError, subprocess.TimeoutExpired):
        p.kill()


def send_whatsapp(con, lid, d):
    row = con.execute("SELECT phone, wa_id, status FROM leads WHERE id = ?", (lid,)).fetchone()
    if not row:
        return 404, {"error": "lead não encontrado"}
    if row["status"] == "nao_contatar":
        return 400, {"error": "Esse lead pediu para não ser contatado."}
    to = row["wa_id"] or wa.to_number(row["phone"])
    if not to:
        return 400, {"error": "Esse lead não tem um telefone válido."}
    if web_status().get("status") == "ready":
        body = (d.get("body") or "").strip()
        if not body:
            return 400, {"error": "Escreva a mensagem primeiro."}
        try:
            res = bridge_call("POST", "/send", {"to": to, "body": body}, timeout=60)
        except BridgeError as e:
            return 502, {"error": str(e)}
        record_message(con, lid, "out", "whatsapp", body, wa_msg_id=res.get("id"), wa_status="sent")
        if res.get("wa_id"):
            con.execute("UPDATE leads SET wa_id = ? WHERE id = ?", (res["wa_id"], lid))
        return 201, lead(con, lid)
    cfg = wa.load()
    if not wa.public(cfg)["configured"]:
        return 400, {"error": "Conecte o WhatsApp primeiro (botão WhatsApp no topo)."}
    template = None
    try:
        if d.get("type") == "template":
            template, params = d.get("name") or "", [wa.clean_param(p) for p in d.get("params") or []]
            if not template:
                return 400, {"error": "Escolha um modelo."}
            if any(not p for p in params):
                return 400, {"error": "Preencha todas as variáveis do modelo."}
            res = wa.send_template(cfg, to, template, d.get("language") or "pt_BR", params, d.get("param_names"))
            body = (d.get("preview") or "").strip() or f"[modelo {template}]"
        else:
            body = (d.get("body") or "").strip()
            if not body:
                return 400, {"error": "Escreva a mensagem primeiro."}
            res = wa.send_text(cfg, to, body)
    except wa.GraphError as e:
        return 502, {"error": str(e)}
    msg_id = (res.get("messages") or [{}])[0].get("id")
    wa_id = (res.get("contacts") or [{}])[0].get("wa_id")
    record_message(con, lid, "out", "whatsapp", body, wa_msg_id=msg_id, wa_status="accepted", template=template)
    if wa_id:
        con.execute("UPDATE leads SET wa_id = ? WHERE id = ?", (wa_id, lid))
    return 201, lead(con, lid)


def wa_counts(con):
    today = datetime.date.today().isoformat()
    sent = con.execute("SELECT COUNT(*) FROM messages WHERE direction = 'out' AND channel = 'whatsapp' "
                       "AND wa_msg_id IS NOT NULL AND created_at >= ?", (today,)).fetchone()[0]
    # first contacts = leads whose first WhatsApp message from us went out today (the risky kind)
    first = con.execute("SELECT COUNT(*) FROM (SELECT MIN(created_at) AS t FROM messages WHERE direction = 'out' "
                        "AND channel = 'whatsapp' AND wa_msg_id IS NOT NULL GROUP BY lead_id) WHERE t >= ?",
                        (today,)).fetchone()[0]
    return {"sent_today": sent, "first_today": first}


def wa_status(con):
    return {**wa.public(wa.load()), **wa_counts(con), "web": web_status()}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    def send(self, code, payload=None, ctype="application/json; charset=utf-8"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def raw_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def body(self):
        return json.loads(self.raw_body() or b"{}")

    def from_this_machine(self):
        """A tunnel also connects from 127.0.0.1, so look at the Host and forwarding headers too."""
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        forwarded = any(self.headers.get(h) for h in ("X-Forwarded-For", "X-Forwarded-Host", "Cf-Connecting-Ip", "Forwarded"))
        return host in ("localhost", "127.0.0.1", "::1") and not forwarded

    def same_origin_json(self):
        """Blocks other websites from driving the API (CSRF): they can't send JSON without a preflight."""
        origin = self.headers.get("Origin")
        port = self.server.server_address[1]
        ok_origin = origin is None or origin in (f"http://localhost:{port}", f"http://127.0.0.1:{port}")
        return ok_origin and (self.headers.get("Content-Type") or "").startswith("application/json")

    def webhook(self, method, query):
        cfg = wa.load()
        if method == "GET":  # Meta's one-time verification handshake
            q = {k: v[0] for k, v in parse_qs(query).items()}
            if q.get("hub.mode") == "subscribe" and cfg.get("verify_token") and q.get("hub.verify_token") == cfg["verify_token"]:
                print("✓ Webhook do WhatsApp verificado pela Meta.")
                return self.send(200, q.get("hub.challenge", "").encode(), "text/plain; charset=utf-8")
            return self.send(403, {"error": "verify token inválido"})
        if method != "POST":
            return self.send(405, {"error": "method not allowed"})
        raw = self.raw_body()
        if not wa.valid_signature(cfg, raw, self.headers.get("X-Hub-Signature-256")):
            print("⚠ Webhook recusado: assinatura inválida (confira a chave secreta do app no CRM).")
            return self.send(403, {"error": "assinatura inválida"})
        with connect() as con:
            handle_webhook(con, json.loads(raw or b"{}"))
        return self.send(200, {"ok": True})

    def route(self, method):
        url = urlparse(self.path)
        path = url.path.rstrip("/") or "/"
        if path == "/webhook":
            return self.webhook(method, url.query)
        if not self.from_this_machine():
            return self.send(403, {"error": "O CRM só aceita acesso deste computador."})
        if method == "GET" and path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                return self.send(200, f.read(), "text/html; charset=utf-8")
        if not path.startswith("/api/"):
            return self.send(404, {"error": "not found"})
        if method != "GET" and not self.same_origin_json():
            return self.send(403, {"error": "requisição recusada"})
        with connect() as con:
            if method == "GET" and path == "/api/leads":
                return self.send(200, all_leads(con))

            # ── online presence check ──
            if method == "GET" and path == "/api/presence":
                return self.send(200, {**JOB, "pending": len(leads_to_check(con, False))})
            if method == "POST" and path == "/api/presence/start":
                if JOB["running"]:
                    return self.send(409, {"error": "A validação já está rodando."})
                ids = leads_to_check(con, bool(self.body().get("all")))
                JOB.update(running=True, done=0, total=len(ids), changed=0, error="", stop=False)
                threading.Thread(target=run_job, args=(ids,), daemon=True).start()
                return self.send(200, JOB)
            if method == "POST" and path == "/api/presence/stop":
                JOB["stop"] = True
                return self.send(200, JOB)
            m = re.fullmatch(r"/api/leads/(\d+)/presence/check", path)
            if m and method == "POST":
                try:
                    check_lead(int(m[1]), retries=1)
                except presence.Throttled:
                    return self.send(429, {"error": "O buscador está limitando as consultas agora. Tente em alguns minutos."})
                return self.send(200, lead(con, int(m[1])))
            m = re.fullmatch(r"/api/leads/(\d+)/links", path)
            if m and method == "POST":
                # add a link by hand, or confirm / reject one the check found
                lid, d = int(m[1]), self.body()
                row = con.execute("SELECT web_links FROM leads WHERE id = ?", (lid,)).fetchone()
                if not row:
                    return self.send(404, {"error": "lead não encontrado"})
                links, url = stored_links(row), (d.get("url") or "").strip()
                if not url:
                    return self.send(400, {"error": "Informe o link."})
                if d.get("action") == "add":
                    if not url.startswith(("http://", "https://")):
                        url = "https://" + url
                    kind, plat, name = presence.classify(url)
                    if kind in ("none", "directory"):
                        kind = "site"
                    links = [l for l in links if l["url"] != url] + [presence.link(url, kind, plat, name, "manual")]
                elif d.get("action") in ("confirm", "reject"):
                    for l in links:
                        if l["url"] == url:
                            l.update(match="forte" if d["action"] == "confirm" else "rejeitado", reviewed=True)
                elif d.get("action") == "remove":
                    links = [l for l in links if l["url"] != url]
                else:
                    return self.send(400, {"error": "ação inválida"})
                con.execute("UPDATE leads SET web_links = ?, updated_at = ? WHERE id = ?",
                            (json.dumps(links, ensure_ascii=False), now(), lid))
                return self.send(200, lead(con, lid))

            if path == "/api/settings":
                if method == "GET":
                    row = con.execute("SELECT value FROM settings WHERE key='config'").fetchone()
                    return self.send(200, json.loads(row[0]) if row else {})
                if method == "PUT":
                    con.execute("INSERT OR REPLACE INTO settings VALUES ('config', ?)",
                                (json.dumps(self.body(), ensure_ascii=False),))
                    return self.send(200, {"ok": True})

            if method == "POST" and path == "/api/import":
                text = (self.body().get("csv") or "").lstrip("﻿")
                rows = list(csv.DictReader(io.StringIO(text)))
                if not rows or "title" not in rows[0]:
                    return self.send(400, {"error": "CSV sem a coluna 'title'. Use um CSV gerado pelo kit."})
                new, total = import_rows(rows)
                return self.send(200, {"rows": len(rows), "new": new, "total": total})

            if method == "GET" and path == "/api/changes":
                # Live updates for the page: leads touched by the webhook since the client's cursor.
                q = parse_qs(url.query)
                seq = con.execute("SELECT COALESCE(MAX(seq), 0) FROM changes").fetchone()[0]
                if "since" not in q:
                    return self.send(200, {"seq": seq, "leads": [], "incoming": []})
                rows = con.execute("SELECT lead_id, kind FROM changes WHERE seq > ?", (int(q["since"][0] or 0),)).fetchall()
                ids = sorted({r[0] for r in rows})
                return self.send(200, {"seq": seq, "leads": [l for l in (lead(con, i) for i in ids) if l],
                                       "incoming": sorted({r[0] for r in rows if r[1] == "in"}),
                                       "web": web_status(), **wa_counts(con)})

            if method == "POST" and path == "/api/bridge/event":
                if not secrets.compare_digest(self.headers.get("X-Bridge-Token") or "", BRIDGE["token"]):
                    return self.send(403, {"error": "token inválido"})
                ev = self.body()
                if ev.get("type") == "message":
                    handle_incoming(con, re.sub(r"\D", "", ev.get("from") or ""), ev.get("name") or "",
                                    ev.get("id"), ev.get("body") or "", wa.local_ts(ev.get("timestamp")))
                elif ev.get("type") == "ack":
                    update_status(con, ev.get("id"), ev.get("status") or "")
                return self.send(200, {"ok": True})

            if method == "GET" and path == "/api/bridge/ping":
                return self.send(200, {"ok": True})
            if method == "GET" and path == "/api/wa/web":
                return self.send(200, web_status())
            if method == "POST" and path == "/api/wa/web/logout":
                try:
                    return self.send(200, bridge_call("POST", "/logout", timeout=20))
                except BridgeError as e:
                    return self.send(502, {"error": str(e)})

            # ── WhatsApp (official Cloud API) ──
            if path == "/api/wa/config":
                if method == "GET":
                    return self.send(200, wa_status(con))
                if method == "PUT":
                    wa.save(self.body())
                    return self.send(200, wa_status(con))
            try:
                if method == "POST" and path == "/api/wa/test":
                    cfg = wa.load()
                    if not wa.public(cfg)["configured"]:
                        return self.send(400, {"error": "Preencha o token e o ID do número."})
                    return self.send(200, wa.phone_info(cfg))
                if method == "GET" and path == "/api/wa/templates":
                    return self.send(200, wa.templates(wa.load()))
                if method == "POST" and path == "/api/wa/templates/suggested":
                    return self.send(200, wa.create_suggested(wa.load()))
            except wa.GraphError as e:
                return self.send(502, {"error": str(e)})

            m = re.fullmatch(r"/api/leads/(\d+)/wa/send", path)
            if m and method == "POST":
                return self.send(*send_whatsapp(con, int(m[1]), self.body()))

            m = re.fullmatch(r"/api/leads/(\d+)", path)
            if m and method == "PATCH":
                data = {k: v for k, v in self.body().items() if k in EDITABLE}
                if "status" in data and data["status"] not in STATUSES:
                    return self.send(400, {"error": "status inválido"})
                if data.get("presence_override") not in (None, "", "sem_site", "so_rede_social", "com_site"):
                    return self.send(400, {"error": "classificação inválida"})
                if data:
                    sets = ", ".join(f"{k} = ?" for k in data)
                    con.execute(f"UPDATE leads SET {sets}, updated_at = ? WHERE id = ?",
                                [*data.values(), now(), int(m[1])])
                return self.send(200, lead(con, int(m[1])))

            m = re.fullmatch(r"/api/leads/(\d+)/messages", path)
            if m and method == "GET":
                rows = con.execute("SELECT * FROM messages WHERE lead_id = ? ORDER BY created_at, id",
                                   (int(m[1]),))
                return self.send(200, [dict(r) for r in rows])
            if m and method == "POST":
                lid, d = int(m[1]), self.body()
                if d.get("direction") not in ("in", "out") or not (d.get("body") or "").strip():
                    return self.send(400, {"error": "mensagem inválida"})
                record_message(con, lid, d["direction"], d.get("channel") or "whatsapp", d["body"].strip())
                return self.send(201, lead(con, lid))

            m = re.fullmatch(r"/api/messages/(\d+)", path)
            if m and method == "DELETE":
                con.execute("DELETE FROM messages WHERE id = ?", (int(m[1]),))
                return self.send(200, {"ok": True})
        return self.send(404, {"error": "not found"})

    def handle_method(self, method):
        try:
            self.route(method)
        except Exception as e:  # report instead of dropping the connection
            self.send(500, {"error": str(e)})

    def do_GET(self): self.handle_method("GET")
    def do_POST(self): self.handle_method("POST")
    def do_PUT(self): self.handle_method("PUT")
    def do_PATCH(self): self.handle_method("PATCH")
    def do_DELETE(self): self.handle_method("DELETE")


class Server(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets a second server bind the same port silently; we want the error.
    allow_reuse_address = os.name != "nt"
    daemon_threads = True


def main():
    ap = argparse.ArgumentParser(description="CRM local de leads")
    ap.add_argument("--import", dest="imp", help="CSV de leads para importar/mesclar")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    # Windows consoles/pipes may use cp1252: never crash on a ✓ or an accented name
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors="replace", line_buffering=True)

    os.makedirs(os.path.dirname(DB), exist_ok=True)
    with connect() as con:
        migrate(con)
        empty = con.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 0
    if a.imp:
        import_csv(a.imp)
    elif empty:
        default = os.path.join(KIT, "leads.csv")
        if os.path.exists(default):
            import_csv(default)
        else:
            print("ℹ Nenhum leads.csv encontrado. Rode com --import <arquivo.csv>.")

    try:
        srv = Server(("127.0.0.1", a.port), Handler)
    except OSError:
        sys.exit(f"✗ A porta {a.port} já está em uso. O CRM já está aberto? Ou use --port 8766.")
    url = f"http://localhost:{a.port}"
    print(f"✓ CRM rodando em {url}  (Ctrl+C para parar)")
    if os.environ.get("CRM_BRIDGE") != "off":
        start_bridge(a.port)
    if not bridge_installed():
        print("ℹ WhatsApp Web desligado. Para enviar pelo CRM, rode instalar-whatsapp.bat uma vez.")
    if wa.public(wa.load())["configured"]:
        print(f"  WhatsApp conectado. Para receber respostas, deixe o túnel apontando para a porta {a.port} (/webhook).")
    if not a.no_browser:
        threading.Timer(0.6, webbrowser.open, (url,)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nCRM parado.")


if __name__ == "__main__":
    main()
