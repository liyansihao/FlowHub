"""Read-only SKU-bound details; only explicitly labelled package measurements."""
import asyncio,copy,fcntl,hashlib,json,re,time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin,urlparse
import httpx
from ..plugin_detail import positive

FIELDS=('weight_g','dimensions_mm','attributes')


def enabled(db):
 p=Path(db.directory)/'direct-first.json'
 return p.exists() and json.loads(p.read_text()).get('enabled',False)


def fresh(detail,key,now):
 value=detail.get(key)
 valid=(positive(value) is not None if key=='weight_g' else isinstance(value,list) and len(value)==3 and all(positive(v) for v in value) if key=='dimensions_mm' else bool(value))
 at=(detail.get('field_observations',{}).get(key) or {}).get('observed_at',detail.get('observed_at',0))
 return valid and isinstance(at,(int,float)) and 0<=now-at<21600


def merge_fields(product,fields,source,observed_at,*,now=None):
 now=observed_at if now is None else now
 p=copy.deepcopy(product);d=p.setdefault('plugin_detail',{});applied=[]
 # Preserve field timestamps before advancing the aggregate snapshot timestamp.
 obs=d.setdefault('field_observations',{})
 for key in FIELDS:
  if key in d and key not in obs:obs[key]={'source':d.get('contract','existing'),'observed_at':d.get('observed_at',0)}
 for key,value in fields.items():
  if key not in FIELDS or fresh(d,key,now):continue
  if not fresh({key:value,'observed_at':observed_at},key,now):continue
  d[key]=value;obs[key]={'source':source,'observed_at':observed_at,'sku':str(p['sku'])};applied.append(key)
 if all(fresh(d,key,now) for key in FIELDS):
  d.update(sku=str(p['sku']),observed_at=min(obs[k]['observed_at'] for k in FIELDS),contract='direct-field-dossier-v1')
 return p,applied


def from_maozi(product,response,observed_at):
 data=response.get('data') or {};flags=response.get('status') or {}
 if str(data.get('sku'))!=str(product['sku']):return product,{'reason':'sku_mismatch','fields':[]}
 # sku3's custom weight/volume lack verified packaging semantics; retain as evidence only.
 return product,{'source':'maozi-sku3-detail','observed_at':observed_at,'fields':[],
  'status':flags,'returned_fields':sorted(data),'reason':'no_verified_package_dossier_fields'}


def from_category(product,response,observed_at):
 step={'source':'maozi-category-by-sku','observed_at':observed_at,'fields':[]}
 if str(response.get('sku'))!=str(product['sku']):return product,step|{'reason':'sku_mismatch'}
 info=response.get('product_info') or {}
 fields={'weight_g':info.get('weight')}
 dims=[positive(info.get(k)) for k in ('depth','width','height')]
 if all(v is not None for v in dims):fields['dimensions_mm']=[v*10 for v in dims]
 p,applied=merge_fields(product,fields,step['source'],observed_at)
 if applied:p['plugin_detail']['sku']=str(product['sku'])
 if applied or (len(response.get('cate') or [])>=3):
  p['direct_source_facts']={'sku':str(product['sku']),'source':step['source'],'observed_at':observed_at,
                            'category_response':response}
  cate=response.get('cate') or []
  if len(cate)>=3 and cate[2]:p['plugin_detail']['description_type']=str(cate[2])
 return p,step|{'fields':applied,'reason':'exact_sku_package_fields','returned_sku':str(response['sku'])}


class JsonLD(HTMLParser):
 def __init__(self):super().__init__();self.active=False;self.parts=[];self.documents=[]
 def handle_starttag(self,tag,attrs):
  if tag=='script' and dict(attrs).get('type')=='application/ld+json':self.active=True;self.parts=[]
 def handle_data(self,data):
  if self.active:self.parts.append(data)
 def handle_endtag(self,tag):
  if tag=='script' and self.active:
   self.active=False
   try:self.documents.append(json.loads(''.join(self.parts)))
   except ValueError:pass


