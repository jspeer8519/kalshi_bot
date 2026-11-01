import base64
import json
import math
import os
import secrets
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from flask import Flask, request, jsonify, make_response
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa, padding
from engine import Engine, SERIES, timestamp, asset_of
import report
from live import KalshiClient, LiveExecutor
from indicators import summarize
from pricefeed import TickFeed
from quotes import QuoteHistory, parse_candles
from indexfeed import IndexFeed

ROOT=Path(__file__).resolve().parent
DATA=Path(os.environ.get('KALSHI_DATA_DIR',ROOT/'data'));DATA.mkdir(parents=True,exist_ok=True)
PRIVATE=ROOT/'.private';PRIVATE.mkdir(mode=0o700,exist_ok=True)
API='https://external-api.kalshi.com/trade-api/v2'
app=Flask(__name__,static_folder=str(ROOT/'static'));app.config['MAX_CONTENT_LENGTH']=16384
TOKEN=secrets.token_urlsafe(32)
credential_lock=threading.Lock()
LOG_PATH=DATA/'trade_log.jsonl'
LIVE_CONFIRM_WORD='LIVE'
VERIFY_TTL=15*60          # a successful connection test is valid for 15 minutes
verified_at=0.0


def load_env_file(path):
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line=line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k,v=line.split('=',1)
        k=k.strip();v=v.strip()
        if not k:
            continue
        if v and v[0] in ('"',"'") and v[-1]==v[0]:
            v=v[1:-1]
        v=v.replace('\\n','\n')
        os.environ.setdefault(k,v)


def normalize_pem(value):
    if value is None:
        return ''
    pem=str(value).strip()
    if not pem:
        return ''
    pem=pem.replace('\\n','\n')
    if not pem.startswith('-----BEGIN'):
        pem='-----BEGIN PRIVATE KEY-----\n'+pem+'\n-----END PRIVATE KEY-----' if 'BEGIN' not in pem and 'END' not in pem else pem
    return pem


def load_runtime_credentials():
    if (PRIVATE/'credentials.json').exists():
        return None
    key_id=os.environ.get('KALSHI_KEY_ID') or os.environ.get('KALSHI_API_KEY_ID')
    pem=os.environ.get('KALSHI_PRIVATE_KEY') or os.environ.get('KALSHI_KEY_PEM')
    if key_id and pem:
        pem_value=str(pem).strip()
        if pem_value.startswith('/') or pem_value.startswith('.'):
            candidate=Path(pem_value)
            if candidate.exists():
                pem_value=candidate.read_text()
        return {'key_id':str(key_id).strip(),'pem':normalize_pem(pem_value)}
    return None


load_env_file(ROOT/'.env')


def load_credentials():
    """Dashboard-saved credentials win; otherwise KALSHI_* values from the environment/.env. Never logged."""
    env_creds=load_runtime_credentials()
    if env_creds is not None:
        return env_creds
    path=PRIVATE/'credentials.json'
    if not path.exists():
        return None
    with credential_lock:
        return json.loads(path.read_text())


def credentials_configured():
    try:return load_credentials() is not None
    except Exception:return False


def live_max_contracts():
    try:value=int(os.environ.get('KALSHI_LIVE_MAX_CONTRACTS','1'))
    except ValueError:value=1
    return max(1,min(value,100))


client=KalshiClient(load_credentials,base=API)
engine=Engine(str(DATA/'paper.sqlite3'),executor=LiveExecutor(client),log_path=str(LOG_PATH),live_max_contracts=live_max_contracts(),
            spot_fn=lambda asset:index_feed.series(asset) or tick_feed.series(asset),   # settlement index, else Coinbase (both defined below)
            index_fn=lambda asset:index_feed.series(asset))   # settlement index only: rules that compare the price with the target


# Spot price chart only (display). Kalshi settles these markets on CF Benchmarks' index,
# so Coinbase BTC-USD is a close reference, not the settlement price.
COINBASE='https://api.exchange.coinbase.com'
PRICE_PRODUCTS={'BTC':'BTC-USD','ETH':'ETH-USD','SOL':'SOL-USD','XRP':'XRP-USD','DOGE':'DOGE-USD','HYPE':'HYPE-USD','BNB':'BNB-USD'}
PRICE_TTL=10
price_cache={};price_lock=threading.Lock()


