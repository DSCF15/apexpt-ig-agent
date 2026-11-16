"""Configuração lida do ambiente / ficheiro .env."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Instagram (API with Instagram Login)
    ig_user_id: str = ""
    ig_access_token: str = ""  # só para o bootstrap; depois vive cifrado na BD
    ig_app_secret: str = ""
    ig_api_version: str = "v25.0"
    webhook_verify_token: str = ""

    # LLM
    anthropic_api_key: str = ""
    model_classify: str = "claude-haiku-5-5"
    model_draft: str = "claude-sonnet-5-5"

    # Infra
    database_url: str = "sqlite:///./agent.db"
    redis_url: str = "redis://localhost:6379/0"
    encryption_key: str = ""  # 32 bytes base64 (scripts/gen_key.py)
    admin_api_key: str = ""

    # Telegram (aprovação humana + alertas)
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_webhook_secret: str = ""

    # Comportamento
    autonomy_mode: str = "approval"  # "approval" = tudo passa por humano; "auto" = segue a política
    post_approval: bool = True  # posts agendados só saem depois de aprovados no Telegram/painel
    dry_run: bool = False  # teste local: nada vai para o Instagram (cliente falso + /dev/simulate)
    reply_delay_min_s: int = 120
    reply_delay_max_s: int = 1200
    max_comment_replies_per_hour: int = 30
    max_dms_per_hour: int = 60
    max_posts_per_day: int = 3
    max_auto_dm_turns: int = 3
    allowed_domains: str = "apexpt.app"  # separados por vírgula
    knowledge_dir: str = "knowledge"

    @property
    def allowed_domain_list(self) -> list[str]:
        return [d.strip().lower() for d in self.allowed_domains.split(",") if d.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
