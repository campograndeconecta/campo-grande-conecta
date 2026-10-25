#!/usr/bin/env python3
"""Campo Grande Conecta: noticias generales y deportivas, con fotografías reales.

Fuentes: titulares de RSS Google News, portada de la fuente original si es
públicamente accesible. No inventa fotos, partidos, marcadores ni resultados.
La rutina existente de GitHub Actions sigue usando este mismo archivo.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import Request, urlopen
import base64
import html
import json
import re
import xml.etree.ElementTree as ET

QUERIES = {
    "local": '"Villa Manzano" OR "Sargento Vidal" OR "El Labrador" OR "San Isidro" OR "Zona Marengo" OR "Campo Grande" "Río Negro"',
    "rionegro": '"Río Negro" provincia noticias',
    "neuquen": 'Neuquén provincia noticias',
    "argentina": 'Argentina noticias principales',
    # El deporte local es más específico para evitar otros "Campo Grande" y "San Isidro".
    "sports_local": '("Villa Manzano" OR "Sargento Vidal" OR "Barda del Medio" OR "El Labrador" OR "Zona Marengo" OR "Campo Grande" "Río Negro") (fútbol OR deportes OR torneo OR campeonato OR vóley OR básquet)',
    "sports_region": '("Liga Confluencia" OR "Obrero Dique" OR "Cinco Saltos" OR "Alto Valle" OR "Río Negro" OR "Neuquén") (fútbol OR deportes OR básquet OR vóley OR ciclismo)',
    "sports_argentina": '("selección argentina" OR "Liga Profesional" OR "Copa Argentina" OR "básquet argentino" OR "vóley argentino" OR "deporte argentino")',
    "sports_world": '("Fórmula 1" OR "Champions League" OR "tenis ATP" OR "NBA") deportes',
}
SPORTS = {"sports_local", "sports_region", "sports_argentina", "sports_world"}
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
    "Accept-Language": "es-AR,es;q=0.9,en;q=0.5",
    "Cookie": "CONSENT=YES+cb; SOCS=CAI",
}
IMAGE_RE = re.compile(r'<img\b[^>]*?\bsrc\s*=\s*["\'](https?://[^"\']+)', re.I)
NEWS_FILE = Path("noticias.json")


def valid_web_url(value):
    if not isinstance(value, str):
        return False
    p = urlparse(value)
    return p.scheme in ("https", "http") and bool(p.hostname) and not p.username and not p.password


def clean(value):
    return html.unescape(re.sub(r"<[^>]*>", " ", value or "")).strip()


def fetch(url, timeout=12, max_bytes=400_000, data=None, headers=None):
    req = Request(url, data=data, headers={**HEADERS, **(headers or {})})
    with urlopen(req, timeout=timeout) as response:
        final = response.geturl()
        mime = response.headers.get("Content-Type", "").lower()
        payload = response.read(max_bytes)
    return final, mime, payload


def image_from_rss(item):
    for child in item:
        tag = child.tag.rsplit("}", 1)[-1].lower()
        if tag in ("content", "thumbnail") or (tag == "enclosure" and child.attrib.get("type", "").startswith("image/")):
            candidate = child.attrib.get("url", "")
            if valid_web_url(candidate):
                return candidate
    for field in ("description", "{http://purl.org/rss/1.0/modules/content/}encoded"):
        match = IMAGE_RE.search(html.unescape(item.findtext(field) or ""))
        if match and valid_web_url(html.unescape(match.group(1))):
            return html.unescape(match.group(1))
    return None


class ArticleHeadParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.image = None
        self.summary = None

    def handle_starttag(self, tag, attrs):
        if tag != "meta":
            return
        props = dict(attrs)
        name = (props.get("property") or props.get("name") or "").lower()
        value = html.unescape(props.get("content", "")).strip()
        if name in ("og:image", "og:image:secure_url", "twitter:image", "twitter:image:src") and not self.image:
            self.image = value
        if name in ("og:description", "description", "twitter:description") and not self.summary:
            self.summary = clean(value)


def resolve_google_link(link):
    """Busca la URL del editor. Si Google modifica su API, conserva el RSS."""
    if not valid_web_url(link):
        return None
    if urlparse(link).hostname != "news.google.com":
        return link
    article_id = urlparse(link).path.rstrip("/").split("/")[-1]
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,}", article_id):
        return None
    try:
        decoded = base64.urlsafe_b64decode(article_id + "=" * (-len(article_id) % 4))
        found = re.search(rb"https?://[^\x00-\x20\x7f]+", decoded)
        if found:
            candidate = found.group(0).decode("utf-8", errors="ignore")
            if valid_web_url(candidate):
                return candidate
    except (ValueError, UnicodeError):
        pass
    try:
        final, _, page = fetch("https://news.google.com/rss/articles/" + article_id, timeout=12, max_bytes=850_000)
        if urlparse(final).hostname not in ("news.google.com", "consent.google.com") and valid_web_url(final):
            return final
        body = page.decode("utf-8", errors="replace")
        sig = re.search(r'data-n-a-sg="([^"]+)"', body)
        stamp = re.search(r'data-n-a-ts="([0-9]+)"', body)
        if not sig or not stamp:
            return None
        inner = (
            '["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",'
            'null,1,null,null,null,null,null,0,1],"X","X",1,[1,1,1],1,1,null,0,0,null,0],'
            + json.dumps(article_id) + "," + stamp.group(1) + "," + json.dumps(sig.group(1)) + "]"
        )
        data = urlencode({"f.req": json.dumps([[["Fbv4je", inner, None, "generic"]]])}).encode("utf-8")
        _, _, encoded = fetch(
            "https://news.google.com/_/DotsSplashUi/data/batchexecute?rpcids=Fbv4je",
            timeout=12,
            max_bytes=80_000,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
        )
        raw = encoded.decode("utf-8", errors="replace")
        pos = raw.find("[[")
        if pos < 0:
            return None
        response_rows = json.loads(raw[pos:])
        for row in response_rows:
            if isinstance(row, list) and len(row) > 2 and row[0] == "wrb.fr" and row[1] == "Fbv4je" and row[2]:
                detail = json.loads(row[2])
                if isinstance(detail, list) and len(detail) > 1 and valid_web_url(detail[1]):
                    return detail[1]
    except (OSError, ValueError, TypeError, IndexError, json.JSONDecodeError) as exc:
        print("No se pudo resolver enlace Google News:", str(exc)[:110])
    return None


def image_from_publisher(article_url):
    if not valid_web_url(article_url) or urlparse(article_url).hostname in ("news.google.com", "consent.google.com"):
        return None, None
    try:
        final, mime, content = fetch(article_url, timeout=10, max_bytes=500_000)
        if "text/html" not in mime and "application/xhtml" not in mime:
            return None, None
        parser = ArticleHeadParser()
        parser.feed(content.decode("utf-8", errors="replace"))
        candidate = urljoin(final, parser.image or "") if parser.image else None
        return (candidate if valid_web_url(candidate) else None), parser.summary
    except (OSError, ValueError, UnicodeError) as exc:
        print("Medio inaccesible:", urlparse(article_url).hostname, str(exc)[:75])
        return None, None


def redundant_summary(summary, title, source):
    """RSS Google News suele repetir el título y añadir la firma del medio."""
    if not summary:
        return True
    norm = lambda value: re.sub(r"\W+", " ", (value or "").casefold()).strip()
    clean_summary = norm(summary)
    title = norm(title)
    source = norm(source)
    # Google News añade el nombre del medio al titular y a la descripción.
    if source and title.endswith(" " + source):
        title = title[:-(len(source) + 1)].strip()
    if source and clean_summary.endswith(" " + source):
        clean_summary = clean_summary[:-(len(source) + 1)].strip()
    return clean_summary == title or len(clean_summary) < 25


def enhance(entry):
    if entry.get("image") and not redundant_summary(entry.get("summary"), entry.get("title"), entry.get("source")):
        return entry
    resolved = resolve_google_link(entry["url"])
    if resolved and urlparse(resolved).hostname != "news.google.com":
        image, summary = image_from_publisher(resolved)
        if image:
            entry["image"] = image
            entry["url"] = resolved  # nota original, no redirección de Google News
        if summary and redundant_summary(entry.get("summary"), entry.get("title"), entry.get("source")):
            if not redundant_summary(summary, entry.get("title"), entry.get("source")):
                entry["summary"] = summary[:350]
    return entry


def get_old_sections():
    try:
        return json.loads(NEWS_FILE.read_text(encoding="utf-8")).get("sections", {})
    except (ValueError, OSError):
        return {}


def main():
    previous = get_old_sections()
    preserved = {}
    for sec in previous.values():
        for it in sec:
            if it.get("image") and valid_web_url(it["image"]):
                preserved[(it.get("title") or "").casefold()] = it["image"]
    sections = {}
    for key, query in QUERIES.items():
        feed_url = "https://news.google.com/rss/search?" + urlencode({
            "q": query, "hl": "es-419", "gl": "AR", "ceid": "AR:es-419"
        })
        try:
            _, _, response = fetch(feed_url, timeout=20, max_bytes=2_000_000)
            root = ET.fromstring(response)
            items = []
            limit = 7 if key in SPORTS else 8
            for node in root.findall("./channel/item")[:limit]:
                title = clean(node.findtext("title"))
                source = clean(node.findtext("source")) or title.rsplit(" - ", 1)[-1]
                summary = clean(node.findtext("description"))[:350]
                entry = {
                    "title": title,
                    "source": source,
                    "url": node.findtext("link") or "",
                    "date": node.findtext("pubDate") or "",
                    "summary": summary,
                }
                photo = image_from_rss(node) or preserved.get(title.casefold())
                if photo:
                    entry["image"] = photo
                items.append(entry)
            sections[key] = items
        except (OSError, ET.ParseError, ValueError) as exc:
            print("Falló RSS para", key, str(exc)[:120])
            sections[key] = previous.get(key, [])

    if not any(sections.values()):
        raise SystemExit("No se recuperaron noticias; no se modifica noticias.json")

    pending = [it for key, news in sections.items() for it in news
               if (not it.get("image") or (key in SPORTS and redundant_summary(it.get("summary"), it.get("title"), it.get("source"))))
               and valid_web_url(it.get("url"))]
    with ThreadPoolExecutor(max_workers=7) as pool:
        tasks = [pool.submit(enhance, entry) for entry in pending]
        for task in as_completed(tasks):
            try:
                task.result()
            except Exception as exc:
                print("No se pudo recuperar portada o resumen:", str(exc)[:90])

    for key, items in sections.items():
        count = sum(bool(item.get("image")) for item in items)
        print(f"{key}: {count}/{len(items)} con fotografía")
    payload = {
        "updated": datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC"),
        "sections": sections,
    }
    NEWS_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Actualización terminada: noticias y deportes con fotos reales cuando están disponibles.")

if __name__ == "__main__":
    main()
