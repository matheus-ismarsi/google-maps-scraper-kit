"""Online presence check for leads: does a business really have no site, or a menu page outside Google?

Google Maps often lists no website for businesses that do have a WhatsMenu, iFood or Anota AI page,
and some "websites" on Maps are really a delivery platform. This module:
- classifies a URL (own site, menu/delivery platform, social profile, link shortener, directory);
- finds links outside Maps: a web search (Bing, DuckDuckGo as fallback), direct probes on platforms whose store
  pages can be verified (WhatsMenu, Foodplus), shorteners resolved and link-in-bio pages expanded;
- keeps only links whose address contains the business name ("forte"); weaker hits are kept as
  suggestions ("parcial") for the user to confirm in the CRM.
Uses only the Python standard library.
"""
import base64, html, json, random, re, time, unicodedata, urllib.error, urllib.parse, urllib.request

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"

# domain -> (kind, platform key used by the CRM, display name). Extend freely.
PLATFORMS = {
    "whatsmenu.com.br": ("cardapio", "cardapio", "WhatsMenu"), "foodplus.com.br": ("cardapio", "cardapio", "Foodplus"),
    "anota.ai": ("cardapio", "cardapio", "Anota AI"), "goomer.app": ("cardapio", "cardapio", "Goomer"),
    "cardapioweb.com": ("cardapio", "cardapio", "Cardápio Web"), "aiqfome.com": ("cardapio", "cardapio", "aiqfome"),
    "menudino.com": ("cardapio", "cardapio", "MenuDino"), "deliverydireto.com.br": ("cardapio", "cardapio", "Delivery Direto"),
    "saipos.com": ("cardapio", "cardapio", "Saipos"), "olaclick.com": ("cardapio", "cardapio", "OlaClick"),
    "instadelivery.com.br": ("cardapio", "cardapio", "InstaDelivery"), "deliverymuch.com.br": ("cardapio", "cardapio", "Delivery Much"),
    "gplustogo.com.br": ("cardapio", "cardapio", "GplusToGo"), "xmenu.com.br": ("cardapio", "cardapio", "xMenu"),
    "wabiz.delivery": ("cardapio", "cardapio", "Wabiz"), "ccmpedidoonline.com.br": ("cardapio", "cardapio", "CCM Pedido Online"),
    "pedir.delivery": ("cardapio", "cardapio", "Pedir Delivery"), "brendi.com.br": ("cardapio", "cardapio", "Brendi"),
    "mechamenozap.com.br": ("cardapio", "cardapio", "Me Chama no Zap"), "mandarpedido.com": ("cardapio", "cardapio", "Mandar Pedido"),
    "viptable.com.br": ("cardapio", "cardapio", "VipTable"), "kyte.site": ("cardapio", "cardapio", "Kyte"),
    "neemo.com.br": ("cardapio", "cardapio", "Neemo"), "menuvem.com.br": ("cardapio", "cardapio", "Menuvem"),
    "ifood.com.br": ("ifood", "ifood", "iFood"), "rappi.com.br": ("ifood", "cardapio", "Rappi"),
    "instagram.com": ("social", "instagram", "Instagram"), "facebook.com": ("social", "facebook", "Facebook"),
    "fb.com": ("social", "facebook", "Facebook"), "tiktok.com": ("social", "tiktok", "TikTok"),
    "linktr.ee": ("social", "linktree", "Linktree"), "linktree.com": ("social", "linktree", "Linktree"),
    "beacons.ai": ("social", "linktree", "Beacons"), "bio.link": ("social", "linktree", "bio.link"),
    "taplink.cc": ("social", "linktree", "Taplink"), "wa.me": ("social", "whatsapp", "WhatsApp"),
    "api.whatsapp.com": ("social", "whatsapp", "WhatsApp"), "whatsapp.com": ("social", "whatsapp", "WhatsApp"),
}
SHORTENERS = {"bit.ly", "tinyurl.com", "cutt.ly", "encurtador.com.br", "abre.ai", "l.ead.me", "rb.gy", "goo.gl", "is.gd", "t.co"}
# Search results that are never the business's own page.
DIRECTORIES = {
    "restaurantguru.com.br", "restaurantguru.com", "tripadvisor.com.br", "tripadvisor.com", "cylex.com.br",
    "locaisdobrasil.com.br", "findglocal.com", "app019.com", "brasillocais.com", "guiamais.com.br", "apontador.com.br",
    "solutudo.com.br", "telelistas.net", "yelp.com", "foursquare.com", "wikipedia.org", "youtube.com", "google.com",
    "google.com.br", "goo.gl", "waze.com", "kekanto.com.br", "encontrasantabarbara.com.br", "econodata.com.br",
    "cnpj.biz", "casadosdados.com.br", "linkedin.com", "twitter.com", "x.com", "pinterest.com", "threads.net",
    "glassdoor.com.br", "indeed.com", "reclameaqui.com.br", "duckduckgo.com", "bing.com", "mercadolivre.com.br",
}
LINK_IN_BIO = {"linktree"}
GENERIC = set("""lanches lanche lanchonete lanchonetes pizzaria pizzarias pizza pizzas restaurante restaurantes hamburgueria
hamburguer hamburger burger burguer burgers bar bistro cafe cafeteria delivery comida comidas japonesa japones sushi temakeria
marmitaria marmitex marmita marmitas self service grill churrascaria espetinho food foods house casa e de da do das dos the and em
sp sbo brasil express oficial unidade loja centro jardim vila""".split())
CITY_WORDS = set("americana limeira nova odessa santa barbara oeste doeste d sao paulo".split())
RANK = {"site": 3, "cardapio": 2, "ifood": 2, "social": 1}


