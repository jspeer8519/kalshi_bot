import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from cryptography.hazmat.primitives import serialization,hashes
from cryptography.hazmat.primitives.asymmetric import ed25519,rsa,padding
import base64
TEMP=tempfile.TemporaryDirectory()
os.environ['KALSHI_DATA_DIR']=TEMP.name
import app as server
server.PRIVATE=Path(TEMP.name)/'private';server.PRIVATE.mkdir(mode=0o700)

class ApiTests(unittest.TestCase):
 def setUp(self):self.client=server.app.test_client();self.base='http://127.0.0.1:8765'
 def post(self,path,data,headers=None):return self.client.post(path,json=data,base_url=self.base,headers=headers or {'X-Desk-Token':server.TOKEN})
 def test_missing_csrf_rejected(self):self.assertEqual(self.client.post('/api/control',json={'action':'start'},base_url=self.base).status_code,403)
 def test_foreign_origin_rejected(self):self.assertEqual(self.post('/api/control',{'action':'start'},{'X-Desk-Token':server.TOKEN,'Origin':'https://evil.example'}).status_code,403)
 def test_foreign_host_rejected(self):self.assertEqual(self.client.get('/api/state',base_url='http://evil.example:8765').status_code,403)
 def test_control(self):
  self.assertEqual(self.post('/api/control',{'action':'start'}).status_code,200)
  self.assertTrue(server.engine.running)
  self.post('/api/control',{'action':'pause'});self.assertFalse(server.engine.running)
 def test_export(self):self.assertIn(b'net_pnl_usd',self.client.get('/api/export',base_url=self.base).data)
 def test_live_action_rejected(self):self.assertEqual(self.post('/api/control',{'action':'live'}).status_code,400)
 def test_invalid_pem_rejected(self):self.assertEqual(self.post('/api/credentials',{'key_id':'example','pem':'invalid'}).status_code,400)
 def test_signing_and_secret_exclusion(self):
  for key in [ed25519.Ed25519PrivateKey.generate(),rsa.generate_private_key(public_exponent=65537,key_size=2048)]:
   pem=key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()).decode()
   response=self.post('/api/credentials',{'key_id':'test-id','pem':pem});self.assertEqual(response.status_code,200)
   self.assertEqual((server.PRIVATE/'credentials.json').stat().st_mode & 0o777,0o600)
   state=self.client.get('/api/state',base_url=self.base).data.decode();self.assertNotIn('PRIVATE KEY',state);self.assertNotIn('test-id',state)
   def fake_get(path,headers,key=key):
    self.assertEqual(path,'/portfolio/balance');msg=(headers['KALSHI-ACCESS-TIMESTAMP']+'GET/trade-api/v2/portfolio/balance').encode();sig=base64.b64decode(headers['KALSHI-ACCESS-SIGNATURE'])
    if isinstance(key,ed25519.Ed25519PrivateKey):key.public_key().verify(sig,msg)
    else:key.public_key().verify(sig,msg,padding.PSS(mgf=padding.MGF1(hashes.SHA256()),salt_length=32),hashes.SHA256())
    return {'balance':12345}
   with patch.object(server,'get',side_effect=fake_get):self.assertEqual(self.post('/api/connection',{}).json['balance'],123.45)
 def test_orderbook_complements(self):
  m={'ticker':'TEST','title':'Test','status':'active','close_time':'2030-01-01T00:00:00Z'}
  with patch.object(server,'get',return_value={'orderbook_fp':{'yes_dollars':[['0.80','2'],['0.81','3']],'no_dollars':[['0.17','4']]}}):
   q=server.market_snapshot(m);self.assertEqual(q['yes_ask'],.83);self.assertEqual(q['yes_ask_size'],4);self.assertEqual(q['no_ask'],.19);self.assertEqual(q['no_ask_size'],3)
 def test_empty_book_not_zero_price(self):
  with patch.object(server,'get',return_value={'orderbook_fp':{}}):
   q=server.market_snapshot({'ticker':'TEST','status':'active','close_time':'2030-01-01T00:00:00Z'});self.assertIsNone(q['yes_ask']);self.assertEqual(q['yes_ask_size'],0)

 def test_live_mode_requires_confirmation_and_verification(self):
  import time
  self.assertEqual(self.post('/api/mode',{'mode':'live'}).status_code,400)
  key=ed25519.Ed25519PrivateKey.generate();pem=key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()).decode()
  self.post('/api/credentials',{'key_id':'test-id','pem':pem})
  server.verified_at=0
  self.assertEqual(self.post('/api/mode',{'mode':'live','confirm':'LIVE'}).status_code,400)
  server.verified_at=time.time()
  self.assertEqual(self.post('/api/mode',{'mode':'live','confirm':'live'}).status_code,400)
  self.assertEqual(self.post('/api/mode',{'mode':'live','confirm':'LIVE'}).status_code,200)
  self.assertEqual(server.engine.mode,'live')
  self.assertEqual(self.post('/api/mode',{'mode':'paper'}).status_code,200);self.assertEqual(server.engine.mode,'paper')
  server.verified_at=0
 def test_trade_log_download(self):
  self.assertEqual(self.client.get('/api/log',base_url=self.base).status_code,200)
 def test_price_candles_parse_and_route(self):
  import io,json as _j
  rows=[[1800000060,100,110,105,108,5],[1800000000,99,106,100,105,4],[1800000120,'bad',1,1,1,1]]
  class R(io.BytesIO):
   def __enter__(self):return self
   def __exit__(self,*a):pass
  server.price_cache.clear()
  with patch.object(server.urllib.request,'urlopen',side_effect=lambda *a,**k:R(_j.dumps(rows[:2]).encode())):
   d=self.client.get('/api/price',base_url=self.base).json
  self.assertEqual([c['t'] for c in d['candles']],[1800000000,1800000060]);self.assertEqual(d['candles'][1]['c'],108);self.assertEqual(d['product'],'BTC-USD')
  server.price_cache.clear()
  with patch.object(server.urllib.request,'urlopen',side_effect=OSError()):
   self.assertEqual(self.client.get('/api/price',base_url=self.base).status_code,502)
 def test_feed_caches_market_details_until_close(self):
  calls=[]
  m={'ticker':'KXBTC15M-X','title':'t','status':'active','close_time':'2099-01-01T00:00:00Z','floor_strike':77000.5}
  def fake(path,headers=None):
   calls.append(path)
   if path.startswith('/markets?'):return {'markets':[m]}
   return {'orderbook_fp':{'yes_dollars':[['0.40','5'],['0.41','7']],'no_dollars':[['0.55','3']]}}
  server.meta_cache.clear()
  with patch.object(server,'get',side_effect=fake):
   s1=server.fetch_asset_market('BTC');s2=server.fetch_asset_market('BTC')
  self.assertEqual(sum(p.startswith('/markets?') for p in calls),1);self.assertEqual(s2['yes_bids'],[[0.41,7.0],[0.4,5.0]]);self.assertEqual(s1['asset'],'BTC')
 def test_shadow_mode_needs_no_credentials_and_live_small_alias_still_needs_confirmation(self):
  self.assertEqual(self.post('/api/mode',{'mode':'shadow'}).status_code,200);self.assertEqual(server.engine.snapshot()['mode'],'SHADOW')
  self.assertEqual(self.post('/api/mode',{'mode':'live_small'}).status_code,400)
  self.assertEqual(self.post('/api/mode',{'mode':'paper'}).status_code,200)
 def test_config_reports_pending(self):
  c=dict(server.engine.s['config'])
  r=self.post('/api/config',c);self.assertEqual(r.status_code,200);self.assertEqual(r.json['pending'],False)
  self.assertEqual(self.post('/api/config',dict(c,fair_value='yes')).status_code,400)
 def test_report_routes(self):
  self.assertEqual(self.client.get('/api/report',base_url=self.base).json,{'rows':[]})
  self.assertTrue(self.client.get('/api/report.csv',base_url=self.base).data.startswith(b'closed,asset,ticker'))
 def test_check_settlements_fetches_due_markets(self):
  from concurrent.futures import ThreadPoolExecutor
  seen=[]
  with patch.object(server.engine,'settlement_due',return_value=['KXBTC15M-A']),patch.object(server.engine,'record_settlement',side_effect=lambda *a:seen.append(a)),\
       patch.object(server,'get',return_value={'market':{'status':'finalized','result':'no'}}) as g:
   server.check_settlements(ThreadPoolExecutor(1))
  self.assertEqual(g.call_args[0][0],'/markets/KXBTC15M-A');self.assertEqual(seen,[('KXBTC15M-A','finalized','no')])
 def test_spot_prices_reach_engine(self):
  with patch.object(server.index_feed,'series',return_value=[]),patch.object(server.tick_feed,'series',return_value=[(1,2.0)]):self.assertEqual(server.engine.spot_fn('BTC'),[(1,2.0)])
 def test_contract_routes(self):
  server.quote_history.add(dict(asset='BTC',ticker='KXBTC15M-Q',status='active',close=2000,fetched=1000.0,yes_bid=.6,yes_ask=.61))
  self.assertEqual(self.client.get('/api/contracts?after=0',base_url=self.base).json['markets']['BTC']['points'],[[1000.0,.6,.61]])
  self.assertEqual(self.client.get('/api/contract_history?asset=NOPE',base_url=self.base).status_code,400)
  with server.engine.lock:saved=server.engine.markets.pop('ETH',None)
  self.assertEqual(self.client.get('/api/contract_history?asset=ETH',base_url=self.base).status_code,404)   # no current market
  with server.engine.lock:server.engine.markets['BTC']=dict(ticker='KXBTC15M-Q',close=2000,status='active')
  try:
   self.assertEqual(self.client.get('/api/contract_history?asset=BTC&ticker=KXBTC15M-OTHER',base_url=self.base).status_code,404)
   calls=[]
   def fake(path,headers=None):
    calls.append(path);return {'candlesticks':[{'end_period_ts':1060,'yes_bid':{'close_dollars':'0.62'},'yes_ask':{'close_dollars':'0.63'}}]}
   server.contract_cache.clear()
   with patch.object(server,'get',side_effect=fake):
    d=self.client.get('/api/contract_history?asset=BTC&ticker=KXBTC15M-Q',base_url=self.base).json
    self.client.get('/api/contract_history?asset=BTC',base_url=self.base)                 # cached
   self.assertEqual(d['minutes'],[[1060.0,.62,.63]]);self.assertEqual(sum('/candlesticks' in c for c in calls),1)   # second request served from cache
   self.assertTrue(calls[0].startswith('/series/KXBTC15M/markets/KXBTC15M-Q/candlesticks?start_ts=1040&'))
   server.contract_cache.clear()
   with patch.object(server,'get',side_effect=OSError()):
    self.assertEqual(self.client.get('/api/contract_history?asset=BTC',base_url=self.base).status_code,502)
  finally:
   with server.engine.lock:
    server.engine.markets.pop('BTC',None)
    if saved:server.engine.markets['ETH']=saved
 def test_wake_sim_orders_fetches_due_markets_now(self):
  got=[]
  with patch.object(server.engine,'sim_due',return_value=[('BTC','KXBTC15M-W')]),patch.object(server.engine,'tick',side_effect=got.append):
   server.wake_sim_orders(fetch=lambda a,t:dict(asset=a,ticker=t,status='active',close=2e9,fetched=1.0,yes_bid=.5,yes_ask=.51))
   self.assertEqual(got[0]['ticker'],'KXBTC15M-W')
   server.wake_sim_orders(fetch=lambda a,t:(_ for _ in ()).throw(OSError()))     # a failed fetch is ignored
 def test_book_now_reuses_market_details_with_one_request(self):
  calls=[]
  with server.engine.lock:server.engine.markets['SOL']=dict(asset='SOL',ticker='KXSOL15M-B',status='active',close=2e9,strike=95.0,fetched=1.0,yes_bid=.1,yes_ask=.2,yes_bids=[[.1,1]])
  try:
   with patch.object(server,'get',side_effect=lambda p,h=None:(calls.append(p),{'orderbook_fp':{'yes_dollars':[['0.60','5']],'no_dollars':[['0.38','7']]}})[1]):
    snap=server.book_now('SOL','KXSOL15M-B')
   self.assertEqual(calls,['/markets/KXSOL15M-B/orderbook?depth=10'])
   self.assertEqual((snap['strike'],snap['yes_bid'],snap['yes_ask'],snap['no_ask'],snap['yes_bids']),(95.0,.6,.62,.4,[[.6,5.0]]))
   self.assertGreater(snap['fetched'],1.0)
  finally:
   with server.engine.lock:server.engine.markets.pop('SOL',None)
 def test_index_route_and_trade_backfill(self):
  server.index_feed.handle({'type':'cfbenchmarks_value_5hz','msg':{'index_id':'BRTI','value_usd':'77164.10000000','source_ts_ms':1793436000000}})
  with patch.object(server,'current_tickers',return_value=[]):
   d=self.client.get('/api/index?after=0',base_url=self.base).json
  self.assertEqual(d['index']['BTC']['points'][-1],[1793436000.0,77164.1]);self.assertIn('status',d)
  pages=[{'trades':[{'trade_id':'x1','ticker':'KXBTC15M-R','yes_price_dollars':'0.82','count_fp':'2.00','taker_side':'yes','created_time':'2026-10-31T08:53:14Z'}],'cursor':'c2'},
         {'trades':[{'trade_id':'x2','ticker':'KXBTC15M-R','yes_price_dollars':'0.81','count_fp':'1.00','taker_side':'no','created_time':'2026-10-31T08:53:15Z'}],'cursor':''}]
  calls=[]
  with patch.object(server,'get',side_effect=lambda p,h=None:(calls.append(p),pages[len(calls)-1])[1]):server.backfill_trades('KXBTC15M-R')
  self.assertEqual(len(calls),2);self.assertIn('&cursor=c2',calls[1])
  self.assertEqual([r[1] for r in server.index_feed.since(0,['KXBTC15M-R'])['trades']['KXBTC15M-R']],[.82,.81])
 def test_fair_value_prefers_the_settlement_index(self):
  with patch.object(server.index_feed,'series',return_value=[(1,77164.0)]),patch.object(server.tick_feed,'series',return_value=[(1,77240.0)]):
   self.assertEqual(server.engine.spot_fn('BTC'),[(1,77164.0)])
  with patch.object(server.index_feed,'series',return_value=[]),patch.object(server.tick_feed,'series',return_value=[(1,77240.0)]):
   self.assertEqual(server.engine.spot_fn('BTC'),[(1,77240.0)])
 def test_market_without_target_is_not_cached(self):
  calls=[]
  m={'ticker':'KXBTC15M-Y','title':'t','status':'active','close_time':'2099-01-01T00:00:00Z'}   # target not published yet
  def fake(path,headers=None):
   calls.append(path)
   return {'markets':[m]} if path.startswith('/markets?') else {'orderbook_fp':{}}
  server.meta_cache.clear()
  with patch.object(server,'get',side_effect=fake):
   server.fetch_asset_market('BTC');m['floor_strike']=77010.25;s2=server.fetch_asset_market('BTC');server.fetch_asset_market('BTC')
  self.assertEqual(sum(p.startswith('/markets?') for p in calls),2);self.assertEqual(s2['strike'],77010.25)   # asked again, then cached
 def test_export_fills_missing_asset(self):
  with server.engine.lock:
   server.engine.s['trades'].insert(0,dict(ticker='KXBTC15M-OLD',side='yes',size=1,entry=.88,exit=.98,entry_fee=.01,exit_fee=.01,cost=.89,pnl=.08,reason='take profit',opened=1,closed=2))
  try:self.assertIn(b'paper,BTC,KXBTC15M-OLD',self.client.get('/api/export',base_url=self.base).data)
  finally:
   with server.engine.lock:server.engine.s['trades'].pop(0)
