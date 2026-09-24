"""Runtime configuration, read from environment variables (or a local .env file)."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    gemini_api_key: str | None
    models: tuple[str, ...]  # first is preferred; the rest are fallbacks on overload / quota errors
    telegram_bot_token: str | None
    telegram_chat_id: str | None
    rss_max_items: int
    transcribe_vocabulary: tuple[str, ...]  # extra words for voice-note transcription (names, products)
    telegram_webhook_secret: str | None     # shared secret Telegram sends with each webhook call
    save_runs: bool                         # write run JSON to runs/ (off on Vercel: read-only filesystem)
    runs_dir: Path



def load_settings() -> Settings:
    return Settings(
        gemini_api_key=os.getenv("GEMINI_API_KEY") or None,
        models=tuple(m.strip() for m in os.getenv(
            "GEMINI_MODELS", "gemini-flash-latest,gemini-3.5-flash,gemini-3-flash-preview").split(",") if m.strip()),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN") or None,
        # The owner's private chat with the bot. Left blank, bot.py pairs with the first person to message it.
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID") or None,
        rss_max_items=int(os.getenv("RSS_MAX_ITEMS", "8")),
        telegram_webhook_secret=os.getenv("TELEGRAM_WEBHOOK_SECRET") or None,
        save_runs=not os.getenv("VERCEL"),
        transcribe_vocabulary=tuple(w.strip() for w in os.getenv("TRANSCRIBE_VOCABULARY", "").split(",") if w.strip()),
        runs_dir=ROOT / "runs",
    )


SYSTEM_PROMPT = (ROOT / "prompts" / "system_prompt.md").read_text(encoding="utf-8")
