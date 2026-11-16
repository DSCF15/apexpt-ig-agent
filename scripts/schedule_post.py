"""Agenda um post pela API do agente.

Exemplo:
  python scripts/schedule_post.py REELS https://media.apexpt.app/reel-01.mp4 \
      "Cria um plano de treino em 2 minutos. apexpt.app" 2026-11-03T18:30:00+00:00
Carrossel: vários URLs separados por vírgula.
"""
import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()
if len(sys.argv) != 5:
    sys.exit(__doc__)

media_type, urls, caption, when = sys.argv[1:]
base = os.getenv("AGENT_URL", "http://localhost:8000")
r = httpx.post(f"{base}/posts", headers={"X-API-Key": os.environ["ADMIN_API_KEY"]}, json={
    "media_type": media_type, "media_urls": urls.split(","), "caption": caption, "publish_at": when,
})
print(r.status_code, r.json())
