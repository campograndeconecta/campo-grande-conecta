#!/usr/bin/env python3
"""Integra Seny Radio (Cinco Saltos) sin alterar las otras fuentes.
Se ejecuta DESPUÉS del actualizador de noticias existente.
Usa solo enlaces y titulares publicados; no inventa fechas ni imágenes.
"""
import json
import re
import html
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

BASE = 'https://seny.com.ar/'
NEWS = Path('noticias.json')
INDEX = Path('index.html')
HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; CampoGrandeConecta/1.0; comunidad informativa)'}
TZ_AR = timezone(timedelta(hours=-3))
MESES = {'enero':1,'febrero':2,'marzo':3,'abril':4,'mayo':5,'junio':6,'julio':7,
         'agosto':8,'septiembre':9,'setiembre':9,'octubre':10,'noviembre':11,'diciembre':12}
ARTICLE_PATH = re.compile(r'^/[^/?#]+/[^/?#]+_[0-9]+/?$')
LOCAL_POBLACIONES = re.compile(r'\b(villa manzano|sargento vidal|barda del medio|contralmirante cordero|zona marengo)\b')
REGIONAL = re.compile(r'\b(cinco saltos|rio negro|alto valle|neuquen|cipolletti|campo grande|centenario|barda del medio|lago pellegrini|ruta 151|ruta 69|ruta 22)\b')
SPORTS = re.compile(r'\b(deporte|futbol|basquet|basket|voley|hockey|boxeo|liga confluencia|newcom|tenis|formula 1|f1|ciclismo|atletismo|rugby|patin|karate)\b')


def normal(x):
    v = unicodedata.normalize('NFKD', str(x or ''))
    return ''.join(ch for ch in v if not unicodedata.combining(ch)).lower()


def get(url, max_bytes=700_000):
    r = Request(url, headers=HEADERS)
    with urlopen(r, timeout=14) as response:
        final = response.geturl()
        if urlparse(final).hostname not in ('seny.com.ar', 'www.seny.com.ar'):
            raise ValueError('Fuente redirigida a otro dominio')
        return response.read(max_bytes).decode('utf-8', errors='replace')


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []
    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            href = dict(attrs).get('href', '')
            dest = urljoin(BASE, href)
            p = urlparse(dest)
            if p.hostname in ('seny.com.ar', 'www.seny.com.ar') and ARTICLE_PATH.match(p.path):
                self.urls.append(p._replace(query='', fragment='').geturl())


class Meta(HTMLParser):
    def __init__(self):
        super().__init__()
        self.data = {}
        self.h1 = []
        self.in_h1 = 0
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'meta':
            key = a.get('property') or a.get('name') or a.get('itemprop')
            if key and a.get('content'):
                self.data.setdefault(key.lower(), html.unescape(a['content']).strip())
        elif tag == 'h1':
            self.in_h1 += 1
    def handle_endtag(self, tag):
        if tag == 'h1':
            self.in_h1 = max(0, self.in_h1-1)
    def handle_data(self, data):
        if self.in_h1 and data.strip():
            self.h1.append(data.strip())


def fecha_articulo(raw, metas):
    for key in ('article:published_time','datepublished','date','publish_date','dc.date','pubdate'):
        stamp = metas.get(key, '')
        if not stamp:
            continue
        try:
            dt = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=TZ_AR)
            return dt
        except ValueError:
            try:
                dt = parsedate_to_datetime(stamp)
                return dt if dt.tzinfo else dt.replace(tzinfo=TZ_AR)
            except (TypeError, ValueError):
                pass
    # Fecha visible al principio del artículo, no fechas históricas en el cuerpo.
    text = re.sub(r'<[^>]*>', ' ', raw[:18_000])
    text = html.unescape(re.sub(r'\s+', ' ', text))
    found = re.search(r'\b(\d{1,2})\s+de\s+(enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|setiembre|octubre|noviembre|diciembre)\s+de\s+(20\d{2})\b', text, re.I)
    if found:
        d, m, y = found.groups()
        try:
            return datetime(int(y), MESES[m.lower()], int(d), 12, 0, tzinfo=TZ_AR)
        except ValueError:
            return None
    return None


