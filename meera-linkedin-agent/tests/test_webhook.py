"""Webhook (Vercel entry point) tests: secret check, owner-only, duplicate skipping."""

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as webapp  # noqa: E402

OWNER = "111"
SECRET = "s3cret"


@pytest.fixture
def client(monkeypatch):
    s = webapp.settings
    monkeypatch.setattr(webapp, "settings", s.__class__(**{
        **s.__dict__, "telegram_chat_id": OWNER, "telegram_webhook_secret": SECRET,
        "telegram_bot_token": "123:abc", "gemini_api_key": "k"}))
    handled = []
    monkeypatch.setattr(webapp, "handle_update", lambda update, *a, **k: handled.append(update["update_id"]))
    monkeypatch.setattr(webapp, "LinkedInPipeline", lambda settings: object())
    webapp._seen.clear()
    c = TestClient(webapp.app)
    c.handled = handled
    return c


def update(update_id, chat_id=OWNER, chat_type="private"):
    return {"update_id": update_id,
            "message": {"message_id": 1, "chat": {"id": int(chat_id), "type": chat_type}, "text": "hello"}}


def post(client, body, secret=SECRET):
    return client.post("/api/telegram", json=body, headers={"X-Telegram-Bot-Api-Secret-Token": secret})


def test_rejects_missing_or_wrong_secret(client):
    assert client.post("/api/telegram", json=update(1)).status_code == 401
    assert post(client, update(1), secret="nope").status_code == 401
    assert client.handled == []


def test_owner_message_is_handled(client):
    r = post(client, update(1))
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert client.handled == [1]


def test_other_people_and_groups_are_ignored(client):
    assert post(client, update(2, chat_id="999")).json()["skipped"] == "not the owner"
    assert post(client, update(3, chat_type="group")).json()["skipped"] == "not a private message"
    assert client.handled == []


def test_resent_update_is_processed_once(client):
    post(client, update(5))
    assert post(client, update(5)).json()["skipped"] == "duplicate"
    assert client.handled == [5]


def test_health_reports_missing_env_without_values(client, monkeypatch):
    body = client.get("/").json()
    assert body["ready"] is True and body["missing_env"] == []
    monkeypatch.setattr(webapp, "settings", webapp.settings.__class__(
        **{**webapp.settings.__dict__, "telegram_webhook_secret": None}))
    assert client.get("/").json()["missing_env"] == ["TELEGRAM_WEBHOOK_SECRET"]
