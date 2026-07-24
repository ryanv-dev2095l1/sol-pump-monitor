import json
import logging
import time
from urllib import error, parse, request

log = logging.getLogger(__name__)


class TelegramNotifier:
    """Sends alert messages to Telegram and generic webhooks with rate limiting."""

    def __init__(
        self,
        bot_token: str | None = None,
        chat_id: str | None = None,
        webhook_url: str | None = None,
        cooldown_seconds: int = 300,
    ):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.webhook_url = webhook_url
        self.cooldown_seconds = cooldown_seconds
        self._last_sent: dict[str, float] = {}

    def _should_throttle(self, key: str, now: float) -> bool:
        last = self._last_sent.get(key, 0.0)
        return (now - last) < self.cooldown_seconds

    def send(self, alert_key: str, message: str, emergency: bool = False) -> bool:
        now = time.monotonic()
        if not emergency and self._should_throttle(alert_key, now):
            log.debug("suppressing %s, cooldown active", alert_key)
            return False

        sent = False
        if self.bot_token and self.chat_id:
            sent = self._send_telegram(message)

        if self.webhook_url:
            hook_sent = self._send_webhook(alert_key, message)
            sent = sent or hook_sent

        if sent:
            self._last_sent[alert_key] = now
        return sent

    def _send_telegram(self, message: str) -> bool:
        # FIXME: support multiple chat IDs if we add secondary notify list
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

        data = json.dumps(payload).encode("utf-8")
        req = request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        # solar site internet cuts out intermittently, try once then brief backoff
        for attempt in (1, 2):
            try:
                with request.urlopen(req, timeout=8) as resp:
                    if resp.status == 200:
                        return True
            except error.HTTPError as exc:
                log.error("telegram http %s: %s", exc.code, exc.read().decode("utf-8", errors="ignore"))
                break
            except (error.URLError, TimeoutError) as exc:
                if attempt == 1:
                    time.sleep(1.5)
                    continue
                log.error("failed reaching telegram on retry: %s", exc)
        return False

    def _send_webhook(self, key: str, message: str) -> bool:
        payload = {"alert": key, "message": message, "ts": int(time.time())}
        data = json.dumps(payload).encode("utf-8")
        req = request.Request(
            self.webhook_url,
            data=data,
            headers={"Content-Type": "application/json", "User-Agent": "sol-pump-monitor"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=5) as resp:
                return resp.status in (200, 201, 202, 204)
        except Exception as exc:
            log.warning("webhook dispatch failed: %s", exc)
            return False
