"""Simulated orders in flight (latency) and fill-comparison records, mixed into Engine.

A simulated order is filled against the first snapshot fetched at or after its due time, which
with ~2-second polling means latency is resolved at the next refresh after it elapses."""
import uuid

KEEP_FILLS = 1000                # comparison records kept


class SimQueue:
    @staticmethod
    def _order_fields(o):
        return {k: o.get(k) for k in ('id','model','observe','decision','asset','ticker','side','intent','reason','count','price','submitted','due')}

    def sim_due(self, now, retry=1.0):
        """(asset, ticker) of in-flight simulated orders that are due, so the app can fetch that book
        immediately rather than waiting for the next scheduled refresh. Each is offered at most once
        per ``retry`` seconds."""
        with self.lock:
            due = {}
            for o in self.s['sim_orders']:
                if o['due'] <= now and now - o.get('woken', 0) >= retry:
                    o['woken'] = now
                    due[(o['asset'], o['ticker'])] = True
            return list(due)

    def queue_sim(self, order):
        order['id'] = uuid.uuid4().hex[:12]
        self.s['sim_orders'].append(order)
        self.log.write('sim_order_submit', **self._order_fields(order))

    def _observe(self, asset, m, order, primary, now):
        """Comparison: record the quote, then simulate the same order under the other fill models."""
        c = self.s['config']
        if not c['fill_comparison']: return
        bids, asks = m.get(order['side']+'_bid'), m.get(order['side']+'_ask')
        self.s['fills'][order['decision']] = dict(asset=asset, ticker=m['ticker'], side=order['side'], intent=order['intent'], reason=order.get('reason'),
                                                  decided=now, ref=asks if order['intent']=='entry' else bids, count=order['count'], primary=primary)
        for model, sim in self.sims.items():
            if model == primary: continue
            if sim.params(c)[2] > 0:
                self.queue_sim(dict(order, model=model, asset=asset, observe=True, submitted=now, due=now+sim.params(c)[2]))
            else:
                self._compare(order['decision'], model, sim.fill(c, m, order))
        while len(self.s['fills']) > KEEP_FILLS:
            self.s['fills'].pop(next(iter(self.s['fills'])))

    def _compare(self, decision, model, fill):
        rec = self.s['fills'].get(decision)
        if rec is not None:
            rec[model] = dict(fill) if fill else dict(size=0)

    def _run_sim_orders(self, asset, m, now):
        """Fill simulated orders whose latency has elapsed against the first snapshot fetched at or after their due time."""
        c = self.s['config']
        for o in [o for o in self.s['sim_orders'] if o['asset'] == asset]:
            if o['ticker'] != m['ticker']:
                if now >= o['close']:
                    self.s['sim_orders'].remove(o); self.log.write('sim_order_expired', why='market closed', **self._order_fields(o))
                    self._finish_sim(asset, o, None, now)
                continue
            if m['fetched'] < o['due']: continue
            self.s['sim_orders'].remove(o)
            if m.get('status') != 'active' or m['fetched'] >= o['close']:
                self.log.write('sim_order_expired', why='market closed before arrival', **self._order_fields(o))
                self._finish_sim(asset, o, None, now); continue
            fill = self.sims[o['model']].fill(c, m, o)
            self.log.write('sim_fill' if fill else 'sim_no_fill', fill=fill, arrived=m['fetched'], **self._order_fields(o))
            self._finish_sim(asset, o, fill, now, m.get(o['side']+'_bid'))

    def _finish_sim(self, asset, o, fill, now, mark=None):
        if o.get('observe'):
            self._compare(o['decision'], o['model'], fill)
        elif o['intent'] == 'entry':
            self._on_entry(asset, self.sims[o['model']], o, fill, now, mark)
        else:
            self._on_exit(asset, self.sims[o['model']], o, fill, now)

