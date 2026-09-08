# sol-pump-monitor

Daemon I wrote to keep tabs on my solar-powered well pump. It polls the DC shunt and relay monitors (Shelly Pro + Victron Venus MQTT/HTTP bridge), tracks pump cycle durations, detects dry-run cavitation from current drop, and pushes alerts if the pump runs too long or voltage sags dangerously.

Stores cycle logs locally in SQLite and exports a tiny `/metrics` endpoint for Prometheus.

## Requirements

- Python 3.10+
- A local HTTP endpoint or Shelly relay exposing power telemetry

## Setup

```bash
git clone https://github.com/username/sol-pump-monitor.git
cd sol-pump-monitor
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

Create a config file or set environment variables:

```bash
export PUMP_ENDPOINT="http://192.168.1.140/rpc/Shelly.GetStatus"
export PUMP_DB_PATH="/var/lib/pumpmon/pump.db"
export PUMP_TELEGRAM_TOKEN="123456:ABC-DEF..."
export PUMP_TELEGRAM_CHAT_ID="-10012345678"
```

## Run

```bash
sol-pump-monitor run --interval 5
```

Or test a single poll cycle:

```bash
sol-pump-monitor check
```

## Systemd

A unit file template is in `contrib/sol-pump-monitor.service` if you want to run it on a Pi or gateway.

<!-- last-sync: 2026-09-08 -->
