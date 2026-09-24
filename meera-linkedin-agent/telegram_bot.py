"""Telegram message handling shared by both ways of running the bot:
  * bot.py  - long polling, for running on your own machine
  * app.py  - webhook, for Vercel
"""

import json
import urllib.request

from config import Settings
from pipeline import LinkedInPipeline
from transcribe import transcribe

MAX_VOICE_BYTES = 20 * 1024 * 1024  # Telegram's getFile download limit for bots

WELCOME = (
    "Hi! Send me a note, typed or as a voice message, about something from your work: "
    "an experience, a number, an opinion.\n\n"
    "If it's strong enough, I'll send back a LinkedIn post ready to copy-paste. "
    "If not, I'll tell you what's missing so you can add more context or try another idea."
)


class Telegram:
    def __init__(self, token: str):
        self.token = token

    def call(self, method: str, **params) -> dict:
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        body = json.dumps(params).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=params.get("timeout", 0) + 20) as resp:
            return json.loads(resp.read())

    def send(self, chat_id, text: str) -> None:
        self.call("sendMessage", chat_id=chat_id, text=text)

    def typing(self, chat_id) -> None:
        try:
            self.call("sendChatAction", chat_id=chat_id, action="typing")
        except Exception:
            pass  # cosmetic only

    def download(self, file_id: str) -> bytes:
        path = self.call("getFile", file_id=file_id)["result"]["file_path"]
        with urllib.request.urlopen(f"https://api.telegram.org/file/bot{self.token}/{path}", timeout=60) as resp:
            return resp.read()


def private_chat_id(update: dict) -> str | None:
    """Chat id of a private message update, or None for anything else (groups, channels, edits...)."""
    chat = (update.get("message") or {}).get("chat", {})
    return str(chat["id"]) if chat.get("type") == "private" else None


def handle_update(update: dict, tg: Telegram, pipeline: LinkedInPipeline, settings: Settings, log=print) -> None:
    """Process one update from the owner: a command, a text note or a voice note."""
    msg = update["message"]
    chat_id = str(msg["chat"]["id"])
    try:
        _handle(msg, chat_id, tg, pipeline, settings, log)
    except Exception as e:  # never let one message take the bot down
        log(f"Error handling message: {e}")
        tg.send(chat_id, "Sorry, something went wrong with that note. Please try again in a minute.")


def _handle(msg: dict, chat_id: str, tg: Telegram, pipeline: LinkedInPipeline, settings: Settings, log) -> None:
    text = (msg.get("text") or "").strip()
    if text.startswith("/"):
        if text.split()[0] in ("/start", "/help"):
            tg.send(chat_id, WELCOME)
        return

    source = "text"
    audio = msg.get("voice") or msg.get("audio")
    if audio:
        source = "voice"
        if audio.get("file_size", 0) > MAX_VOICE_BYTES:
            tg.send(chat_id, "That voice note is too long for me (20 MB max). Could you send a shorter one?")
            return
        tg.typing(chat_id)
        log(f"--- Voice note ({audio.get('duration', '?')}s) ---")
        data = tg.download(audio["file_id"])
        text, model = transcribe(data, audio.get("mime_type") or "audio/ogg", settings, pipeline.client)
        log(f"[transcript via {model}] {text}")
        if not text:
            tg.send(chat_id, "I couldn't make out any words in that voice note. Could you try again, "
                             "or type the note instead?")
            return
    elif not text:
        tg.send(chat_id, "I can read text messages and voice notes. Send your idea as one of those.")
        return
    else:
        log(f"--- Text note ({len(text)} chars) ---")

    tg.typing(chat_id)
    result = pipeline.run(text, chat_id=chat_id, source=source)
    log(f"[result] score={result.score} outcome={result.outcome} model={result.model}")
    if not result.telegram_message:
        log(f"[error] {result.error}")
        tg.send(chat_id, "Sorry, I couldn't process that note right now. Please send it again in a minute.")
