"""API HTTP: webhooks da Meta e do Telegram + endpoints de administração.

Arranque: uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from .config import get_settings
from .db import Draft, ScheduledPost
from .deps import get_agent

log = logging.getLogger(__name__)
app = FastAPI(title="ApexPT Instagram Agent")


def _tasks():
    from . import tasks  # import tardio: a API arranca mesmo sem worker
    return tasks


def verify_signature(body: bytes, header: str | None, app_secret: str) -> bool:
    if not app_secret or not header:
        return False
    expected = "sha256=" + hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def require_admin(x_api_key: str | None = Header(default=None)):
    key = get_settings().admin_api_key
    if not key or not x_api_key or not hmac.compare_digest(key, x_api_key):
        raise HTTPException(401, "API key inválida")


@app.get("/health")
def health():
    return {"ok": True}


# ------------------------------------------------------------ Instagram
@app.get("/webhook/instagram")
def ig_verify(request: Request):
    q = request.query_params
    s = get_settings()
    if q.get("hub.mode") == "subscribe" and s.webhook_verify_token and \
            hmac.compare_digest(q.get("hub.verify_token", ""), s.webhook_verify_token):
        return Response(q.get("hub.challenge", ""), media_type="text/plain")
    raise HTTPException(403)


@app.post("/webhook/instagram")
async def ig_receive(request: Request):
    body = await request.body()
    if not verify_signature(body, request.headers.get("X-Hub-Signature-256"), get_settings().ig_app_secret):
        raise HTTPException(401, "assinatura inválida")
    _tasks().process_webhook.delay(await request.json())  # responder já: a Meta quer 200 rápido
    return {"ok": True}


# ------------------------------------------------------------ Telegram
@app.post("/webhook/telegram")
async def tg_receive(request: Request,
                     x_telegram_bot_api_secret_token: str | None = Header(default=None)):
    s = get_settings()
    if not s.telegram_webhook_secret or x_telegram_bot_api_secret_token != s.telegram_webhook_secret:
        raise HTTPException(401)
    update = await request.json()
    handle_telegram_update(update)
    return {"ok": True}


def handle_telegram_update(update: dict) -> str:
    """Botões Publicar/Descartar/Esconder, edição por resposta e comandos /pausa /retoma /estado."""
    s, agent = get_settings(), get_agent()
    notifier = agent.notifier

    if cb := update.get("callback_query"):
        if str(cb.get("message", {}).get("chat", {}).get("id")) != str(s.telegram_chat_id):
            return "ignored"
        action, _, raw_id = (cb.get("data") or "").partition(":")
        if action in ("cap", "capnew", "capx", "when"):
            return _content_callback(cb, action, raw_id, agent)
        draft_id = int(raw_id) if raw_id.isdigit() else 0
        result = "?"
        label = f"#{draft_id}"
        if action in ("post_ok", "post_no"):
            label = f"Post #{draft_id}"
            if action == "post_ok" and agent.approve_post(draft_id):
                result = "Aprovado: sai à hora marcada"
            elif action == "post_no" and agent.reject_post(draft_id):
                result = "Rejeitado"
            else:
                result = "Já tratado ou inexistente"
        elif action == "approve" and agent.approve(draft_id):
            _tasks().publish_draft.delay(draft_id)
            result = "Aprovado: a publicar"
        elif action == "discard" and agent.discard(draft_id):
            result = "Descartado"
        elif action == "hide" and agent.hide(draft_id):
            result = "Comentário escondido"
        else:
            result = "Já tratado ou inexistente"
        if hasattr(notifier, "answer_callback"):
            notifier.answer_callback(cb["id"], result)
        msg_id = cb.get("message", {}).get("message_id")
        if msg_id:
            notifier.edit_status(msg_id, f"{label}: {result}")
        return result

    msg = update.get("message") or {}
    if str(msg.get("chat", {}).get("id")) != str(s.telegram_chat_id):
        return "ignored"
    text = (msg.get("text") or "").strip()

    if any(k in msg for k in ("photo", "video", "document")):
        return _content_media(msg, agent)

    if reply_to := msg.get("reply_to_message"):
        studio = _studio(agent)
        if (prop := studio.by_telegram_message(reply_to.get("message_id"))) and text:
            return _content_reply(prop, text, studio, agent)
        draft = agent.draft_by_telegram_message(reply_to.get("message_id"))
        if draft and text and agent.approve(draft.id, edited_text=text):
            _tasks().publish_draft.delay(draft.id)
            notifier.alert(f"#{draft.id}: texto editado aprovado, a publicar")
            return "edited"
        post = agent.post_by_telegram_message(reply_to.get("message_id"))
        if post and text and agent.approve_post(post.id, caption=text):
            notifier.alert(f"Post #{post.id}: legenda nova aprovada, sai à hora marcada")
            return "post_edited"
        return "ignored"

    if text.startswith("/pausa"):
        agent.limiter.pause()
        notifier.alert("Agente em pausa: nada automático será publicado.")
        return "paused"
    if text.startswith("/retoma"):
        agent.limiter.resume()
        notifier.alert("Agente retomado.")
        return "resumed"
    if text.startswith("/estado"):
        with agent.sf() as db:
            pending = db.scalars(select(Draft).where(Draft.status == "awaiting_approval")).all()
            posts = db.scalars(select(ScheduledPost).where(ScheduledPost.status == "pending")).all()
            to_approve = db.scalars(select(ScheduledPost).where(
                ScheduledPost.status == "awaiting_approval")).all()
        exp = agent.tokens.expires_at()
        token_line = f"Token expira: {exp:%Y-%m-%d}" if exp else "Token: expiração desconhecida"
        notifier.alert(
            f"Pausa: {'sim' if agent.limiter.is_paused() else 'não'} · modo: {s.autonomy_mode}\n"
            f"Rascunhos à espera: {len(pending)} · posts por aprovar: {len(to_approve)} · "
            f"posts agendados: {len(posts)}\n{token_line}")
        return "status"
    return "ignored"


# ------------------------------------------------------------ Conteúdo (ficheiro → legenda → hora)
def _studio(agent):
    from .content import ContentStudio
    return ContentStudio(agent)


def _content_media(msg: dict, agent) -> str:
    from .content import new_file_name
    notifier, studio = agent.notifier, _studio(agent)
    if photos := msg.get("photo"):
        f, mime, media_type = photos[-1], "image/jpeg", "IMAGE"  # a última é a maior resolução
    elif video := msg.get("video"):
        f, mime, media_type = video, video.get("mime_type", "video/mp4"), "REELS"
    else:
        f = msg["document"]
        mime = f.get("mime_type", "")
        media_type = "REELS" if mime.startswith("video/") else "IMAGE"
    name = new_file_name(mime)
    if not name:
        notifier.alert(f"Formato não suportado ({mime or 'desconhecido'}). Usa JPG, PNG, MP4 ou MOV.")
        return "unsupported"
    if (f.get("file_size") or 0) > 20 * 1024 * 1024:
        notifier.alert("Ficheiro acima de 20 MB: o Telegram não deixa o bot descarregá-lo. "
                       "Envia uma versão comprimida.")
        return "too_big"
    if not hasattr(notifier, "download_file"):
        return "no_telegram"
    notifier.download_file(f["file_id"], studio.media_dir / name)
    prop = studio.start(file_name=name, media_type=media_type, hint=(msg.get("caption") or "").strip())
    notifier.alert(f"Recebido (proposta #{prop.id}, {prop.media_type}). A preparar 3 legendas…")
    _tasks().propose_content.delay(prop.id)
    return "content_started"


def _content_callback(cb: dict, action: str, raw: str, agent) -> str:
    from datetime import timezone
    notifier, studio = agent.notifier, _studio(agent)
    pid_s, _, arg = raw.partition(":")
    pid = int(pid_s) if pid_s.isdigit() else 0
    result = "Já tratado ou inexistente"
    if action == "cap" and arg.isdigit() and (p := studio.choose_caption(pid, option=int(arg))):
        result = f"Legenda {int(arg) + 1} escolhida"
    elif action == "capnew" and studio.regenerate(pid):
        _tasks().propose_content.delay(pid)
        result = "A pensar noutras legendas…"
    elif action == "capx" and studio.discard(pid):
        result = "Cancelado"
    elif action == "when" and arg.isdigit():
        when = datetime.fromtimestamp(int(arg), tz=timezone.utc)
        current = studio.get(pid)
        if current and current.status == "choose_time" and when <= datetime.now(timezone.utc):
            result = "Essa hora já passou: responde à mensagem com outra hora"
        else:
            try:
                if p := studio.choose_time(pid, when):
                    result = f"Agendado (post #{p.post_id})"
            except ValueError as e:
                result = str(e)
    if hasattr(notifier, "answer_callback"):
        notifier.answer_callback(cb["id"], result)
    if msg_id := cb.get("message", {}).get("message_id"):
        notifier.edit_status(msg_id, f"Proposta #{pid}: {result}")
    return result


def _content_reply(prop, text: str, studio, agent) -> str:
    from .content import parse_when
    notifier = agent.notifier
    if prop.status == "choose_caption" and studio.choose_caption(prop.id, text=text):
        notifier.edit_status(prop.telegram_message_id, f"Proposta #{prop.id}: legenda tua escolhida")
        return "content_caption"
    if prop.status == "choose_time":
        when = parse_when(text)
        if not when:
            notifier.alert("Não percebi a hora. Usa HH:MM, DD/MM HH:MM ou agora (hora de Lisboa, no futuro).")
            return "bad_time"
        try:
            p = studio.choose_time(prop.id, when)
        except ValueError as e:
            notifier.alert(str(e))
            return "error"
        if p:
            notifier.edit_status(prop.telegram_message_id, f"Proposta #{prop.id}: agendado (post #{p.post_id})")
            return "content_scheduled"
    return "ignored"


# ------------------------------------------------------------ Administração
class DecisionIn(BaseModel):
    action: str = Field(examples=["approve"],
                        description="rascunhos: approve | discard | hide; posts: approve | reject")
    edited_text: str | None = None


class PostIn(BaseModel):
    media_type: str = Field(examples=["REELS"])
    media_urls: list[str]
    caption: str = ""
    publish_at: datetime = Field(description="ISO 8601 com fuso, ex.: 2026-11-03T18:30:00+00:00")


@app.post("/posts", dependencies=[Depends(require_admin)])
def create_post(body: PostIn):
    try:
        p = get_agent().schedule_post(**body.model_dump())
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return {"id": p.id, "status": p.status, "publish_at": p.publish_at}


@app.get("/posts", dependencies=[Depends(require_admin)])
def list_posts(status: str | None = None):
    with get_agent().sf() as db:
        q = select(ScheduledPost).order_by(ScheduledPost.publish_at.desc()).limit(100)
        if status:
            q = q.where(ScheduledPost.status == status)
        return [{"id": p.id, "media_type": p.media_type, "caption": p.caption[:80],
                 "publish_at": p.publish_at, "status": p.status, "error": p.error,
                 "ig_media_id": p.ig_media_id} for p in db.scalars(q)]


@app.delete("/posts/{post_id}", dependencies=[Depends(require_admin)])
def cancel_post(post_id: int):
    with get_agent().sf() as db:
        p = db.get(ScheduledPost, post_id)
        if not p or p.status not in ("pending", "awaiting_approval"):
            raise HTTPException(409, "só posts pendentes podem ser cancelados")
        p.status = "cancelled"
        db.commit()
    return {"id": post_id, "status": "cancelled"}


@app.post("/posts/{post_id}/decision", dependencies=[Depends(require_admin)])
def decide_post(post_id: int, body: DecisionIn):
    """Aprovar (com legenda editada, opcional) ou rejeitar um post agendado."""
    agent = get_agent()
    if body.action == "approve":
        p, status = agent.approve_post(post_id, caption=body.edited_text), "pending"
    elif body.action == "reject":
        p, status = agent.reject_post(post_id), "rejected"
    else:
        raise HTTPException(422, "action deve ser approve ou reject")
    if not p:
        raise HTTPException(409, "já tratado, inexistente ou legenda inválida")
    if p.telegram_message_id:
        word = "aprovado" if status == "pending" else "rejeitado"
        agent.notifier.edit_status(p.telegram_message_id, f"Post #{post_id}: {word} (painel)")
    return {"id": post_id, "status": status}


@app.get("/drafts", dependencies=[Depends(require_admin)])
def list_drafts(status: str | None = None):
    with get_agent().sf() as db:
        q = select(Draft).order_by(Draft.created_at.desc()).limit(100)
        if status:
            q = q.where(Draft.status == status)
        return [{"id": d.id, "kind": d.kind, "username": d.username, "intent": d.intent,
                 "source": d.source_text, "reply": d.reply_text, "status": d.status,
                 "problems": d.problems, "decided_by": d.decided_by,
                 "created_at": d.created_at} for d in db.scalars(q)]


@app.post("/drafts/{draft_id}/decision", dependencies=[Depends(require_admin)])
def decide_draft(draft_id: int, body: DecisionIn):
    """O mesmo que os botões do Telegram, para o painel."""
    agent = get_agent()
    if body.action == "approve":
        d = agent.approve(draft_id, edited_text=body.edited_text)
        status = "approved"
    elif body.action == "discard":
        d, status = agent.discard(draft_id), "discarded"
    elif body.action == "hide":
        d, status = agent.hide(draft_id), "hidden"
    else:
        raise HTTPException(422, "action deve ser approve, discard ou hide")
    if not d:
        raise HTTPException(409, "já tratado ou inexistente")
    if status == "approved":
        _tasks().publish_draft.delay(draft_id)
    if d.telegram_message_id:
        agent.notifier.edit_status(d.telegram_message_id, f"#{draft_id}: {status} (painel)")
    return {"id": draft_id, "status": status}


@app.get("/admin/status", dependencies=[Depends(require_admin)])
def status():
    s, agent = get_settings(), get_agent()
    try:
        paused, redis_ok = agent.limiter.is_paused(), True
    except Exception:  # Redis em baixo: o painel continua a abrir
        paused, redis_ok = None, False
    with agent.sf() as db:
        pending = len(db.scalars(select(Draft.id).where(Draft.status == "awaiting_approval")).all())
        posts = len(db.scalars(select(ScheduledPost.id).where(ScheduledPost.status == "pending")).all())
        to_approve = len(db.scalars(select(ScheduledPost.id).where(
            ScheduledPost.status == "awaiting_approval")).all())
    return {"paused": paused, "redis": redis_ok, "autonomy_mode": s.autonomy_mode,
            "dry_run": s.dry_run, "post_approval": s.post_approval,
            "drafts_pending": pending, "posts_pending": posts, "posts_awaiting": to_approve,
            "token_expires_at": agent.tokens.expires_at(),
            "configured": {"instagram": bool(s.ig_user_id), "anthropic": bool(s.anthropic_api_key),
                           "telegram": bool(s.telegram_bot_token and s.telegram_chat_id)}}


class SimulateIn(BaseModel):
    kind: str = Field(examples=["comment"], description="comment | dm")
    text: str
    username: str = "cliente_teste"


@app.post("/dev/simulate", dependencies=[Depends(require_admin)])
def simulate(body: SimulateIn):
    """Só com DRY_RUN: faz entrar um comentário/DM falso pelo fluxo real (LLM, guardrails, Telegram)."""
    if not get_settings().dry_run:
        raise HTTPException(403, "só disponível com DRY_RUN=true")
    agent, uid = get_agent(), f"sim-{secrets.token_hex(4)}"
    if body.kind == "comment":
        d = agent.handle_comment(comment_id=uid, text=body.text, username=body.username,
                                 author_id=f"u-{body.username}", media_id="sim-media")
    elif body.kind == "dm":
        d = agent.handle_dm(igsid=f"u-{body.username}", text=body.text, mid=uid)
    else:
        raise HTTPException(422, "kind deve ser comment ou dm")
    if d.status == "approved":  # decisão automática do agente
        _tasks().publish_draft.delay(d.id)
    return {"id": d.id, "status": d.status, "intent": d.intent, "reply": d.reply_text,
            "problems": d.problems}


@app.get("/media/{name}", include_in_schema=False)
def media(name: str):
    """Ficheiros dos posts: a Meta descarrega-os daqui no momento de publicar."""
    from .content import SAFE_NAME
    path = Path(get_settings().media_dir) / name
    if not SAFE_NAME.match(name) or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path)


PANEL = Path(__file__).parent / "static" / "painel.html"


@app.get("/", include_in_schema=False)
def panel():
    return FileResponse(PANEL)


@app.post("/admin/pause", dependencies=[Depends(require_admin)])
def pause():
    get_agent().limiter.pause()
    return {"paused": True}


@app.post("/admin/resume", dependencies=[Depends(require_admin)])
def resume():
    get_agent().limiter.resume()
    return {"paused": False}
