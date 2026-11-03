#!/usr/bin/env python3
"""Campo Grande Conecta: noticias generales y deportivas, con fotografías reales.

Fuentes: titulares de RSS Google News, portada de la fuente original si es
públicamente accesible. No inventa fotos, partidos, marcadores ni resultados.
La rutina existente de GitHub Actions sigue usando este mismo archivo.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
import unicodedata
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import Request, urlopen
import base64
import html
import json
import re
import xml.etree.ElementTree as ET

# Búsquedas acotadas al período reciente. Google Noticias puede devolver
# titulares antiguos; además se comprueba la fecha publicada de cada nota.
QUERIES = {
    "local": '("Villa Manzano" OR "Sargento Vidal" OR "Barda del Medio" OR "Contralmirante Cordero" OR "Campo Grande Río Negro" OR "El Labrador Río Negro" OR "San Isidro Campo Grande Río Negro" OR "Zona Marengo Campo Grande") when:3d',
    "rionegro": '"Río Negro" provincia actualidad noticias when:7d',
    "neuquen": 'Neuquén provincia actualidad noticias when:7d',
    "argentina": 'Argentina últimas noticias actualidad when:7d',
    "sports_local": '("Villa Manzano" OR "Sargento Vidal" OR "Barda del Medio" OR "Obrero Dique" OR "Campo Grande Río Negro") (fútbol OR vóley OR newcom OR básquet OR hockey OR deporte) when:30d',
    "sports_region": '("Liga Confluencia" OR "Alto Valle" OR "Cipolletti" OR "Neuquén" OR "Río Negro") (fútbol OR básquet OR hockey OR vóley OR ciclismo) when:7d',
    "sports_argentina": '("Liga Profesional" OR "Selección Argentina" OR "Copa Argentina" OR "básquet argentino" OR "hockey argentino" OR "Las Leonas") deporte when:7d',
    "sports_world": '("Fórmula 1" OR "NBA" OR "Champions League" OR "tenis ATP") deportes when:7d',
}
SPORTS = {"sports_local", "sports_region", "sports_argentina", "sports_world"}
# La portada sólo considera actual una nota publicada en los últimos días.
# No se rellena con notas viejas cuando no hay novedades en las localidades.
MAX_AGE_DAYS = {"local": 3, "sports_local": 3}
DEFAULT_MAX_AGE_DAYS = 2


def normalized(text):
    """Comparaciones sin tildes ni diferencias entre mayúsculas y minúsculas."""
    value = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in value if not unicodedata.combining(c)).casefold()


# Palabras deportivas explícitas: "partido", "club" y "selección" solos
# también aparecen en política y policiales y no son evidencia suficiente.
SPORT_WORDS = re.compile(
    r"\b(futbol|futbolistas?|liga confluencia|liga profesional|federal a|"
    r"basquet(?:bol)?|basquetbolistas?|basketball|basket|hockey|leonas|"
    r"voley|voleibol|newcom|ciclismo|atletismo|maraton|patin|"
    r"tenis|rugby|formula 1|formula uno|f1|colapinto|nba|euroliga|"
    r"champions(?: league)?|copa argentina|copa de la liga|"
    r"juegos binacionales|juegos olimpicos|araucania|messi|scaloni|superclasico|"
    r"futsal|futbol de salon|handball|balonmano|padel|pelota paleta|"
    r"boxeo|boxeadores?|natacion|triatlon|duatlon|running|trail|"
    r"gimnasia|taekwondo|karate|judo|aikido|kickboxing|artes marciales|"
    r"automovilismo|motociclismo|motocross|enduro|karting|bmx|skate|"
    r"canotaje|remo|pesca deportiva|ajedrez|bochas|equitacion|esgrima|"
    r"tenis de mesa|levantamiento de pesas|powerlifting)\b"
)
POLICE_WORDS = re.compile(
    r"\b(policial(?:es)?|policia(?:s)?|robo(?:s)?|robaron|robado[sa]?|"
    r"asalto(?:s)?|asaltaron|homicidio(?:s)?|femicidio(?:s)?|asesin\w*|"
    r"crimen(?:es)?|delito(?:s)?|delincuente(?:s)?|detenid[oa]s?|"
    r"detencion(?:es)?|allanamiento(?:s)?|narcotrafico|balacera(?:s)?|"
    r"balead[oa]s?|violacion(?:es)?|abuso sexual|accidente(?:s)?|"
    r"choque(?:s)?|estafa(?:s)?|secuestro(?:s)?|ataque(?:s)?|explosiv\w*|"
    r"tiroteo(?:s)?|atentado(?:s)?|violencia|agresion(?:es)?|apunal\w*)\b"
)


LOCAL_SPORT_CONTEXT = re.compile(
    r"\b(deporte(?:s)?|deportiv\w*|torneos?|campeonatos?|competencias?|"
    r"competiciones?|olimpiadas?|bailes?|danzas?|bailarin\w*|folklore|folclore|"
    r"taller de (?:tango|salsa|bachata)|zumba)\b"
)


def is_sport_article(title, summary="", source=""):
    title = normalized(title.rsplit(" - ", 1)[0])
    summary = normalized(summary)
    if POLICE_WORDS.search(title + " " + summary):
        return False
    return bool(SPORT_WORDS.search(title) or (
        LOCAL_SPORT_CONTEXT.search(title) and trusted_locality(title, source, summary)
    ))


# Una consulta por disciplina evita que fútbol o F1 oculten básquet y hockey.
SPORT_GEOGRAPHY = {
    "sports_local": '("Villa Manzano" OR "Sargento Vidal" OR "Barda del Medio" OR "Campo Grande Río Negro" OR "Obrero Dique" OR "Zona Marengo" OR "San Isidro Campo Grande" OR "El Labrador Río Negro")',
    "sports_region": '("Liga Confluencia" OR "Alto Valle" OR "Cipolletti" OR "Neuquén" OR "Río Negro")',
    "sports_argentina": 'Argentina',
    "sports_world": 'deportes',
}
SPORT_DISCIPLINES = (
    '(fútbol OR "Liga Confluencia" OR "Liga Profesional" OR "Champions League")',
    '(básquet OR basquetbol OR basketball OR NBA)',
    '(hockey OR "Las Leonas")',
)


def queries_for_section(key):
    if key not in SPORTS:
        return [QUERIES[key]]
    days = 3 if key == "sports_local" else 2
    disciplines = list(SPORT_DISCIPLINES)
    if key == "sports_local":
        # Cobertura abierta, incluyendo disciplinas no enumeradas.
        disciplines += [
            '(deportes OR deportivo OR deportiva OR torneo OR campeonato OR competencia)',
            '(vóley OR newcom OR handball OR futsal OR pádel OR patín OR gimnasia)',
            '(ciclismo OR atletismo OR maratón OR natación OR boxeo OR "artes marciales")',
            '(automovilismo OR motocross OR enduro OR canotaje OR "pesca deportiva" OR ajedrez OR bochas)',
            '(baile OR danza OR bailarines OR folklore OR folclore OR zumba)',
        ]
    return [f"{SPORT_GEOGRAPHY[key]} {discipline} when:{days}d"
            for discipline in disciplines] + [QUERIES[key]]


def balanced_items(buckets, limit):
    """Alterna consultas y elimina duplicados antes de aplicar el cupo."""
    result, seen = [], set()
    for index in range(max((len(bucket) for bucket in buckets), default=0)):
        for bucket in buckets:
            if index >= len(bucket):
                continue
            item = bucket[index]
            fingerprint = news_fingerprint(item["title"])
            if fingerprint not in seen:
                seen.add(fingerprint)
                result.append(item)
                if len(result) == limit:
                    return result
    return result


LOCAL_WORDS = re.compile(
    r"\b(villa manzano|sargento vidal|barda del medio|zona marengo|"
    r"campo grande|obrero dique|el labrador)\b"
)
REGION_WORDS = re.compile(
    r"\b(rio negro|neuquen|alto valle|liga confluencia|cipolletti|"
    r"cinco saltos|general roca|allen|villa regina|obrero dique|bariloche)\b"
)


def trusted_locality(title, source="", summary=""):
    """Detecta noticias de Campo Grande (Río Negro) por evidencia positiva.

    Ante una coincidencia ambigua, devuelve None en lugar de inventar ubicación.
    """
    title = normalized(title.rsplit(" - ", 1)[0])
    source = normalized(source)
    summary = normalized(summary)
    evidence = " ".join((title, source, summary))
    if re.search(
        r"\b(galdar|papaguevos|canarias|gran canaria|tenerife|las palmas|"
        r"salamanca|espana|madrid|barcelona|jockey club|beccar|boulogne|"
        r"vicente lopez|conurbano|hipodromo|mato grosso|brasil|paraguay|misiones)\b",
        evidence,
    ):
        return None
    direct = (
        ("Villa Manzano", "villa manzano"),
        ("Sargento Vidal", "sargento vidal"),
        ("Barda del Medio", "barda del medio"),
        ("Contralmirante Cordero", "contralmirante cordero"),
    )
    for label, phrase in direct:
        if re.search(r"\b" + re.escape(phrase) + r"\b", title):
            return label
    local_geo = bool(re.search(
        r"\b(rio negro|villa manzano|sargento vidal|barda del medio|"
        r"contralmirante cordero|alto valle|ruta 69|ruta 151)\b", title
    ))
    regional_source = bool(re.search(
        r"\b(gobierno de rio negro|diario rio negro|lm cipolletti|noticiasnqn|"
        r"policia de rio negro|municipalidad de campo grande|diario neuquino)\b",
        source,
    ))
    if re.search(r"\bcampo grande\b", title) and (local_geo or regional_source):
        if re.search(r"\bsan isidro\b", title):
            return "San Isidro"
        if re.search(r"\bel labrador\b", title):
            return "El Labrador"
        if re.search(r"\bzona marengo\b", title):
            return "Zona Marengo"
        return "Campo Grande"
    if re.search(r"\bsan isidro\b", title) and local_geo:
        return "San Isidro"
    if re.search(r"\bel labrador\b", title) and local_geo and not re.search(
        r"\b(refran|perro|canino|mascota|cachorro)\b", title
    ):
        return "El Labrador"
    if re.search(r"\bzona marengo\b", title) and (local_geo or regional_source):
        return "Zona Marengo"
    return None


def relevant_title(title, key, source="", summary=""):
    """Exige contexto geográfico real para noticias de localidades pequeñas."""
    title_only = normalized(title.rsplit(" - ", 1)[0])
    if key == "local":
        return bool(trusted_locality(title, source, summary))
    if key in SPORTS and not is_sport_article(title, summary, source):
        return False
    if key == "sports_local":
        return bool(trusted_locality(title, source, summary))
    if key == "sports_region":
        return bool(REGION_WORDS.search(title_only))
    return True


def recent_publication(pub_date, key, now=None):
    """Acepta una noticia sólo si fue publicada en el período mostrado."""
    if not pub_date:
        return False
    try:
        published = parsedate_to_datetime(pub_date)
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, IndexError):
        return False
    now = now or datetime.now(timezone.utc)
    age = now - published.astimezone(timezone.utc)
    return timedelta(days=-1) <= age <= timedelta(days=MAX_AGE_DAYS.get(key, DEFAULT_MAX_AGE_DAYS))


def news_fingerprint(title):
    """Evita titulares idénticos de una misma búsqueda."""
    return re.sub(r"\W+", "", title.rsplit(" - ", 1)[0].casefold())


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
    now = datetime.now(timezone.utc)
    for key, query in QUERIES.items():
        limit = None if key == "sports_local" else (7 if key in SPORTS else 8)
        buckets = []
        for query in queries_for_section(key):
            if key == "argentina":
                feed_url = "https://news.google.com/rss?" + urlencode({
                    "hl": "es-419", "gl": "AR", "ceid": "AR:es-419"
                })
            else:
                feed_url = "https://news.google.com/rss/search?" + urlencode({
                    "q": query, "hl": "es-419", "gl": "AR", "ceid": "AR:es-419"
                })
            try:
                _, _, response = fetch(feed_url, timeout=20, max_bytes=2_000_000)
                root = ET.fromstring(response)
                items = []
                for node in root.findall("./channel/item"):
                    title = clean(node.findtext("title"))
                    date = node.findtext("pubDate") or ""
                    source = clean(node.findtext("source")) or title.rsplit(" - ", 1)[-1]
                    summary = clean(node.findtext("description"))[:350]
                    if not title or not recent_publication(date, key, now) or not relevant_title(title, key, source, summary):
                        continue
                    entry = {
                        "title": title, "source": source,
                        "url": node.findtext("link") or "", "date": date,
                        "summary": summary,
                    }
                    photo = image_from_rss(node) or preserved.get(title.casefold())
                    if photo:
                        entry["image"] = photo
                    items.append(entry)
                items.sort(key=lambda i: parsedate_to_datetime(i["date"]), reverse=True)
                buckets.append(items)
            except (OSError, ET.ParseError, ValueError, TypeError) as exc:
                print("Falló RSS para", key, str(exc)[:120])
                # Un error de una consulta no descarta las otras disciplinas.
                buckets.append([it for it in previous.get(key, [])
                                if recent_publication(it.get("date"), key, now)
                                and relevant_title(it.get("title", ""), key,
                                                   it.get("source", ""), it.get("summary", ""))])
        sections[key] = balanced_items(buckets, limit)
        sections[key].sort(key=lambda i: parsedate_to_datetime(i["date"]), reverse=True)

    if not any(sections.values()):
        # No sobrescribir los datos publicados cuando fallen todas las fuentes.
        raise SystemExit("No se recuperaron noticias recientes; se mantiene noticias.json")

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

    for key in SPORTS:
        sections[key] = [it for it in sections[key] if is_sport_article(it["title"], it.get("summary", ""), it.get("source", ""))]

    for key, items in sections.items():
        print(f"{key}: {len(items)} notas recientes; {sum(bool(it.get('image')) for it in items)} con fotografía")
    payload = {
        "updated": now.strftime("%d/%m/%Y %H:%M UTC"),
        "sections": sections,
    }
    NEWS_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Actualización terminada: se descartan las noticias antiguas de los feeds.")


if __name__ == "__main__":
    main()
