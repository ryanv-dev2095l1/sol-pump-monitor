import sqlite3
import time
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple


class Storage:
    """SQLite storage backend for pump events, cycle stats, and bus voltage readings."""

    def __init__(self, db_path: str = "/var/lib/sol_pump_monitor/metrics.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = None
        self.init_db()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path), timeout=10.0)
            self._conn.row_factory = sqlite3.Row
            # quick writes without holding up collector loop
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA synchronous=NORMAL;")
        return self._conn

    def init_db(self):
        conn = self._get_conn()
        with conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pump_cycles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    start_ts REAL NOT NULL,
                    stop_ts REAL NOT NULL,
                    duration_sec REAL NOT NULL,
                    peak_current_a REAL NOT NULL,
                    avg_power_w REAL NOT NULL,
                    energy_wh REAL NOT NULL,
                    end_reason TEXT NOT NULL
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS voltage_samples (
                    ts REAL PRIMARY KEY,
                    bus_voltage REAL NOT NULL,
                    panel_voltage REAL,
                    current_a REAL NOT NULL
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS voltage_dips (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    pre_dip_v REAL NOT NULL,
                    min_v REAL NOT NULL,
                    recovery_v REAL NOT NULL,
                    duration_ms REAL NOT NULL,
                    inrush_a REAL NOT NULL
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS state_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    old_state TEXT NOT NULL,
                    new_state TEXT NOT NULL,
                    trigger_reason TEXT
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_cycles_start ON pump_cycles(start_ts);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_samples_ts ON voltage_samples(ts);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_dips_ts ON voltage_dips(ts);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON state_events(ts);")

    def record_cycle(self, start_ts: float, stop_ts: float, peak_current: float,
                     avg_power: float, energy_wh: float, reason: str = "normal") -> int:
        duration = max(0.0, stop_ts - start_ts)
        conn = self._get_conn()
        with conn:
            cur = conn.execute(
                """
                INSERT INTO pump_cycles (start_ts, stop_ts, duration_sec, peak_current_a, avg_power_w, energy_wh, end_reason)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (start_ts, stop_ts, duration, peak_current, avg_power, energy_wh, reason)
            )
            # print(f"DEBUG: cycle saved id={cur.lastrowid} dur={duration}")
            return cur.lastrowid

    def record_sample(self, ts: float, bus_v: float, panel_v: Optional[float], current_a: float):
        conn = self._get_conn()
        with conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO voltage_samples (ts, bus_voltage, panel_voltage, current_a)
                VALUES (?, ?, ?, ?)
                """,
                (ts, bus_v, panel_v, current_a)
            )

    def record_voltage_dip(self, ts: float, pre_dip_v: float, min_v: float,
                           recovery_v: float, duration_ms: float, inrush_a: float):
        conn = self._get_conn()
        with conn:
            conn.execute(
                """
                INSERT INTO voltage_dips (ts, pre_dip_v, min_v, recovery_v, duration_ms, inrush_a)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (ts, pre_dip_v, min_v, recovery_v, duration_ms, inrush_a)
            )

    def record_state_transition(self, ts: float, old_state: str, new_state: str, reason: str = ""):
        conn = self._get_conn()
        with conn:
            conn.execute(
                """
                INSERT INTO state_events (ts, old_state, new_state, trigger_reason)
                VALUES (?, ?, ?, ?)
                """,
                (ts, old_state, new_state, reason)
            )

    def get_recent_cycles(self, limit: int = 10) -> List[Dict[str, Any]]:
        conn = self._get_conn()
        cur = conn.execute(
            "SELECT * FROM pump_cycles ORDER BY start_ts DESC LIMIT ?",
            (limit,)
        )
        return [dict(row) for row in cur.fetchall()]

    def get_cycles_between(self, start_ts: float, end_ts: float) -> List[Dict[str, Any]]:
        conn = self._get_conn()
        cur = conn.execute(
            """
            SELECT * FROM pump_cycles
            WHERE start_ts >= ? AND start_ts <= ?
            ORDER BY start_ts ASC
            """,
            (start_ts, end_ts)
        )
        return [dict(row) for row in cur.fetchall()]

    def get_voltage_history(self, since_ts: float) -> List[Tuple[float, float, float]]:
        conn = self._get_conn()
        cur = conn.execute(
            """
            SELECT ts, bus_voltage, current_a
            FROM voltage_samples
            WHERE ts >= ?
            ORDER BY ts ASC
            """,
            (since_ts,)
        )
        return [(r["ts"], r["bus_voltage"], r["current_a"]) for r in cur.fetchall()]

    def prune_old_samples(self, keep_days: int = 7) -> int:
        # raw 1-second ticks eat flash memory fast on sd cards
        cutoff = time.time() - (keep_days * 86400)
        conn = self._get_conn()
        with conn:
            cur = conn.execute("DELETE FROM voltage_samples WHERE ts < ?", (cutoff,))
            # TODO: add a rollup table if raw samples exceed 100k rows on the pi zero
            return cur.rowcount

    def close(self):
        if self._conn is not None:
            self._conn.close()
            self._conn = None
