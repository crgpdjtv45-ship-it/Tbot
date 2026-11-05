"""Persistent paper checkpoints and exclusive worker ownership across Render deploys."""
import json
import threading

import psycopg


class PostgresStateStore:
    backend = 'postgresql'
    LOCK_ID = 0x54424F5450415045

    def __init__(self, url):
        self.connection = psycopg.connect(url, autocommit=True, connect_timeout=5,
                                           options='-c statement_timeout=5000')
        self.lock = threading.RLock()
        self.owned = False
        self.connection.execute('CREATE TABLE IF NOT EXISTS paper_checkpoint (id INTEGER PRIMARY KEY CHECK(id=1), body JSONB NOT NULL)')

    @property
    def writable(self):
        return self.owned and not self.connection.closed

    def acquire(self):
        with self.lock:
            if not self.owned:
                self.owned = bool(self.connection.execute('SELECT pg_try_advisory_lock(%s)', (self.LOCK_ID,)).fetchone()[0])
            return self.writable

    def load(self):
        with self.lock:
            row = self.connection.execute('SELECT body FROM paper_checkpoint WHERE id=1').fetchone()
            return row[0] if row else None

    def save(self, state):
        with self.lock:
            if not self.writable:
                raise RuntimeError('This instance does not own the paper journal')
            body = json.dumps(state, ensure_ascii=False, allow_nan=False)
            with self.connection.transaction():
                self.connection.execute('INSERT INTO paper_checkpoint VALUES(1,%s::jsonb) ON CONFLICT(id) DO UPDATE SET body=excluded.body', (body,))

    def close(self):
        with self.lock:
            self.owned = False
            self.connection.close()
