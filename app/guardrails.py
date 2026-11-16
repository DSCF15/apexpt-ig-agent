"""Verificações determinísticas aplicadas DEPOIS do LLM. Qualquer problema -> aprovação humana."""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

URL_RE = re.compile(r"(?:https?://|www\.)([^\s/]+)|\b([a-z0-9-]+\.(?:app|com|pt|io|net|org|me|link))\b", re.I)
PHONE_RE = re.compile(r"(?:\+?\d[\s-]?){9,}")
EURO_RE = re.compile(r"(\d+(?:[.,]\d{1,2})?)\s*€|€\s*(\d+(?:[.,]\d{1,2})?)")
EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF☀-➿]")
HEALTH_TERMS = [
    "diagnost", "lesao", "lesoes", "tendinite", "hernia", "fratura", "medicament",
    "anti-inflamatorio", "ibuprofeno", "dieta para", "calorias por dia", "suplement",
]
MAX_WORDS = {"comment": 30, "dm": 70}
ESCALATE = "ESCALAR"


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower())
    return "".join(ch for ch in s if not unicodedata.combining(ch))


def _amounts(text: str) -> set[str]:
    out = set()
    for a, b in EURO_RE.findall(text):
        v = (a or b).replace(",", ".")
        out.add(str(float(v)))
    return out


def check(reply: str, kind: str, *, allowed_domains: list[str], knowledge_text: str,
          recent: list[str] | None = None) -> list[str]:
    """Devolve a lista de problemas (vazia = OK para publicar)."""
    problems: list[str] = []
    text = (reply or "").strip()
    if not text or text.upper().startswith(ESCALATE):
        return ["o modelo pediu escalamento ou não respondeu"]

    if len(text.split()) > MAX_WORDS[kind]:
        problems.append(f"demasiado longo (> {MAX_WORDS[kind]} palavras)")

    for m in URL_RE.finditer(text):
        domain = (m.group(1) or m.group(2) or "").lower().removeprefix("www.")
        if not any(domain == d or domain.endswith("." + d) for d in allowed_domains):
            problems.append(f"link fora da allowlist: {domain}")

    if PHONE_RE.search(text):
        problems.append("contém número de telefone")

    unknown = _amounts(text) - _amounts(knowledge_text)
    if unknown:
        problems.append(f"valores em € que não estão na base de conhecimento: {sorted(unknown)}")

    norm = _norm(text)
    hits = [t for t in HEALTH_TERMS if t in norm]
    if hits:
        problems.append(f"tema de saúde ({', '.join(hits)})")

    if len(EMOJI_RE.findall(text)) > 2:
        problems.append("demasiados emojis")

    for prev in recent or []:
        if SequenceMatcher(None, _norm(prev), norm).ratio() > 0.85:
            problems.append("quase igual a uma resposta recente")
            break
    return problems
