"""WhatsApp Business Cloud API (Meta's official API) for the CRM: send, templates, webhook.

Config lives in crm/data/whatsapp.json (gitignored). The access token and app secret stay on
this machine: the browser only ever sees whether they are set.
Uses only the Python standard library.
"""
import datetime, hashlib, hmac, json, os, re, secrets, unicodedata, urllib.error, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.environ.get("CRM_WA_CONFIG") or os.path.join(HERE, "data", "whatsapp.json")
GRAPH = os.environ.get("WA_GRAPH_BASE") or "https://graph.facebook.com"  # WA_GRAPH_BASE: tests use a mock
DEFAULT_VERSION = "v23.0"
FIELDS = ("token", "phone_number_id", "waba_id", "app_secret", "verify_token", "api_version")
SECRET_FIELDS = ("token", "app_secret")


def load():
    try:
        with open(CONFIG, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}
    cfg["api_version"] = cfg.get("api_version") or DEFAULT_VERSION
    return cfg


def save(update):
    """Merge non-empty fields into the stored config. A blank secret keeps the stored one."""
    cfg = load()
    for k in FIELDS:
        v = update.get(k)
        if v is None or (k in SECRET_FIELDS and not str(v).strip()):
            continue
        cfg[k] = str(v).strip()
    cfg["verify_token"] = cfg.get("verify_token") or secrets.token_urlsafe(18)
    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    with open(CONFIG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    return cfg


def public(cfg):
    """The browser-safe view of the config: never the token or the app secret."""
    token = cfg.get("token", "")
    return {"configured": bool(token and cfg.get("phone_number_id")),
            "phone_number_id": cfg.get("phone_number_id", ""), "waba_id": cfg.get("waba_id", ""),
            "api_version": cfg.get("api_version") or DEFAULT_VERSION,
            "verify_token": cfg.get("verify_token", ""),
            "has_token": bool(token), "token_hint": "…" + token[-4:] if token else "",
            "has_secret": bool(cfg.get("app_secret"))}


# ── Graph API ──
FRIENDLY = {
    190: "Token de acesso inválido ou expirado. Gere um token permanente (usuário do sistema) e salve de novo.",
    131047: "Passaram mais de 24h desde a última mensagem do lead. Fora dessa janela só dá para enviar um modelo aprovado.",
    131026: "Não foi possível entregar: o número pode não ter WhatsApp.",
    131030: "Esse número não está na lista de destinatários de teste. Cadastre-o em WhatsApp > Configuração da API, ou use seu número de produção.",
    131042: "Problema de pagamento na conta do WhatsApp Business. Confira a forma de pagamento no WhatsApp Manager.",
    131048: "A Meta limitou os envios deste número (muitas mensagens sem resposta ou bloqueios). Espere e envie menos por dia.",
    131049: "A Meta não entregou para preservar a experiência do usuário (limite de mensagens de marketing para essa pessoa). Tente outro dia.",
    131056: "Muitas mensagens para o mesmo número em pouco tempo. Espere um pouco.",
    130429: "Limite de velocidade da API atingido. Espere um pouco.",
    132000: "A quantidade de variáveis não bate com o modelo.",
    132001: "Esse modelo não existe ou ainda não foi aprovado nesse idioma.",
    132015: "Esse modelo foi pausado pela Meta por baixa qualidade.",
    133010: "O número ainda não está registrado na Cloud API.",
    368: "A conta foi bloqueada temporariamente por violar políticas do WhatsApp.",
}


class GraphError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def friendly(code, detail):
    return FRIENDLY.get(code) or detail or f"Erro {code}"


def graph(cfg, method, path, body=None, query=""):
    url = f"{GRAPH}/{cfg.get('api_version') or DEFAULT_VERSION}/{path}" + (f"?{query}" if query else "")
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {cfg.get('token', '')}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read()).get("error", {})
        except ValueError:
            err = {}
        code = err.get("code", e.code)
        detail = (err.get("error_data") or {}).get("details") or err.get("error_user_msg") or err.get("message")
        raise GraphError(code, f"{friendly(code, detail)} (código {code})")
    except urllib.error.URLError as e:
        raise GraphError(0, f"Sem conexão com a Meta: {e.reason}")


