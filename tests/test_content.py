from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

import app.main as main
from app.content import ContentStudio, parse_when, suggest_slots
from app.db import ScheduledPost
from tests.test_api import client  # noqa: F401  (fixture)

LIS = ZoneInfo("Europe/Lisbon")


@pytest.fixture
def studio(settings, agent, tmp_path):
    settings.media_dir = str(tmp_path / "media")
    settings.public_base_url = "https://agente.apexpt.app"
    settings.post_approval = True
    n = agent.notifier
    n.sent = []
    n.send_caption_options = lambda p: n.sent.append(("caps", p.id)) or 5000 + len(n.sent)
    n.send_time_options = lambda p, slots: n.sent.append(("times", p.id, slots)) or 5000 + len(n.sent)
    n.send_post = MagicMock(return_value=None)
    n.edited = []
    n.edit_status = lambda mid, text: n.edited.append(text)
    return ContentStudio(agent)


def _post(agent, pid):
    with agent.sf() as s:
        return s.get(ScheduledPost, pid)


def test_slots_and_parse_when():
    now = datetime(2026, 11, 16, 18, 20, tzinfo=LIS)  # 18:20 → 18:30 está a menos de 20 min
    slots = [s.astimezone(LIS).strftime("%d %H:%M") for s in suggest_slots(now)]
    assert slots == ["16 21:00", "17 12:30", "17 18:30"]
    assert parse_when("21:15", now).astimezone(LIS).strftime("%d %H:%M") == "16 21:15"
    assert parse_when("09:00", now).astimezone(LIS).strftime("%d %H:%M") == "17 09:00"
    assert parse_when("24/12 10h30", now).astimezone(LIS).strftime("%Y-%m-%d %H:%M") == "2026-12-24 10:30"
    assert parse_when("05/01 10:00", now).astimezone(LIS).year == 2027
    assert parse_when("agora", now) > now
    assert parse_when("amanhã", now) is None and parse_when("31/02 10:00", now) is None
    assert parse_when("16/11/2026 10:00", now) is None  # no passado


def test_full_flow_with_option(studio, agent):
    p = studio.start(file_name="a" * 32 + ".jpg", media_type="IMAGE", hint="avaliações físicas")
    p = studio.generate(p.id)
    assert p.status == "choose_caption" and len(p.options) == 3
    assert p.options[0]["caption"].endswith("#pt #fitness")
    assert p.telegram_message_id == 5001
    p = studio.choose_caption(p.id, option=1)
    assert p.status == "choose_time" and p.caption.startswith("Legenda 2")
    assert studio.choose_caption(p.id, option=0) is None  # já escolhida
    assert studio.choose_time(p.id, datetime.now(timezone.utc) - timedelta(minutes=1)) is None
    p = studio.choose_time(p.id, datetime.now(timezone.utc) + timedelta(hours=1))
    post = _post(agent, p.post_id)
    assert p.status == "scheduled" and post.status == "pending"  # já aprovado: não volta a pedir
    assert post.media_urls == [f"https://agente.apexpt.app/media/{'a' * 32}.jpg"]
    agent.notifier.send_post.assert_not_called()


def test_story_tag_regenerate_discard(studio):
    p = studio.start(file_name="b" * 32 + ".mp4", media_type="REELS", hint="bastidores #story")
    assert p.media_type == "STORIES" and p.hint == "bastidores"
    studio.generate(p.id)
    assert studio.regenerate(p.id).status == "proposing"
    assert studio.generate(p.id).status == "choose_caption"
    (studio.media_dir / p.file_name).write_bytes(b"x")
    assert studio.discard(p.id).status == "discarded"
    assert not (studio.media_dir / p.file_name).exists()
    assert studio.choose_caption(p.id, option=0) is None


def test_llm_failure_is_reported(studio, agent, llm):
    llm.captions = []
    p = studio.start(file_name="c" * 32 + ".jpg", media_type="IMAGE", hint="")
    assert studio.generate(p.id).status == "failed"
    assert any("não consegui criar legendas" in a for a in agent.notifier.alerts)


