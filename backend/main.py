from __future__ import annotations
import asyncio, hashlib, hmac, json, logging, os, sqlite3, time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from urllib.parse import parse_qsl
import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
load_dotenv(); logging.basicConfig(level=logging.INFO); log=logging.getLogger('solwatcher')
TOKEN=os.getenv('TELEGRAM_BOT_TOKEN',''); ALLOWED_USER_ID=os.getenv('TELEGRAM_USER_ID','').strip(); DB=os.getenv('DB_PATH','data/alerts.db'); INTERVAL=max(60,int(os.getenv('CHECK_INTERVAL_SECONDS','60')))
COINS={'BTC':('bitcoin','BTCUSDT'),'ETH':('ethereum','ETHUSDT'),'SOL':('solana','SOLUSDT'),'BNB':('binancecoin','BNBUSDT'),'XRP':('ripple','XRPUSDT'),'ADA':('cardano','ADAUSDT'),'DOGE':('dogecoin','DOGEUSDT'),'AVAX':('avalanche-2','AVAXUSDT'),'LINK':('chainlink','LINKUSDT'),'DOT':('polkadot','DOTUSDT')}
LOOKBACK=20; MAX_WINDOW=900; client=None; cache={'at':0,'data':None}
def dbconn():
 os.makedirs(os.path.dirname(DB) or '.',exist_ok=True); c=sqlite3.connect(DB,timeout=15); c.row_factory=sqlite3.Row; c.execute('PRAGMA journal_mode=WAL'); return c
def init_db():
 with dbconn() as c:
  c.execute('''CREATE TABLE IF NOT EXISTS alerts(id INTEGER PRIMARY KEY AUTOINCREMENT,chat_id INTEGER NOT NULL,coin TEXT NOT NULL,target REAL,created_at TEXT NOT NULL,direction TEXT NOT NULL DEFAULT 'above',alert_type TEXT NOT NULL DEFAULT 'price',enabled INTEGER NOT NULL DEFAULT 1,threshold REAL,window_minutes INTEGER,cooldown_seconds INTEGER NOT NULL DEFAULT 900,last_triggered_at REAL)''')
  cols={r['name'] for r in c.execute('PRAGMA table_info(alerts)')}
  for k,v in {'direction':"TEXT NOT NULL DEFAULT 'above'",'alert_type':"TEXT NOT NULL DEFAULT 'price'",'enabled':'INTEGER NOT NULL DEFAULT 1','threshold':'REAL','window_minutes':'INTEGER','cooldown_seconds':'INTEGER NOT NULL DEFAULT 900','last_triggered_at':'REAL'}.items():
   if k not in cols:c.execute(f'ALTER TABLE alerts ADD COLUMN {k} {v}')
  c.execute('''CREATE TABLE IF NOT EXISTS alert_history(id INTEGER PRIMARY KEY AUTOINCREMENT,alert_id INTEGER,chat_id INTEGER NOT NULL,coin TEXT NOT NULL,alert_type TEXT NOT NULL,message TEXT NOT NULL,triggered_at TEXT NOT NULL)'''); c.execute('CREATE INDEX IF NOT EXISTS idx_alerts_chat ON alerts(chat_id)'); c.execute('CREATE INDEX IF NOT EXISTS idx_history_chat ON alert_history(chat_id,id DESC)')
def identity(raw):
 if not TOKEN:raise HTTPException(503,'Telegram bot is not configured')
 try:
  d=dict(parse_qsl(raw,keep_blank_values=True)); sig=d.pop('hash'); ts=int(d['auth_date'])
  if time.time()-ts>86400 or ts>time.time()+60:raise ValueError()
  key=hmac.new(b'WebAppData',TOKEN.encode(),hashlib.sha256).digest(); check='\n'.join(f'{k}={v}' for k,v in sorted(d.items())); expected=hmac.new(key,check.encode(),hashlib.sha256).hexdigest()
  if not hmac.compare_digest(sig,expected):raise ValueError()
  u=json.loads(d['user'])
  if ALLOWED_USER_ID and str(u.get('id'))!=ALLOWED_USER_ID:raise HTTPException(403,'This app is not enabled for this Telegram account')
  return u
 except HTTPException:raise
 except Exception:raise HTTPException(401,'Invalid or expired Telegram Mini App session')