def phone_info(cfg):
    return graph(cfg, "GET", cfg["phone_number_id"], query="fields=display_phone_number,verified_name,quality_rating")


def templates(cfg):
    if not cfg.get("waba_id"):
        raise GraphError(0, "Preencha o ID da conta do WhatsApp Business para listar os modelos.")
    res = graph(cfg, "GET", f"{cfg['waba_id']}/message_templates",
                query="fields=name,status,language,category,components,rejected_reason&limit=200")
    return res.get("data", [])


def clean_param(v):
    """Template variables may not contain newlines, tabs or runs of 4+ spaces."""
    return re.sub(r"\s+", " ", str(v or "")).strip()


def send_text(cfg, to, body):
    return graph(cfg, "POST", f"{cfg['phone_number_id']}/messages",
                 {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to,
                  "type": "text", "text": {"preview_url": False, "body": body}})


def send_template(cfg, to, name, language, params, param_names=None):
    components = []
    if params:
        if param_names:
            parameters = [{"type": "text", "parameter_name": n, "text": v} for n, v in zip(param_names, params)]
        else:
            parameters = [{"type": "text", "text": v} for v in params]
        components = [{"type": "body", "parameters": parameters}]
    return graph(cfg, "POST", f"{cfg['phone_number_id']}/messages",
                 {"messaging_product": "whatsapp", "to": to, "type": "template",
                  "template": {"name": name, "language": {"code": language}, "components": components}})


# Suggested first-contact templates, one per web presence. Meta reviews them before use.
# The footer gives an easy opt-out; an incoming "SAIR" moves the lead to "Não contatar".
FOOTER = "Responda SAIR para não receber mais mensagens."
SUGGESTED = [
    {"name": "prospeccao_sem_site",
     "text": "Olá, tudo bem? Aqui é {{1}}.\n\nEncontrei a {{2}} no Google Maps, em {{3}}, e vi que vocês ainda "
             "não têm um site.\n\n{{4}}\n\nPosso te mandar um exemplo de como ficaria? É rapidinho e sem compromisso.",
     "example": ["Ana, da Agência X", "Pizzaria Exemplo", "Americana",
                 "Um cardápio online com pedido direto no WhatsApp ajuda a vender mais, sem comissão de aplicativo."]},
    {"name": "prospeccao_rede_social",
     "text": "Olá, tudo bem? Aqui é {{1}}.\n\nVi a {{2}} no Google Maps e reparei que o link de vocês leva direto "
             "para o {{3}}. Dá para ter também uma página própria com cardápio, horários e pedido direto no "
             "WhatsApp.\n\n{{4}}\n\nPosso te mostrar um exemplo?",
     "example": ["Ana, da Agência X", "Hamburgueria Exemplo", "Instagram",
                 "Um cardápio digital com fotos e pedido direto no WhatsApp ajuda a vender mais combos."]},
    {"name": "prospeccao_com_site",
     "text": "Olá, tudo bem? Aqui é {{1}}.\n\nVi a {{2}} no Google Maps e dei uma olhada no site de vocês."
             "\n\n{{3}}\n\nTenho algumas ideias para trazer mais pedidos direto pelo site. Posso te mandar?",
     "example": ["Ana, da Agência X", "Restaurante Exemplo",
                 "Um botão de pedido pelo WhatsApp e o cardápio atualizado ajudam a converter mais visitas."]},
]


