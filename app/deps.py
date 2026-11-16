"""Constrói o Agent real a partir da configuração (um por processo)."""
from __future__ import annotations

from functools import lru_cache

import redis
from anthropic import Anthropic

from .agent import Agent, TokenStore
from .brain import Brain
from .config import get_settings
from .db import make_session_factory
from .ig import InstagramClient
from .ratelimit import Limiter
from .telegram import Notifier, TelegramNotifier


@lru_cache
def get_agent() -> Agent:
    s = get_settings()
    sf = make_session_factory(s.database_url)
    tokens = TokenStore(sf, s)
    if s.dry_run:
        from .dev import FakeInstagramClient, OfflineLLM
        ig = FakeInstagramClient()
        llm = Anthropic(api_key=s.anthropic_api_key) if s.anthropic_api_key else OfflineLLM()
    else:
        ig = InstagramClient(tokens.get, s.ig_user_id, s.ig_api_version)
        llm = Anthropic(api_key=s.anthropic_api_key or None)
    brain = Brain(llm, model_classify=s.model_classify, model_draft=s.model_draft,
                  knowledge_dir=s.knowledge_dir)
    limiter = Limiter(redis.Redis.from_url(s.redis_url))
    notifier: Notifier = (TelegramNotifier(s.telegram_bot_token, s.telegram_chat_id)
                          if s.telegram_bot_token and s.telegram_chat_id else Notifier())
    return Agent(s, sf, ig, brain, limiter, notifier)
