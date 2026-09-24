"""Run the pipeline once on a transcribed note.

    python cli.py "note text"
    python cli.py --file note.txt
    echo "note text" | python cli.py
"""

import argparse
import sys

from config import load_settings
from pipeline import LinkedInPipeline


def main() -> int:
    parser = argparse.ArgumentParser(description="Turn Meera's voice note into feedback or a LinkedIn post.")
    parser.add_argument("text", nargs="?", help="The transcribed note")
    parser.add_argument("--file", help="Read the note from a text file")
    args = parser.parse_args()

    if args.file:
        with open(args.file, encoding="utf-8") as f:
            text = f.read()
    elif args.text:
        text = args.text
    elif not sys.stdin.isatty():
        text = sys.stdin.read()
    else:
        parser.error("provide the note as an argument, --file, or stdin")
    if not text.strip():
        parser.error("the note is empty")

    settings = load_settings()
    if not settings.gemini_api_key:
        parser.error("GEMINI_API_KEY is not set (add it to .env)")
    result = LinkedInPipeline(settings).run(text)

    print("\n" + "=" * 60)
    searches = ", ".join(f"{q['query']!r} [{q['region']}]" for q in result.rss_queries) or "-"
    print(f"Score: {result.score}   Outcome: {result.outcome}   Allowed regions: {result.allowed_regions}")
    print(f"Searches: {searches}")
    if result.telegram_message:
        status = "DRY RUN (not sent)" if result.dry_run else "Delivered to Telegram"
        print(f"{status}:\n\n{result.telegram_message}")
    for w in result.warnings:
        print(f"Warning: {w}")
    if result.error:
        print(f"Error: {result.error}")
    return 0 if result.telegram_message else 1


if __name__ == "__main__":
    sys.exit(main())
