import json, datetime, urllib.parse, urllib.request, xml.etree.ElementTree as ET, email.utils
from pathlib import Path
QUERIES={
 'local':'"Villa Manzano" OR "Sargento Vidal" OR "El Labrador" OR "San Isidro" "Río Negro" OR "Zona Marengo"',
 'rionegro':'"Río Negro" provincia noticias',
 'neuquen':'Neuquén provincia noticias',
 'argentina':'Argentina noticias principales'
}
sections={}
for key,q in QUERIES.items():
 url='https://news.google.com/rss/search?'+urllib.parse.urlencode({'q':q,'hl':'es-419','gl':'AR','ceid':'AR:es-419'})
 try:
  req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0 CampoGrandeConecta/1.0'})
  with urllib.request.urlopen(req,timeout=20) as response: root=ET.fromstring(response.read())
  items=[]
  for item in root.findall('.//channel/item')[:8]:
   get=lambda tag:(item.findtext(tag) or '').strip()
   title=get('title'); source=get('source') or (title.rsplit(' - ',1)[-1] if ' - ' in title else 'Medio de noticias')
   desc=get('description'); desc=__import__('re').sub('<[^>]*>',' ',desc)
   desc=__import__('html').unescape(desc)
   items.append({'title':title,'source':source,'url':get('link'),'date':get('pubDate')[:25],'summary':desc[:350]})
  sections[key]=items
 except Exception as e:
  print('Error',key,e)
  old=Path('noticias.json')
  if old.exists():
   try:sections[key]=json.loads(old.read_text()).get('sections',{}).get(key,[])
   except Exception:sections[key]=[]
  else:sections[key]=[]
if not any(sections.values()): raise SystemExit('No se recuperaron noticias: se conserva el archivo anterior')
Path('noticias.json').write_text(json.dumps({'updated':datetime.datetime.now(datetime.timezone.utc).strftime('%d/%m/%Y %H:%M UTC'),'sections':sections},ensure_ascii=False,indent=2),encoding='utf-8')
