import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from sol_pump_monitor.collector import PumpCollector
from sol_pump_monitor.storage import MetricsDB
from sol_pump_monitor.notifier import TelegramNotifier

def load_config(path_str: str) -> dict:
    p = Path(path_str)
    if not p.exists():
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.warning(f"Could not parse config at {path_str}: {e}")
        return {}

def build_parser():
    p = argparse.ArgumentParser(prog="sol-pump", description="Solar well pump monitoring daemon")
    p.add_argument("-c", "--config", default="/etc/sol-pump.json", help="Path to config file")
    p.add_argument("--db", default=None, help="SQLite database path (overrides config)")
    p.add_argument("--port", default=None, help="Serial port for shunt/modbus (e.g. /dev/ttyUSB0)")
    p.add_argument("--dry-threshold-w", type=float, default=None, help="Wattage below which running pump is considered dry")
    p.add_argument("-v", "--verbose", action="store_true", help="Enable debug logs")
    
    subs = p.add_subparsers(dest="command")
    subs.add_parser("run", help="Start the daemon loop")
    subs.add_parser("status", help="Show latest readouts and pump state")
    subs.add_parser("test-alert", help="Send a test message to Telegram")
    
    dump_p = subs.add_parser("dump", help="Dump recent raw metrics to stdout")
    dump_p.add_argument("-n", "--limit", type=int, default=20, help="Number of rows to return")
    return p

async def run_daemon(cfg: dict, db_path: str, port: str | None):
    # ensure folder exists if db is outside current dir
    parent = Path(db_path).parent
    if not parent.exists():
        parent.mkdir(parents=True, exist_ok=True)

    db = MetricsDB(db_path)
    db.init_schema()
    
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN") or cfg.get("telegram_token", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID") or cfg.get("telegram_chat_id", "")
    
    if not bot_token:
        logging.warning("No Telegram bot token provided. Alerts will be logged to console only.")
        
    notifier = TelegramNotifier(token=bot_token, chat_id=chat_id)
    
    # serial device might be emulated in tests or set via env
    dev_port = port or cfg.get("serial_port", "/dev/ttyUSB0")
    dry_w = cfg.get("dry_threshold_w", 280.0)
    
    collector = PumpCollector(
        db=db,
        notifier=notifier,
        port=dev_port,
        dry_run_power_threshold=dry_w,
        poll_interval=cfg.get("poll_interval_sec", 2.0)
    )
    
    # print("DEBUG starting collector on", dev_port)
    await collector.start()

def main():
    parser = build_parser()
    args = parser.parse_args()
    
    log_lvl = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=log_lvl, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    
    cfg = load_config(args.config)
    if args.dry_threshold_w is not None:
        cfg["dry_threshold_w"] = args.dry_threshold_w
        
    db_path = args.db or cfg.get("db_path", "/var/lib/sol-pump/metrics.db")
    
    if not args.command or args.command == "run":
        try:
            asyncio.run(run_daemon(cfg, db_path, args.port))
        except KeyboardInterrupt:
            logging.info("Stopping on keyboard interrupt")
            return 0
    elif args.command == "status":
        db = MetricsDB(db_path)
        last = db.get_latest_metrics()
        if not last:
            print("No metrics recorded yet.")
            return 0
        print(f"Timestamp:   {last.get('timestamp')}")
        print(f"Bus Voltage: {last.get('bus_voltage', 0.0):.2f} V")
        print(f"Current:     {last.get('current_draw', 0.0):.2f} A")
        print(f"Power:       {last.get('power_w', 0.0):.1f} W")
        print(f"State:       {last.get('cycle_state')} (running={last.get('is_running')})")
    elif args.command == "dump":
        db = MetricsDB(db_path)
        rows = db.get_recent_history(limit=args.limit)
        for r in rows:
            print(f"{r['timestamp']} | {r['bus_voltage']:.2f}V | {r['current_draw']:.2f}A | {r['power_w']:.1f}W | {r['cycle_state']}")
    elif args.command == "test-alert":
        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN") or cfg.get("telegram_token", "")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID") or cfg.get("telegram_chat_id", "")
        if not bot_token or not chat_id:
            print("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set.", file=sys.stderr)
            return 1
        notifier = TelegramNotifier(token=bot_token, chat_id=chat_id)
        ok = asyncio.run(notifier.send("Test alert from solar pump monitor"))
        if not ok:
            print("Failed to send test alert. Check token and chat_id.", file=sys.stderr)
            return 1
        print("Test alert sent successfully.")
    return 0
