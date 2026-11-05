"""Opt-in integration check against a fresh, empty test journal."""
import os
import time

import pytest

from src.runtime import PaperEngine
from src.storage.postgres_state import PostgresStateStore


@pytest.mark.skipif(not os.environ.get('TBOT_TEST_DATABASE_URL'), reason='No disposable PostgreSQL configured')
def test_postgres_checkpoint_and_deploy_handoff():
    url = os.environ['TBOT_TEST_DATABASE_URL']
    first, second = PostgresStateStore(url), None
    assert first.acquire()
    assert first.load() is None, 'Integration check refuses a nonempty journal'
    engine1, engine2 = None, None
    run_id = None
    now = time.time()
    pair = 'BTCUSDT/ETHUSDT'
    ticks = {'BTCUSDT': {'price': 102.5, 'timestamp': now}, 'ETHUSDT': {'price': 50., 'timestamp': now}}

    class TestMarket:
        def tickers(self):
            return ticks

        def candles(self, *args):
            raise ValueError('No models required for handoff test')

    try:
        engine1 = PaperEngine(first, clock=lambda: now)
        run_id = engine1.state['run_id']
        second = PostgresStateStore(url)
        assert not second.acquire()
        engine2 = PaperEngine(second, clock=lambda: now, market=TestMarket())
        with pytest.raises(ValueError, match='ownership'):
            engine2.resume()
        engine1.models[pair] = {'alpha': 0., 'beta': 2., 'mean': 0., 'std': 1., 'updated_at': now,
                                'is_cointegrated': True, 'p_value': .01, 'labels': [], 'z_score': []}
        engine1.quotes = ticks
        engine1.resume()
        opened = engine1.open(pair, -1)
        engine1.stop(liquidate=False)
        # Candidate starts while previous instance owns the journal: read-only until handoff.
        engine2.start()
        assert not second.writable
        engine1.shutdown()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with engine2.lock:
                if second.writable and pair in engine2.state['positions']:
                    break
            time.sleep(.05)
        with engine2.lock:
            assert second.writable
            assert not engine2.state['autopilot']
            assert engine2.state['positions'][pair]['id'] == opened['id']
            assert engine2.state['run_id'] == run_id
        engine2.shutdown()
    finally:
        if engine1:
            engine1.shutdown()
        else:
            first.close()
        if engine2:
            engine2.shutdown()
        elif second:
            second.close()
        # Remove only this test's checkpoint; the cloud runtime has not been deployed yet.
        if run_id:
            cleanup = PostgresStateStore(url)
            try:
                assert cleanup.acquire()
                cleanup.connection.execute("DELETE FROM paper_checkpoint WHERE body->>'run_id' = %s", (run_id,))
            finally:
                cleanup.close()