def leer_nota(url):
    try:
        raw = get(url)
        m = Meta()
        m.feed(raw)
        title = m.data.get('og:title') or m.data.get('twitter:title') or ' '.join(m.h1)
        title = html.unescape(re.sub(r'\s+', ' ', title)).strip()
        # Los títulos de Seny a veces incluyen el nombre del medio al final.
        title = re.sub(r'\s*[-|]\s*Seny(?:\s+Radio)?(?:\s*99\.9)?\s*$', '', title, flags=re.I)
        if len(title) < 12 or len(title) > 260:
            return None
        pub = fecha_articulo(raw, m.data)
        if not pub:
            return None
        now = datetime.now(timezone.utc)
        age = now - pub.astimezone(timezone.utc)
        regional_title = normal(title)
        is_local = bool(LOCAL_POBLACIONES.search(regional_title))
        if age < timedelta(days=-1) or age > timedelta(days=3 if is_local else 2):
            return None
        path = normal(urlparse(url).path)
        sport = bool(SPORTS.search(regional_title)) or '/deporte/' in path
        if sport:
            section = 'sports_region' if REGIONAL.search(regional_title) or '/locales/' in path else 'sports_argentina'
        elif is_local:
            section = 'local'
        elif REGIONAL.search(regional_title) or '/locales/' in path or '/editorial/' in path:
            section = 'rionegro'
        else:
            section = 'argentina'
        item = {'title': title, 'source': 'Seny Radio 99.9', 'url': url,
                'date': format_datetime(pub),
                'summary': 'Noticia publicada por Seny Radio, medio de Cinco Saltos.'}
        photo = m.data.get('og:image') or m.data.get('twitter:image')
        if photo:
            photo = urljoin(BASE, photo)
            if urlparse(photo).scheme == 'https' and urlparse(photo).hostname:
                item['image'] = photo
        return section, item
    except Exception as exc:
        print('Nota Seny no procesada:', str(exc)[:140])
        return None


def incorporar_enlace():
    if not INDEX.exists():
        return False
    original = INDEX.read_text(encoding='utf-8')
    if 'Seny Radio 99.9 · Cinco Saltos' in original:
        return False
    marker = '<div id="news-list"></div>'
    if original.count(marker) != 1:
        print('No se modificó index.html: bloque de Noticias no identificable.')
        return False
    # Se inserta un enlace a la FUENTE original, no contenido copiado.
    fragment = ('<p style="background:#eff5fb;padding:12px;border-radius:10px;font-size:14px">'
                '📻 Medio de Cinco Saltos: <a href="https://seny.com.ar/" target="_blank" '
                'rel="noopener noreferrer"><strong>Seny Radio 99.9 · Cinco Saltos</strong></a>. '
                'Consultá sus noticias locales, regionales, nacionales y deportivas en la fuente original.</p>')
    INDEX.write_text(original.replace(marker, fragment+marker, 1), encoding='utf-8')
    return True


def actualizar_noticias():
    if not NEWS.exists():
        print('No se encuentra noticias.json; no se cambian los datos.')
        return 0
    try:
        payload = json.loads(NEWS.read_text(encoding='utf-8'))
    except (ValueError, OSError):
        print('noticias.json inválido; se conserva sin cambios.')
        return 0
    try:
        parser = Links()
        parser.feed(get(BASE))
        # También incluye la portada de locales y notas generales si son visibles.
        for page in ('locales', 'actualidad', 'deporte'):
            try:
                parser.feed(get(urljoin(BASE, page)))
            except Exception as ex:
                print('Sección Seny no accesible:', page, str(ex)[:90])
        urls = list(dict.fromkeys(parser.urls))[:24]
        print('Enlaces de artículos Seny encontrados:', len(urls))
    except Exception as ex:
        print('Seny no disponible en este momento:', str(ex)[:160])
        return 0
    found = []
    with ThreadPoolExecutor(max_workers=5) as pool:
        tasks = [pool.submit(leer_nota, url) for url in urls]
        for task in as_completed(tasks):
            result = task.result()
            if result:
                found.append(result)
    sections = payload.setdefault('sections', {})
    inserted = 0
    for section, item in found:
        current = sections.setdefault(section, [])
        if not any(normal(x.get('title')) == normal(item['title']) or x.get('url') == item['url'] for x in current):
            current.append(item)
            inserted += 1
            current.sort(key=lambda x: (date_key(x.get('date'))), reverse=True)
            # Mantener variedad de fuentes y limitar la cantidad de tarjetas.
            sections[section] = current[:14]
    if inserted:
        NEWS.write_text(json.dumps(payload, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    return inserted


def date_key(date):
    try:
        dt = parsedate_to_datetime(date)
        return dt.timestamp() if dt.tzinfo else dt.replace(tzinfo=TZ_AR).timestamp()
    except (ValueError, TypeError):
        return 0


def main():
    changed = incorporar_enlace()
    count = actualizar_noticias()
    print('Seny Radio: enlace en la portada', 'agregado' if changed else 'conservado',
          '| noticias recientes incluidas:', count)


if __name__ == '__main__':
    main()
