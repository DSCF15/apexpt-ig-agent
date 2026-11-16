"""Publicar a partir do Telegram: ficheiro → 3 legendas → escolha → hora → post agendado.

Nada é publicado sem duas escolhas tuas (legenda e hora). O post resultante já conta como
aprovado, por isso não volta a pedir aprovação.
"""
from __future__ import annotations

import logging
import re
import secrets
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .db import ContentProposal, utcnow

log = logging.getLogger(__name__)
LISBON = ZoneInfo("Europe/Lisbon")
# Horas por omissão até haver dados da conta (a análise de insights vai afiná-las)
DEFAULT_SLOTS = (time(12, 30), time(18, 30), time(21, 0))
EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png", "video/mp4": ".mp4", "video/quicktime": ".mov"}
SAFE_NAME = re.compile(r"^[a-f0-9]{32}\.(jpg|png|mp4|mov)$")


def new_file_name(mime: str) -> str | None:
    ext = EXTENSIONS.get(mime)
    return f"{secrets.token_hex(16)}{ext}" if ext else None


def suggest_slots(now: datetime | None = None, n: int = 3) -> list[datetime]:
    """Próximas n horas boas (Lisboa), pelo menos 20 min no futuro. Devolve em UTC."""
    now = (now or utcnow()).astimezone(LISBON)
    out: list[datetime] = []
    day = now.date()
    while len(out) < n:
        for t in DEFAULT_SLOTS:
            dt = datetime.combine(day, t, tzinfo=LISBON)
            if dt >= now + timedelta(minutes=20) and len(out) < n:
                out.append(dt.astimezone(timezone.utc))
        day += timedelta(days=1)
    return out


def parse_when(text: str, now: datetime | None = None) -> datetime | None:
    """'agora' | 'HH:MM' (hoje, ou amanhã se já passou) | 'DD/MM HH:MM' (hora de Lisboa) → UTC."""
    now = (now or utcnow()).astimezone(LISBON)
    text = text.strip().lower()
    if text == "agora":
        return (now + timedelta(minutes=2)).astimezone(timezone.utc)
    m = re.fullmatch(r"(?:(\d{1,2})/(\d{1,2})(?:/(\d{4}))?\s+)?(\d{1,2})[:h](\d{2})", text)
    if not m:
        return None
    d, mo, y, hh, mm = m.groups()
    try:
        if d:
            dt = datetime(int(y or now.year), int(mo), int(d), int(hh), int(mm), tzinfo=LISBON)
            if not y and dt < now - timedelta(days=1):  # "05/01" em dezembro → ano seguinte
                dt = dt.replace(year=dt.year + 1)
        else:
            dt = datetime.combine(now.date(), time(int(hh), int(mm)), tzinfo=LISBON)
            if dt <= now:
                dt += timedelta(days=1)
    except ValueError:
        return None
    return dt.astimezone(timezone.utc) if dt > now else None


class ContentStudio:
    def __init__(self, agent):
        self.agent, self.s, self.sf = agent, agent.s, agent.sf

    @property
    def media_dir(self) -> Path:
        p = Path(self.s.media_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p

    def public_url(self, file_name: str) -> str:
        base = self.s.public_base_url.rstrip("/")
        if not base:
            if self.s.dry_run:
                return f"https://exemplo.invalid/media/{file_name}"
            raise ValueError("falta PUBLIC_BASE_URL no .env (ex.: https://agente.apexpt.app)")
        return f"{base}/media/{file_name}"

    def get(self, proposal_id: int) -> ContentProposal | None:
        with self.sf() as s:
            return s.get(ContentProposal, proposal_id)

    def by_telegram_message(self, message_id: int) -> ContentProposal | None:
        with self.sf() as s:
            return s.query(ContentProposal).filter_by(telegram_message_id=message_id).first()

    def _update(self, proposal_id: int, **fields) -> ContentProposal:
        with self.sf() as s:
            p = s.get(ContentProposal, proposal_id)
            for k, v in fields.items():
                setattr(p, k, v)
            s.commit()
            return p

    # 1) chegou um ficheiro
    def start(self, *, file_name: str, media_type: str, hint: str) -> ContentProposal:
        if "#story" in hint.lower():
            media_type, hint = "STORIES", re.sub(r"(?i)#story", "", hint).strip()
        with self.sf() as s:
            p = ContentProposal(media_type=media_type, file_name=file_name, hint=hint[:1000])
            s.add(p)
            s.commit()
            s.refresh(p)
        return p

    # 2) o Claude propõe (corre no worker)
    def generate(self, proposal_id: int) -> ContentProposal:
        p = self.get(proposal_id)
        if not p or p.status not in ("proposing", "choose_caption"):
            return p
        try:
            options = self.agent.brain.propose_captions(media_type=p.media_type, hint=p.hint)
        except Exception as e:
            log.exception("falha a propor legendas")
            self.agent.notifier.alert(f"Proposta #{p.id}: não consegui criar legendas ({e}). "
                                      "Responde à mensagem do ficheiro com a tua legenda ou envia-o de novo.")
            return self._update(p.id, status="failed", error=str(e))
        p = self._update(p.id, options=options, status="choose_caption")
        msg_id = self.agent.notifier.send_caption_options(p)
        return self._update(p.id, telegram_message_id=msg_id) if msg_id else p

    # 3) escolheste a legenda (número da opção ou texto teu)
    def choose_caption(self, proposal_id: int, *, option: int | None = None,
                       text: str | None = None) -> ContentProposal | None:
        p = self.get(proposal_id)
        if not p or p.status != "choose_caption":
            return None
        if text:
            caption = text.strip()
        elif option is not None and p.options and 0 <= option < len(p.options):
            caption = p.options[option]["caption"]
        else:
            return None
        if len(caption) > 2200:
            return None
        p = self._update(p.id, caption=caption, status="choose_time")
        msg_id = self.agent.notifier.send_time_options(p, suggest_slots())
        return self._update(p.id, telegram_message_id=msg_id) if msg_id else p

    def regenerate(self, proposal_id: int) -> ContentProposal | None:
        p = self.get(proposal_id)
        if not p or p.status != "choose_caption":
            return None
        return self._update(p.id, status="proposing")

    # 4) escolheste a hora → post agendado (já aprovado)
    def choose_time(self, proposal_id: int, when: datetime) -> ContentProposal | None:
        p = self.get(proposal_id)
        if not p or p.status != "choose_time" or when <= utcnow():
            return None
        post = self.agent.schedule_post(media_type=p.media_type, media_urls=[self.public_url(p.file_name)],
                                        caption=p.caption or "", publish_at=when, approved=True)
        return self._update(p.id, status="scheduled", post_id=post.id)

    def discard(self, proposal_id: int) -> ContentProposal | None:
        p = self.get(proposal_id)
        if not p or p.status in ("scheduled", "discarded"):
            return None
        (self.media_dir / p.file_name).unlink(missing_ok=True)
        return self._update(p.id, status="discarded")
