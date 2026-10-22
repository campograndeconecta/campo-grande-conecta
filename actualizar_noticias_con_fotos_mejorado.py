"""Actualiza noticias.json e intenta recuperar fotos reales de las noticias."""
import json
import datetime
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import re
import html
from html.parser import HTMLParser
from pathlib import Path

QUERIES = {
    'local': '"Villa Manzano" OR "Sargento Vidal" OR "El Labrador" OR "San Isidro" OR "Zona Marengo" OR "Campo Grande" "Río Negro"',
    'rionegro': '"Río Negro" provincia noticias',
    'neuquen': 'Neuquén provincia noticias',
    'argentina': 'Argentina noticias principales',
}
HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; CampoGrandeConecta/1.1)'}
IMAGE_RE = re.compile(r'<img\b[^>]*\bsrc=["\'](https?://[^"\']+)', re.I)


def valid_url(url):
    return isinstance(url, str) and url.startswith(('https://', 'http://'))


def clean(text):
    return html.unescape(re.sub(r'<[^>]*>', ' ', text or '')).strip()


def image_from_item(item):
    for child in item:
        name = child.tag.rsplit('}', 1)[-1].lower()
        if name in ('content', 'thumbnail'):
            url = child.attrib.get('url', '')
            if valid_url(url):
                return url
        if name == 'enclosure' and child.attrib.get('type', '').startswith('image/'):
            url = child.attrib.get('url', '')
            if valid_url(url):
                return url
    for field in ('description', '{http://purl.org/rss/1.0/modules/content/}encoded'):
        raw = html.unescape(item.findtext(field) or '')
        match = IMAGE_RE.search(raw)
        if match:
            return html.unescape(match.group(1))
    return None


class ArticleImageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.image = None
        self.canonical = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'meta':
            key = (attrs.get('property') or attrs.get('name') or '').lower()
            if key in ('og:image', 'og:image:url', 'twitter:image', 'twitter:image:src'):
                candidate = html.unescape(attrs.get('content', ''))
                if valid_url(candidate) and not self.image:
                    self.image = candidate
        elif tag == 'link' and attrs.get('rel', '').lower() == 'canonical':
            self.canonical = attrs.get('href')


def image_from_article(url):
    """Solo lee metadatos públicos; algunas webs bloquean acceso o no incluyen foto."""
    if not valid_url(url):
        return None
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=7) as response:
            if 'text/html' not in response.headers.get('Content-Type', '').lower():
                return None
            # Limitar lectura para evitar descargar páginas enormes.
            content = response.read(350_000).decode('utf-8', errors='replace')
            final_url = response.geturl()
        parser = ArticleImageParser()
        parser.feed(content)
        if parser.image:
            return urllib.parse.urljoin(final_url, parser.image)
    except (OSError, ValueError, UnicodeError):
        pass
    return None


old_path = Path('noticias.json')
try:
    old_sections = json.loads(old_path.read_text(encoding='utf-8')).get('sections', {}) if old_path.exists() else {}
except (ValueError, OSError):
    old_sections = {}

sections = {}
for key, q in QUERIES.items():
    url = 'https://news.google.com/rss/search?' + urllib.parse.urlencode({
        'q': q, 'hl': 'es-419', 'gl': 'AR', 'ceid': 'AR:es-419'
    })
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=20) as response:
            root = ET.fromstring(response.read())
        items = []
        for item in root.findall('./channel/item')[:8]:
            title = clean(item.findtext('title'))
            source = clean(item.findtext('source')) or (title.rsplit(' - ', 1)[-1] if ' - ' in title else 'Medio de noticias')
            description = clean(item.findtext('description'))
            link = item.findtext('link') or ''
            entry = {
                'title': title,
                'source': source,
                'url': link,
                'date': item.findtext('pubDate') or '',
                'summary': description[:350],
            }
            image = image_from_item(item)
            if not image:
                image = image_from_article(link)
            if image:
                entry['image'] = image
            items.append(entry)
        sections[key] = items
        print(f'{key}: {len(items)} noticias, {sum(bool(x.get("image")) for x in items)} con foto')
    except Exception as exc:
        print(f'Error al consultar {key}: {exc}')
        sections[key] = old_sections.get(key, [])

if not any(sections.values()):
    raise SystemExit('No se recuperaron noticias: se conserva el archivo anterior')

output = {
    'updated': datetime.datetime.now(datetime.timezone.utc).strftime('%d/%m/%Y %H:%M UTC'),
    'sections': sections,
}
old_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding='utf-8')
print('Noticias actualizadas. Las fotos aparecen cuando las fuentes permiten recuperarlas.')
