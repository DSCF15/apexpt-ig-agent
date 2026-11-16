"""PoC no terminal: encontra o comentário mais recente sem resposta, corre o pipeline
completo (classificar → decidir → redigir → guardrails) e pergunta antes de publicar.

Não precisa de Redis, Celery nem Telegram. Uso: python scripts/reply_once.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from anthropic import Anthropic  # noqa: E402

from app import guardrails  # noqa: E402
from app.agent import TokenStore  # noqa: E402
from app.brain import Brain  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import make_session_factory  # noqa: E402
from app.ig import InstagramClient  # noqa: E402
from app.policy import decide  # noqa: E402

s = get_settings()
ig = InstagramClient(TokenStore(make_session_factory(s.database_url), s).get, s.ig_user_id, s.ig_api_version)
brain = Brain(Anthropic(api_key=s.anthropic_api_key or None), model_classify=s.model_classify,
              model_draft=s.model_draft, knowledge_dir=s.knowledge_dir)

me = ig.me()["username"]
target = None
for media in ig.list_media(limit=5):
    for c in ig.list_comments(media["id"]):
        replied = any(r.get("username") == me for r in c.get("replies", {}).get("data", []))
        if c.get("username") != me and not replied:
            target = (media, c)
            break
    if target:
        break

if not target:
    sys.exit("Sem comentários por responder nos últimos 5 posts.")

media, comment = target
cls = brain.classify(comment["text"], "comment")
action = decide(cls, "comment", "auto")
print(f"\n@{comment['username']}: {comment['text']}")
print(f"intenção={cls.intent} confiança={cls.confidence:.2f} toxicidade={cls.toxicity:.2f} → ação={action}")
if action in ("hide", "ignore"):
    sys.exit(f"A política diz '{action}'. Nada a responder.")

reply = brain.draft(kind="comment", text=comment["text"], username=comment["username"],
                    caption=media.get("caption"))
problems = guardrails.check(reply, "comment", allowed_domains=s.allowed_domain_list,
                            knowledge_text=brain.knowledge)
print(f"\nResposta proposta: {reply}")
for p in problems:
    print(f"  ⚠ {p}")

if input("\nPublicar? [s/N] ").strip().lower() == "s":
    print("Publicado:", ig.reply_comment(comment["id"], reply))
else:
    print("Não publicado.")
