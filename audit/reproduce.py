"""Offline audit reproductions. No exchange/Telegram requests or project state writes."""
import asyncio
import gc
import inspect
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
import pandas as pd
import yaml
from loguru import logger
from src.execution.order_manager import OrderManager
from src.risk.risk_manager import RiskManager
from src.bot.tg_reporter import TelegramReporter
from src.backtesting.engine import PairBacktestEngine
from src.backtesting.metrics import calculate_backtest_metrics
from src.storage.db_manager import DatabaseManager
from src.webapp import server
from src.analytics.screener import PairScreener
from statsmodels.tsa.stattools import coint

logger.remove()
results = {}

# Closing rejected by exchange still removes tracked exposure.
c = Mock()
c.place_market_order.side_effect = ['A', 'B', None, None]
om = OrderManager(c)
om.open_pair_position('A', 'B', 1, 1, 1, -2, 1)
closed = om.close_pair_position('A/B')
results['rejected_close'] = {'reported_closed': bool(closed), 'tracked_positions': len(om.active_positions)}

# Failed hedge and failed compensation leave no tracked position.
c = Mock()
c.place_market_order.side_effect = ['A', None, None]
om = OrderManager(c)
om.open_pair_position('A', 'B', 1, 1, 1, -2, 1)
results['failed_unwind'] = {'tracked_positions': len(om.active_positions), 'requests': c.place_market_order.call_count}
results['telegram_interface'] = {
    'send_message_exists': hasattr(TelegramReporter, 'send_message'),
    'notify_signal_exists': hasattr(TelegramReporter, 'notify_signal'),
    'notify_entry_async': inspect.iscoroutinefunction(TelegramReporter.notify_entry),
    'notify_exit_signature': str(inspect.signature(TelegramReporter.notify_exit)),
}

async def web_checks(tmp):
    cfg = tmp / 'config.yaml'
    cfg.write_text(yaml.safe_dump({'bybit': {'testnet': True}, 'strategy': {}, 'risk': {}}), encoding='utf-8')
    state = tmp / 'state.json'
    risk = RiskManager(Mock())
    orders = OrderManager(Mock())
    client = SimpleNamespace(api_key='', testnet=True)
    db = DatabaseManager(tmp / 'journal.db')
    with patch.object(server, 'CONFIG_PATH', cfg), patch.object(server, 'STATE_PATH', state), \
         patch.object(server, 'DatabaseManager', return_value=db), patch.object(server.threading.Thread, 'start'):
        app = server.create_app(bybit_client=client, order_manager=orders, risk_manager=risk)
        routes = {r.path: r for r in app.routes if hasattr(r, 'endpoint')}
        # Exercise actual ASGI stack anonymously, without HTTP client dependencies.
        async def request(path, payload):
            body = json.dumps(payload).encode()
            sent = []
            async def receive():
                return {'type': 'http.request', 'body': body, 'more_body': False}
            async def send(msg):
                sent.append(msg)
            await app({'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
                       'method': 'POST', 'scheme': 'http', 'path': path, 'raw_path': path.encode(),
                       'query_string': b'', 'root_path': '', 'headers': [(b'content-type', b'application/json')],
                       'client': ('127.0.0.1', 1234), 'server': ('test', 80)}, receive, send)
            return next(m['status'] for m in sent if m['type'] == 'http.response.start')
        results['anonymous_settings_http_status'] = await request('/api/settings', {'max_open_pairs': -99, 'testnet': 'false'})
        saved = yaml.safe_load(cfg.read_text(encoding='utf-8'))
        results['invalid_settings'] = {'saved_max_open_pairs': saved['risk']['max_open_pairs'],
                                       'saved_testnet_for_string_false': saved['bybit']['testnet'],
                                       'runtime_max_open_pairs': risk.max_open_pairs}
        await routes['/api/demo/open_trade'].endpoint({'pair': 'BTCUSDT/ETHUSDT'})
        risk.trigger_kill_switch('simulated Telegram /stop')
        results['telegram_stop_web_state'] = app.get_state()['is_kill_switch_active']
        await routes['/api/kill'].endpoint()
        results['web_kill_remaining_positions'] = app.get_state()['active_positions_count']
        await routes['/api/demo/open_trade'].endpoint({'pair': 'unknown-pair'})
        results['manual_entry_while_killed'] = app.get_state()['active_positions_count']
        await routes['/api/positions/close'].endpoint({'pair': 'BTCUSDT/ETHUSDT'})
        results['manual_close_journal_entries'] = len(db.get_trades())
        marker = '<b>AUDIT_MARKER</b>'
        await routes['/api/demo/open_trade'].endpoint({'pair': marker})
        logs = await routes['/api/logs'].endpoint()
        results['unescaped_pair_html_in_log'] = any(marker in x['msg'] for x in logs['logs'])
        results['analytics_without_trades'] = await routes['/api/analytics'].endpoint()
        # A real journal record causes saved paused state to be ignored after restart.
        db.log_trade({'pair': 'A/B', 'pnl_num': 1})
        state.write_text(json.dumps({'balance': 10001, 'is_autopilot_active': False}), encoding='utf-8')
        restarted = server.create_app(bybit_client=client, risk_manager=risk, order_manager=orders)
        results['restart'] = {'paused_after_restart': restarted.get_state()['is_kill_switch_active'],
                              'positions_after_restart': restarted.get_state()['active_positions_count']}
        for _ in range(500):
            db.log_trade({'pair': 'A/B', 'pnl_num': 1})
        results['journal_501_trades'] = db.get_stats()