def norm(s):
    s = unicodedata.normalize("NFD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def clean_title(title, city=""):
    """Maps names often carry a tagline: 'Famburger | Hamburgueria em Santa Bárbara' -> 'Famburger',
    and sometimes the tagline comes first: 'Restaurante em Limeira - Pinho's Bistrô' -> 'Pinho's Bistrô'.
    Picks the first part that has a word other than generic and city words."""
    parts = [p.strip() for p in re.split(r"\s+[|–—-]\s+|\s*\|\s*|\s*\(|\)", title or "") if len(p.strip()) >= 3]
    skip = GENERIC | CITY_WORDS | set(norm(city).split())
    for part in parts:
        if any(w not in skip for w in norm(part).split()):
            return part
    return parts[0] if parts else (title or "").strip()


def name_keys(title, city=""):
    """-> (strong key, partial key): the business name squashed, without generic and city words."""
    city_words = set(norm(city).split()) | CITY_WORDS
    words = norm(title).split()
    distinct = [w for w in words if w not in GENERIC and w not in city_words]
    strong = "".join(distinct)
    if not distinct:  # only generic words ("Bar e Restaurante"): no key is safe to match on
        return "", ""
    if len(strong) < 4:  # e.g. "Add Burger": fall back to the whole name
        strong = "".join(w for w in words if w not in {"e", "de", "da", "do", "das", "dos", "the"} and w not in city_words)
    longest = max(distinct, key=len) if distinct else ""
    return strong, (longest if len(longest) >= 6 and longest != strong else "")


def host_of(url):
    try:
        h = (urllib.parse.urlparse(url if "://" in url else "http://" + url).hostname or "").lower()
    except ValueError:
        return ""
    return h[4:] if h.startswith("www.") else h


def domain_of(host):
    parts = host.split(".")
    return ".".join(parts[-3:]) if host.endswith((".com.br", ".net.br", ".org.br")) else ".".join(parts[-2:])


def classify(url):
    """-> (kind, platform key, display name). kind: none, site, cardapio, ifood, social, short, directory."""
    if not url:
        return "none", "", ""
    host = host_of(url)
    if not host:
        return "none", "", ""
    for dom, info in PLATFORMS.items():
        if host == dom or host.endswith("." + dom):
            return info
    if host in SHORTENERS or domain_of(host) in SHORTENERS:
        return "short", "", ""
    if domain_of(host) in DIRECTORIES or host in DIRECTORIES:
        return "directory", "", ""
    return "site", "", ""


def slug_text(url):
    p = urllib.parse.urlparse(url if "://" in url else "http://" + url)
    return re.sub(r"[^a-z0-9]", "", norm((p.hostname or "") + " " + urllib.parse.unquote(p.path)))


def match(url, kind, strong, partial):
    """'forte' when the address carries the business name, 'parcial' for the longest word only."""
    if kind == "site":  # an own site must carry the name in its domain, not just in a path
        text = re.sub(r"[^a-z0-9]", "", domain_of(host_of(url)).split(".")[0])
    else:
        text = slug_text(url)
        for dom in PLATFORMS:  # don't let the platform's own name count as a match
            text = text.replace(re.sub(r"[^a-z0-9]", "", dom), "")
    if strong and strong in text:
        return "forte"
    if partial and partial in text:
        return "parcial"
    return ""


def link(url, kind, platform, name, source, strength="forte"):
    return {"url": url, "kind": kind, "platform": platform, "name": name, "source": source, "match": strength}


def fetch(url, timeout=15, limit=400_000):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "pt-BR,pt;q=0.9"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.geturl(), r.read(limit).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, url, ""
    except Exception:
        return 0, url, ""


class Throttled(Exception):
    pass


def search_bing(query):
    url = "https://www.bing.com/search?" + urllib.parse.urlencode({"q": query, "setlang": "pt-BR", "cc": "BR"})
    code, _, text = fetch(url, timeout=20, limit=800_000)
    if code != 200 or "captcha" in text.lower() or ("b_algo" not in text and "b_no" not in text):
        raise Throttled(f"Bing recusou a consulta (HTTP {code})")
    out = []
    for href in re.findall(r'<li class="b_algo"[^>]*>.*?<a[^>]+href="([^"]+)"', text, re.S):
        href = html.unescape(href)
        enc = urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("u", [""])[0]
        if enc.startswith("a1"):  # bing.com/ck/a?...&u=a1<base64url of the target>
            try:
                href = base64.urlsafe_b64decode(enc[2:] + "=" * (-len(enc[2:]) % 4)).decode("utf-8", "replace")
            except ValueError:
                continue
        if href.startswith("http") and "bing.com/" not in href:
            out.append(href)
    return out


def search_ddg(query):
    url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query, "kl": "br-pt"})
    code, _, text = fetch(url, timeout=20)
    if code != 200 or "anomaly" in text.lower() or ("result__a" not in text and "no-results" not in text):
        raise Throttled(f"DuckDuckGo recusou a consulta (HTTP {code})")
    out = []
    for href in re.findall(r'class="result__a" href="([^"]+)"', text):
        target = urllib.parse.parse_qs(urllib.parse.urlparse(html.unescape(href)).query).get("uddg", [html.unescape(href)])[0]
        if target.startswith("http"):
            out.append(target)
    return out


