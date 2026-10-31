import tempfile
import unittest
from pathlib import Path
from engine import Engine,DEFAULTS,fee,validate

class PaperTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.path=str(Path(self.temp.name)/'paper.db');self.e=Engine(self.path);self.now=1800000000
 def tearDown(self):self.e.db.close();self.temp.cleanup()
 def market(self,**kw):
  return dict(dict(asset='BTC',ticker='TEST-1',status='active',result='',close=self.now+120,fetched=self.now,yes_ask=.85,no_ask=.17,yes_bid=.83,no_bid=.15,yes_ask_size=10,no_ask_size=10,yes_bid_size=10,no_bid_size=10),**kw)
 def enter(self):self.e.control('start');self.e.tick(self.market(),self.now)
 def test_paused_no_entries(self):self.e.tick(self.market(),self.now);self.assertIsNone(self.e.s['positions'].get('BTC'))
 def test_stale_no_entries(self):self.e.control('start');self.e.tick(self.market(fetched=self.now-11),self.now);self.assertIsNone(self.e.s['positions'].get('BTC'))
 def test_size_gate(self):self.e.control('start');self.e.tick(self.market(yes_ask_size=.5),self.now);self.assertIsNone(self.e.s['positions'].get('BTC'))
 def test_fee_rounding(self):self.assertEqual(fee(.85,1),.01);self.assertEqual(fee(.5,1),.02)
 def test_entry_exit_accounting_and_no_reentry(self):
  self.enter();self.assertAlmostEqual(self.e.s['cash'],999.14)
  self.e.tick(self.market(yes_bid=.96,fetched=self.now+2),self.now+2)
  self.assertAlmostEqual(self.e.s['cash'],1000.09);self.assertAlmostEqual(self.e.s['trades'][0]['pnl'],.09)
  self.e.tick(self.market(fetched=self.now+3),self.now+3);self.assertIsNone(self.e.s['positions'].get('BTC'))
 def test_stop_confirmation_and_paused_management(self):
  self.enter();self.e.control('pause');self.e.tick(self.market(yes_bid=.30),self.now)
  self.assertIsNotNone(self.e.s['positions'].get('BTC'));self.e.tick(self.market(yes_bid=.30,fetched=self.now+1),self.now+1)
  self.assertIsNone(self.e.s['positions'].get('BTC'));self.assertAlmostEqual(self.e.s['trades'][0]['pnl'],-.58)
 def test_insufficient_exit_liquidity(self):
  self.enter();self.e.tick(self.market(yes_bid=.96,yes_bid_size=0),self.now);self.assertIsNotNone(self.e.s['positions'].get('BTC'))
 def test_settlement_requires_final_status(self):
  self.enter();self.e.tick(self.market(status='closed',result='yes',close=self.now-1),self.now)
  self.assertIsNotNone(self.e.s['positions'].get('BTC'));self.e.tick(self.market(status='finalized',result='yes'),self.now)
  self.assertAlmostEqual(self.e.s['cash'],1000.14)
 def test_restart_preserves_position_pauses_entries(self):
  self.enter();other=Engine(self.path)
  self.assertFalse(other.running);self.assertEqual(other.s['positions'].get('BTC')['ticker'],'TEST-1');self.assertAlmostEqual(other.s['cash'],999.14);other.db.close()
 def test_limit_blocks_new_entries(self):
  self.enter();self.e.s['config']['daily_loss']=.1;self.e.tick(self.market(yes_bid=.30),self.now);self.e.tick(self.market(yes_bid=.30,fetched=self.now+2),self.now+2)
  self.assertFalse(self.e.running);self.e.control('start');self.e.tick(self.market(ticker='TEST-2',fetched=self.now+3),self.now+3);self.assertIsNone(self.e.s['positions'].get('BTC'))
 def test_settings_pending_while_position_open(self):
  self.enter();self.e.control('pause')
  r=self.e.configure(dict(DEFAULTS,dollars=30));self.assertTrue(r['pending'])
  self.assertEqual(self.e.s['config']['dollars'],20);self.assertEqual(self.e.s['pending_config']['dollars'],30)
 def test_invalid_settings(self):
  for patch in [dict(entry_min=float('nan')),dict(contracts=-1),dict(assets=['BAD']),dict(stop=.99)]:
   with self.assertRaises(ValueError):validate(dict(DEFAULTS,**patch))
 def test_missing_bid_resets_stop_confirmation(self):
  self.enter();self.e.tick(self.market(yes_bid=.30),self.now);self.e.tick(self.market(yes_bid=None,fetched=self.now+1),self.now+1);self.e.tick(self.market(yes_bid=.30,fetched=self.now+2),self.now+2);self.assertIsNotNone(self.e.s['positions'].get('BTC'))
 def test_expired_no_entry(self):self.e.control('start');self.e.tick(self.market(close=self.now-1),self.now);self.assertIsNone(self.e.s['positions'].get('BTC'))
 def test_stale_exit_does_not_fill(self):self.enter();self.e.tick(self.market(yes_bid=.96,fetched=self.now-20),self.now);self.assertIsNotNone(self.e.s['positions'].get('BTC'))

 def test_dollar_sizing_fits_budget_with_fees(self):
  from engine import order_count
  c=dict(DEFAULTS,size_mode='dollars',dollars=10)
  n=order_count(c,.85);self.assertEqual(n,11);self.assertLessEqual(.85*n+fee(.85,n),10)
  self.assertEqual(order_count(dict(c,dollars=1),.85),1);self.assertEqual(order_count(dict(c,dollars=1),.99),1);self.assertEqual(order_count(dict(c,dollars=1),.995),0)
 def test_dollar_sizing_paper_entry(self):
  self.e.s['config'].update(size_mode='dollars',dollars=10);self.e.control('start');self.e.tick(self.market(yes_ask_size=20),self.now)
  p=self.e.s['positions'].get('BTC');self.assertEqual(p['size'],11);self.assertLessEqual(p['cost'],10)
 def test_dollar_sizing_needs_book_size(self):
  self.e.s['config'].update(size_mode='dollars',dollars=10);self.e.control('start');self.e.tick(self.market(yes_ask_size=5,no_ask_size=5),self.now);self.assertIsNone(self.e.s['positions'].get('BTC'))
 def test_old_saved_config_gets_new_settings(self):
  self.e.s['config'].pop('size_mode');self.e.s['config'].pop('dollars');self.e.save();other=Engine(self.path)
  self.assertEqual(other.s['config']['size_mode'],'contracts');validate(dict(other.s['config']));other.db.close()
 def test_invalid_size_settings(self):
  for patch in [dict(size_mode='percent'),dict(dollars=0),dict(dollars=5000)]:
   with self.assertRaises(ValueError):validate(dict(DEFAULTS,**patch))

 def test_stop_zero_and_tp_one_hold_to_settlement(self):
  self.e.s['config'].update(stop=0,take_profit=1);validate(dict(self.e.s['config']));self.enter()
  for i,b in enumerate([.01,.99,.01]):self.e.tick(self.market(yes_bid=b,fetched=self.now+i+1),self.now+i+1)
  self.assertIsNotNone(self.e.s['positions'].get('BTC'))
  self.e.tick(self.market(status='finalized',result='yes',close=self.now-1,fetched=self.now+9),self.now+9)
  self.assertIsNone(self.e.s['positions'].get('BTC'));self.assertEqual(self.e.s['trades'][0]['reason'],'settlement');self.assertEqual(self.e.s['trades'][0]['exit'],1.0)

if __name__=='__main__':unittest.main()