def price_candles(asset):
    """Last 60 one-minute candles, oldest first: dict(t,o,h,l,c), plus EMA 9/21, RSI 14 and
    ATR 14 computed over the full history Coinbase returns (up to 300 minutes) so the
    displayed values are past their warm-up. Cached for PRICE_TTL seconds."""
    product=PRICE_PRODUCTS.get(asset)
    if not product:raise LookupError(f'No price chart source for {asset}')
    with price_lock:
        hit=price_cache.get(product)
        if hit and time.time()-hit['fetched']<PRICE_TTL:return hit
    req=urllib.request.Request(f'{COINBASE}/products/{product}/candles?granularity=60',headers={'Accept':'application/json','User-Agent':'KalshiDesk/1.1'})
    with urllib.request.urlopen(req,timeout=6) as r:rows=json.load(r)
    candles=[]
    for row in rows:  # Coinbase row: [time, low, high, open, close, volume], newest first
        t,lo,hi,op,cl=(float(x) for x in row[:5])
        if not all(map(math.isfinite,(lo,hi,op,cl))) or lo<=0 or hi<lo:continue
        candles.append(dict(t=t,o=op,h=hi,l=lo,c=cl))
    candles=sorted(candles,key=lambda c:c['t'])
    if not candles:raise ValueError('Empty candle response')
    result=dict(asset=asset,product=product,source='Coinbase',candles=candles[-60:],indicators=summarize(candles,60),fetched=time.time())
    with price_lock:price_cache[product]=result
    return result


def get(path,headers=None):
    req=urllib.request.Request(API+path,headers={'Accept':'application/json','User-Agent':'KalshiDesk/1.0',**(headers or {})})
    with urllib.request.urlopen(req,timeout=8) as r:return json.load(r)

def market_snapshot(m):
    ticker=m['ticker'];status=m['status']
    out=dict(ticker=ticker,title=m.get('title',ticker),subtitle=m.get('subtitle',''),status=status,result=m.get('result'),close=timestamp(m['close_time']),fetched=time.time())
    try:out['strike']=float(m['floor_strike']) if m.get('floor_strike') is not None else None
    except (TypeError,ValueError):out['strike']=None
    if status!='active':return out
    return with_book(out)

def with_book(out):
    """Fill snapshot ``out`` with the market's current order book (one request)."""
    book=get('/markets/'+urllib.parse.quote(out['ticker'],safe='')+'/orderbook?depth=10').get('orderbook_fp',{})
    for side in ('yes','no'):
        levels=sorted(((float(p),float(q)) for p,q in book.get(side+'_dollars',[]) if 0<=float(p)<=1 and float(q)>0),reverse=True)
        out[side+'_bids']=[list(x) for x in levels[:10]]   # best first; exits walk these levels
        best=levels[0] if levels else None
        out[side+'_bid']=best[0] if best else None
        out[side+'_bid_size']=best[1] if best else 0
    for side,other in [('yes','no'),('no','yes')]:
        out[side+'_ask']=round(1-out[other+'_bid'],4) if out[other+'_bid'] is not None else None
        out[side+'_ask_size']=out[other+'_bid_size']
    if out['yes_bid'] is not None and out['no_bid'] is not None and out['yes_bid']+out['no_bid']>1:raise ValueError('Crossed book')
    out['fetched']=time.time()
    return out

quote_history=QuoteHistory()     # Kalshi YES/NO prices of each current market, for the contract charts
index_feed=IndexFeed(load_credentials,lambda:current_tickers())   # CF Benchmarks index + trades (Kalshi WebSocket, read-only)
TRADE_PAGES=5                    # REST backfill: up to 5 x 1000 earlier trades per market


def current_tickers():
    with engine.lock:return [m['ticker'] for m in engine.markets.values() if m.get('status')=='active']


def backfill_trades(ticker):
    """Trades made before the WebSocket subscription (public REST, newest first, paged)."""
    cursor,rows='',[]
    for _ in range(TRADE_PAGES):
        d=get('/markets/trades?ticker='+urllib.parse.quote(ticker,safe='')+'&limit=1000'+('&cursor='+urllib.parse.quote(cursor,safe='') if cursor else ''))
        rows+=d.get('trades') or [];cursor=d.get('cursor')
        if not cursor:break
    index_feed.add_trades(ticker,rows)
