import pytest
from sol_pump_monitor.collector import CollectorConfig, PumpState, Sample, StateEvaluator


@pytest.fixture
def evaluator():
    cfg = CollectorConfig(
        nominal_min_watts=450.0,
        nominal_max_watts=1100.0,
        dry_run_watts=280.0,
        min_bus_voltage=44.0,
        dry_run_grace_seconds=4.0,
        max_cycle_seconds=60.0,
        inrush_ignore_seconds=2.0,
    )
    return StateEvaluator(cfg)


def test_idle_when_no_current(evaluator):
    sample = Sample(timestamp=100.0, bus_voltage=52.4, current_amps=0.1, power_watts=5.2, relay_closed=False)
    state, alert = evaluator.evaluate(sample)
    assert state == PumpState.IDLE
    assert alert is None


def test_normal_pumping_run(evaluator):
    s1 = Sample(timestamp=100.0, bus_voltage=51.0, current_amps=18.0, power_watts=918.0, relay_closed=True)
    state1, alert1 = evaluator.evaluate(s1)
    assert state1 == PumpState.RUNNING
    assert alert1 is None

    s2 = Sample(timestamp=110.0, bus_voltage=50.2, current_amps=14.5, power_watts=728.0, relay_closed=True)
    state2, alert2 = evaluator.evaluate(s2)
    assert state2 == PumpState.RUNNING
    assert alert2 is None


def test_dry_run_triggers_after_grace_period(evaluator):
    s0 = Sample(timestamp=100.0, bus_voltage=51.5, current_amps=14.0, power_watts=720.0, relay_closed=True)
    evaluator.evaluate(s0)

    s1 = Sample(timestamp=102.0, bus_voltage=52.8, current_amps=3.5, power_watts=185.0, relay_closed=True)
    st1, al1 = evaluator.evaluate(s1)
    assert st1 == PumpState.UNLOADED_SUSPECT
    assert al1 is None

    s2 = Sample(timestamp=107.0, bus_voltage=53.0, current_amps=3.4, power_watts=180.0, relay_closed=True)
    st2, al2 = evaluator.evaluate(s2)
    assert st2 == PumpState.DRY_RUN
    assert al2 is not None
    assert "dry run" in al2.lower()


def test_low_voltage_fault_takes_precedence(evaluator):
    # battery sagged below cutoff limit
    s = Sample(timestamp=100.0, bus_voltage=41.2, current_amps=12.0, power_watts=494.0, relay_closed=True)
    st, alert = evaluator.evaluate(s)
    assert st == PumpState.LOW_VOLTAGE
    assert alert is not None
    assert "low voltage" in alert.lower()


def test_stuck_relay_detected_when_commanded_off(evaluator):
    # controller commanded relay OFF, but power is still flowing
    s = Sample(timestamp=100.0, bus_voltage=50.0, current_amps=14.0, power_watts=700.0, relay_closed=False)
    st, alert = evaluator.evaluate(s)
    assert st == PumpState.STUCK_RELAY
    assert alert is not None
    assert "stuck relay" in alert.lower()


def test_max_cycle_time_exceeded(evaluator):
    t = 200.0
    evaluator.evaluate(Sample(timestamp=t, bus_voltage=50.0, current_amps=14.0, power_watts=700.0, relay_closed=True))

    # 65s later without stopping (configured limit is 60s)
    st, alert = evaluator.evaluate(
        Sample(timestamp=t + 65.0, bus_voltage=49.5, current_amps=14.0, power_watts=693.0, relay_closed=True)
    )
    assert st == PumpState.RUNNING
    assert alert is not None
    assert "runtime exceeded" in alert.lower()
