"""Single paper-trading coordinator. This module never submits exchange orders."""
import copy
import json
import math
import os
import sqlite3
import threading
import time
import urllib.request
import uuid
from contextlib import closing
from pathlib import Path

import numpy as np
import pandas as pd
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, model_validator
from statsmodels.tsa.stattools import coint

ROOT = Path(__file__).resolve().parents[1]
PAIRS = [
    ('BTCUSDT', 'ETHUSDT'), ('ETHUSDT', 'SOLUSDT'), ('SOLUSDT', 'AVAXUSDT'),
    ('NEARUSDT', 'RENDERUSDT'), ('ARBUSDT', 'OPUSDT'),
]


class PaperSettings(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)
    mode: str = 'paper'
    initial_capital: float = Field(default=10000., gt=0)
    entry_z_score: float = Field(default=2., gt=0, le=5)
    exit_z_score: float = Field(default=.2, ge=0)
    stop_loss_z_score: float = Field(default=3.8, gt=0, le=10)
    max_open_pairs: int = Field(default=2, ge=1, le=5)
    capital_per_pair_pct: float = Field(default=15., gt=0, le=25)
    leverage: int = Field(default=1, ge=1, le=2)
    max_account_drawdown_pct: float = Field(default=10., gt=0, le=25)
    max_loss_pct: float = Field(default=2., gt=0, le=10)
    max_holding_hours: float = Field(default=36., gt=0, le=168)
    fee_rate: float = Field(default=.00055, ge=0, le=.01)
    slippage: float = Field(default=.0005, ge=0, le=.01)
    stale_seconds: float = Field(default=20., ge=5, le=60)
    poll_seconds: float = Field(default=5., ge=2, le=30)
    scan_seconds: float = Field(default=300., ge=60, le=3600)
    cooldown_seconds: float = Field(default=600., ge=60)
    p_value_threshold: float = Field(default=.05, gt=0, le=.1)
    max_half_life_days: float = Field(default=7., gt=0, le=14)
    timeframe: str = '60'
    history_bars: int = Field(default=400, ge=200, le=1000)

    @model_validator(mode='after')
    def validate_strategy(self):
        if self.mode != 'paper':
            raise ValueError('Only paper mode is supported by this tested runtime')
        if not self.exit_z_score < self.entry_z_score < self.stop_loss_z_score:
            raise ValueError('Require exit_z_score < entry_z_score < stop_loss_z_score')
        if self.max_open_pairs * self.capital_per_pair_pct > 80:
            raise ValueError('Total allocated margin must not exceed 80%')
        if self.timeframe not in {'15', '30', '60', '120', '240'}:
            raise ValueError('Supported candle intervals: 15,30,60,120,240 minutes')
        return self


class StateStore:
    """Atomic checkpoints, including the complete paper ledger, with one process owner."""
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lockfile = open(str(self.path) + '.lock', 'a+b')
        self.lockfile.seek(0)
        self.lockfile.write(b'1')
        self.lockfile.flush()
        self.lockfile.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.lockfile.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lockfile.close()
            raise RuntimeError('Another bot already owns this paper database') from None
        try:
            with closing(self.connect()) as conn, conn:
                conn.execute('CREATE TABLE IF NOT EXISTS checkpoint (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL)')
        except Exception:
            self.close()
            raise

    def connect(self):
        conn = sqlite3.connect(str(self.path), timeout=5)
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA synchronous=FULL')
        return conn

    def load(self):
        with closing(self.connect()) as conn:
            row = conn.execute('SELECT body FROM checkpoint WHERE id=1').fetchone()
        return json.loads(row[0]) if row else None

    def save(self, state):
        body = json.dumps(state, ensure_ascii=False, allow_nan=False)
        with closing(self.connect()) as conn, conn:
            conn.execute('INSERT INTO checkpoint VALUES(1, ?) ON CONFLICT(id) DO UPDATE SET body=excluded.body', (body,))

    def close(self):
        if not self.lockfile.closed:
            self.lockfile.close()

    backend = 'sqlite'

    @property
    def writable(self):
        return not self.lockfile.closed

    def acquire(self):
        return self.writable