def search(query):
    """Web results for a query: Bing first, DuckDuckGo if Bing pushes back. Raises Throttled if both do."""
    errors = []
    for engine in (search_bing, search_ddg):
        try:
            return engine(query)
        except Throttled as e:
            errors.append(str(e))
    raise Throttled("; ".join(errors))


def resolve(url):
    code, final, _ = fetch(url, timeout=12, limit=2000)
    return final if code and final != url else ""


def expand_bio(url):
    """Links inside a Linktree-style page that point to a site or a menu/delivery platform."""
    code, _, text = fetch(url, timeout=12)
    if code != 200:
        return []
    found = []
    for href in set(re.findall(r'href="(https?://[^"]+)"', text)):
        href = html.unescape(href)
        kind, plat, name = classify(href)
        if kind in ("cardapio", "ifood", "site") and host_of(href) not in host_of(url):
            found.append(link(href, kind, plat, name, "linktree"))
    return found[:6]


def page_title(text):
    t = re.search(r"<title[^>]*>(.*?)</title>", text or "", re.S | re.I)
    return re.sub(r"[^a-z0-9]", "", norm(html.unescape(t.group(1)))) if t else ""


def probe_platforms(title, strong, city_key):
    """Platforms whose store pages can be checked by URL. Store slugs repeat across cities
    ('famburger' vs 'famburger2'), so a page only counts as 'forte' when it is clearly this store:
    WhatsMenu when its title is exactly the business name, Foodplus when the page names the city."""
    full = re.sub(r"[^a-z0-9]", "", norm(title))
    found = []
    for slug in dict.fromkeys([full, strong]):
        if not slug:
            continue
        code, final, text = fetch(f"https://whatsmenu.com.br/{slug}", timeout=12)
        t = page_title(text)
        if code == 200 and t and strong in t:
            strength = "forte" if t in (full, strong) else "parcial"
            found.append(link(f"https://whatsmenu.com.br/{slug}", "cardapio", "cardapio", "WhatsMenu", "whatsmenu", strength))
            break
    partial = None
    for slug in dict.fromkeys([full, strong, strong + "2", strong + "3"]):
        if not slug:
            continue
        code, final, text = fetch(f"https://site.foodplus.com.br/{slug}", timeout=12)
        if code != 200 or strong not in page_title(text):
            continue
        if city_key and city_key in norm(re.sub(r"<[^>]+>", " ", text)).replace(" ", ""):
            found.append(link(final, "cardapio", "cardapio", "Foodplus", "foodplus"))
            partial = None
            break
        partial = partial or link(final, "cardapio", "cardapio", "Foodplus", "foodplus", "parcial")
    return found + ([partial] if partial else [])


