from datetime import datetime, timedelta, timezone

import pytest

import app.main as main
from app.db import ScheduledPost
from tests.test_api import client  # noqa: F401  (fixture)

H = {"X-API-Key": "adm"}


@pytest.fixture
def approval(settings, agent):
    settings.post_approval = True
    agent.notifier.posts = []
    agent.notifier.send_post = lambda p: agent.notifier.posts.append(p) or 2000 + p.id
    return agent


def _schedule(agent, minutes=60, caption="Legenda"):
    return agent.schedule_post(media_type="IMAGE", media_urls=["https://cdn/x.jpg"], caption=caption,
                               publish_at=datetime.now(timezone.utc) + timedelta(minutes=minutes))


def _status(agent, pid):
    with agent.sf() as s:
        return s.get(ScheduledPost, pid)


def test_post_waits_for_approval_and_goes_to_telegram(approval):
    p = _schedule(approval)
    assert p.status == "awaiting_approval" and p.telegram_message_id == 2000 + p.id
    assert approval.notifier.posts[0].id == p.id
    # mesmo vencido, um post por aprovar nunca é publicado
    assert approval.claim_due_posts(datetime.now(timezone.utc) + timedelta(hours=2)) == []
    assert _status(approval, p.id).status == "expired"
    assert any("sem aprovação" in a for a in approval.notifier.alerts)


def test_approve_then_publish(approval):
    p = _schedule(approval)
    assert approval.approve_post(p.id, caption="Nova legenda").status == "pending"
    assert approval.approve_post(p.id) is None  # já tratado
    ids = approval.claim_due_posts(datetime.now(timezone.utc) + timedelta(hours=2))
    assert ids == [p.id]
    assert approval.publish_post(p.id) == "published"
    assert _status(approval, p.id).caption == "Nova legenda"


def test_telegram_buttons_and_reply_for_posts(client, approval):  # noqa: F811
    p1, p2, p3 = _schedule(approval), _schedule(approval), _schedule(approval)
    cb = lambda data, mid: {"callback_query": {"id": "cb", "data": data,  # noqa: E731
                                               "message": {"chat": {"id": 42}, "message_id": mid}}}
    assert main.handle_telegram_update(cb(f"post_ok:{p1.id}", p1.telegram_message_id)).startswith("Aprovado")
    assert main.handle_telegram_update(cb(f"post_no:{p2.id}", p2.telegram_message_id)) == "Rejeitado"
    assert main.handle_telegram_update(cb(f"post_no:{p2.id}", p2.telegram_message_id)).startswith("Já tratado")
    res = main.handle_telegram_update({"message": {"chat": {"id": 42}, "text": "Legenda do Telegram",
                                                   "reply_to_message": {"message_id": p3.telegram_message_id}}})
    assert res == "post_edited"
    assert [_status(approval, p.id).status for p in (p1, p2, p3)] == ["pending", "rejected", "pending"]
    assert _status(approval, p3.id).caption == "Legenda do Telegram"
    client.tasks.publish_draft.delay.assert_not_called()


def test_panel_post_decisions(client, approval):  # noqa: F811
    p1, p2 = _schedule(approval), _schedule(approval)
    url = "/posts/{}/decision"
    assert client.post(url.format(p1.id), json={"action": "approve"}).status_code == 401
    assert client.post(url.format(p1.id), json={"action": "x"}, headers=H).status_code == 422
    assert client.post(url.format(p1.id), json={"action": "approve"}, headers=H).json()["status"] == "pending"
    assert client.post(url.format(p1.id), json={"action": "approve"}, headers=H).status_code == 409
    assert client.post(url.format(p2.id), json={"action": "reject"}, headers=H).json()["status"] == "rejected"
    s = client.get("/admin/status", headers=H).json()
    assert s["posts_pending"] == 1 and s["posts_awaiting"] == 0 and s["post_approval"] is True
    p3 = _schedule(approval)
    assert client.delete(f"/posts/{p3.id}", headers=H).json()["status"] == "cancelled"


def test_simulate_only_in_dry_run(client, settings, agent):  # noqa: F811
    body = {"kind": "comment", "text": "Isto dá para PTs online?", "username": "ana"}
    assert client.post("/dev/simulate", json=body, headers=H).status_code == 403
    settings.dry_run, settings.autonomy_mode = True, "approval"
    r = client.post("/dev/simulate", json=body, headers=H).json()
    assert r["status"] == "awaiting_approval" and r["reply"]
    r = client.post("/dev/simulate", json={**body, "kind": "dm"}, headers=H).json()
    assert r["status"] == "awaiting_approval"
    assert client.post("/dev/simulate", json={**body, "kind": "x"}, headers=H).status_code == 422


def test_offline_llm_and_fake_ig_drive_the_real_agent(settings):
    from app.agent import Agent
    from app.brain import Brain
    from app.db import make_session_factory
    from app.dev import FakeInstagramClient, OfflineLLM
    from app.ratelimit import Limiter
    from app.telegram import Notifier
    import fakeredis

    settings.autonomy_mode = "approval"
    brain = Brain(OfflineLLM(), model_classify="h", model_draft="s", knowledge_dir=settings.knowledge_dir)
    a = Agent(settings, make_session_factory(settings.database_url), FakeInstagramClient(), brain,
              Limiter(fakeredis.FakeRedis()), Notifier())
    d = a.handle_comment(comment_id="c", text="Quanto custa?", username="ze", author_id="u", media_id="m")
    assert d.status == "awaiting_approval" and d.intent == "preco"
    a.approve(d.id)
    assert a.publish_draft(d.id) == "published"


def test_telegram_post_message_format():
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from app.telegram import TelegramNotifier

    http = MagicMock()
    http.post.return_value.json.return_value = {"ok": True, "result": {"message_id": 77}}
    tg = TelegramNotifier("T", "42", http=http)
    post = SimpleNamespace(id=5, media_type="REELS", media_urls=["https://cdn/r.mp4?a=1&b=2"],
                           caption="<b>Olá</b> & bem-vindos",
                           publish_at=datetime(2026, 7, 1, 17, 30, tzinfo=timezone.utc))
    assert tg.send_post(post) == 77
    payload = http.post.call_args.kwargs["json"]
    assert "01/07/2026 18:30 (Lisboa)" in payload["text"]  # verão: UTC+1
    assert "&lt;b&gt;Olá&lt;/b&gt; &amp; bem-vindos" in payload["text"]
    assert "a=1&amp;b=2" in payload["text"]
    buttons = payload["reply_markup"]["inline_keyboard"][0]
    assert [b["callback_data"] for b in buttons] == ["post_ok:5", "post_no:5"]