contract_cache={};contract_lock=threading.Lock()
CONTRACT_TTL=30                  # seconds Kalshi's 1-minute history is reused per market

META_TTL=30          # seconds a market's details (ticker, close time, target) are reused before re-checking
FEED_INTERVAL=2      # target seconds between refreshes of each market
meta_cache={};meta_lock=threading.Lock()


def fetch_asset_market(asset,ticker=None):
    """Snapshot of the market holding our position, else the soonest-closing active one.

    Market details for a flat asset are cached for META_TTL seconds (never past close), so a
    normal refresh costs one order-book request instead of two."""
    now=time.time()
    if ticker:m=get('/markets/'+urllib.parse.quote(ticker,safe=''))['market']
    else:
        with meta_lock:hit=meta_cache.get(asset)
        if hit and now-hit[1]<META_TTL and timestamp(hit[0]['close_time'])>now+1:m=hit[0]
        else:
            markets=get('/markets?series_ticker='+SERIES[asset]+'&status=open&limit=100')['markets']
            markets=[m for m in markets if m['status']=='active' and timestamp(m['close_time'])>now]
            if not markets:raise ValueError('No active market')
            m=min(markets,key=lambda x:timestamp(x['close_time']))
            if m.get('floor_strike') is not None:   # Kalshi publishes the target shortly after open: keep asking until it does
                with meta_lock:meta_cache[asset]=(m,now)
    snap=market_snapshot(m);snap['asset']=asset
    return snap


def feed():
    """Refresh every watched market in parallel about every FEED_INTERVAL seconds, then apply
    the snapshots one at a time. One market failing never blocks the others."""
    from concurrent.futures import ThreadPoolExecutor
    pool=ThreadPoolExecutor(max_workers=len(SERIES))
    while True:
        started=time.time()
        with engine.lock:
            assets=engine.watched_assets()
            tickers={a:p['ticker'] for book in (engine.s['shadow_positions'],engine.s['positions']) for a,p in book.items()}   # paper/live wins
        jobs={a:pool.submit(fetch_asset_market,a,tickers.get(a)) for a in assets}
        for asset,job in jobs.items():
            try:
                snap=job.result()
                quote_history.add(snap)
                with engine.lock:
                    if asset in engine.watched_assets():engine.tick(snap)
            except Exception as e:
                engine.feed_error(asset,'Market feed unavailable ('+type(e).__name__+'). No fills until fresh data returns.')
        check_settlements(pool)
        time.sleep(max(0.2,FEED_INTERVAL-(time.time()-started)))

def book_now(asset,ticker):
    """Fresh order book for the market already being tracked (reuses its details: one request)."""
    with engine.lock:m=dict(engine.markets.get(asset) or {})
    if m.get('ticker')!=ticker or m.get('status')!='active':return fetch_asset_market(asset,ticker)
    return with_book({k:v for k,v in m.items() if not k.startswith(('yes_','no_'))})

def wake_sim_orders(fetch=None):
    """Resolve simulated latency on time: fetch the book of every market with a due simulated order
    now, so a 250 ms latency fills against the book about 250 ms (+ one request) later, not at the
    next 2-second refresh."""
    fetch=fetch or book_now
    for asset,ticker in engine.sim_due(time.time()):
        try:
            snap=fetch(asset,ticker);quote_history.add(snap);engine.tick(snap)
        except Exception:
            pass                       # the scheduled refresh resolves it instead

def sim_waker():
    while True:
        wake_sim_orders()
        time.sleep(0.05)

def check_settlements(pool):
    """Ask Kalshi how early-exited markets settled (settlement tracking). Failures retry later."""
    jobs={t:pool.submit(get,'/markets/'+urllib.parse.quote(t,safe='')) for t in engine.settlement_due()}
    for ticker,job in jobs.items():
        try:
            m=job.result()['market']
            engine.record_settlement(ticker,m.get('status'),m.get('result'))
        except Exception:
            pass

tick_feed=TickFeed(PRICE_PRODUCTS,lambda:engine.watched_assets())


