"""Lógica do agente, independente do Celery/FastAPI (fácil de testar).

As tarefas Celery (tasks.py) e a API (main.py) são camadas finas por cima disto.
"""
from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from . import guardrails
from .brain import Brain
from .config import Settings
from .crypto import decrypt, encrypt
from .db import Credential, Draft, Message, ProcessedEvent, ScheduledPost, recent_replies, utcnow
from .ig import IGError, InstagramClient
from .policy import APPROVAL, AUTO, HIDE, IGNORE, decide
from .ratelimit import Limiter
from .telegram import Notifier

log = logging.getLogger(__name__)
TOKEN_NAME = "ig_access_token"
VALID_MEDIA = {"IMAGE", "REELS", "STORIES", "CAROUSEL"}


class Deferred(Exception):
    """Não é erro: tentar outra vez daqui a `countdown` segundos."""

    def __init__(self, reason: str, countdown: int):
        super().__init__(reason)
        self.countdown = countdown


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:  # SQLite devolve datas sem fuso
        return dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------- tokens
class TokenStore:
    def __init__(self, session_factory, settings: Settings):
        self.sf, self.s = session_factory, settings

    def get(self) -> str:
        with self.sf() as s:
            cred = s.get(Credential, TOKEN_NAME)
            if cred:
                return decrypt(cred.value_enc, self.s.encryption_key)
        if self.s.ig_access_token:
            return self.s.ig_access_token
        raise RuntimeError("Sem token do Instagram: corre scripts/bootstrap.py")

    def save(self, token: str, expires_in_s: int | None) -> None:
        with self.sf() as s:
            cred = s.get(Credential, TOKEN_NAME) or Credential(name=TOKEN_NAME)
            cred.value_enc = encrypt(token, self.s.encryption_key)
            cred.expires_at = utcnow() + timedelta(seconds=expires_in_s) if expires_in_s else None
            s.add(cred)
            s.commit()

    def expires_at(self) -> datetime | None:
        with self.sf() as s:
            cred = s.get(Credential, TOKEN_NAME)
            return _aware(cred.expires_at) if cred else None