async def user(x_telegram_init_data:str=Header(default='')):return identity(x_telegram_init_data)
async def get_prices(force=False):
 global cache
 now=time.monotonic()
 if cache['data'] and now-cache['at']<30 and not force:return cache['data']
 ids=','.join(x[0] for x in COINS.values()); r=await client.get('https://api.coingecko.com/api/v3/simple/price',params={'ids':ids,'vs_currencies':'usd','include_24hr_change':'true'}); r.raise_for_status(); raw=r.json()
 data={s:{'usd':float(raw[cid]['usd']),'change24h':float(raw[cid].get('usd_24h_change',0) or 0)} for s,(cid,_) in COINS.items() if cid in raw and 'usd' in raw[cid]}
 if not data:raise HTTPException(502,'Price feed unavailable')
 cache={'at':now,'data':data};return data
async def candles(coin,limit):
 r=await client.get('https://data-api.binance.vision/api/v3/klines',params={'symbol':COINS[coin][1],'interval':'1m','limit':limit});r.raise_for_status();now=int(time.time()*1000);return [x for x in r.json() if int(x[6])<now]
def packed(r):
 d=dict(r);d['enabled']=bool(d['enabled']);return d
class NewAlert(BaseModel):
 coin:str;alert_type:str=Field(pattern='^(price|change|volume)$');direction:str='above';target:float|None=None;threshold:float|None=None;window_minutes:int|None=None
@asynccontextmanager
async def lifespan(app):
 global client
 init_db();client=httpx.AsyncClient(timeout=15,headers={'User-Agent':'SolWatcher/2.0'});task=asyncio.create_task(worker());yield;task.cancel()
 try:await task
 except asyncio.CancelledError:pass
 await client.aclose();client=None
app=FastAPI(title='SolWatcher API',lifespan=lifespan)
origins=[x.strip() for x in os.getenv('CORS_ORIGINS','').split(',') if x.strip()]
if not origins:
 origins=['https://plexi-nosleep.github.io']
app.add_middleware(CORSMiddleware,allow_origins=origins,allow_methods=['*'],allow_headers=['*'])
@app.get('/api/health')
async def health():return {'ok':True}
@app.get('/api/me')
async def me(u=Depends(user)):return {'id':u['id'],'first_name':u.get('first_name','Trader')}
@app.get('/api/prices')
async def prices(u=Depends(user),refresh:bool=False):return {'prices':await get_prices(refresh),'updated_at':datetime.now(timezone.utc).isoformat()}
@app.get('/api/alerts')
async def alerts(u=Depends(user)):
 with dbconn() as c:r=c.execute('SELECT * FROM alerts WHERE chat_id=? ORDER BY id DESC',(u['id'],)).fetchall()
 return [packed(x) for x in r]
@app.post('/api/alerts',status_code=201)
async def create(p:NewAlert,u=Depends(user)):
 coin=p.coin.upper().strip();k=p.alert_type
 if coin not in COINS:raise HTTPException(422,'Unsupported coin')
 if k=='price' and (p.direction not in ('above','below') or p.target is None or p.target<=0):raise HTTPException(422,'Enter a positive target and choose above or below')
 if k=='change' and (p.threshold is None or p.threshold<=0 or p.window_minutes is None or not 1<=p.window_minutes<=MAX_WINDOW):raise HTTPException(422,'Movement threshold must be positive and window 1–900 minutes')
 if k=='volume' and (p.threshold is None or p.threshold<=0):raise HTTPException(422,'Volume multiple must be positive')
 with dbconn() as c:
  cur=c.execute('INSERT INTO alerts(chat_id,coin,target,created_at,direction,alert_type,enabled,threshold,window_minutes,cooldown_seconds) VALUES(?,?,?,?,?,?,1,?,?,900)',(u['id'],coin,p.target,datetime.now(timezone.utc).isoformat(),p.direction,k,p.threshold,p.window_minutes));r=c.execute('SELECT * FROM alerts WHERE id=?',(cur.lastrowid,)).fetchone()
 return packed(r)
@app.patch('/api/alerts/{aid}')
async def toggle(aid:int,enabled:bool=Query(...),u=Depends(user)):
 with dbconn() as c:
  cur=c.execute('UPDATE alerts SET enabled=? WHERE id=? AND chat_id=?',(int(enabled),aid,u['id']));r=c.execute('SELECT * FROM alerts WHERE id=? AND chat_id=?',(aid,u['id'])).fetchone()
 if not cur.rowcount:raise HTTPException(404,'Alert not found')
 return packed(r)