class PublicMarket:
    """Public data only, bounded requests, no credentials and no fallback prices."""
    def get(self, endpoint, params):
        from urllib.parse import urlencode
        errors = []
        for base in ('https://api.bybit.com', 'https://api.bytick.com'):
            try:
                req = urllib.request.Request(base + '/v5/market/' + endpoint + '?' + urlencode(params),
                                             headers={'User-Agent': 'Tbot-Paper/2'})
                with urllib.request.urlopen(req, timeout=5) as response:
                    result = json.load(response)
                if result.get('retCode') != 0:
                    raise ValueError('Market API rejected request')
                if abs(time.time() - int(result['time']) / 1000) > 20:
                    raise ValueError('Market response timestamp is stale; check system clock')
                result['_received_at'] = time.time()
                self.clock_offset_seconds = int(result['time']) / 1000 - result['_received_at']
                return result
            except Exception as exc:
                errors.append(type(exc).__name__ + ': ' + str(exc)[:160])
        raise RuntimeError('; '.join(errors))

    def tickers(self):
        result = self.get('tickers', {'category': 'linear'})
        wanted = {s for p in PAIRS for s in p}
        ticks = {}
        for item in result['result']['list']:
            if item['symbol'] not in wanted:
                continue
            price = float(item['lastPrice'])
            bid, ask = float(item.get('bid1Price') or 0), float(item.get('ask1Price') or 0)
            if not all(math.isfinite(x) and x > 0 for x in (price, bid, ask)) or bid > ask:
                continue
            ticks[item['symbol']] = {'price': price, 'timestamp': result['_received_at'],
                                     'funding_rate': float(item.get('fundingRate') or 0),
                                     'next_funding': int(item.get('nextFundingTime') or 0) / 1000}
        if not ticks:
            raise ValueError('No valid quotes')
        return ticks

    def candles(self, symbol, settings):
        data = self.get('kline', {'category': 'linear', 'symbol': symbol,
                                  'interval': settings.timeframe, 'limit': settings.history_bars})
        frame = pd.DataFrame(data['result']['list'], columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'turnover'])
        frame['timestamp'] = frame['timestamp'].astype('int64') / 1000
        frame['close'] = frame['close'].astype(float)
        seconds = int(settings.timeframe) * 60
        return frame.loc[frame.timestamp + seconds <= time.time(), ['timestamp', 'close']].sort_values('timestamp')


def fit_model(df_a, df_b, settings, now=None):
    """Engle-Granger on training levels; fixed OLS hedge, held-out residual calibration."""
    now = time.time() if now is None else now
    merged = pd.merge(df_a, df_b, on='timestamp', suffixes=('_a', '_b')).sort_values('timestamp')
    if len(merged) < 180 or merged.timestamp.duplicated().any():
        raise ValueError('Insufficient aligned closed candles')
    seconds = int(settings.timeframe) * 60
    if (not np.allclose(np.diff(merged.timestamp), seconds)
            or not seconds <= now - merged.timestamp.iloc[-1] <= 2 * seconds + 30):
        raise ValueError('Stale or gapped candle history')
    values = merged[['close_a', 'close_b']].to_numpy()
    if not np.isfinite(values).all() or (values <= 0).any():
        raise ValueError('Invalid candle prices')
    train, calibration = values[:-48], values[-48:]
    if np.std(train[:, 0]) < 1e-10 or np.std(train[:, 1]) < 1e-10:
        raise ValueError('Constant price history')
    alpha, beta = np.linalg.lstsq(np.column_stack([np.ones(len(train)), train[:, 1]]), train[:, 0], rcond=None)[0]
    p_value = float(coint(train[:, 0], train[:, 1], trend='c', autolag='aic')[1])
    residual = train[:, 0] - alpha - beta * train[:, 1]
    from src.analytics.spread_calculator import calculate_half_life
    half_life = calculate_half_life(pd.Series(residual)) * seconds / 86400
    spread = calibration[:, 0] - alpha - beta * calibration[:, 1]
    mean, std = float(np.mean(spread)), float(np.std(spread, ddof=1))
    if std <= 1e-10 or not all(math.isfinite(x) for x in (alpha, beta, p_value, mean, std)):
        raise ValueError('Degenerate spread model')
    return {'alpha': float(alpha), 'beta': float(beta), 'mean': mean, 'std': std,
            'p_value': p_value, 'correlation': float(np.corrcoef(train.T)[0, 1]),
            'half_life': half_life if math.isfinite(half_life) else None,
            'is_cointegrated': bool(beta > 0 and p_value < settings.p_value_threshold and 0 < half_life < settings.max_half_life_days),
            'updated_at': now, 'candle_time': float(merged.timestamp.iloc[-1]),
            'labels': [time.strftime('%m-%d %H:%M', time.gmtime(x)) for x in merged.timestamp.iloc[-48:]],
            'z_score': [float(x) for x in (spread - mean) / std]}


