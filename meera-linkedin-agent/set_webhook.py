"""Point the Telegram bot at the Vercel deployment (or check / remove the webhook).

    python set_webhook.py https://your-project.vercel.app   # register
    python set_webhook.py --info                            # show current status
    python set_webhook.py --delete                          # remove (e.g. to go back to bot.py)

Uses TELEGRAM_BOT_TOKEN and TELEGRAM_WEBHOOK_SECRET from .env. The secret must be the same
value you set in the Vercel project's environment variables.
"""

import argparse
import json

from config import load_settings
from telegram_bot import Telegram


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("base_url", nargs="?", help="Deployment URL, e.g. https://your-project.vercel.app")
    parser.add_argument("--info", action="store_true")
    parser.add_argument("--delete", action="store_true")
    args = parser.parse_args()

    settings = load_settings()
    if not settings.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set in .env")
    tg = Telegram(settings.telegram_bot_token)

    if args.delete:
        print(tg.call("deleteWebhook"))
    elif args.base_url:
        if not settings.telegram_webhook_secret:
            raise SystemExit("TELEGRAM_WEBHOOK_SECRET is not set in .env")
        url = args.base_url.rstrip("/") + "/api/telegram"
        print(tg.call(
            "setWebhook",
            url=url,
            secret_token=settings.telegram_webhook_secret,
            allowed_updates=["message"],
            max_connections=1,         # one note at a time, in order
            drop_pending_updates=True,  # don't replay messages sent while nothing was listening
        ))
    elif not args.info:
        parser.error("give a deployment URL, --info or --delete")

    info = tg.call("getWebhookInfo")["result"]
    print(json.dumps({k: info.get(k) for k in
                      ("url", "pending_update_count", "last_error_date", "last_error_message", "max_connections")},
                     indent=2))


if __name__ == "__main__":
    main()
