"""Vercel entry point: Telegram sends each update to POST /api/telegram.

The note is processed inside the request (a run takes ~20-60 s; the function allows 300 s, see
vercel.json), then Telegram gets its 200. Protections:
  * the X-Telegram-Bot-Api-Secret-Token header must match TELEGRAM_WEBHOOK_SECRET
  * only TELEGRAM_CHAT_ID (the owner's private chat) is served; everyone else is ignored
  * an update Telegram re-sends (after a slow reply) is skipped if this instance already has it
"""

import hmac
from collections import OrderedDict
from threading import Lock

from fastapi import FastAPI, Header, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from config import load_settings
from pipeline import LinkedInPipeline
from telegram_bot import Telegram, handle_update, private_chat_id

settings = load_settings()
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

_pipeline: LinkedInPipeline | None = None
_seen: OrderedDict[int, None] = OrderedDict()  # recent update ids, per warm instance
_seen_lock = Lock()


def _first_time(update_id: int) -> bool:
    with _seen_lock:
        if update_id in _seen:
            return False
        _seen[update_id] = None
        while len(_seen) > 500:
            _seen.popitem(last=False)
        return True


@app.get("/")
def health() -> dict:
    missing = [name for name, value in [
        ("GEMINI_API_KEY", settings.gemini_api_key),
        ("TELEGRAM_BOT_TOKEN", settings.telegram_bot_token),
        ("TELEGRAM_CHAT_ID", settings.telegram_chat_id),
        ("TELEGRAM_WEBHOOK_SECRET", settings.telegram_webhook_secret),
    ] if not value]
    return {"service": "meera-linkedin-agent", "ready": not missing, "missing_env": missing}


@app.post("/api/telegram")
async def telegram_webhook(request: Request,
                           x_telegram_bot_api_secret_token: str | None = Header(default=None)) -> dict:
    global _pipeline
    secret = settings.telegram_webhook_secret
    if not secret or not hmac.compare_digest(x_telegram_bot_api_secret_token or "", secret):
        raise HTTPException(status_code=401, detail="bad secret")

    update = await request.json()
    chat_id = private_chat_id(update)
    if chat_id is None:
        return {"ok": True, "skipped": "not a private message"}
    if chat_id != settings.telegram_chat_id:
        print(f"Ignored a message from chat {chat_id} (not the owner)")
        return {"ok": True, "skipped": "not the owner"}
    if not _first_time(update["update_id"]):
        print(f"Skipped repeated update {update['update_id']}")
        return {"ok": True, "skipped": "duplicate"}

    if _pipeline is None:
        _pipeline = LinkedInPipeline(settings)
    # Blocking work (Gemini, RSS, Telegram): run it off the event loop.
    await run_in_threadpool(handle_update, update, Telegram(settings.telegram_bot_token), _pipeline, settings)
    return {"ok": True}