@app.before_request
def guard():
    if request.host not in ('127.0.0.1:8765','localhost:8765'):
        return jsonify(error='Local access only'),403
    if request.method=='POST':
        if request.headers.get('X-Desk-Token')!=TOKEN:return jsonify(error='Invalid session; reload the dashboard'),403
        if request.headers.get('Origin') not in (None,'http://127.0.0.1:8765','http://localhost:8765'):return jsonify(error='Invalid origin'),403

@app.after_request
def headers(response):
    response.headers['Cache-Control']='no-store'
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    return response

@app.get('/')
def home():return (ROOT/'static/index.html').read_text().replace('__TOKEN__',TOKEN)

@app.get('/api/state')
def state():
    s=engine.snapshot();s['credentials_configured']=credentials_configured()
    s['connection_verified']=time.time()-verified_at<VERIFY_TTL
    return jsonify(s)

@app.get('/api/price')
def price():
    asset=request.args.get('asset','BTC')
    if asset not in SERIES:return jsonify(error='Unsupported asset'),400
    try:return jsonify(price_candles(asset))
    except LookupError as e:return jsonify(error=str(e),asset=asset),404
    except Exception as e:return jsonify(error='Price feed unavailable ('+type(e).__name__+')',asset=asset),502

@app.get('/api/ticks')
def ticks():
    try:after=float(request.args.get('after','0'))
    except ValueError:after=0
    return jsonify(tick_feed.since(after))

@app.post('/api/control')
def control():
    try:engine.control(request.json['action']);return jsonify(ok=True)
    except (ValueError,KeyError,TypeError) as e:return jsonify(error=str(e)),400

@app.post('/api/mode')
def mode():
    try:
        d=request.json;target=d['mode']
        if target=='live_small':target='live'
        if target=='live':
            if d.get('confirm')!=LIVE_CONFIRM_WORD:raise ValueError('Type LIVE to confirm real-money trading')
            if not credentials_configured():raise ValueError('Save Kalshi credentials on the Connection page first')
            if time.time()-verified_at>=VERIFY_TTL:raise ValueError('Run "Test account connection" first (valid for 15 minutes)')
        engine.set_mode(target);return jsonify(ok=True)
    except (ValueError,KeyError,TypeError) as e:return jsonify(error=str(e)),400

@app.post('/api/config')
def config():
    try:return jsonify(ok=True,**engine.configure(request.json))
    except (ValueError,TypeError) as e:return jsonify(error=str(e)),400

@app.post('/api/credentials')
def credentials():
    try:
        d=request.json;key_id=d['key_id'].strip();pem=d['pem']
        if not key_id or len(key_id)>128 or any(x.isspace() for x in key_id):raise ValueError()
        key=serialization.load_pem_private_key(pem.encode(),password=None)
        if not isinstance(key,(rsa.RSAPrivateKey,ed25519.Ed25519PrivateKey)):raise ValueError()
        if isinstance(key,rsa.RSAPrivateKey) and key.key_size<2048:raise ValueError()
        with credential_lock:
            temporary=PRIVATE/'credentials.tmp'
            fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
            with os.fdopen(fd,'w') as f:json.dump(dict(key_id=key_id,pem=pem),f)
            temporary.replace(PRIVATE/'credentials.json')
        return jsonify(ok=True)
    except Exception:return jsonify(error='Provide a valid key ID and unencrypted RSA (2048+ bit) or Ed25519 private-key PEM file.'),400


@app.post('/api/connection')
def connection():
    global verified_at
    try:
        d=load_credentials()
        key=serialization.load_pem_private_key(d['pem'].encode(),password=None)
        stamp=str(int(time.time()*1000));path='/trade-api/v2/portfolio/balance';message=(stamp+'GET'+path).encode()
        signature=key.sign(message) if isinstance(key,ed25519.Ed25519PrivateKey) else key.sign(message,padding.PSS(mgf=padding.MGF1(hashes.SHA256()),salt_length=32),hashes.SHA256())
        result=get('/portfolio/balance',{'KALSHI-ACCESS-KEY':d['key_id'],'KALSHI-ACCESS-TIMESTAMP':stamp,'KALSHI-ACCESS-SIGNATURE':base64.b64encode(signature).decode()})
        balance=float(result['balance_dollars']) if result.get('balance_dollars') is not None else result['balance']/100
        verified_at=time.time()
        with engine.lock:engine.s['live_balance']=balance
        return jsonify(ok=True,balance=balance)
    except Exception:return jsonify(error='Connection not verified. Check your key pair, production account permissions, and network.'),400

