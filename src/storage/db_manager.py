import sqlite3
import time
from pathlib import Path
from typing import Dict, Any, List, Optional
from loguru import logger

DB_PATH = Path("configs/trading_journal.db")

class DatabaseManager:
    """
    ACID Transactional SQLite Journal for persistent StatArb records.
    Ensures zero trade loss across server restarts and container rollouts.
    """
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        # Enable Write-Ahead Logging for high concurrency and resilience
        conn.execute("PRAGMA journal_mode=WAL;")
        return conn

    def _init_db(self):
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS closed_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pair TEXT NOT NULL,
                    sector TEXT,
                    side TEXT NOT NULL,
                    entry_z REAL,
                    exit_z REAL,
                    pnl_usd REAL NOT NULL,
                    pnl_pct REAL NOT NULL,
                    duration TEXT,
                    exit_reason TEXT,
                    date TEXT,
                    timestamp REAL NOT NULL
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS equity_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    balance REAL NOT NULL,
                    unrealized_pnl REAL NOT NULL,
                    active_positions INTEGER NOT NULL
                );
            """)
            conn.commit()
            logger.info(f"SQLite Trading Journal initialized at {self.db_path}")

    def log_trade(self, trade: Dict[str, Any]) -> int:
        """Saves closed trade record into database."""
        pnl_val = trade.get("pnl_num", 0.0)
        if pnl_val == 0.0:
            txt = str(trade.get("profit_usd", "$0")).replace("$", "").replace("+", "").strip()
            try:
                pnl_val = float(txt)
            except Exception:
                pnl_val = 0.0

        pct_val = trade.get("pnl_pct_num", 0.0)
        if pct_val == 0.0:
            txt_pct = str(trade.get("profit_pct", "0%")).replace("%", "").replace("+", "").strip()
            try:
                pct_val = float(txt_pct)
            except Exception:
                pct_val = 0.0

        with self._get_connection() as conn:
            cur = conn.execute("""
                INSERT INTO closed_trades (
                    pair, sector, side, entry_z, exit_z, pnl_usd, pnl_pct, duration, exit_reason, date, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trade.get("pair", ""),
                trade.get("sector", "StatArb"),
                trade.get("type", "SPREAD"),
                float(trade.get("entry_z", 0.0)),
                float(trade.get("exit_z", 0.0)),
                pnl_val,
                pct_val,
                trade.get("duration", "0м"),
                trade.get("exit_reason", "Нормализация спреда"),
                trade.get("date", time.strftime("%H:%M")),
                time.time()
            ))
            conn.commit()
            return cur.lastrowid

    def get_trades(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Retrieves latest closed trades in reverse chronological order."""
        with self._get_connection() as conn:
            rows = conn.execute("""
                SELECT * FROM closed_trades ORDER BY id DESC LIMIT ?
            """, (limit,)).fetchall()
            
            result = []
            for r in rows:
                pnl_usd = r["pnl_usd"]
                pnl_pct = r["pnl_pct"]
                result.append({
                    "id": r["id"],
                    "pair": r["pair"],
                    "sector": r["sector"],
                    "type": r["side"],
                    "entry_z": r["entry_z"],
                    "exit_z": r["exit_z"],
                    "profit_usd": f"{'+' if pnl_usd >= 0 else ''}${pnl_usd:.2f}",
                    "profit_pct": f"{'+' if pnl_pct >= 0 else ''}{pnl_pct:.2f}%",
                    "pnl_num": pnl_usd,
                    "duration": r["duration"],
                    "exit_reason": r["exit_reason"],
                    "date": r["date"]
                })
            return result

    def get_stats(self) -> Dict[str, Any]:
        """Calculates exact portfolio statistics from trade history."""
        trades = self.get_trades(limit=500)
        total = len(trades)
        if total == 0:
            return {
                "total_trades": 0,
                "win_rate": 0.0,
                "profit_factor": 0.0,
                "total_realized_pnl": 0.0,
                "winning_trades": 0,
                "losing_trades": 0
            }

        profits = [t["pnl_num"] for t in trades if t["pnl_num"] > 0]
        losses = [abs(t["pnl_num"]) for t in trades if t["pnl_num"] < 0]

        win_cnt = len(profits)
        loss_cnt = len(losses)
        win_rate = round((win_cnt / total) * 100.0, 1)
        sum_profit = sum(profits)
        sum_loss = sum(losses)
        pf = round(sum_profit / sum_loss, 2) if sum_loss > 0 else (round(sum_profit, 2) if sum_profit > 0 else 1.0)
        net_pnl = round(sum_profit - sum_loss, 2)

        return {
            "total_trades": total,
            "win_rate": win_rate,
            "profit_factor": pf,
            "total_realized_pnl": net_pnl,
            "winning_trades": win_cnt,
            "losing_trades": loss_cnt
        }

    def log_equity_snapshot(self, balance: float, unrealized_pnl: float, active_positions: int):
        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO equity_history (timestamp, balance, unrealized_pnl, active_positions)
                VALUES (?, ?, ?, ?)
            """, (time.time(), balance, unrealized_pnl, active_positions))
            conn.commit()
