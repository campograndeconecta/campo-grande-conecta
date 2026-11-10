#!/usr/bin/env python3
"""Integra las notas publicadas en seny.com.ar en el muro de Campo Grande Conecta.

No lee Instagram ni Facebook. Nunca atribuye fechas o fotos ficticias.
Se ejecuta tras actualizar_noticias... y actualiza index.html y noticias.json.
"""
import html
import json
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlsplit, urlunsplit, quote
from urllib.request import Request, urlopen

BASE = 'https://seny.com.ar/'
INDEX = Path('index.html')
NEWS = Path('noticias.json')
HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; CampoGrandeConecta/1.0)'}
TZ_AR = timezone(timedelta(hours=-3))
MESES = {'enero':1, 'febrero':2, 'marzo':3, 'abril':4, 'mayo':5, 'junio':6,
         'julio':7, 'agosto':8, 'septiembre':9, 'setiembre':9, 'octubre':10,
         'noviembre':11, 'diciembre':12}
NOTE = re.compile(r'^/[^/?#]+/[^/?#]+_\d+/?$')
START = '<!-- SENY_MURO_INICIO -->'
END = '<!-- SENY_MURO_FIN -->'
MARKER = '<div id="news-list"></div>'
OLD_LINK = re.compile(
    r'<p style="background:#eff5fb;padding:12px;border-radius:10px;font-size:14px">'
    r'📻 Medio de Cinco Saltos: .*?'
    r'Consultá sus noticias locales, regionales, nacionales y deportivas en la fuente original\.</p>',
    re.S,
)


def url_segura(url):
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc,
        quote(p.path, safe="/%:@!$&'()*+,;=-._~"),
        quote(p.query, safe="=&%:@!$'()*+,;/-._~"), ''))


def get(url):
    req = Request(url_segura(url), headers=HEADERS)
    with urlopen(req, timeout=16) as r:
        if urlparse(r.geturl()).hostname not in ('seny.com.ar', 'www.seny.com.ar'):
            raise ValueError('Redirección a fuente externa')
        return r.read(900_000).decode('utf-8', errors='replace')


