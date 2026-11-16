"""Peças para testar em local sem tocar no Instagram (DRY_RUN=true).

- FakeInstagramClient: mesmos métodos que InstagramClient, mas só regista no log.
- OfflineLLM: substitui o Claude quando não há ANTHROPIC_API_KEY (respostas fixas,
  tudo vai para aprovação). Com chave, o Claude real é usado mesmo em DRY_RUN.
"""
from __future__ import annotations

import itertools
import json
import logging
from types import SimpleNamespace

log = logging.getLogger("dry_run")
_ids = itertools.count(1)


def _fake_id(prefix: str) -> str:
    return f"{prefix}-{next(_ids)}"


class FakeInstagramClient:
    def me(self) -> dict:
        return {"user_id": "dry-run", "username": "apexpt_teste"}

    def refresh_token(self) -> tuple[str, int]:
        raise RuntimeError("DRY_RUN: não há token para renovar")

    def list_media(self, limit: int = 10) -> list[dict]:
        return []

    def get_media(self, media_id: str) -> dict:
        return {"id": media_id, "caption": "Post de teste da ApexPT sobre avaliações físicas."}

    def publishing_limit(self) -> dict:
        return {"quota_usage": 0, "config": {"quota_total": 100}}

    def create_container(self, **kw) -> str:
        log.info("[DRY_RUN] criar contentor %s", kw)
        return _fake_id("container")

    def wait_until_ready(self, container_id: str, **kw) -> None:
        return None

    def publish(self, container_id: str) -> str:
        media_id = _fake_id("media")
        log.warning("[DRY_RUN] post publicado (falso): %s", media_id)
        return media_id

    def reply_comment(self, comment_id: str, message: str) -> str:
        log.warning("[DRY_RUN] resposta ao comentário %s: %s", comment_id, message)
        return _fake_id("reply")

    def hide_comment(self, comment_id: str, hide: bool = True) -> None:
        log.warning("[DRY_RUN] comentário %s escondido", comment_id)

    def send_message(self, igsid: str, text: str) -> dict:
        log.warning("[DRY_RUN] DM para %s: %s", igsid, text)
        return {"message_id": _fake_id("mid")}

    def send_private_reply(self, comment_id: str, text: str) -> dict:
        log.warning("[DRY_RUN] resposta privada ao comentário %s: %s", comment_id, text)
        return {"message_id": _fake_id("mid")}


class OfflineLLM:
    """Imita anthropic.Anthropic().messages.create sem rede."""

    def __init__(self):
        self.messages = self

    def create(self, **kw):
        props = ((kw.get("output_config") or {}).get("format") or {}).get("schema", {}).get("properties", {})
        if "intent" in props:
            text = str(kw.get("messages", "")).lower()
            intent = ("preco" if any(w in text for w in ("preço", "preco", "€", "quanto custa"))
                      else "pergunta_produto" if "?" in text else "elogio")
            return _json({"intent": intent, "language": "pt", "toxicity": 0.0, "needs_reply": True,
                          "confidence": 0.5, "summary": "classificação offline (sem Claude)"})
        if "grounded" in props:
            return _json({"grounded": False, "safe": True, "reason": "modo offline: rever à mão"})
        if "options" in props:
            return _json({"options": [
                {"angle": "teste A", "caption": "Legenda de teste A (sem Claude). Sabe mais em apexpt.app",
                 "hashtags": ["personaltrainer", "apexpt"]},
                {"angle": "teste B", "caption": "Legenda de teste B (sem Claude). Guarda este post!",
                 "hashtags": ["treino", "ptportugal"]},
                {"angle": "teste C", "caption": "Legenda de teste C (sem Claude). Qual é a tua maior dor como PT?",
                 "hashtags": ["coachingonline"]},
            ]})
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(
            type="text", text="Obrigado pela mensagem! (resposta de teste, sem Claude)")])


def _json(data: dict):
    return SimpleNamespace(stop_reason="end_turn",
                           content=[SimpleNamespace(type="text", text=json.dumps(data, ensure_ascii=False))])
