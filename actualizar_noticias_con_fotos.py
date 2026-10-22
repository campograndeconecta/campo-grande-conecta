import json
import datetime
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import email.utils
import re
import html
from pathlib import Path

QUERIES = {
    'local': '"Villa Manzano" OR "Sargento Vidal" OR "El Labrador" OR "San Isidro" "Río Negro" OR "Zona Marengo"',
    'rionegro': '"Río Negro" provincia noticias',
    'neuquen': 'Neuquén provincia noticias',
    'argentina': 'Argentina noticias principales',
}
HEADERS = {'User-Agent': 'Mozilla/5.0 CampoGrandeConecta/1.0'}
IMAGE_RE = re.compile(r'<img\b[^>]*\bsrc=["\'](https?://[^"\']+)', re.I)

def image_from_item(item):
    # Only use an image actually supplied by the RSS publisher.
    for child in item:
        name = child.tag.rsplit('}', 1)[-1].lower()
        if name in ('content', 'thumbnail'):
            url = child.attrib.get('url', '')
            if url.startswith(('https://', 'http://')):
                return url
        if name == 'enclosure' and child.attrib.get('type', '').startswith('image/'):
            url = child.attrib.get('url', '')
            if url.startswith(('https://', 'http://')):
                return url
    for field in ('description', '{http://purl.org/rss/1.0/modules/content/}encoded'):
        raw = html.unescape(item.findtext(field) or '')
        match = IMAGE_RE.search(raw)
        if match:
            return html.unescape(match.group(1))
    return None

def clean(text):
    return html.unescape(re.sub(r'<[^>]*>', ' ', text or '')).strip()

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
            entry = {
                'title': title,
                'source': source,
                'url': item.findtext('link') or '',
                'date': item.findtext('pubDate') or '',
                'summary': description[:350],
            }
            image = image_from_item(item)
            if image:
                entry['image'] = image
            items.append(entry)
        sections[key] = items
    except Exception as exc:
        print(f'Error al consultar {key}: {exc}')
        old_path = Path('noticias.json')
        if old_path.exists():
            try:
                sections[key] = json.loads(old_path.read_text(encoding='utf-8')).get('sections', {}).get(key, [])
            except (ValueError, OSError):
                sections[key] = []
        else:
            sections[key] = []

if not any(sections.values()):
    raise SystemExit('No se recuperaron noticias: se conserva el archivo anterior')

output = {
    'updated': datetime.datetime.now(datetime.timezone.utc).strftime('%d/%m/%Y %H:%M UTC'),
    'sections': sections,
}
Path('noticias.json').write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding='utf-8')
print('Noticias actualizadas. Imágenes incluidas cuando el RSS las proporciona.')
