import time
import logging
from dataclasses import dataclass
from typing import Optional, Dict, Any
import httpx

logger = logging.getLogger("sol_pump_monitor.collector")


@dataclass
class TelemetrySample:
    timestamp: float
    power_w: float
    voltage_v: Optional[float]
    relay_on: bool
    temp_c: Optional[float] = None
    dc_bus_v: Optional[float] = None


@dataclass
class CycleEvent:
    event_type: str  # "started", "stopped", "dry_run", "stuck_relay"
    start_time: float
    duration_s: float
    avg_power_w: float
    peak_power_w: float
    bus_v_start: Optional[float] = None
    bus_v_end: Optional[float] = None


class PumpTracker:
    """Tracks running state, cycle durations, and detects dry-run under-current."""

    def __init__(
        self,
        running_min_w: float = 200.0,
        dry_run_max_w: float = 450.0,
        max_run_s: float = 3600.0,
        startup_grace_s: float = 12.0,
    ):
        self.running_min_w = running_min_w
        self.dry_run_max_w = dry_run_max_w
        self.max_run_s = max_run_s
        self.startup_grace_s = startup_grace_s

        self.state = "IDLE"
        self.cycle_start_ts: Optional[float] = None
        self.samples_in_cycle: list[TelemetrySample] = []
        self.dry_run_counter = 0

    def update(self, sample: TelemetrySample) -> Optional[CycleEvent]:
        now = sample.timestamp
        power = sample.power_w
        is_drawing_power = power >= self.running_min_w

        # print(f"DEBUG: state={self.state} pwr={power} relay={sample.relay_on}")

        if self.state == "IDLE":
            if sample.relay_on and is_drawing_power:
                self.state = "PUMPING"
                self.cycle_start_ts = now
                self.samples_in_cycle = [sample]
                self.dry_run_counter = 0
                return CycleEvent(
                    event_type="started",
                    start_time=now,
                    duration_s=0.0,
                    avg_power_w=power,
                    peak_power_w=power,
                    bus_v_start=sample.dc_bus_v,
                )

        elif self.state == "PUMPING":
            self.samples_in_cycle.append(sample)
            duration = now - (self.cycle_start_ts or now)

            # Allow capacitor start motor to settle before tripping dry-run logic
            if duration > self.startup_grace_s:
                # Submersible drops from ~800W down to ~350W when sucking air
                if power < self.dry_run_max_w and sample.relay_on:
                    self.dry_run_counter += 1
                    if self.dry_run_counter >= 3:  # 3 consecutive polling ticks
                        self.state = "DRY_RUN"
                        return self._finish_cycle("dry_run", sample)
                else:
                    self.dry_run_counter = 0

            # Overrun check - stuck float switch or pipe burst
            if duration > self.max_run_s:
                self.state = "STUCK_RELAY"
                return self._finish_cycle("stuck_relay", sample)

            # Normal pump shutdown
            if not sample.relay_on or not is_drawing_power:
                self.state = "IDLE"
                return self._finish_cycle("stopped", sample)

        elif self.state in ("DRY_RUN", "STUCK_RELAY"):
            # Stay locked in alert state until relay explicitly opens and power stays down
            if not sample.relay_on and not is_drawing_power:
                self.state = "IDLE"
                self.samples_in_cycle = []
                self.cycle_start_ts = None
                self.dry_run_counter = 0

        return None

    def _finish_cycle(self, event_type: str, last_sample: TelemetrySample) -> CycleEvent:
        start = self.cycle_start_ts or last_sample.timestamp
        dur = max(0.0, last_sample.timestamp - start)
        powers = [s.power_w for s in self.samples_in_cycle] or [last_sample.power_w]
        bus_start = self.samples_in_cycle[0].dc_bus_v if self.samples_in_cycle else None

        return CycleEvent(
            event_type=event_type,
            start_time=start,
            duration_s=dur,
            avg_power_w=sum(powers) / len(powers),
            peak_power_w=max(powers),
            bus_v_start=bus_start,
            bus_v_end=last_sample.dc_bus_v,
        )


class TelemetryCollector:
    def __init__(
        self,
        shelly_host: str,
        victron_host: Optional[str] = None,
        timeout_s: float = 4.0,
    ):
        self.shelly_host = shelly_host.rstrip("/")
        self.victron_host = victron_host.rstrip("/") if victron_host else None
        self.client = httpx.Client(timeout=timeout_s)
        self._is_gen2: Optional[bool] = None

    def _detect_gen(self) -> bool:
        if self._is_gen2 is not None:
            return self._is_gen2
        try:
            resp = self.client.get(f"{self.shelly_host}/shelly")
            if resp.status_code == 200:
                data = resp.json()
                self._is_gen2 = data.get("gen", 1) >= 2
            else:
                self._is_gen2 = False
        except Exception:
            self._is_gen2 = False
        return self._is_gen2

    def poll_shelly(self) -> TelemetrySample:
        is_gen2 = self._detect_gen()
        now = time.time()

        if is_gen2:
            # Shelly Plus / Pro (Gen2 RPC)
            resp = self.client.get(f"{self.shelly_host}/rpc/Switch.GetStatus?id=0")
            resp.raise_for_status()
            data = resp.json()
            relay_on = data.get("output", False)
            power = data.get("apower", 0.0)
            voltage = data.get("voltage", None)
            temp_data = data.get("temperature", {})
            temp_c = temp_data.get("tC") if isinstance(temp_data, dict) else None
        else:
            # Gen1 Shelly 1PM
            resp = self.client.get(f"{self.shelly_host}/status")
            resp.raise_for_status()
            data = resp.json()
            relays = data.get("relays", [])
            meters = data.get("meters", [])
            relay_on = relays[0].get("ison", False) if relays else False
            power = meters[0].get("power", 0.0) if meters else 0.0
            voltage = data.get("voltage", None)
            temp_c = data.get("temperature", None)

        return TelemetrySample(
            timestamp=now,
            power_w=float(power) if power is not None else 0.0,
            voltage_v=float(voltage) if voltage is not None else None,
            relay_on=bool(relay_on),
            temp_c=float(temp_c) if temp_c is not None else None,
        )

    def poll_victron_bus(self) -> Optional[float]:
        if not self.victron_host:
            return None
        try:
            # Venus OS systemcalc battery service voltage
            url = f"{self.victron_host}/system/0/Batteries"
            resp = self.client.get(url)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list) and len(data) > 0:
                    return float(data[0].get("Voltage", 0.0))
                elif isinstance(data, dict) and "value" in data:
                    return float(data["value"])
        except Exception as err:
            # Don't let victron timeouts kill the whole monitoring tick
            logger.debug("failed to read victron bus voltage: %s", err)
            return None
        return None

    def collect(self) -> TelemetrySample:
        sample = self.poll_shelly()
        sample.dc_bus_v = self.poll_victron_bus()
        return sample

    def close(self):
        self.client.close()
