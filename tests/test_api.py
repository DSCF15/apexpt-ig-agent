import hashlib
import hmac
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import app.main as main


@pytest.fixture
def client(monkeypatch, agent, settings):
    settings.webhook_verify_token = "vt"
    settings.admin_api_key = "adm"
    settings.telegram_webhook_secret = "tgs"
    settings.telegram_chat_id = "42"
    tasks = SimpleNamespace(process_webhook=MagicMock(), publish_draft=MagicMock())
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "get_agent", lambda: agent)
    monkeypatch.setattr(main, "_tasks", lambda: tasks)
    c = TestClient(main.app)
    c.tasks = tasks
    return c


def test_verify_handshake(client):
    r = client.get("/webhook/instagram", params={"hub.mode": "subscribe", "hub.verify_token": "vt",
                                                 "hub.challenge": "123"})
    assert r.status_code == 200 and r.text == "123"
    assert client.get("/webhook/instagram", params={"hub.mode": "subscribe", "hub.verify_token": "x",
                                                    "hub.challenge": "1"}).status_code == 403


def test_webhook_signature(client):
    body = json.dumps({"entry": []}).encode()
    sig = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert client.post("/webhook/instagram", content=body,
                       headers={"X-Hub-Signature-256": "sha256=bad"}).status_code == 401
    r = client.post("/webhook/instagram", content=body,
                    headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"})
    assert r.status_code == 200
    client.tasks.process_webhook.delay.assert_called_once_with({"entry": []})


def test_schedule_post_api(client):
    payload = {"media_type": "REELS", "media_urls": ["https://cdn/r.mp4"], "caption": "x",
               "publish_at": "2026-11-03T18:30:00+00:00"}
    assert client.post("/posts", json=payload).status_code == 401
    r = client.post("/posts", json=payload, headers={"X-API-Key": "adm"})
    assert r.status_code == 200 and r.json()["status"] == "pending"
    bad = {**payload, "media_urls": ["http://x"]}
    assert client.post("/posts", json=bad, headers={"X-API-Key": "adm"}).status_code == 422
    assert len(client.get("/posts", headers={"X-API-Key": "adm"}).json()) == 1
    pid = r.json()["id"]
    assert client.delete(f"/posts/{pid}", headers={"X-API-Key": "adm"}).json()["status"] == "cancelled"


def test_telegram_approve_button_and_edit(client, agent, llm):
    llm.verification = {"grounded": False, "safe": True, "reason": "x"}
    d = agent.handle_comment(comment_id="c1", text="?", username="ana", author_id="u1", media_id="m1")
    h = {"X-Telegram-Bot-Api-Secret-Token": "tgs"}
    assert client.post("/webhook/telegram", json={}, headers={"X-Telegram-Bot-Api-Secret-Token": "x"}).status_code == 401

    # mensagem de um chat estranho é ignorada
    assert main.handle_telegram_update({"callback_query": {"id": "1", "data": f"approve:{d.id}",
                                        "message": {"chat": {"id": 7}, "message_id": 5}}}) == "ignored"
    r = client.post("/webhook/telegram", headers=h, json={"callback_query": {
        "id": "cb", "data": f"approve:{d.id}", "message": {"chat": {"id": 42}, "message_id": d.telegram_message_id}}})
    assert r.status_code == 200
    client.tasks.publish_draft.delay.assert_called_once_with(d.id)

    d2 = agent.handle_comment(comment_id="c2", text="??", username="rui", author_id="u2", media_id="m2")
    res = main.handle_telegram_update({"message": {"chat": {"id": 42}, "text": "Texto final",
                                                   "reply_to_message": {"message_id": d2.telegram_message_id}}})
    assert res == "edited"
    with agent.sf() as s:
        from app.db import Draft
        assert s.get(Draft, d2.id).reply_text == "Texto final"


def test_telegram_commands(client, agent):
    assert main.handle_telegram_update({"message": {"chat": {"id": 42}, "text": "/pausa"}}) == "paused"
    assert agent.limiter.is_paused()
    assert main.handle_telegram_update({"message": {"chat": {"id": 42}, "text": "/estado"}}) == "status"
    assert main.handle_telegram_update({"message": {"chat": {"id": 42}, "text": "/retoma"}}) == "resumed"
    assert not agent.limiter.is_paused()


def test_panel_decisions(client, agent, llm, ig):
    llm.verification = {"grounded": False, "safe": True, "reason": "x"}
    h = {"X-API-Key": "adm"}
    d1 = agent.handle_comment(comment_id="c1", text="?", username="ana", author_id="u1", media_id="m1")
    d2 = agent.handle_comment(comment_id="c2", text="??", username="rui", author_id="u2", media_id="m1")
    d3 = agent.handle_comment(comment_id="c3", text="spam", username="bot", author_id="u3", media_id="m1")
    url = "/drafts/{}/decision"

    assert client.post(url.format(d1.id), json={"action": "approve"}).status_code == 401
    assert client.post(url.format(d1.id), json={"action": "x"}, headers=h).status_code == 422

    r = client.post(url.format(d1.id), json={"action": "approve", "edited_text": "Texto final"}, headers=h)
    assert r.json() == {"id": d1.id, "status": "approved"}
    client.tasks.publish_draft.delay.assert_called_once_with(d1.id)
    assert client.post(url.format(d1.id), json={"action": "approve"}, headers=h).status_code == 409

    assert client.post(url.format(d2.id), json={"action": "discard"}, headers=h).json()["status"] == "discarded"
    assert client.post(url.format(d3.id), json={"action": "hide"}, headers=h).json()["status"] == "hidden"
    ig.hide_comment.assert_called_once_with("c3")

    with agent.sf() as s:
        from app.db import Draft
        assert s.get(Draft, d1.id).reply_text == "Texto final"


def test_panel_status_and_page(client, agent):
    h = {"X-API-Key": "adm"}
    assert client.get("/admin/status").status_code == 401
    s = client.get("/admin/status", headers=h).json()
    assert s["paused"] is False and s["redis"] is True and s["drafts_pending"] == 0
    assert s["configured"]["instagram"] is True and s["configured"]["telegram"] is False

    agent.limiter.is_paused = lambda: (_ for _ in ()).throw(ConnectionError())
    s = client.get("/admin/status", headers=h).json()
    assert s["paused"] is None and s["redis"] is False

    r = client.get("/")
    assert r.status_code == 200 and "Painel do agente" in r.text
