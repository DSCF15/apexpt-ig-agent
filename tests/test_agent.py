from datetime import datetime, timedelta, timezone

import pytest

from app.agent import Deferred
from app.db import Draft, Message, ScheduledPost
from app.ig import IGError


def comment_webhook(cid="c1", user_id="u1", username="ana", text="Adorei a demo!", media="m1"):
    return {"object": "instagram", "entry": [{"id": "999", "time": 0, "changes": [{
        "field": "comments",
        "value": {"id": cid, "text": text, "from": {"id": user_id, "username": username},
                  "media": {"id": media, "media_product_type": "REELS"}}}]}]}


def dm_webhook(mid="mid1", sender="u7", text="Olá, como funciona?", echo=False):
    msg = {"mid": mid, "text": text}
    if echo:
        msg["is_echo"] = True
    return {"object": "instagram", "entry": [{"id": "999", "time": 0, "messaging": [{
        "sender": {"id": "999" if echo else sender}, "recipient": {"id": sender if echo else "999"},
        "timestamp": 0, "message": msg}]}]}


# ---------------- webhooks
def test_ingest_dedupes_and_skips_self(agent):
    assert agent.ingest_webhook(comment_webhook()) == [("comment", {
        "comment_id": "c1", "text": "Adorei a demo!", "username": "ana", "author_id": "u1", "media_id": "m1"})]
    assert agent.ingest_webhook(comment_webhook()) == []  # reenvio da Meta
    assert agent.ingest_webhook(comment_webhook(cid="c2", user_id="999")) == []  # a própria conta


def test_ingest_dm_and_echo(agent):
    assert agent.ingest_webhook(dm_webhook()) == [("dm", {"igsid": "u7", "text": "Olá, como funciona?", "mid": "mid1"})]
    assert agent.ingest_webhook(dm_webhook(mid="mid2", echo=True, text="resposta humana")) == []
    with agent.sf() as s:
        out = s.query(Message).filter_by(direction="out").one()
        assert out.igsid == "u7" and not out.automated


# ---------------- comentários
def test_comment_auto_flow(agent, ig):
    d = agent.handle_comment(comment_id="c1", text="Adorei a demo!", username="ana", author_id="u1", media_id="m1")
    assert d.status == "approved" and d.decided_by == "agent"
    assert agent.publish_draft(d.id) == "published"
    ig.reply_comment.assert_called_once_with("c1", d.reply_text)


def test_comment_spam_hidden(agent, ig, llm):
    llm.classification = {**llm.classification, "intent": "spam"}
    d = agent.handle_comment(comment_id="c9", text="Ganha seguidores em bit.ly/x", username="bot",
                             author_id="u9", media_id="m1")
    assert d.status == "hidden"
    ig.hide_comment.assert_called_once_with("c9")


def test_comment_guardrail_sends_to_approval(agent, llm, ig):
    llm.draft_text = "Custa 5 € por mês!"
    d = agent.handle_comment(comment_id="c1", text="Quanto custa?", username="ana", author_id="u1", media_id="m1")
    assert d.status == "awaiting_approval" and d.telegram_message_id == 1000 + d.id
    assert any("€" in p for p in d.problems)
    assert agent.publish_draft(d.id) == "awaiting_approval"  # não publica sem humano
    ig.reply_comment.assert_not_called()


def test_failed_verification_goes_to_approval(agent, llm):
    llm.verification = {"grounded": False, "safe": True, "reason": "inventou funcionalidade"}
    d = agent.handle_comment(comment_id="c1", text="Tem app iOS?", username="ana", author_id="u1", media_id="m1")
    assert d.status == "awaiting_approval"
    assert any("inventou" in p for p in d.problems)


def test_human_edit_and_approve(agent, llm, ig):
    llm.verification = {"grounded": False, "safe": True, "reason": "x"}
    d = agent.handle_comment(comment_id="c1", text="?", username="ana", author_id="u1", media_id="m1")
    assert agent.approve(d.id, edited_text="Texto do David") is not None
    assert agent.approve(d.id) is None  # já não está à espera
    assert agent.publish_draft(d.id) == "published"
    ig.reply_comment.assert_called_once_with("c1", "Texto do David")


def test_one_reply_per_user_per_post(agent):
    d1 = agent.handle_comment(comment_id="c1", text="Top", username="ana", author_id="u1", media_id="m1")
    agent.publish_draft(d1.id)
    d2 = agent.handle_comment(comment_id="c2", text="Outra vez top", username="ana", author_id="u1", media_id="m1")
    assert d2.status == "awaiting_approval"


def test_kill_switch_blocks_agent_but_not_human(agent, llm):
    d = agent.handle_comment(comment_id="c1", text="Top", username="ana", author_id="u1", media_id="m1")
    agent.limiter.pause()
    with pytest.raises(Deferred):
        agent.publish_draft(d.id)
    llm.verification = {"grounded": False, "safe": True, "reason": "x"}
    d2 = agent.handle_comment(comment_id="c2", text="?", username="rui", author_id="u2", media_id="m2")
    agent.approve(d2.id)
    assert agent.publish_draft(d2.id) == "published"


def test_rate_limit_defers(agent, settings):
    settings.max_comment_replies_per_hour = 1
    a = agent.handle_comment(comment_id="c1", text="Top", username="ana", author_id="u1", media_id="m1")
    b = agent.handle_comment(comment_id="c2", text="Boa", username="rui", author_id="u2", media_id="m1")
    assert agent.publish_draft(a.id) == "published"
    with pytest.raises(Deferred):
        agent.publish_draft(b.id)


