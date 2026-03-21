import httpx
import logging

log = logging.getLogger("telegram")

class TelegramBot:
    def __init__(self, token: str, chat_id: str):
        self.token   = token
        self.chat_id = chat_id
        self.client  = httpx.AsyncClient(timeout=10.0)

    async def send(self, text: str):
        if not self.token or not self.chat_id:
            return
        try:
            r = await self.client.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"}
            )
            log.debug(f"[TG] Sent OK ({r.status_code})")
        except Exception as e:
            log.warning(f"[TG] Send failed: {e}")

    async def close(self):
        await self.client.aclose()