def create_suggested(cfg):
    if not cfg.get("waba_id"):
        raise GraphError(0, "Preencha o ID da conta do WhatsApp Business primeiro.")
    out = []
    for t in SUGGESTED:
        body = {"name": t["name"], "language": "pt_BR", "category": "MARKETING",
                "components": [{"type": "BODY", "text": t["text"], "example": {"body_text": [t["example"]]}},
                               {"type": "FOOTER", "text": FOOTER}]}
        try:
            res = graph(cfg, "POST", f"{cfg['waba_id']}/message_templates", body)
            out.append({"name": t["name"], "ok": True, "status": res.get("status", "PENDING")})
        except GraphError as e:
            exists = "already" in str(e).lower() or "já existe" in str(e).lower()
            out.append({"name": t["name"], "ok": exists, "status": "EXISTE" if exists else "ERRO",
                        "error": "" if exists else str(e)})
    return out


# ── phone numbers ──
def to_number(phone):
    """Digits WhatsApp expects for a Brazilian number: 55 + DDD + number."""
    d = re.sub(r"\D", "", phone or "")
    if d.startswith("55") and len(d) >= 12:
        return d
    d = d.lstrip("0")
    return "55" + d if len(d) in (10, 11) else ""


def candidates(phone):
    """Every form WhatsApp may report a Brazilian number in: with and without the mobile 9th digit."""
    d = to_number(phone)
    if not d:
        return set()
    local = d[2:]
    if len(local) == 11 and local[2] == "9":
        return {d, "55" + local[:2] + local[3:]}
    if len(local) == 10 and local[2] in "6789":
        return {d, "55" + local[:2] + "9" + local[2:]}
    return {d}


# ── webhook ──
def valid_signature(cfg, raw, header):
    secret = cfg.get("app_secret") or ""
    if not secret or not (header or "").startswith("sha256="):
        return False
    mac = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(mac, header[7:])


MEDIA = {"image": "📷 Imagem", "audio": "🎤 Áudio", "video": "🎬 Vídeo", "document": "📄 Documento",
         "sticker": "Figurinha", "location": "📍 Localização", "contacts": "👤 Contato"}


def message_text(m):
    t = m.get("type")
    part = m.get(t) if isinstance(m.get(t), dict) else {}
    if t == "text":
        return part.get("body", "")
    if t == "button":
        return part.get("text", "")
    if t == "interactive":
        reply = part.get("button_reply") or part.get("list_reply") or {}
        return reply.get("title", "")
    if t == "reaction":
        return f"Reagiu com {part.get('emoji', '')}"
    caption = part.get("caption", "")
    return f"[{MEDIA.get(t, t)} recebido. O CRM ainda não baixa mídias.]" + (f"\n{caption}" if caption else "")


def local_ts(unix):
    try:
        return datetime.datetime.fromtimestamp(int(unix)).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        return datetime.datetime.now().isoformat(timespec="seconds")


def parse_webhook(payload):
    """Cloud API webhook body -> (incoming messages, delivery statuses)."""
    msgs, statuses = [], []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            v = change.get("value", {})
            names = {c.get("wa_id"): (c.get("profile") or {}).get("name", "") for c in v.get("contacts", [])}
            for m in v.get("messages", []):
                msgs.append({"from": m.get("from", ""), "id": m.get("id"), "ts": local_ts(m.get("timestamp")),
                             "name": names.get(m.get("from"), ""), "body": message_text(m)})
            for s in v.get("statuses", []):
                err = (s.get("errors") or [{}])[0]
                detail = (err.get("error_data") or {}).get("details") or err.get("title") or err.get("message")
                statuses.append({"id": s.get("id"), "status": s.get("status", ""),
                                 "error": friendly(err.get("code"), detail) if err else ""})
    return msgs, statuses


OPT_OUT = {"sair", "pare", "parar", "stop", "cancelar", "descadastrar", "remover", "nao quero", "nao tenho interesse"}


def is_opt_out(text):
    t = unicodedata.normalize("NFD", text or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z ]", "", t).strip() in OPT_OUT
