"""Decisão determinística: o que fazer com cada comentário/DM. O LLM classifica, o código decide."""
from __future__ import annotations

from dataclasses import dataclass

INTENTS = [
    "elogio", "pergunta_produto", "preco", "suporte", "lesao_saude",
    "reclamacao", "spam", "outro",
]

AUTO, APPROVAL, HIDE, IGNORE = "auto", "approval", "hide", "ignore"

# intenção -> (ação em comentário público, ação em DM)
POLICY: dict[str, tuple[str, str]] = {
    "elogio": (AUTO, AUTO),
    "pergunta_produto": (AUTO, AUTO),
    "preco": (APPROVAL, AUTO),
    "suporte": (APPROVAL, APPROVAL),
    "lesao_saude": (APPROVAL, APPROVAL),  # nunca automático
    "reclamacao": (APPROVAL, APPROVAL),
    "spam": (HIDE, IGNORE),
    "outro": (APPROVAL, APPROVAL),
}
NEVER_AUTO = {"lesao_saude", "reclamacao", "suporte"}


@dataclass
class Classification:
    intent: str = "outro"
    language: str = "pt"
    toxicity: float = 0.0
    needs_reply: bool = True
    confidence: float = 0.0
    summary: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "Classification":
        intent = d.get("intent", "outro")
        return cls(
            intent=intent if intent in INTENTS else "outro",
            language=str(d.get("language", "pt"))[:8],
            toxicity=min(max(float(d.get("toxicity", 0) or 0), 0.0), 1.0),
            needs_reply=bool(d.get("needs_reply", True)),
            confidence=min(max(float(d.get("confidence", 0) or 0), 0.0), 1.0),
            summary=str(d.get("summary", ""))[:300],
        )


def decide(c: Classification, kind: str, autonomy_mode: str, *, dm_auto_turns: int = 0,
           max_auto_dm_turns: int = 3) -> str:
    """kind: 'comment' | 'dm'. Devolve AUTO, APPROVAL, HIDE ou IGNORE."""
    if c.toxicity >= 0.8 or c.intent == "spam":
        return HIDE if kind == "comment" else IGNORE
    if not c.needs_reply and c.intent in ("elogio", "outro") and kind == "comment":
        return IGNORE

    action = POLICY[c.intent][0 if kind == "comment" else 1]
    if action != AUTO:
        return action
    if c.intent in NEVER_AUTO or c.confidence < 0.7 or c.toxicity >= 0.4:
        return APPROVAL
    if kind == "dm" and dm_auto_turns >= max_auto_dm_turns:
        return APPROVAL
    if autonomy_mode != "auto":
        return APPROVAL
    return AUTO
