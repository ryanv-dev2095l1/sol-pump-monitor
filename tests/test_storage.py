import sqlite3
import tempfile
from pathlib import Path
import pytest

from sol_pump_monitor.storage import Storage


@pytest.fixture
def tmp_db(tmp_path):
    db_path = tmp_path / "test_pump.db"
    storage = Storage(str(db_path))
    storage.init_db()
    return storage


def test_init_creates_tables(tmp_db):
    with sqlite3.connect(tmp_db.db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = {row[0] for row in cursor.fetchall()}
    
    assert "readings" in tables
    assert "pump_cycles" in tables
    assert "alerts" in tables


def test_record_and_fetch_reading(tmp_db):
    tmp_db.save_reading(ts=1710000000, bus_voltage=49.2, current=8.5, power=418.2)
    tmp_db.save_reading(ts=1710000005, bus_voltage=48.9, current=8.7, power=425.4)
    
    latest = tmp_db.get_latest_reading()
    assert latest is not None
    assert latest["ts"] == 1710000005
    assert pytest.approx(latest["bus_voltage"]) == 48.9
    assert pytest.approx(latest["power"]) == 425.4


def test_record_pump_cycle(tmp_db):
    cycle_id = tmp_db.record_cycle(
        start_ts=1710000100,
        end_ts=1710000400,
        duration_sec=300,
        avg_power=420.0,
        peak_power=480.0,
        min_voltage=47.5,
        energy_wh=35.0,
        was_dry_run=False,
    )
    assert cycle_id > 0

    cycles = tmp_db.get_recent_cycles(limit=10)
    assert len(cycles) == 1
    c = cycles[0]
    assert c["duration_sec"] == 300
    assert c["was_dry_run"] == 0
    assert pytest.approx(c["energy_wh"]) == 35.0


def test_daily_summary_aggregation(tmp_db):
    # 3 normal cycles and 1 dry run in the same 24h window
    base_ts = 1710050000
    tmp_db.record_cycle(base_ts, base_ts + 600, 600, 430.0, 490.0, 48.0, 71.6, False)
    tmp_db.record_cycle(base_ts + 1000, base_ts + 1600, 600, 425.0, 485.0, 47.9, 70.8, False)
    tmp_db.record_cycle(base_ts + 2000, base_ts + 2045, 45, 180.0, 210.0, 49.5, 2.2, True)

    summary = tmp_db.get_daily_summary(since_ts=base_ts - 10)
    assert summary["total_cycles"] == 3
    assert summary["dry_runs"] == 1
    assert summary["total_runtime_sec"] == 1245
    assert pytest.approx(summary["total_energy_wh"], 0.1) == 144.6


def test_prune_old_raw_readings(tmp_db):
    # readings older than cutoff should get dropped, cycles stay untouched
    now = 1710090000
    tmp_db.save_reading(ts=now - 100000, bus_voltage=48.0, current=0.0, power=0.0)
    tmp_db.save_reading(ts=now - 500, bus_voltage=48.2, current=5.0, power=241.0)
    tmp_db.record_cycle(now - 100000, now - 99000, 1000, 400.0, 450.0, 47.0, 110.0, False)

    deleted = tmp_db.prune_readings(older_than_ts=now - 10000)
    assert deleted == 1

    # reading from 500s ago still exists
    assert tmp_db.get_latest_reading()["ts"] == now - 500
    # old cycle not affected
    assert len(tmp_db.get_recent_cycles()) == 1


def test_empty_database_returns_safe_defaults(tmp_db):
    assert tmp_db.get_latest_reading() is None
    assert tmp_db.get_recent_cycles() == []
    summary = tmp_db.get_daily_summary(since_ts=0)
    assert summary["total_cycles"] == 0
    assert summary["total_energy_wh"] == 0.0
