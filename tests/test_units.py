import fakeredis
import httpx
import pytest
import respx

from app import guardrails
from app.crypto import decrypt, encrypt, generate_key
from app.ig import IGError, InstagramClient, RateLimited
from app.main import verify_signature
from app.policy import APPROVAL, AUTO, HIDE, IGNORE, Classification, decide
from app.ratelimit import Limiter

KB = "Plano Pro: 19,99 € por mês."


def test_crypto_roundtrip():
    k = generate_key()
    assert decrypt(encrypt("segredo", k), k) == "segredo"
    assert encrypt("segredo", k) != encrypt("segredo", k)  # nonce aleatório


@pytest.mark.parametrize("cls,kind,mode,expected", [
    (dict(intent="elogio", confidence=0.9), "comment", "auto", AUTO),
    (dict(intent="elogio", confidence=0.9), "comment", "approval", APPROVAL),
    (dict(intent="elogio", confidence=0.5), "comment", "auto", APPROVAL),
    (dict(intent="lesao_saude", confidence=0.99), "dm", "auto", APPROVAL),
    (dict(intent="spam", confidence=0.9), "comment", "auto", HIDE),
    (dict(intent="spam", confidence=0.9), "dm", "auto", IGNORE),
    (dict(intent="outro", toxicity=0.9, confidence=0.9), "comment", "auto", HIDE),
    (dict(intent="outro", needs_reply=False, confidence=0.9), "comment", "auto", IGNORE),
    (dict(intent="preco", confidence=0.9), "comment", "auto", APPROVAL),
    (dict(intent="preco", confidence=0.9), "dm", "auto", AUTO),
])
def test_policy(cls, kind, mode, expected):
    assert decide(Classification.from_dict(cls), kind, mode) == expected


def test_policy_dm_turn_limit():
    c = Classification.from_dict(dict(intent="pergunta_produto", confidence=0.9))
    assert decide(c, "dm", "auto", dm_auto_turns=3, max_auto_dm_turns=3) == APPROVAL


def test_unknown_intent_falls_back_to_outro():
    assert Classification.from_dict({"intent": "hack"}).intent == "outro"


@pytest.mark.parametrize("text,needle", [
    ("Vê em https://evil.com já", "allowlist"),
    ("Liga para 912 345 678", "telefone"),
    ("Custa só 9 € por mês", "€"),
    ("Para a tua lesão no joelho faz gelo", "saúde"),
    ("ESCALAR", "escalamento"),
    (" ".join(["palavra"] * 40), "longo"),
    ("Top 🔥🔥🔥", "emojis"),
])
def test_guardrails_block(text, needle):
    problems = guardrails.check(text, "comment", allowed_domains=["apexpt.app"], knowledge_text=KB)
    assert any(needle in p for p in problems), problems


def test_guardrails_allow():
    ok = "Sim! O Plano Pro custa 19,99 € por mês. Vê em https://apexpt.app/precos"
    assert guardrails.check(ok, "dm", allowed_domains=["apexpt.app"], knowledge_text=KB) == []


def test_guardrails_similarity():
    p = guardrails.check("Obrigado pelo apoio!", "comment", allowed_domains=[], knowledge_text="",
                         recent=["Obrigado pelo apoio!!"])
    assert any("recente" in x for x in p)


def test_limiter_and_kill_switch():
    lim = Limiter(fakeredis.FakeRedis())
    assert [lim.allow("x", 2, 3600) for _ in range(3)] == [True, True, False]
    assert not lim.is_paused()
    lim.pause()
    assert lim.is_paused()
    lim.resume()
    assert not lim.is_paused()


def test_signature():
    body = b'{"a":1}'
    import hashlib
    import hmac
    sig = "sha256=" + hmac.new(b"s3", body, hashlib.sha256).hexdigest()
    assert verify_signature(body, sig, "s3")
    assert not verify_signature(body, sig, "outro")
    assert not verify_signature(body, None, "s3")


@respx.mock
def test_ig_client_reply_and_usage_header():
    route = respx.post("https://graph.instagram.com/v25.0/c1/replies").mock(
        return_value=httpx.Response(200, json={"id": "r1"},
                                    headers={"x-app-usage": '{"call_count":42,"total_time":10}'}))
    ig = InstagramClient(lambda: "TOK", "999")
    assert ig.reply_comment("c1", "olá") == "r1"
    req = route.calls.last.request
    assert req.url.params["access_token"] == "TOK" and req.url.params["message"] == "olá"
    assert ig.last_usage_pct == 42


@respx.mock
def test_ig_client_errors():
    respx.post("https://graph.instagram.com/v25.0/c1/replies").mock(
        return_value=httpx.Response(400, json={"error": {"code": 4, "message": "limit"}}))
    respx.post("https://graph.instagram.com/v25.0/c2/replies").mock(
        return_value=httpx.Response(400, json={"error": {"code": 100, "message": "bad"}}))
    respx.post("https://graph.instagram.com/v25.0/c3/replies").mock(return_value=httpx.Response(503))
    ig = InstagramClient(lambda: "TOK", "999")
    with pytest.raises(RateLimited):
        ig.reply_comment("c1", "x")
    with pytest.raises(IGError) as e:
        ig.reply_comment("c2", "x")
    assert not e.value.transient
    with pytest.raises(IGError) as e:
        ig.reply_comment("c3", "x")
    assert e.value.transient


@respx.mock
def test_ig_send_message_and_refresh():
    msg = respx.post("https://graph.instagram.com/v25.0/999/messages").mock(
        return_value=httpx.Response(200, json={"recipient_id": "u1", "message_id": "m"}))
    respx.get("https://graph.instagram.com/refresh_access_token").mock(
        return_value=httpx.Response(200, json={"access_token": "NEW", "expires_in": 5184000}))
    ig = InstagramClient(lambda: "TOK", "999")
    ig.send_message("u1", "olá")
    assert b'"recipient": {"id": "u1"}' in msg.calls.last.request.content or \
        b'"recipient":{"id":"u1"}' in msg.calls.last.request.content
    assert ig.refresh_token() == ("NEW", 5184000)


def test_wait_until_ready():
    ig = InstagramClient(lambda: "T", "1")
    seq = iter(["IN_PROGRESS", "IN_PROGRESS", "FINISHED"])
    ig.container_status = lambda cid: next(seq)
    ig.wait_until_ready("c", sleep=lambda s: None)
    ig.container_status = lambda cid: "ERROR"
    with pytest.raises(IGError):
        ig.wait_until_ready("c", sleep=lambda s: None)
