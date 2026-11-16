"""Gera uma ENCRYPTION_KEY (AES-256) e segredos aleatórios para o .env."""
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.crypto import generate_key  # noqa: E402

print(f"ENCRYPTION_KEY={generate_key()}")
print(f"WEBHOOK_VERIFY_TOKEN={secrets.token_urlsafe(32)}")
print(f"TELEGRAM_WEBHOOK_SECRET={secrets.token_urlsafe(32)}")
print(f"ADMIN_API_KEY={secrets.token_urlsafe(32)}")
