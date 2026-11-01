"""Live trading is disabled unless the operator explicitly enables it; exits of existing live positions are never blocked."""
import os
import tempfile
import unittest
from pathlib import Path

from engine import Engine
from helpers import FakeExecutor


class LiveGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)

    def engine(self, **kw):
        e = Engine(str(Path(self.temp.name) / 'p.db'), executor=FakeExecutor(), log_path=str(Path(self.temp.name) / 'l.jsonl'), **kw)
        self.addCleanup(e.db.close); return e

    def test_default_engine_refuses_live(self):
        e = self.engine()
        with self.assertRaisesRegex(ValueError, 'LIVE_TRADING_ENABLED'):
            e.set_mode('live')
        self.assertEqual(e.mode, 'paper')
        e.set_mode('shadow'); self.assertEqual(e.mode, 'shadow')        # non-live modes unaffected

    def test_enabled_engine_allows_live(self):
        e = self.engine(live_enabled=True); e.set_mode('live'); self.assertEqual(e.mode, 'live')

    def test_app_flag_parsing_defaults_to_off(self):
        src = (Path(__file__).resolve().parent.parent / 'app.py').read_text()
        start = src.index('def live_trading_enabled'); end = src.index('\n\n\n', start)
        ns = {'os': os}; exec(src[start:end], ns)
        for value, want in ((None, False), ('', False), ('false', False), ('1', False), ('yes', False), ('TRUE', True), (' true ', True)):
            old = os.environ.pop('LIVE_TRADING_ENABLED', None)
            try:
                if value is not None: os.environ['LIVE_TRADING_ENABLED'] = value
                self.assertEqual(ns['live_trading_enabled'](), want, value)
            finally:
                os.environ.pop('LIVE_TRADING_ENABLED', None)
                if old is not None: os.environ['LIVE_TRADING_ENABLED'] = old

    def test_app_wires_the_flag_into_the_engine(self):
        src = (Path(__file__).resolve().parent.parent / 'app.py').read_text()
        self.assertIn('live_enabled=live_trading_enabled()', src)


if __name__ == '__main__':
    unittest.main()
