"""Chart vertical scale (static/app.js, display only).

LIVE (30 s) and 5M zoom to the price like Kalshi's market page; only 15M (whole market) always
includes the target. With no data at all the geometry is null ("Collecting…"), never ±Infinity.
Runs the real functions from app.js in Node with stub data; skipped if Node is not installed."""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / 'static' / 'app.js'
NODE = shutil.which('node')


def extract(src, name):
    m = re.search(r'^function ' + name + r'\(.*?(?=^function )', src, re.S | re.M)
    if not m:
        raise AssertionError(f'{name} not found in app.js')
    return m.group(0)


HARNESS = r'''
const CASE = JSON.parse(process.argv[1]);
const now = Date.now() / 1000;
const strike = CASE.strike;
const state = {markets: {BTC: {close: Math.ceil(now / 900) * 900, strike}}};
const ranges = {BTC: CASE.range}, candleMode = {BTC: false}, prices = {}, ticks = {BTC: []};
const pts = CASE.values.map((v, i, a) => [now - (a.length - 1 - i), v]);
const idx = {BTC: {points: pts, avg: pts}};
const idxLive = () => CASE.useIdx, basisFor = () => null;
__FUNCS__
const g = CASE.fn === 'kalshi' ? kalshiGeom('BTC') : liveGeom('BTC');
console.log(JSON.stringify(g ? {lo: g.lo, hi: g.hi} : null));
'''


@unittest.skipUnless(NODE, 'node not installed')
class ChartScaleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        src = APP.read_text()
        cls.funcs = extract(src, 'kalshiGeom') + '\n' + extract(src, 'liveGeom')

    def geom(self, fn, rng, values, strike, use_idx=True):
        case = dict(fn=fn, range=rng, values=values, strike=strike, useIdx=use_idx)
        out = subprocess.run([NODE, '-e', HARNESS.replace('__FUNCS__', self.funcs), json.dumps(case)],
                             capture_output=True, text=True, timeout=20)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    PRICES = [77480 + i % 10 for i in range(25)]   # a $9 wiggle, $80+ above the target
    STRIKE = 77400

    def test_live_and_5m_zoom_to_the_price(self):
        for fn in ('kalshi', 'live'):
            for rng in ('live', '5m'):
                g = self.geom(fn, rng, self.PRICES, self.STRIKE)
                self.assertGreater(g['lo'], self.STRIKE, (fn, rng))          # target off-screen, pinned
                self.assertLess(g['hi'] - g['lo'], 15, (fn, rng))            # the $9 move fills the chart

    def test_whole_market_always_shows_the_target(self):
        for fn in ('kalshi', 'live'):
            g = self.geom(fn, '15m', self.PRICES, self.STRIKE)
            self.assertLessEqual(g['lo'], self.STRIKE, fn)

    def test_target_only_when_there_is_no_price_yet(self):
        g = self.geom('kalshi', 'live', [], self.STRIKE)
        self.assertLessEqual(g['lo'], self.STRIKE); self.assertGreaterEqual(g['hi'], self.STRIKE)

    def test_no_data_and_no_target_is_null_not_infinite(self):
        for fn in ('kalshi', 'live'):
            self.assertIsNone(self.geom(fn, 'live', [], None, use_idx=(fn == 'kalshi')), fn)


if __name__ == '__main__':
    unittest.main()