# ---------------------------------------------------------------- agente
class Agent:
    def __init__(self, settings: Settings, session_factory, ig: InstagramClient, brain: Brain,
                 limiter: Limiter, notifier: Notifier, rng: random.Random | None = None):
        self.s, self.sf, self.ig, self.brain = settings, session_factory, ig, brain
        self.limiter, self.notifier = limiter, notifier
        self.rng = rng or random.Random()
        self.tokens = TokenStore(session_factory, settings)

    # ============ webhooks ============
    def _first_time(self, session, key: str, kind: str) -> bool:
        if session.get(ProcessedEvent, key):
            return False
        session.add(ProcessedEvent(key=key, kind=kind))
        return True

    def ingest_webhook(self, payload: dict) -> list[tuple[str, dict]]:
        """Transforma o payload da Meta em trabalhos ('comment' | 'dm' | 'mention', dados).

        Deduplica por ID, ignora ações da própria conta e regista mensagens enviadas por humanos
        a partir da app (echo) para que o histórico fique completo.
        """
        jobs: list[tuple[str, dict]] = []
        me = self.s.ig_user_id
        with self.sf() as s:
            for entry in payload.get("entry", []):
                for change in entry.get("changes", []):
                    field, v = change.get("field"), change.get("value", {})
                    if field in ("comments", "live_comments"):
                        author = v.get("from", {})
                        if not v.get("id") or author.get("id") == me:
                            continue
                        if self._first_time(s, f"comment:{v['id']}", "comment"):
                            jobs.append(("comment", {
                                "comment_id": v["id"], "text": v.get("text", ""),
                                "username": author.get("username"), "author_id": author.get("id"),
                                "media_id": v.get("media", {}).get("id"),
                            }))
                    elif field == "mentions":
                        key = f"mention:{v.get('comment_id') or v.get('media_id')}"
                        if self._first_time(s, key, "mention"):
                            jobs.append(("mention", v))

                for m in entry.get("messaging", []):
                    msg = m.get("message")
                    if not msg or not msg.get("mid"):
                        continue  # reações, leituras, etc.
                    if not self._first_time(s, f"msg:{msg['mid']}", "dm"):
                        continue
                    if msg.get("is_echo"):
                        # Enviada pela conta: se não fomos nós (agente), foi um humano na app.
                        s.add(Message(igsid=m.get("recipient", {}).get("id", ""), direction="out",
                                      text=msg.get("text", ""), automated=False))
                        continue
                    sender = m.get("sender", {}).get("id")
                    if sender and sender != me and msg.get("text"):
                        jobs.append(("dm", {"igsid": sender, "text": msg["text"], "mid": msg["mid"]}))
            s.commit()
        return jobs

    # ============ comentários ============
    def handle_comment(self, *, comment_id: str, text: str, username: str | None,
                       author_id: str | None, media_id: str | None) -> Draft:
        c = self.brain.classify(text, "comment")
        action = decide(c, "comment", self.s.autonomy_mode)
        draft = Draft(kind="comment", target_id=comment_id, media_id=media_id, author_id=author_id,
                      username=username, source_text=text, intent=c.intent,
                      classification=c.__dict__, action=action, problems=[])

        if action == HIDE:
            try:
                self.ig.hide_comment(comment_id)
                draft.status, draft.decided_by = "hidden", "agent"
            except IGError as e:
                draft.status, draft.error = "failed", str(e)
            return self._save(draft)
        if action == IGNORE:
            draft.status = "ignored"
            return self._save(draft)

        with self.sf() as s:
            already = s.scalar(select(func.count(Draft.id)).where(
                Draft.kind == "comment", Draft.media_id == media_id, Draft.author_id == author_id,
                Draft.status == "published"))
        if already:
            draft.problems.append("já respondemos a esta pessoa neste post")

        caption = None
        if media_id:
            try:
                caption = self.ig.get_media(media_id).get("caption")
            except IGError:
                log.warning("não consegui ler a legenda de %s", media_id)

        draft.reply_text = self.brain.draft(kind="comment", text=text, username=username, caption=caption)
        return self._finish(draft, "comment")

    # ============ DMs ============
    def handle_dm(self, *, igsid: str, text: str, mid: str | None = None) -> Draft:
        with self.sf() as s:
            s.add(Message(igsid=igsid, direction="in", text=text))
            s.commit()
            rows = s.execute(select(Message.direction, Message.text).where(Message.igsid == igsid)
                             .order_by(Message.created_at.desc(), Message.id.desc()).limit(11)).all()
            history = [(d, t) for d, t in reversed(rows)][:-1]  # sem a mensagem atual
            automated_total = s.scalar(select(func.count(Message.id)).where(
                Message.igsid == igsid, Message.direction == "out", Message.automated.is_(True)))
            since = utcnow() - timedelta(hours=24)
            auto_turns = s.scalar(select(func.count(Message.id)).where(
                Message.igsid == igsid, Message.direction == "out", Message.automated.is_(True),
                Message.created_at >= since))

        c = self.brain.classify(text, "dm")
        action = decide(c, "dm", self.s.autonomy_mode, dm_auto_turns=auto_turns or 0,
                        max_auto_dm_turns=self.s.max_auto_dm_turns)
        draft = Draft(kind="dm", target_id=igsid, source_text=text, intent=c.intent,
                      classification=c.__dict__, action=action, problems=[])
        if action in (IGNORE, HIDE):
            draft.status = "ignored"
            return self._save(draft)

        draft.reply_text = self.brain.draft(kind="dm", text=text, username=None, history=history,
                                            first_automated_dm=not automated_total)
        return self._finish(draft, "dm")

    # ============ comum ============
    def _finish(self, draft: Draft, kind: str) -> Draft:
        with self.sf() as s:
            recent = recent_replies(s)
        draft.problems = list(draft.problems or []) + guardrails.check(
            draft.reply_text or "", kind, allowed_domains=self.s.allowed_domain_list,
            knowledge_text=self.brain.knowledge, recent=recent)

        if draft.action == AUTO and not draft.problems:
            v = self.brain.verify(source=draft.source_text, reply=draft.reply_text or "")
            if not v.ok:
                draft.problems.append(f"verificação: {v.reason}")
        if draft.problems:
            draft.action = APPROVAL

        if draft.action == AUTO:
            draft.status, draft.decided_by = "approved", "agent"
            return self._save(draft)

        draft.status = "awaiting_approval"
        draft = self._save(draft)
        try:
            msg_id = self.notifier.send_draft(draft)
        except Exception:
            log.exception("falha a enviar rascunho para aprovação")
            msg_id = None
        if msg_id:
            with self.sf() as s:
                d = s.get(Draft, draft.id)
                d.telegram_message_id = msg_id
                s.commit()
            draft.telegram_message_id = msg_id
        return draft

    def _save(self, draft: Draft) -> Draft:
        with self.sf() as s:
            s.add(draft)
            s.commit()
            s.refresh(draft)
        return draft

    def reply_delay(self) -> int:
        return self.rng.randint(self.s.reply_delay_min_s, self.s.reply_delay_max_s)

    # ============ decisões humanas ============
    def approve(self, draft_id: int, edited_text: str | None = None) -> Draft | None:
        with self.sf() as s:
            d = s.get(Draft, draft_id)
            if not d or d.status != "awaiting_approval":
                return None
            if edited_text:
                d.reply_text = edited_text.strip()
            d.status, d.decided_by = "approved", "human"
            s.commit()
            return d

    def discard(self, draft_id: int) -> Draft | None:
        with self.sf() as s:
            d = s.get(Draft, draft_id)
            if not d or d.status != "awaiting_approval":
                return None
            d.status, d.decided_by = "discarded", "human"
            s.commit()
            return d

    def hide(self, draft_id: int) -> Draft | None:
        with self.sf() as s:
            d = s.get(Draft, draft_id)
            if not d or d.kind != "comment" or d.status != "awaiting_approval":
                return None
            self.ig.hide_comment(d.target_id)
            d.status, d.decided_by = "hidden", "human"
            s.commit()
            return d

    def draft_by_telegram_message(self, message_id: int) -> Draft | None:
        with self.sf() as s:
            return s.scalar(select(Draft).where(Draft.telegram_message_id == message_id))

    # ============ publicar respostas ============
    def publish_draft(self, draft_id: int) -> str:
        with self.sf() as s:
            d = s.get(Draft, draft_id)
            if not d or d.status != "approved":
                return d.status if d else "missing"
            if d.decided_by == "agent" and self.limiter.is_paused():
                raise Deferred("agente em pausa", 900)

            if d.kind == "comment":
                if not self.limiter.allow("comment_reply", self.s.max_comment_replies_per_hour, 3600):
                    raise Deferred("limite horário de respostas", 600)
            else:
                if not self.limiter.allow("dm", self.s.max_dms_per_hour, 3600):
                    raise Deferred("limite horário de DMs", 600)
                last_in = s.scalar(select(func.max(Message.created_at)).where(
                    Message.igsid == d.target_id, Message.direction == "in"))
                if not last_in or _aware(last_in) < utcnow() - timedelta(hours=24):
                    d.status, d.error = "failed", "fora da janela de 24 h"
                    s.commit()
                    self.notifier.alert(f"DM #{d.id} não enviada: fora da janela de 24 h")
                    return d.status

            try:
                if d.kind == "comment":
                    self.ig.reply_comment(d.target_id, d.reply_text)
                else:
                    self.ig.send_message(d.target_id, d.reply_text)
                    s.add(Message(igsid=d.target_id, direction="out", text=d.reply_text,
                                  automated=d.decided_by == "agent"))
            except IGError as e:
                if e.transient:
                    raise Deferred(str(e), 300) from e
                d.status, d.error = "failed", str(e)
                s.commit()
                self.notifier.alert(f"Falha a publicar rascunho #{d.id}: {e}")
                return d.status

            d.status, d.published_at = "published", utcnow()
            s.commit()
            return d.status

    # ============ posts agendados ============
    def schedule_post(self, *, media_type: str, media_urls: list[str], caption: str,
                      publish_at: datetime, approved: bool = False) -> ScheduledPost:
        """approved=True: já foi decidido por ti (ex.: escolhido no Telegram), não pede outra aprovação."""
        media_type = media_type.upper()
        if media_type not in VALID_MEDIA:
            raise ValueError(f"media_type deve ser um de {sorted(VALID_MEDIA)}")
        if media_type == "CAROUSEL" and not 2 <= len(media_urls) <= 10:
            raise ValueError("um carrossel precisa de 2 a 10 ficheiros")
        if media_type != "CAROUSEL" and len(media_urls) != 1:
            raise ValueError("este tipo de post leva exatamente 1 ficheiro")
        if any(not u.startswith("https://") for u in media_urls):
            raise ValueError("os ficheiros têm de estar num URL https público")
        if len(caption) > 2200:
            raise ValueError("legenda acima de 2200 caracteres")
        if publish_at.tzinfo is None:
            raise ValueError("publish_at precisa de fuso horário")
        post = ScheduledPost(media_type=media_type, media_urls=media_urls, caption=caption,
                             publish_at=publish_at.astimezone(timezone.utc),
                             status="awaiting_approval" if self.s.post_approval and not approved else "pending")
        with self.sf() as s:
            s.add(post)
            s.commit()
            s.refresh(post)
        if post.status == "awaiting_approval":
            try:
                msg_id = self.notifier.send_post(post)
            except Exception:
                log.exception("falha a enviar post para aprovação")
                msg_id = None
            if msg_id:
                with self.sf() as s:
                    s.get(ScheduledPost, post.id).telegram_message_id = msg_id
                    s.commit()
                post.telegram_message_id = msg_id
        return post

    def approve_post(self, post_id: int, caption: str | None = None) -> ScheduledPost | None:
        with self.sf() as s:
            p = s.get(ScheduledPost, post_id)
            if not p or p.status != "awaiting_approval":
                return None
            if caption:
                if len(caption) > 2200:
                    return None
                p.caption = caption.strip()
            p.status = "pending"
            s.commit()
            return p

    def reject_post(self, post_id: int) -> ScheduledPost | None:
        with self.sf() as s:
            p = s.get(ScheduledPost, post_id)
            if not p or p.status != "awaiting_approval":
                return None
            p.status = "rejected"
            s.commit()
            return p

    def post_by_telegram_message(self, message_id: int) -> ScheduledPost | None:
        with self.sf() as s:
            return s.scalar(select(ScheduledPost).where(ScheduledPost.telegram_message_id == message_id))

    def claim_due_posts(self, now: datetime | None = None) -> list[int]:
        now = now or utcnow()
        with self.sf() as s:
            # chegou a hora sem aprovação: não sai, e avisamos
            for p in s.scalars(select(ScheduledPost).where(
                    ScheduledPost.status == "awaiting_approval", ScheduledPost.publish_at <= now)):
                p.status, p.error = "expired", "não foi aprovado a tempo"
                self.notifier.alert(f"Post #{p.id} não foi publicado: chegou a hora sem aprovação. "
                                    "Agenda-o de novo se ainda o quiseres.")
            s.commit()
            posts = s.scalars(select(ScheduledPost).where(
                ScheduledPost.status == "pending", ScheduledPost.publish_at <= now)
                .with_for_update(skip_locked=True)).all()
            for p in posts:
                p.status = "publishing"
            s.commit()
            return [p.id for p in posts]

    def publish_post(self, post_id: int) -> str:
        with self.sf() as s:
            p = s.get(ScheduledPost, post_id)
            if not p or p.status != "publishing":
                return p.status if p else "missing"

            published_24h = s.scalar(select(func.count(ScheduledPost.id)).where(
                ScheduledPost.status == "published",
                ScheduledPost.published_at >= utcnow() - timedelta(hours=24)))
            if published_24h >= self.s.max_posts_per_day:
                return self._postpone(s, p, 3600, f"limite próprio de {self.s.max_posts_per_day} posts/24 h")

            try:
                quota = self.ig.publishing_limit()
                total = (quota.get("config") or {}).get("quota_total")
                if total and quota.get("quota_usage", 0) >= total:
                    return self._postpone(s, p, 3600, "quota de publicação da Meta esgotada")

                urls = p.media_urls
                if p.media_type == "CAROUSEL":
                    children = []
                    for u in urls:
                        kind = "VIDEO" if u.lower().split("?")[0].endswith((".mp4", ".mov")) else "IMAGE"
                        cid = self.ig.create_container(media_type=kind, url=u, is_carousel_item=True)
                        self.ig.wait_until_ready(cid, timeout_s=600)
                        children.append(cid)
                    container = self.ig.create_container(media_type="CAROUSEL", children=children,
                                                         caption=p.caption)
                else:
                    container = self.ig.create_container(media_type=p.media_type, url=urls[0],
                                                         caption=p.caption)
                self.ig.wait_until_ready(container, timeout_s=600)
                p.ig_media_id = self.ig.publish(container)
            except IGError as e:
                p.attempts += 1
                if e.transient and p.attempts < 3:
                    return self._postpone(s, p, 120 * 2 ** p.attempts, f"erro temporário: {e}", alert=False)
                p.status, p.error = "failed", str(e)
                s.commit()
                self.notifier.alert(f"Post #{p.id} falhou: {e}")
                return p.status

            p.status, p.published_at, p.error = "published", utcnow(), None
            s.commit()
            return p.status

    def _postpone(self, s, p: ScheduledPost, seconds: int, reason: str, alert: bool = True) -> str:
        p.status, p.error = "pending", reason
        p.publish_at = utcnow() + timedelta(seconds=seconds)
        s.commit()
        if alert:
            self.notifier.alert(f"Post #{p.id} adiado {seconds // 60} min: {reason}")
        return "postponed"

    # ============ manutenção ============
    def refresh_token(self) -> datetime | None:
        token, expires_in = self.ig.refresh_token()
        self.tokens.save(token, expires_in)
        return self.tokens.expires_at()