def verify(lead, pause=(2.5, 4.5)):
    """Collect presence links for one lead (dict with title, city, website). Raises Throttled."""
    city, website = lead.get("city") or "", lead.get("website") or ""
    title = clean_title(lead.get("title"), city)
    strong, partial = name_keys(title, city)
    city_key = max((w for w in norm(city).split() if w not in {"d", "doeste", "oeste", "de", "do", "da"}), key=len, default="")
    links = []
    kind, plat, name = classify(website)
    if kind == "short":
        final = resolve(website)
        if final:
            k, p, n = classify(final)
            links.append(link(final, k, p, n, "redirect"))
            kind, plat, name, website = k, p, n, final
    if plat in LINK_IN_BIO:
        links += expand_bio(website)
    if kind == "site":  # an own website on Maps already answers the question
        return links
    if not strong:
        return links
    for i, query in enumerate((f"{title} {city}", f"{title} {city} cardápio delivery")):
        if i and any(l["kind"] in ("site", "cardapio", "ifood") for l in links):
            break
        time.sleep(random.uniform(*pause))
        for url in search(query):
            k, p, n = classify(url)
            if k in ("directory", "short", "none"):
                continue
            strength = match(url, k, strong, partial)
            if not strength or (k == "site" and strength != "forte"):
                continue
            if p == "instagram" and re.search(r"instagram\.com/(p|reel|reels|stories|explore)/", url):
                continue  # a post, not the profile
            if "ifood.com.br/delivery/" in url and city_key and city_key not in slug_text(url):
                continue  # same name, another city's store
            links.append(link(url, k, p, n, "busca", strength))
    if not any(l["kind"] == "cardapio" and l["match"] == "forte" for l in links):
        links += probe_platforms(title, strong, city_key)
    for l in list(links):
        if l["platform"] in LINK_IN_BIO and l["match"] == "forte":
            links += expand_bio(l["url"])
    return dedupe(links)


def dedupe(links):
    seen, out = set(), []
    for l in links:
        key = re.sub(r"[?#].*$", "", l["url"]).rstrip("/").lower()
        if key not in seen:
            seen.add(key)
            out.append(l)
    return out


def effective(row):
    """The presence the CRM shows: Maps' website, corrected by verified links and manual override.
    Mutates and returns the row: site_type/platform become effective, Maps' own values are kept."""
    try:
        links = json.loads(row.get("web_links") or "[]")
    except ValueError:
        links = []
    kind, plat, name = classify(row.get("website"))
    if kind == "short":
        red = next((l for l in links if l.get("source") == "redirect"), None)
        kind, plat, name = (red["kind"], red["platform"], red["name"]) if red else ("site", "", "")
    if kind == "directory":
        kind = "none"
    maps_type = {"none": "sem_site", "site": "com_site"}.get(kind, "so_rede_social")
    strong_links = [l for l in links if l.get("match") == "forte"]
    best = max(strong_links, key=lambda l: RANK.get(l.get("kind"), 0), default=None)
    site_type, off = maps_type, False
    if maps_type != "com_site" and best and RANK.get(best.get("kind"), 0):
        if best["kind"] == "site":
            site_type, plat, name, off = "com_site", "", "", True
        elif maps_type == "sem_site":
            site_type, plat, name, off = "so_rede_social", best["platform"], best["name"], True
    if row.get("presence_override") in ("sem_site", "so_rede_social", "com_site"):
        site_type = row["presence_override"]
        off = off and site_type != "sem_site"
    if not row.get("instagram"):
        ig = next((l["url"] for l in strong_links if l.get("platform") == "instagram"), "")
        row["instagram"] = ig
    row.update(maps_site_type=maps_type, site_type=site_type, platform=plat, platform_name=name,
               off_google=off, web_links=links)
    return row
