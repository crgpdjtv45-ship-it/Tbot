import asyncio
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from src.runtime import PaperEngine, PaperSettings, StateStore, fit_model
from src.webapp.server import create_app
from src.api.bybit_client import BybitClient
from src.execution.order_manager import OrderManager
from src.storage.db_manager import DatabaseManager
from src.backtesting.metrics import calculate_backtest_metrics

PAIR = 'BTCUSDT/ETHUSDT'


@pytest.fixture
def engine(tmp_path):
    now = [1_800_000_000.]
    engine = PaperEngine(StateStore(tmp_path / 'test.db'), clock=lambda: now[0])
    engine.test_time = now
    engine.models[PAIR] = {'alpha': 0., 'beta': 2., 'mean': 0., 'std': 1.,
        'updated_at': now[0], 'is_cointegrated': True, 'p_value': .01, 'labels': [], 'z_score': []}
    engine.quotes = quotes(now[0], 102.5, 50.)
    yield engine
    engine.shutdown()


def quotes(now, pa=102.5, pb=50., **kw):
    return {'BTCUSDT': {'price': pa, 'timestamp': now, **kw}, 'ETHUSDT': {'price': pb, 'timestamp': now, **kw}}


def test_paper_hedge_fee_conservation_and_cooldown(engine):
    pos = engine.open(PAIR, -1)
    assert pos['qty_b'] == pytest.approx(2 * pos['qty_a'])
    engine.test_time[0] += 5
    engine.tick(quotes(engine.clock(), 100., 50.))
    assert not engine.state['positions']
    trade = engine.state['trades'][0]
    assert trade['pnl_num'] > 0
    assert engine.state['balance'] == pytest.approx(engine.settings.initial_capital + trade['pnl_num'])
    with pytest.raises(ValueError, match='cooldown'):
        engine.open(PAIR, -1)


def test_restart_restores_open_position_pause_and_ledger(engine):
    pos = engine.open(PAIR, -1)
    engine.stop(liquidate=False)
    path = engine.store.path
    engine.shutdown()
    restored = PaperEngine(StateStore(path), clock=engine.clock)
    try:
        assert restored.state['positions'][PAIR]['id'] == pos['id']
        assert not restored.state['autopilot']
        assert restored.state['balance'] == pytest.approx(engine.state['balance'])
        assert not restored.snapshot()['ready']
        with pytest.raises(ValueError):
            restored.open(PAIR, -1)
    finally:
        restored.shutdown()
    # fixture shutdown is deliberately idempotent


def test_emergency_stop_waits_for_real_fresh_prices_and_never_reopens(engine):
    engine.open(PAIR, -1)
    engine.test_time[0] += 60
    result = engine.stop()
    assert result == {'closed': [], 'pending': [PAIR]}
    assert engine.state['positions'][PAIR]['close_requested']
    with pytest.raises(ValueError):
        engine.resume()
    engine.tick(quotes(engine.clock()))
    assert not engine.state['positions']
    assert len(engine.state['trades']) == 1
    assert not engine.state['autopilot']
    engine.tick(quotes(engine.clock()))
    assert len(engine.state['trades']) == 1


def test_no_stale_missing_unknown_or_outside_stop_entry(engine):
    with pytest.raises(ValueError):
        engine.open('<img src=x>', -1)
    engine.quotes = {}
    with pytest.raises(ValueError, match='Fresh'):
        engine.open(PAIR, -1)
    engine.tick(quotes(engine.clock(), 105., 50.))
    assert not engine.state['positions']
    with pytest.raises(ValueError, match='statistical'):
        engine.open(PAIR, -1)


def test_no_second_owner(engine):
    with pytest.raises(RuntimeError, match='already owns'):
        StateStore(engine.store.path)


def test_account_drawdown_stops_all_and_persists_pause(engine):
    engine.open(PAIR, -1)
    engine.state['balance'] -= 1500
    engine.tick(quotes(engine.clock()))
    assert not engine.state['autopilot']
    assert not engine.state['positions']
    assert engine.store.load()['autopilot'] is False


def test_missing_leg_does_not_close_at_entry_or_create_profit(engine):
    engine.open(PAIR, -1)
    initial = copy.deepcopy(engine.state['positions'][PAIR])
    engine.tick({'BTCUSDT': {'price': 80., 'timestamp': engine.clock()}})
    assert engine.state['positions'][PAIR]['unrealized_pnl'] == initial['unrealized_pnl']
    result = engine.stop()
    assert result['pending'] == [PAIR]
    assert not engine.state['trades']


def test_supported_settings_are_applied_and_models_invalidated(engine):
    engine.update_settings({'entry_z_score': 2.5, 'max_open_pairs': 1})
    assert engine.settings.entry_z_score == 2.5
    assert engine.store.load()['settings']['max_open_pairs'] == 1
    assert not engine.models