def page_fields(product,html,observed_at):
 parser=JsonLD();parser.feed(html);products=[]
 def walk(value):
  if isinstance(value,list):
   for v in value:walk(v)
  elif isinstance(value,dict):
   if value.get('@type')=='Product' and str(value.get('sku'))==str(product['sku']):products.append(value)
   if '@graph' in value:walk(value['@graph'])
 for d in parser.documents:walk(d)
 if len(products)!=1:return product,{'source':'ozon-product-page','fields':[],'reason':'exact_product_unavailable'}
 row=products[0];fields={};properties=row.get('additionalProperty') or []
 if isinstance(properties,dict):properties=[properties]
 for prop in properties:
  name=str(prop.get('name','')).strip().lower();value=str(prop.get('value','')).strip()
  if name in ('вес с упаковкой, г','вес в упаковке, г') and positive(value.replace(',','.')):
   fields['weight_g']=positive(value.replace(',','.'))
  if name in ('размеры в упаковке, мм','размеры упаковки, мм','размеры в упаковке, см','размеры упаковки, см'):
   if re.fullmatch(r'\d+(?:[.,]\d+)?\s*[xх×*]\s*\d+(?:[.,]\d+)?\s*[xх×*]\s*\d+(?:[.,]\d+)?',value):
    factor=10 if name.endswith('см') else 1
    fields['dimensions_mm']=[float(v.replace(',','.'))*factor for v in re.split(r'\s*[xх×*]\s*',value)]
 p,applied=merge_fields(product,fields,'ozon-product-page',observed_at)
 image=row.get('image')
 if isinstance(image,list):image=image[0] if image else None
 if isinstance(image,dict):image=image.get('url')
 for key,value in [('title',row.get('name')),('image',image),('url',f"https://www.ozon.ru/product/{product['sku']}/")]:
  if not p.get(key) and isinstance(value,str) and value:
   if key in ('image','url') and not value.startswith('https://'):continue
   p[key]=value;applied.append(key)
 # Display characteristics are not canonical Ozon publishing attribute IDs.
 p.setdefault('collection_evidence',{})['public_characteristics']={'sku':str(product['sku']),'observed_at':observed_at,'values':properties}
 return p,{'source':'ozon-product-page','fields':applied,'reason':'exact_product_read','observed_at':observed_at}


async def session_detail(db,product,settings):
 path=Path(db.directory);now=time.time()
 cooldown=path/'session-detail-backoff.json'
 if cooldown.exists() and json.loads(cooldown.read_text()).get('until',0)>now:
  return product,{'source':'ozon-session-page','fields':[],'reason':'session_backoff'}
 with (path/'session-detail.lock').open('a') as lock:
  try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  except BlockingIOError:return product,{'source':'ozon-session-page','fields':[],'reason':'session_busy'}
  process=None
  try:
   bridge=Path(__file__).resolve().parents[2]/'bridges/session-product-detail.cjs'
   process=await asyncio.create_subprocess_exec('node',str(bridge),stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
   output,_=await asyncio.wait_for(process.communicate(json.dumps(settings|{'sku':str(product['sku'])}).encode()),65)
   result=json.loads(output)
   if process.returncode or result.get('error'):raise ValueError('session_read_failed')
   html=result['html'];p,step=page_fields(product,html,now)
   step.update(source='ozon-session-page',transport='existing-playwright-session',session=settings['session'],
               initial_http_status=result.get('initial_status'),url=result['url'],body_sha256=hashlib.sha256(html.encode()).hexdigest())
   # Exact product validation in page_fields is decisive, not the initial navigation code.
   if step['reason']!='exact_product_read':raise ValueError('session_product_identity_unconfirmed')
   artifact=path/'session-detail-evidence';artifact.mkdir(exist_ok=True,mode=0o700)
   target=artifact/(str(product['sku'])+'.html');target.write_text(html);target.chmod(0o600)
   return p,step
  except (ValueError,OSError,TimeoutError) as error:
   cooldown.write_text(json.dumps({'until':time.time()+120,'reason':type(error).__name__}))
   return product,{'source':'ozon-session-page','fields':[],'reason':type(error).__name__}
  finally:
   if process and process.returncode is None:
    process.kill();await process.wait()


async def public_detail(db,product):
 cfg=Path(db.directory)/'direct-first.json'
 settings=(json.loads(cfg.read_text()).get('browser_session') or {}) if cfg.exists() else {}
 if settings.get('enabled'):return await session_detail(db,product,settings)
 path=Path(db.directory)/'public-detail-backoff.json';now=time.time()
 if path.exists():
  try:
   if json.loads(path.read_text()).get('until',0)>now:return product,{'source':'ozon-product-page','fields':[],'reason':'access_backoff'}
  except ValueError:pass
 url=f"https://www.ozon.ru/product/{product['sku']}/"
 try:
  async with asyncio.timeout(10), httpx.AsyncClient(timeout=8,trust_env=False,follow_redirects=False) as client:
   target=url
   for attempt in range(4):
    response=await client.get(target)
    if response.status_code not in (301,302,303,307,308):break
    redirect=urljoin(target,response.headers.get('location',''))
    parsed=urlparse(redirect)
    if parsed.scheme!='https' or parsed.netloc!='www.ozon.ru' or redirect==target:break
    target=redirect
  if response.status_code!=200:
   if response.status_code in (301,302,303,307,308,403,429):
    path.write_text(json.dumps({'until':now+900,'http_status':response.status_code}))
   return product,{'source':'ozon-product-page','fields':[],'reason':'http_'+str(response.status_code)}
  p,step=page_fields(product,response.text,now)
  step.update(url=url,body_sha256=hashlib.sha256(response.content).hexdigest())
  return p,step
 except (httpx.HTTPError,TimeoutError) as error:
  path.write_text(json.dumps({'until':now+300,'reason':type(error).__name__}))
  return product,{'source':'ozon-product-page','fields':[],'reason':type(error).__name__}