@app.delete('/api/alerts/{aid}',status_code=204)
async def remove(aid:int,u=Depends(user)):
 with dbconn() as c:cur=c.execute('DELETE FROM alerts WHERE id=? AND chat_id=?',(aid,u['id']))
 if not cur.rowcount:raise HTTPException(404,'Alert not found')
@app.get('/api/history')
async def history(u=Depends(user),limit:int=Query(50,ge=1,le=200)):
 with dbconn() as c:r=c.execute('SELECT * FROM alert_history WHERE chat_id=? ORDER BY id DESC LIMIT ?',(u['id'],limit)).fetchall()
 return [dict(x) for x in r]
async def fire(a,msg,once):
 with dbconn() as c:
  c.execute('INSERT INTO alert_history(alert_id,chat_id,coin,alert_type,message,triggered_at) VALUES(?,?,?,?,?,?)',(a['id'],a['chat_id'],a['coin'],a['alert_type'],msg,datetime.now(timezone.utc).isoformat()))
  if once:c.execute('DELETE FROM alerts WHERE id=?',(a['id'],))
  else:c.execute('UPDATE alerts SET last_triggered_at=? WHERE id=?',(time.time(),a['id']))
 try:
  r=await client.post(f'https://api.telegram.org/bot{TOKEN}/sendMessage',json={'chat_id':a['chat_id'],'text':msg});r.raise_for_status()
 except Exception:log.exception('Telegram notification failed')
async def worker():
 while True:
  try:await scan()
  except asyncio.CancelledError:raise
  except Exception:log.exception('Alert worker failed')
  await asyncio.sleep(INTERVAL)
async def scan():
 with dbconn() as c:rows=c.execute('SELECT * FROM alerts WHERE enabled=1 ORDER BY id').fetchall()
 if not rows:return
 try:market=await get_prices()
 except Exception:log.exception('Price provider unavailable');market={}
 moves={};vols={}
 for a in rows:
  coin=a['coin'];kind=a['alert_type'] or 'price'
  try:
   if kind=='price':
    p=market.get(coin,{}).get('usd')
    if p is not None and (p>=a['target'] if a['direction']=='above' else p<=a['target']):await fire(a,f"🔔 Price alert: {coin} is ${p:,.4f} ({a['direction']} ${a['target']:,.4f}).",True)
   elif kind=='change':
    w=int(a['window_minutes']);key=(coin,w)
    if key not in moves:
     cs=await candles(coin,w+1);moves[key]=(float(cs[-1][4])-float(cs[0][4]))/float(cs[0][4])*100 if len(cs)>=w+1 and float(cs[0][4])>0 else None
    move=moves[key];cool=a['last_triggered_at'] is None or time.time()-a['last_triggered_at']>=(a['cooldown_seconds'] or 900)
    if move is not None and abs(move)>=a['threshold'] and cool:await fire(a,f"📈 Movement alert: {coin} moved {move:+.2f}% over {w} minutes (threshold {a['threshold']:g}%).",False)
   elif kind=='volume':
    if coin not in vols:
     cs=await candles(coin,LOOKBACK+1)
     if len(cs)>=LOOKBACK+1:
      v=[float(x[7]) for x in cs[-(LOOKBACK+1):-1]];avg=sum(v)/len(v);vols[coin]=(float(cs[-1][7])/avg,float(cs[-1][7])) if avg>0 else None
     else:vols[coin]=None
    result=vols[coin];cool=a['last_triggered_at'] is None or time.time()-a['last_triggered_at']>=(a['cooldown_seconds'] or 900)
    if result and result[0]>=a['threshold'] and cool:await fire(a,f"📊 Volume spike: {coin} 1m volume was {result[0]:.2f}× 20-candle average (≈ ${result[1]:,.0f}).",False)
  except Exception:log.exception('Alert evaluation failed: %s',a['id'])
static=os.path.abspath(os.path.join(os.path.dirname(__file__),'..','frontend'))
if os.path.isdir(static):app.mount('/',StaticFiles(directory=static,html=True),name='frontend')