def test_stale_model_disables_readiness_and_entries(engine):
    engine.test_time[0] += 1000
    engine.tick(quotes(engine.clock()))
    assert not engine.snapshot()['ready']
    assert not engine.state['positions']


def test_telegram_stop_uses_same_runtime_and_ignores_other_chats(engine):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from aiogram import Dispatcher
    from src.bot.tg_reporter import TelegramReporter
    reporter = TelegramReporter()
    reporter.dp = Dispatcher()
    reporter.runtime = engine
    reporter.chat_id = '42'
    reporter._register_handlers()
    callback = reporter.dp.message.handlers[0].callback
    engine.open(PAIR, -1)
    async def run():
        outsider = SimpleNamespace(chat=SimpleNamespace(id=1), text='/stop', answer=AsyncMock())
        await callback(outsider)
        assert engine.state['autopilot']
        assert engine.state['positions']
        owner = SimpleNamespace(chat=SimpleNamespace(id=42), text='/stop', answer=AsyncMock())
        await callback(owner)
        assert not engine.state['autopilot']
        assert not engine.state['positions']
        owner.answer.assert_awaited_once()
    asyncio.run(run())


def test_application_lifespan_restores_and_releases_journal(tmp_path):
    async def run():
        app = create_app(data_dir=tmp_path, token='t' * 40, start_workers=False)
        async with app.router.lifespan_context(app):
            app.state.engine.stop(liquidate=False)
            assert app.get_state()['is_kill_switch_active']
        second = create_app(data_dir=tmp_path, token='t' * 40, start_workers=False)
        async with second.router.lifespan_context(second):
            assert second.get_state()['is_kill_switch_active']
    asyncio.run(run())


def test_public_market_uses_validated_receipt_time_for_clock_skew(monkeypatch):
    from src.runtime import PublicMarket
    market = PublicMarket()
    monkeypatch.setattr(market, 'get', lambda *args: {'time': 100_014_000, '_received_at': 100_000.,
        'result': {'list': [{'symbol': 'BTCUSDT', 'lastPrice': '100', 'bid1Price': '99', 'ask1Price': '101'}]}})
    assert market.tickers()['BTCUSDT']['timestamp'] == 100_000.


def test_concurrent_closes_are_exactly_once(engine):
    engine.open(PAIR, -1)
    def close():
        try:
            return engine.close(PAIR)
        except ValueError:
            return None
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: close(), range(8)))
    assert len(engine.state['trades']) == 1
    assert engine.state['balance'] == pytest.approx(engine.settings.initial_capital + engine.state['trades'][0]['pnl_num'])


def test_funding_is_applied_once_and_in_ledger(engine):
    engine.quotes = quotes(engine.clock(), funding_rate=.001, next_funding=engine.clock() + 10)
    engine.open(PAIR, -1)
    engine.stop(liquidate=False)
    engine.test_time[0] += 11
    engine.tick(quotes(engine.clock(), funding_rate=.001, next_funding=engine.clock() + 100))
    funding = engine.state['positions'][PAIR]['funding_pnl']
    assert funding != 0
    engine.tick(quotes(engine.clock(), funding_rate=.001, next_funding=engine.clock() + 100))
    assert engine.state['positions'][PAIR]['funding_pnl'] == funding
    engine.close(PAIR)
    assert engine.state['balance'] == pytest.approx(engine.settings.initial_capital + engine.state['trades'][0]['pnl_num'])


def test_storage_failure_pauses_and_rejects_new_entries(engine, monkeypatch):
    def fail(state):
        raise OSError('simulated disk failure')
    monkeypatch.setattr(engine.store, 'save', fail)
    with pytest.raises(OSError):
        engine.tick(quotes(engine.clock(), 100., 50.))
    assert not engine.state['autopilot']
    assert engine.storage_error
    assert not engine.snapshot()['ready']
    with pytest.raises(ValueError, match='paused'):
        engine.open(PAIR, -1)
    monkeypatch.undo()


@pytest.mark.parametrize('changes', [dict(max_open_pairs=-1), dict(mode='live'), dict(entry_z_score=5.),
    dict(max_open_pairs=True), dict(initial_capital=float('nan')), dict(timeframe='D')])
def test_invalid_configuration(changes):
    with pytest.raises(ValidationError):
        PaperSettings(**changes)