class Enlaces(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []
    def handle_starttag(self, tag, attrs):
        if tag != 'a':
            return
        link = urljoin(BASE, dict(attrs).get('href', ''))
        p = urlparse(link)
        if p.hostname in ('seny.com.ar', 'www.seny.com.ar') and NOTE.fullmatch(p.path):
            self.urls.append(url_segura(p._replace(query='', fragment='').geturl()))


class Metas(HTMLParser):
    def __init__(self):
        super().__init__()
        self.metadatos = {}
        self.h1 = []
        self.inside = False
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'meta':
            k = a.get('property') or a.get('name') or a.get('itemprop')
            if k and a.get('content'):
                self.metadatos.setdefault(k.lower(), html.unescape(a['content']).strip())
        elif tag == 'h1':
            self.inside = True
    def handle_endtag(self, tag):
        if tag == 'h1':
            self.inside = False
    def handle_data(self, d):
        if self.inside and d.strip():
            self.h1.append(d.strip())


def fecha_articulo(raw, meta):
    for key in ('article:published_time', 'datepublished', 'date', 'publish_date', 'dc.date', 'pubdate'):
        value = meta.get(key, '')
        if value:
            try:
                dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
                return dt if dt.tzinfo else dt.replace(tzinfo=TZ_AR)
            except ValueError:
                try:
                    dt = parsedate_to_datetime(value)
                    return dt if dt.tzinfo else dt.replace(tzinfo=TZ_AR)
                except (TypeError, ValueError):
                    pass
    # En Seny las fechas aparecen junto al título de la noticia.
    visible = html.unescape(re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', raw[:25000])))
    match = re.search(
        r'\b(\d{1,2})\s+de\s+(enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|setiembre|octubre|noviembre|diciembre)\s+de\s+(20\d{2})\b',
        visible, re.I,
    )
    if match:
        d, m, y = match.groups()
        try:
            return datetime(int(y), MESES[m.lower()], int(d), 12, tzinfo=TZ_AR)
        except ValueError:
            pass
    return None


def articulo(url):
    try:
        raw = get(url)
        parser = Metas()
        parser.feed(raw)
        m = parser.metadatos
        title = html.unescape(re.sub(r'\s+', ' ',
            m.get('og:title') or m.get('twitter:title') or ' '.join(parser.h1))).strip()
        title = re.sub(r'\s*[-|]\s*Seny(?:\s+Radio)?(?:\s+99\.9)?\s*$', '', title, flags=re.I)
        if not 12 <= len(title) <= 240:
            return None
        published = fecha_articulo(raw, m)
        if published is None or published.astimezone(timezone.utc) > datetime.now(timezone.utc) + timedelta(days=1):
            return None
        item = {'title': title, 'source': 'Seny Radio 99.9', 'url': url,
                'date': format_datetime(published), 'summary': 'Nota de Seny Radio (Cinco Saltos).'}
        image = m.get('og:image') or m.get('twitter:image')
        if image:
            image = url_segura(urljoin(BASE, image))
            if urlparse(image).scheme == 'https' and urlparse(image).hostname in ('seny.com.ar', 'www.seny.com.ar'):
                item['image'] = image
        return item
    except Exception as ex:
        print('Nota Seny no procesada:', str(ex)[:140])
        return None


def clave(item):
    try:
        return parsedate_to_datetime(item['date']).timestamp()
    except (TypeError, ValueError, KeyError):
        return 0


def obtener():
    p = Enlaces()
    for path in ('', 'actualidad', 'locales', 'deporte', 'editorial'):
        try:
            p.feed(get(urljoin(BASE, path)))
        except Exception as ex:
            print('Sección Seny no disponible:', path, str(ex)[:80])
    urls = list(dict.fromkeys(p.urls))[:35]
    print('Artículos Seny encontrados:', len(urls))
    items = []
    with ThreadPoolExecutor(max_workers=5) as pool:
        for task in as_completed([pool.submit(articulo, url) for url in urls]):
            item = task.result()
            if item:
                items.append(item)
    # No presentar como noticias actuales los textos de años anteriores.
    cutoff = datetime.now(timezone.utc) - timedelta(days=180)
    items = [item for item in items if parsedate_to_datetime(item['date']).astimezone(timezone.utc) >= cutoff]
    items.sort(key=clave, reverse=True)
    return items[:6]


def escapar(value):
    return html.escape(str(value or ''), quote=True)


def crear_muro(items):
    if not items:
        return ''
    now = datetime.now(timezone.utc)
    lines = [
        '<section class="news-group" aria-label="Notas de Seny Radio">',
        '<h3>📻 Seny Radio 99.9 · Cinco Saltos</h3>',
        '<p class="news-group-description">Notas del sitio web de Seny Radio, con fecha de publicación original. '
        'Las publicaciones de Instagram y Facebook no están incluidas.</p>',
        '<div class="news-stack">',
    ]
    for item in items:
        dt = parsedate_to_datetime(item['date']).astimezone(TZ_AR)
        is_archive = now - dt.astimezone(timezone.utc) > timedelta(days=7)
        label = 'Archivo · ' if is_archive else ''
        pic = item.get('image')
        lines.append('<article class="feed-card'+(' news-no-photo' if not pic else '')+'">')
        if pic:
            lines.append('<div class="news-photo"><img loading="lazy" referrerpolicy="no-referrer" '
                'src="'+escapar(pic)+'" alt="Fotografía publicada por Seny Radio" '
                'onerror="this.parentNode.remove()"></div>')
        lines.extend([
            '<div class="news-body">',
            '<span class="news-source">Seny Radio 99.9</span>',
            '<h4>'+escapar(item['title'])+'</h4>',
            '<p class="feed-date">'+label+dt.strftime('%d/%m/%Y')+'</p>',
            '<a class="news-source-link" href="'+escapar(item['url'])+'" '
            'target="_blank" rel="noopener noreferrer">Leer la noticia original ↗</a>',
            '</div></article>',
        ])
    lines.append('</div></section>')
    return START + ''.join(lines) + END


def actualizar_portada(items):
    if not INDEX.exists() or not items:
        return False
    page = INDEX.read_text(encoding='utf-8')
    fragment = crear_muro(items)
    cleaned = OLD_LINK.sub('', page)
    pattern = re.compile(re.escape(START) + r'.*?' + re.escape(END), re.S)
    if pattern.search(cleaned):
        changed = pattern.sub(lambda _: fragment, cleaned, count=1)
    elif cleaned.count(MARKER) == 1:
        changed = cleaned.replace(MARKER, fragment + MARKER, 1)
    else:
        print('No se encontró la zona de Noticias en index.html')
        return False
    if changed != page:
        INDEX.write_text(changed, encoding='utf-8')
        return True
    return False


def actualizar_json(items):
    if not NEWS.exists() or not items:
        return False
    try:
        data = json.loads(NEWS.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        print('noticias.json inválido; no se toca')
        return False
    section = data.setdefault('sections', {})
    if section.get('seny') == items:
        return False
    section['seny'] = items
    NEWS.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return True


def main():
    items = obtener()
    if not items:
        print('No se obtuvieron notas de Seny. Se conserva el contenido previo.')
        return
    page = actualizar_portada(items)
    data = actualizar_json(items)
    print('Seny Radio: notas en el muro:', len(items),
          '| portada modificada:', page, '| JSON actualizado:', data)
    print('Nota: las publicaciones de Instagram y Facebook requieren otra integración.')


if __name__ == '__main__':
    main()
