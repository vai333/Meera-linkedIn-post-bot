"""Run the bot on your own machine with long polling (no public URL needed).

The bot belongs to one person. TELEGRAM_CHAT_ID holds their private chat id. If it's blank, the
first person to message the bot becomes the owner, and their id is saved to .env. Everyone else
is ignored.

Telegram delivers updates either by polling or to a webhook, not both: this script removes any
webhook (e.g. the Vercel one) when it starts. Run set_webhook.py again afterwards to switch back.

    python bot.py
"""

import time
import urllib.error

from dotenv import set_key

from config import ROOT, load_settings
from pipeline import LinkedInPipeline
from telegram_bot import Telegram, handle_update, private_chat_id


def main() -> None:
    settings = load_settings()
    if not settings.telegram_bot_token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in .env first.")
    if not settings.gemini_api_key:
        raise SystemExit("Set GEMINI_API_KEY in .env first.")

    tg = Telegram(settings.telegram_bot_token)
    if tg.call("getWebhookInfo")["result"].get("url"):
        tg.call("deleteWebhook")
        print("Removed the existing webhook so polling can work (run set_webhook.py to restore it).")
    pipeline = LinkedInPipeline(settings)
    owner = settings.telegram_chat_id
    bot_name = tg.call("getMe")["result"]["username"]
    print(f"@{bot_name} is listening. Owner chat: {owner or 'not paired yet (first private message pairs)'}")

    offset = 0
    while True:
        try:
            updates = tg.call("getUpdates", offset=offset, timeout=50,
                              allowed_updates=["message"]).get("result", [])
        except urllib.error.HTTPError as e:
            hint = " (another copy of the bot is running?)" if e.code == 409 else ""
            print(f"getUpdates failed: HTTP {e.code}{hint}; retrying in 5s")
            time.sleep(5)
            continue
        except Exception as e:
            print(f"getUpdates failed: {e}; retrying in 5s")
            time.sleep(5)
            continue

        for update in updates:
            offset = update["update_id"] + 1
            chat_id = private_chat_id(update)
            if chat_id is None:
                continue
            if owner is None:
                owner = chat_id
                set_key(str(ROOT / ".env"), "TELEGRAM_CHAT_ID", owner, quote_mode="never")
                print(f"Paired with chat {owner}; saved to .env")
            if chat_id != owner:
                print(f"Ignored a message from chat {chat_id} (not the owner)")
                continue
            print()
            handle_update(update, tg, pipeline, settings)


if __name__ == "__main__":
    main()