def test_permanent_ig_error_marks_failed(agent, ig):
    ig.reply_comment.side_effect = IGError("comentário apagado", status=400, code=100)
    d = agent.handle_comment(comment_id="c1", text="Top", username="ana", author_id="u1", media_id="m1")
    assert agent.publish_draft(d.id) == "failed"
    assert agent.notifier.alerts


# ---------------- DMs
def test_dm_flow_with_disclosure_and_window(agent, llm, ig):
    llm.classification = {**llm.classification, "intent": "pergunta_produto"}
    d = agent.handle_dm(igsid="u7", text="Como funciona?")
    assert d.status == "approved"
    system_prompt = llm.calls[1]["system"]
    assert "assistente automático" in system_prompt  # primeira resposta automática divulga
    assert agent.publish_draft(d.id) == "published"
    ig.send_message.assert_called_once()
    d2 = agent.handle_dm(igsid="u7", text="E para clientes?")
    assert "assistente automático" not in llm.calls[-2]["system"]
    assert d2.status in ("approved", "awaiting_approval")


def test_dm_outside_24h_window(agent, llm):
    llm.classification = {**llm.classification, "intent": "pergunta_produto"}
    d = agent.handle_dm(igsid="u7", text="Olá")
    with agent.sf() as s:
        for m in s.query(Message).filter_by(igsid="u7"):
            m.created_at = datetime.now(timezone.utc) - timedelta(hours=30)
        s.commit()
    assert agent.publish_draft(d.id) == "failed"


def test_dm_health_never_auto(agent, llm):
    llm.classification = {**llm.classification, "intent": "lesao_saude", "confidence": 1.0}
    d = agent.handle_dm(igsid="u8", text="Tenho uma dor no joelho, que faço?")
    assert d.status == "awaiting_approval"


# ---------------- posts agendados
def test_schedule_validation(agent):
    with pytest.raises(ValueError):
        agent.schedule_post(media_type="CAROUSEL", media_urls=["https://a/1.jpg"], caption="",
                            publish_at=datetime.now(timezone.utc))
    with pytest.raises(ValueError):
        agent.schedule_post(media_type="IMAGE", media_urls=["http://a/1.jpg"], caption="",
                            publish_at=datetime.now(timezone.utc))
    with pytest.raises(ValueError):
        agent.schedule_post(media_type="IMAGE", media_urls=["https://a/1.jpg"], caption="",
                            publish_at=datetime(2026, 11, 3, 18, 30))


def test_publish_due_post(agent, ig):
    p = agent.schedule_post(media_type="REELS", media_urls=["https://cdn/r.mp4"], caption="Reel",
                            publish_at=datetime.now(timezone.utc) - timedelta(minutes=1))
    agent.schedule_post(media_type="IMAGE", media_urls=["https://cdn/f.jpg"], caption="Futuro",
                        publish_at=datetime.now(timezone.utc) + timedelta(days=1))
    assert agent.claim_due_posts() == [p.id]
    assert agent.claim_due_posts() == []  # não reclama duas vezes
    assert agent.publish_post(p.id) == "published"
    ig.create_container.assert_called_with(media_type="REELS", url="https://cdn/r.mp4", caption="Reel")
    ig.publish.assert_called_once_with("c-REELS")


def test_publish_carousel(agent, ig):
    p = agent.schedule_post(media_type="CAROUSEL", media_urls=["https://cdn/1.jpg", "https://cdn/2.mp4"],
                            caption="Carrossel", publish_at=datetime.now(timezone.utc))
    agent.claim_due_posts()
    assert agent.publish_post(p.id) == "published"
    kinds = [c.kwargs["media_type"] for c in ig.create_container.call_args_list]
    assert kinds == ["IMAGE", "VIDEO", "CAROUSEL"]


def test_daily_limit_postpones(agent, settings):
    settings.max_posts_per_day = 1
    now = datetime.now(timezone.utc)
    a = agent.schedule_post(media_type="IMAGE", media_urls=["https://c/a.jpg"], caption="a", publish_at=now)
    b = agent.schedule_post(media_type="IMAGE", media_urls=["https://c/b.jpg"], caption="b", publish_at=now)
    agent.claim_due_posts()
    assert agent.publish_post(a.id) == "published"
    assert agent.publish_post(b.id) == "postponed"
    with agent.sf() as s:
        assert s.get(ScheduledPost, b.id).status == "pending"


def test_transient_error_retries_then_fails(agent, ig):
    ig.publish.side_effect = IGError("503", status=503, transient=True)
    p = agent.schedule_post(media_type="IMAGE", media_urls=["https://c/a.jpg"], caption="a",
                            publish_at=datetime.now(timezone.utc))
    results = []
    for _ in range(3):
        with agent.sf() as s:  # força o post a estar vencido outra vez
            post = s.get(ScheduledPost, p.id)
            if post.status == "pending":
                post.publish_at = datetime.now(timezone.utc) - timedelta(seconds=1)
                s.commit()
        agent.claim_due_posts()
        results.append(agent.publish_post(p.id))
    assert results == ["postponed", "postponed", "failed"]


# ---------------- tokens
def test_token_store_encrypts(agent, ig):
    ig.refresh_token.return_value = ("NOVO", 5184000)
    exp = agent.refresh_token()
    assert agent.tokens.get() == "NOVO"
    assert exp > datetime.now(timezone.utc) + timedelta(days=59)
    with agent.sf() as s:
        from app.db import Credential
        assert "NOVO" not in s.get(Credential, "ig_access_token").value_enc


def test_draft_audit_trail(agent):
    d = agent.handle_comment(comment_id="c1", text="Top", username="ana", author_id="u1", media_id="m1")
    with agent.sf() as s:
        row = s.get(Draft, d.id)
        assert row.classification["intent"] == "elogio" and row.reply_text