@app.get('/api/export')
def export():
    import csv,io
    out=io.StringIO();w=csv.writer(out);w.writerow(['mode','asset','ticker','side','contracts','entry_usd','exit_usd','fees_usd','net_pnl_usd','reason','entry_order_id','exit_order_id','closed_utc','settled_result','hold_pnl_usd','exit_vs_hold_usd'])
    from datetime import datetime,timezone
    for t in engine.snapshot()['trades']:
        w.writerow([t.get('mode','paper'),t.get('asset') or asset_of(t['ticker']) or '',t['ticker'],t['side'],t['size'],t['entry'],t['exit'],t['entry_fee']+t['exit_fee'],t['pnl'],t['reason'],t.get('order_id',''),t.get('exit_order_id',''),datetime.fromtimestamp(t['closed'],timezone.utc).isoformat(),t.get('settled_result') or '',t.get('hold_pnl',''),t.get('exit_vs_hold','')])
    r=make_response(out.getvalue());r.headers['Content-Type']='text/csv';r.headers['Content-Disposition']='attachment; filename=kalshi-trades.csv';return r

@app.get('/api/log')
def trade_log():
    body=LOG_PATH.read_text() if LOG_PATH.exists() else ''
    r=make_response(body);r.headers['Content-Type']='application/x-ndjson';r.headers['Content-Disposition']='attachment; filename=kalshi-trade-log.jsonl';return r

@app.get('/api/contracts')
def contracts():
    try:after=float(request.args.get('after','0'))
    except ValueError:after=0
    return jsonify(markets=quote_history.since(after),now=time.time())

@app.get('/api/index')
def index_data():
    try:after=float(request.args.get('after','0'))
    except ValueError:after=0
    return jsonify(now=time.time(),**index_feed.since(after,current_tickers()))

@app.get('/api/contract_history')
def contract_history():
    """Kalshi's 1-minute YES bid/ask closes for an asset's current market (only that market)."""
    asset=request.args.get('asset','')
    if asset not in SERIES:return jsonify(error='Unsupported asset'),400
    with engine.lock:m=dict(engine.markets.get(asset) or {})
    if not m.get('ticker') or request.args.get('ticker',m['ticker'])!=m['ticker']:return jsonify(error='Not the current market'),404
    ticker,close=m['ticker'],m['close']
    with contract_lock:
        hit=contract_cache.get(ticker)
        if hit and time.time()-hit['fetched']<CONTRACT_TTL:return jsonify(hit)
    start,end=int(close-960),int(min(time.time(),close))
    try:body=get('/series/'+SERIES[asset]+'/markets/'+urllib.parse.quote(ticker,safe='')+f'/candlesticks?start_ts={start}&end_ts={end}&period_interval=1')
    except Exception as e:return jsonify(error='Kalshi history unavailable ('+type(e).__name__+')'),502
    try:backfill_trades(ticker)
    except Exception:pass                 # the WebSocket still delivers new trades
    trades=index_feed.since(0,[ticker])['trades'][ticker]
    result=dict(asset=asset,ticker=ticker,close=close,minutes=parse_candles(body),trades=trades,fetched=time.time())
    with contract_lock:contract_cache[ticker]=result
    return jsonify(result)

@app.get('/api/report')
def comparison_report():
    return jsonify(rows=engine.comparison_report())

@app.get('/api/report.csv')
def comparison_report_csv():
    r=make_response(report.to_csv(engine.comparison_report()));r.headers['Content-Type']='text/csv';r.headers['Content-Disposition']='attachment; filename=kalshi-fill-comparison.csv';return r

if __name__=='__main__':
    threading.Thread(target=feed,daemon=True).start()
    threading.Thread(target=tick_feed.run,daemon=True).start()
    threading.Thread(target=sim_waker,daemon=True).start()
    threading.Thread(target=index_feed.run,daemon=True).start()
    app.run(host='127.0.0.1',port=8765,debug=False,use_reloader=False)