class PaperEngine:
    def __init__(self, store, settings=None, market=None, clock=time.time):
        self.store, self.clock = store, clock
        saved = store.load()
        self.settings = PaperSettings(**saved['settings']) if saved else settings or PaperSettings()
        self.state = saved or {'schema': 1, 'mode': 'paper', 'run_id': uuid.uuid4().hex,
                              'started_at': self.clock(), 'balance': self.settings.initial_capital,
                              'peak_equity': self.settings.initial_capital, 'autopilot': os.environ.get('TBOT_START_PAUSED') != '1',
                              'positions': {}, 'trades': [], 'cooldowns': {}, 'disabled': [], 'equity': []}
        if self.state.get('schema') != 1 or self.state.get('mode') != 'paper':
            raise ValueError('Unsupported state database')
        self.lock = threading.RLock()
        self.market = market or PublicMarket()
        self.models, self.quotes, self.events = {}, {}, []
        self.last_tick = self.last_scan = 0.
        self.last_error = self.storage_error = None
        self.halt = threading.Event()
        self.threads = []
        self.closed = False
        if store.writable:
            self._persist()
        else:
            self.storage_error = 'Waiting for journal ownership after deploy'

    def require_owner(self):
        if not self.store.writable:
            raise ValueError('This instance is waiting for exclusive journal ownership')

    def _persist(self):
        self.require_owner()
        self.state['settings'] = self.settings.model_dump()
        try:
            self.store.save(self.state)
            self.storage_error = None
        except Exception:
            self.state['autopilot'] = False
            self.storage_error = 'Checkpoint failed; trading paused until storage is repaired'
            logger.exception(self.storage_error)
            raise

    def event(self, message, kind='info'):
        with self.lock:
            self.events.insert(0, {'time': time.strftime('%H:%M:%S'), 'type': kind, 'msg': message})
            del self.events[100:]
        logger.info(message)

    def fresh(self, pair):
        now = self.clock()
        return all(s in self.quotes and 0 <= now - self.quotes[s]['timestamp'] <= self.settings.stale_seconds for s in pair.split('/'))

    def zscore(self, model, pair):
        a, b = pair.split('/')
        return (self.quotes[a]['price'] - model['alpha'] - model['beta'] * self.quotes[b]['price'] - model['mean']) / model['std']

    def _mark(self, pos):
        a, b = pos['pair'].split('/')
        pa, pb = self.quotes[a]['price'], self.quotes[b]['price']
        # Estimate funding using the last observed rate/schedule, once per settlement.
        for symbol, direction, qty, price in ((a, pos['side'], pos['qty_a'], pa), (b, -pos['side'], pos['qty_b'], pb)):
            previous = pos['funding'].get(symbol)
            if previous and previous['next_funding'] > 0 and self.clock() >= previous['next_funding']:
                if previous['next_funding'] > pos['entry_time']:
                    pos['funding_pnl'] -= direction * qty * price * previous['funding_rate']
                pos['funding'].pop(symbol, None)
            tick = self.quotes[symbol]
            if tick.get('next_funding', 0) > self.clock():
                pos['funding'][symbol] = {k: tick[k] for k in ('next_funding', 'funding_rate')}
        raw = pos['side'] * (pos['qty_a'] * (pa - pos['entry_price_a']) - pos['qty_b'] * (pb - pos['entry_price_b']))
        exit_cost = (pa * pos['qty_a'] + pb * pos['qty_b']) * (self.settings.fee_rate + self.settings.slippage)
        pos.update(current_price_a=pa, current_price_b=pb, current_z=self.zscore(pos['model'], pos['pair']),
                   unrealized_pnl=raw - exit_cost + pos['funding_pnl'],
                   unrealized_pnl_pct=(raw - exit_cost - pos['entry_fees'] + pos['funding_pnl']) / pos['capital'] * 100)

    def _open(self, pair, side):
        if pair not in {'/'.join(p) for p in PAIRS} or side not in (-1, 1):
            raise ValueError('Unknown pair or invalid side')
        if not self.state['autopilot'] or self.storage_error:
            raise ValueError('Trading is paused')
        if pair in self.state['positions'] or pair in self.state['disabled']:
            raise ValueError('Pair is already open or disabled')
        if len(self.state['positions']) >= self.settings.max_open_pairs or self.clock() < self.state['cooldowns'].get(pair, 0):
            raise ValueError('Position limit or cooldown')
        model = self.models.get(pair)
        if not self.fresh(pair) or not model or self.clock() - model['updated_at'] > self.settings.scan_seconds * 3:
            raise ValueError('Fresh quotes and a current model are required')
        if not model['is_cointegrated'] or model['beta'] <= 0 or abs(self.zscore(model, pair)) >= self.settings.stop_loss_z_score:
            raise ValueError('Pair failed statistical risk checks')
        a, b = pair.split('/')
        pa, pb = self.quotes[a]['price'], self.quotes[b]['price']
        equity = self.state['balance'] + sum(p['unrealized_pnl'] for p in self.state['positions'].values())
        used = sum(p['capital'] for p in self.state['positions'].values())
        capital = equity * self.settings.capital_per_pair_pct / 100
        if capital <= 0 or used + capital > equity * .8:
            raise ValueError('Insufficient paper margin')
        notional = capital * self.settings.leverage
        qa = notional / (pa + model['beta'] * pb)
        qb = qa * model['beta']
        entry_fee = notional * (self.settings.fee_rate + self.settings.slippage)
        pos = {'id': uuid.uuid4().hex, 'pair': pair, 'symbol_a': a, 'symbol_b': b, 'side': side,
               'side_a': 'Buy' if side == 1 else 'Sell', 'side_b': 'Sell' if side == 1 else 'Buy',
               'qty_a': qa, 'qty_b': qb, 'entry_price_a': pa, 'entry_price_b': pb, 'capital': capital,
               'entry_time': self.clock(), 'entry_z': self.zscore(model, pair), 'entry_fees': entry_fee,
               'model': copy.deepcopy(model), 'funding': {}, 'funding_pnl': 0., 'close_requested': False,
               'status': 'PAPER'}
        self._mark(pos)
        self.state['balance'] -= entry_fee
        self.state['positions'][pair] = pos
        self.event(f'PAPER entry {pair}, Z={pos["entry_z"]:+.2f}', 'trade')

    def open(self, pair, side):
        with self.lock:
            self.require_owner()
            self._open(pair, side)
            self._persist()
            return copy.deepcopy(self.state['positions'][pair])

    def _close(self, pair, reason):
        pos = self.state['positions'][pair]
        if not self.fresh(pair):
            pos['close_requested'] = True
            return False
        self._mark(pos)
        pnl = pos['unrealized_pnl'] - pos['entry_fees']
        self.state['balance'] += pos['unrealized_pnl']
        self.state['trades'].append({'id': pos['id'], 'pair': pair, 'type': 'LONG SPREAD' if pos['side'] == 1 else 'SHORT SPREAD',
            'entry_z': pos['entry_z'], 'exit_z': pos['current_z'], 'pnl_num': pnl,
            'profit_usd': f'{pnl:+.2f}$', 'profit_pct': f'{pnl / pos["capital"] * 100:+.2f}%',
            'entry_time': pos['entry_time'], 'timestamp': self.clock(), 'date': time.strftime('%m-%d %H:%M'),
            'duration': f'{int((self.clock() - pos["entry_time"]) / 60)}m', 'exit_reason': reason,
            'funding_pnl': pos['funding_pnl']})
        del self.state['positions'][pair]
        self.state['cooldowns'][pair] = self.clock() + self.settings.cooldown_seconds
        self.event(f'PAPER exit {pair}: {pnl:+.2f} USDT ({reason})', 'trade')
        return True

    def close(self, pair=None, reason='Manual close'):
        with self.lock:
            self.require_owner()
            if pair and pair not in self.state['positions']:
                raise ValueError('Position not found')
            pairs = [pair] if pair else list(self.state['positions'])
            for key in pairs:
                self._close(key, reason)
            self._persist()
            return {'closed': [p for p in pairs if p not in self.state['positions']],
                    'pending': [p for p in pairs if p in self.state['positions']]}

    def stop(self, liquidate=True):
        with self.lock:
            self.require_owner()
            self.state['autopilot'] = False
            result = self.close(reason='Emergency stop') if liquidate else {'closed': [], 'pending': []}
            self._persist()
            return result

    def resume(self):
        with self.lock:
            self.require_owner()
            if self.storage_error:
                raise ValueError('Repair storage and restart before resuming')
            if any(p.get('close_requested') for p in self.state['positions'].values()):
                raise ValueError('Emergency closes are still pending')
            self.state['autopilot'] = True
            self._persist()

    def update_settings(self, changes):
        with self.lock:
            self.require_owner()
            if self.state['positions']:
                raise ValueError('Close positions before changing settings')
            if {'mode', 'initial_capital'} & changes.keys():
                raise ValueError('Mode and initial capital are immutable for a journal')
            settings = PaperSettings(**(self.settings.model_dump() | changes))
            self.settings = settings
            self.models.clear()
            self._persist()

    def tick(self, quotes):
        with self.lock:
            self.require_owner()
            now = self.clock()
            self.quotes = {s: dict(q) for s, q in quotes.items() if math.isfinite(q['price']) and q['price'] > 0
                           and math.isfinite(q['timestamp']) and 0 <= now - q['timestamp'] <= self.settings.stale_seconds}
            if not self.quotes:
                raise ValueError('No fresh prices')
            self.last_tick, self.last_error = now, None
            for pair, pos in list(self.state['positions'].items()):
                if not self.fresh(pair):
                    continue
                self._mark(pos)
                z, loss = pos['current_z'], pos['unrealized_pnl_pct']
                reason = None
                if pos['close_requested']:
                    reason = 'Requested emergency/manual close'
                elif loss <= -self.settings.max_loss_pct:
                    reason = 'Max loss'
                elif abs(z) >= self.settings.stop_loss_z_score:
                    reason = 'Statistical stop'
                elif now - pos['entry_time'] >= self.settings.max_holding_hours * 3600:
                    reason = 'Time stop'
                elif (pos['side'] == 1 and z >= -self.settings.exit_z_score) or (pos['side'] == -1 and z <= self.settings.exit_z_score):
                    reason = 'Mean reversion'
                if reason:
                    self._close(pair, reason)
            equity = self.state['balance'] + sum(p['unrealized_pnl'] for p in self.state['positions'].values())
            self.state['peak_equity'] = max(self.state['peak_equity'], equity)
            if equity <= self.state['peak_equity'] * (1 - self.settings.max_account_drawdown_pct / 100):
                self.state['autopilot'] = False
                for pair in list(self.state['positions']):
                    self._close(pair, 'Account drawdown stop')
            if self.state['autopilot'] and not self.storage_error:
                for pair, model in self.models.items():
                    if self.fresh(pair):
                        z = self.zscore(model, pair)
                        if self.settings.entry_z_score <= abs(z) < self.settings.stop_loss_z_score:
                            try:
                                self._open(pair, -1 if z > 0 else 1)
                            except ValueError:
                                continue
            equity = self.state['balance'] + sum(p['unrealized_pnl'] for p in self.state['positions'].values())
            if not self.state['equity'] or now - self.state['equity'][-1][0] >= 60:
                self.state['equity'].append([now, equity])
                self.state['equity'] = self.state['equity'][-10000:]
            self._persist()

    def scan(self):
        settings = self.settings
        candles = {}
        for a, b in PAIRS:
            if self.halt.is_set():
                return
            pair = f'{a}/{b}'
            try:
                for symbol in (a, b):
                    if symbol not in candles:
                        candles[symbol] = self.market.candles(symbol, settings)
                model = fit_model(candles[a], candles[b], settings, self.clock())
                with self.lock:
                    if self.settings is settings:
                        self.models[pair] = model
                self.event(f'Model {pair}: p={model["p_value"]:.4f}, eligible={model["is_cointegrated"]}')
            except Exception as exc:
                with self.lock:
                    self.models.pop(pair, None)
                self.event(f'Model unavailable {pair}: {type(exc).__name__}: {exc}', 'error')
        self.last_scan = self.clock()

    def start(self):
        def prices():
            while not self.halt.is_set():
                try:
                    self.tick(self.market.tickers())
                except Exception as exc:
                    with self.lock:
                        self.last_error = f'{type(exc).__name__}: {exc}'
                    logger.error('Price worker: {}', self.last_error)
                self.halt.wait(self.settings.poll_seconds)
        def models():
            while not self.halt.is_set():
                try:
                    self.scan()
                except Exception:
                    logger.exception('Model worker failed')
                self.halt.wait(self.settings.scan_seconds)
        def take_ownership():
            while not self.halt.is_set():
                try:
                    if self.store.acquire():
                        with self.lock:
                            saved = self.store.load()
                            if saved:
                                self.settings = PaperSettings(**saved['settings'])
                                self.state = saved
                            self._persist()
                        if self.halt.is_set():
                            return
                        for target in (prices, models):
                            t = threading.Thread(target=target, daemon=True, name='paper-' + target.__name__)
                            self.threads.append(t)
                            t.start()
                        return
                except Exception:
                    logger.error('Journal ownership is unavailable; workers remain stopped')
                self.halt.wait(1)
        coordinator = threading.Thread(target=take_ownership, daemon=True, name='paper-owner')
        self.threads.append(coordinator)
        coordinator.start()

    def shutdown(self):
        if self.closed:
            return
        self.halt.set()
        for thread in self.threads:
            thread.join(timeout=15)
        if any(t.is_alive() for t in self.threads):
            raise RuntimeError('Worker shutdown timed out; journal lock retained')
        with self.lock:
            try:
                if self.store.writable:
                    self._persist()
            finally:
                self.store.close()
                self.closed = True

    def snapshot(self):
        with self.lock:
            trades = self.state['trades']
            wins = [t['pnl_num'] for t in trades if t['pnl_num'] > 0]
            losses = [-t['pnl_num'] for t in trades if t['pnl_num'] < 0]
            stats = {'total_trades': len(trades), 'winning_trades': len(wins), 'losing_trades': len(losses),
                     'win_rate': len(wins) / len(trades) * 100 if trades else 0,
                     'profit_factor': sum(wins) / sum(losses) if losses else None,
                     'total_realized_pnl': sum(t['pnl_num'] for t in trades)}
            pos = copy.deepcopy(self.state['positions'])
            for pair, p in pos.items():
                p['stale'] = not self.fresh(pair)
                p.pop('model', None)
            ready = bool(any(self.fresh(pair) and 0 <= self.clock() - model['updated_at'] <= self.settings.scan_seconds * 3
                             for pair, model in self.models.items()) and not self.storage_error)
            return {'mode': 'PAPER', 'balance': self.state['balance'], 'unrealized_pnl': sum(p['unrealized_pnl'] for p in pos.values()),
                    'run_id': self.state.get('run_id'), 'started_at': self.state.get('started_at'),
                    'journal_backend': self.store.backend, 'journal_owner': self.store.writable,
                    'active_positions': pos, 'active_positions_count': len(pos), 'is_kill_switch_active': not self.state['autopilot'],
                    'total_pairs_count': len(PAIRS), 'enabled_pairs_count': len(PAIRS) - len(self.state['disabled']),
                    'closed_trades_count': len(trades), 'stats': stats, 'recent_trades': copy.deepcopy(trades[-50:][::-1]),
                    'closed_trades': copy.deepcopy(trades[-50:][::-1]), 'ready': ready, 'last_tick': self.last_tick,
                    'last_scan': self.last_scan, 'last_error': self.storage_error or self.last_error,
                    'market_clock_offset_seconds': getattr(self.market, 'clock_offset_seconds', None),
                    'funding_model': 'Estimated from last observed settlement rate; missed offline settlements are not reconstructed'}

    def pairs(self):
        with self.lock:
            rows = []
            for a, b in PAIRS:
                pair = a + '/' + b
                m = self.models.get(pair, {})
                rows.append({'pair': pair, 'symbol_a': a, 'symbol_b': b, 'type': 'crypto', 'sector': 'Crypto / PAPER',
                             'enabled': pair not in self.state['disabled'], 'price_a': self.quotes.get(a, {}).get('price'),
                             'price_b': self.quotes.get(b, {}).get('price'), 'current_z': self.zscore(m, pair) if m and self.fresh(pair) else None,
                             'p_value': m.get('p_value'), 'correlation': m.get('correlation'), 'half_life': m.get('half_life'),
                             'is_cointegrated': m.get('is_cointegrated', False), 'fresh': self.fresh(pair)})
            return rows
