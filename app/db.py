"""Modelos e sessão SQLAlchemy."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Integer, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Credential(Base):
    """Segredos cifrados (ex.: token de acesso do Instagram)."""

    __tablename__ = "credentials"
    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    value_enc: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ScheduledPost(Base):
    __tablename__ = "scheduled_posts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    media_type: Mapped[str] = mapped_column(String(16))  # IMAGE | REELS | STORIES | CAROUSEL
    media_urls: Mapped[list] = mapped_column(JSON)  # 1 URL, ou 2–10 no carrossel
    caption: Mapped[str] = mapped_column(Text, default="")
    publish_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    # awaiting_approval | pending | publishing | published | failed | cancelled | rejected | expired
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    ig_media_id: Mapped[str | None] = mapped_column(String(64))
    telegram_message_id: Mapped[int | None] = mapped_column(Integer, index=True)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProcessedEvent(Base):
    """Idempotência: a Meta reenvia webhooks."""

    __tablename__ = "processed_events"
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Message(Base):
    """Histórico de DMs (para contexto e para a janela de 24 h)."""

    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    igsid: Mapped[str] = mapped_column(String(64), index=True)
    direction: Mapped[str] = mapped_column(String(3))  # in | out
    text: Mapped[str] = mapped_column(Text, default="")
    automated: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Draft(Base):
    """Uma resposta proposta pelo agente, com todo o rasto de decisão (auditoria)."""

    __tablename__ = "drafts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(8))  # comment | dm
    target_id: Mapped[str] = mapped_column(String(64), index=True)  # comment_id ou IGSID
    media_id: Mapped[str | None] = mapped_column(String(64))
    author_id: Mapped[str | None] = mapped_column(String(64))
    username: Mapped[str | None] = mapped_column(String(64))
    source_text: Mapped[str] = mapped_column(Text)
    intent: Mapped[str | None] = mapped_column(String(32))
    classification: Mapped[dict | None] = mapped_column(JSON)
    action: Mapped[str | None] = mapped_column(String(16))  # auto | approval | hide | ignore
    reply_text: Mapped[str | None] = mapped_column(Text)
    problems: Mapped[list | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), default="new", index=True)
    # new | awaiting_approval | approved | published | discarded | hidden | ignored | failed
    decided_by: Mapped[str | None] = mapped_column(String(32))  # agent | human
    telegram_message_id: Mapped[int | None] = mapped_column(Integer, index=True)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


def make_session_factory(database_url: str) -> sessionmaker:
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    engine = create_engine(database_url, connect_args=connect_args, pool_pre_ping=True)
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def recent_replies(session, limit: int = 50) -> list[str]:
    rows = session.scalars(
        select(Draft.reply_text)
        .where(Draft.status == "published", Draft.reply_text.is_not(None))
        .order_by(Draft.published_at.desc())
        .limit(limit)
    )
    return [r for r in rows if r]


class ContentProposal(Base):
    """Um ficheiro enviado pelo Telegram, as legendas propostas e as escolhas até virar post."""

    __tablename__ = "content_proposals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    media_type: Mapped[str] = mapped_column(String(16))  # IMAGE | REELS | STORIES
    file_name: Mapped[str] = mapped_column(String(64))  # em MEDIA_DIR, servido em /media/<nome>
    hint: Mapped[str] = mapped_column(Text, default="")  # o que escreveste junto ao ficheiro
    options: Mapped[list | None] = mapped_column(JSON)  # [{angle, caption}]
    caption: Mapped[str | None] = mapped_column(Text)  # a legenda escolhida
    status: Mapped[str] = mapped_column(String(20), default="proposing", index=True)
    # proposing | choose_caption | choose_time | scheduled | discarded | failed
    telegram_message_id: Mapped[int | None] = mapped_column(Integer, index=True)
    post_id: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