def worker_checks(tmp):
    class StopWorker(BaseException):
        pass
    client = SimpleNamespace(api_key='', testnet=True, market_session=SimpleNamespace(
        get_tickers=lambda **kw: {'retCode': 0, 'result': {'list': []}}))
    with patch.object(server, 'STATE_PATH', tmp / 'worker-state.json'), \
         patch.object(server, 'DatabaseManager', return_value=DatabaseManager(tmp / 'worker.db')), \
         patch.object(server.threading.Thread, 'start', autospec=True) as start:
        app = server.create_app(bybit_client=client)
        worker = start.call_args.args[0]._target
        with patch('urllib.request.urlopen', side_effect=OSError('offline audit')), \
             patch.object(server.time, 'sleep', side_effect=StopWorker), \
             patch.object(server.logger, 'error') as errors:
            try:
                worker()
            except StopWorker:
                pass
            results['entries_without_market_data'] = app.get_state()['active_positions_count']
            registry = inspect.getclosurevars(worker).nonlocals['active_pair_registry']
            registry['XAUUSDT/XAGUSDT']['mean_ratio'] = 0
            try:
                worker()
            except StopWorker:
                pass
            results['worker_exit_errors'] = [str(c.args[0]) for c in errors.call_args_list]

with tempfile.TemporaryDirectory(prefix='tbot-audit-') as tmp:
    asyncio.run(web_checks(Path(tmp)))
    worker_checks(Path(tmp))
    gc.collect()  # sqlite3 connection context managers commit but do not close.

results['metrics_with_unclosed_loss'] = calculate_backtest_metrics([], pd.Series([10000., 9000.]), 10000.)
dates = pd.date_range('2026-01-01', periods=200, freq='h')
b = 100 + np.sin(np.linspace(0, 10, 200)) * 5
a = b.copy()
a[100:105] += 15
engine = PairBacktestEngine(z_window=30, entry_z=1.5, stop_z=5, leverage=1)
bt = engine.run('A', 'B', pd.DataFrame({'timestamp': dates, 'close': a}), pd.DataFrame({'timestamp': dates, 'close': b}))
results['existing_synthetic_test'] = {'first_trade': str(bt['trades'][0]['entry_time']),
                                     'injected_deviation': str(dates[100]),
                                     'first_simulated_bar': str(bt['equity_curve'].index[0])}

# Independent random walks: compare adaptive residual ADF against Engle-Granger.
rng = np.random.default_rng(42)
a = 100 + np.cumsum(rng.normal(size=400))
b = 100 + np.cumsum(rng.normal(size=400))
mock = Mock()
mock.get_historical_klines.side_effect = [pd.DataFrame({'timestamp': pd.date_range('2026-01-01', periods=400, freq='h'), 'close': x}) for x in (a, b)]
with patch('time.sleep'):
    screen = PairScreener(mock).analyze_pair('A', 'B')
results['independent_walks'] = {'screener_cointegrated': screen['is_cointegrated'], 'screener_p': screen['p_value'],
                                'engle_granger_p': float(coint(a, b)[1])}
output = json.dumps(results, indent=2, ensure_ascii=False, default=str)
Path(__file__).with_name('reproductions.json').write_text(output, encoding='utf-8')
print(output)