def test_missing_public_url_blocks_scheduling(studio, settings):
    settings.public_base_url = ""
    p = studio.start(file_name="d" * 32 + ".jpg", media_type="IMAGE", hint="")
    studio.generate(p.id)
    studio.choose_caption(p.id, text="A minha legenda")
    with pytest.raises(ValueError, match="PUBLIC_BASE_URL"):
        studio.choose_time(p.id, datetime.now(timezone.utc) + timedelta(hours=1))


def test_telegram_flow(client, studio, agent):  # noqa: F811
    client.tasks.propose_content = MagicMock()
    n = agent.notifier
    n.download_file = lambda file_id, dest: dest.write_bytes(b"jpeg") or 4
    chat = {"id": 42}

    # 1) envias uma foto com descrição
    res = main.handle_telegram_update({"message": {"chat": chat, "caption": "novo painel de evolução",
                                                   "photo": [{"file_id": "s"}, {"file_id": "big", "file_size": 10}]}})
    assert res == "content_started"
    pid = client.tasks.propose_content.delay.call_args.args[0]
    p = studio.generate(pid)  # o que o worker faria

    # 2) carregas no botão "2"
    cb = lambda data, mid: {"callback_query": {"id": "c", "data": data,  # noqa: E731
                                               "message": {"chat": chat, "message_id": mid}}}
    assert main.handle_telegram_update(cb(f"cap:{pid}:1", p.telegram_message_id)) == "Legenda 2 escolhida"
    p = studio.get(pid)

    # 3) respondes com uma hora tua
    res = main.handle_telegram_update({"message": {"chat": chat, "text": "não sei",
                                                   "reply_to_message": {"message_id": p.telegram_message_id}}})
    assert res == "bad_time"
    res = main.handle_telegram_update({"message": {"chat": chat, "text": "agora",
                                                   "reply_to_message": {"message_id": p.telegram_message_id}}})
    assert res == "content_scheduled"
    p = studio.get(pid)
    assert p.status == "scheduled" and _post(agent, p.post_id).caption.startswith("Legenda 2")
    assert (studio.media_dir / p.file_name).read_bytes() == b"jpeg"

    # o ficheiro fica disponível para a Meta, e só ficheiros com nome válido
    assert client.get(f"/media/{p.file_name}").content == b"jpeg"
    assert client.get("/media/..%2F.env").status_code == 404
    assert client.get("/media/" + "0" * 32 + ".jpg").status_code == 404


def test_telegram_custom_caption_and_buttons(client, studio, agent):  # noqa: F811
    client.tasks.propose_content = MagicMock()
    agent.notifier.download_file = lambda file_id, dest: dest.write_bytes(b"mp4") or 3
    chat = {"id": 42}
    main.handle_telegram_update({"message": {"chat": chat, "video": {"file_id": "v", "mime_type": "video/mp4"}}})
    pid = client.tasks.propose_content.delay.call_args.args[0]
    p = studio.generate(pid)
    assert p.media_type == "REELS"

    res = main.handle_telegram_update({"message": {"chat": chat, "text": "Legenda minha",
                                                   "reply_to_message": {"message_id": p.telegram_message_id}}})
    assert res == "content_caption" and studio.get(pid).caption == "Legenda minha"
    when = int((datetime.now(timezone.utc) + timedelta(hours=3)).timestamp())
    cb = {"callback_query": {"id": "c", "data": f"when:{pid}:{when}",
                             "message": {"chat": chat, "message_id": studio.get(pid).telegram_message_id}}}
    assert main.handle_telegram_update(cb).startswith("Agendado")
    assert main.handle_telegram_update(cb) == "Já tratado ou inexistente"


def test_telegram_rejects_bad_media(client, studio, agent):  # noqa: F811
    chat = {"id": 42}
    assert main.handle_telegram_update({"message": {"chat": chat, "document": {
        "file_id": "x", "mime_type": "application/pdf"}}}) == "unsupported"
    assert main.handle_telegram_update({"message": {"chat": chat, "video": {
        "file_id": "x", "mime_type": "video/mp4", "file_size": 30 * 1024 * 1024}}}) == "too_big"
    assert main.handle_telegram_update({"message": {"chat": {"id": 7}, "photo": [{"file_id": "x"}]}}) == "ignored"