def test_fit_model_rejects_independent_walks_and_gaps():
    now = 1_800_000_000.
    rng = np.random.default_rng(42)
    timestamps = now - np.arange(400, 0, -1) * 3600
    a = pd.DataFrame({'timestamp': timestamps, 'close': 100 + rng.normal(size=400).cumsum()})
    b = pd.DataFrame({'timestamp': timestamps, 'close': 100 + rng.normal(size=400).cumsum()})
    model = fit_model(a, b, PaperSettings(), now)
    assert not model['is_cointegrated']
    with pytest.raises(ValueError, match='gapped'):
        fit_model(a.drop(index=300), b, PaperSettings(), now)


def test_legacy_order_rejection_and_failed_unwind_are_retained():
    client = Mock()
    client.place_market_order.side_effect = ['A', 'B', None, 'C', 'D']
    om = OrderManager(client)
    om.open_pair_position('A', 'B', 1, 1, 1, -2, 1)
    assert om.close_pair_position('A/B') is None
    assert om.active_positions['A/B']['qty_a'] == 1
    assert om.active_positions['A/B']['qty_b'] == 0
    assert om.close_pair_position('A/B')
    assert not om.active_positions
    client.place_market_order.side_effect = ['A', None, None]
    om.open_pair_position('A', 'B', 1, 1, 1, -2, 1)
    assert om.active_positions['A/B']['status'] == 'UNWIND_REQUIRED'


def test_exchange_submissions_fail_closed():
    client = BybitClient.__new__(BybitClient)
    client.api_key = 'fake-key'
    with pytest.raises(RuntimeError, match='disabled'):
        client.place_market_order('BTCUSDT', 'Buy', 1)


def test_journal_full_history_and_connection_close(tmp_path):
    db = DatabaseManager(tmp_path / 'legacy.db')
    for _ in range(501):
        db.log_trade({'pair': 'A/B', 'pnl_num': 1})
    assert db.get_stats()['total_realized_pnl'] == 501
    assert db.get_stats()['total_trades'] == 501
    # Windows permits this only once all SQLite handles are closed.
    db.db_path.rename(tmp_path / 'moved.db')


def test_equity_metrics_without_closed_trades():
    result = calculate_backtest_metrics([], pd.Series([10000., 9000.]), 10000.)
    assert result['final_equity'] == 9000
    assert result['max_drawdown_pct'] == 10
    assert result['winning_trades'] == 0


async def request(app, path, method='GET', payload=None, token=None, cookie=None, origin=None):
    headers = [(b'host', b'test'), (b'content-type', b'application/json')]
    if token:
        headers.append((b'authorization', ('Bearer ' + token).encode()))
    if cookie:
        headers.append((b'cookie', cookie.encode()))
    if origin:
        headers.append((b'origin', origin.encode()))
    body = json.dumps(payload or {}).encode()
    headers.append((b'content-length', str(len(body)).encode()))
    output = []
    async def receive():
        return {'type': 'http.request', 'body': body, 'more_body': False}
    async def send(message):
        output.append(message)
    await app({'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1', 'method': method,
               'scheme': 'http', 'path': path, 'raw_path': path.encode(), 'query_string': b'', 'root_path': '',
               'headers': headers, 'client': ('127.0.0.1', 1234), 'server': ('test', 80)}, receive, send)
    start = next(x for x in output if x['type'] == 'http.response.start')
    body = b''.join(x.get('body', b'') for x in output if x['type'] == 'http.response.body')
    return start['status'], body, dict(start['headers'])


def test_http_security_validation_and_shared_stop(engine, tmp_path):
    async def run():
        token = 't' * 40
        app = create_app(engine=engine, token=token, data_dir=tmp_path, start_workers=False)
        assert (await request(app, '/api/status'))[0] == 401
        assert (await request(app, '/api/settings', 'POST', {'max_open_pairs': 1}))[0] == 401
        assert (await request(app, '/api/stop', 'POST', token=token, origin='https://attacker.example'))[0] == 403
        assert (await request(app, '/api/status', token=token))[0] == 200
        assert (await request(app, '/api/demo/open_trade', 'POST', {'pair': '<b>bad</b>'}, token))[0] == 400
        assert (await request(app, '/api/demo/open_trade', 'POST', {'pair': PAIR, 'side': True}, token))[0] == 422
        assert (await request(app, '/api/settings', 'POST', {'max_open_pairs': -1}, token))[0] == 400
        status, _, headers = await request(app, '/session', 'POST', token=token)
        assert status == 200
        cookie = headers[b'set-cookie'].decode().split(';')[0]
        assert 'HttpOnly' in headers[b'set-cookie'].decode()
        assert (await request(app, '/api/status', cookie=cookie))[0] == 200
        engine.open(PAIR, -1)
        assert (await request(app, '/api/stop', 'POST', token=token))[0] == 200
        assert not engine.state['positions']
        assert not engine.state['autopilot']
        assert len(engine.state['trades']) == 1
    asyncio.run(run())
