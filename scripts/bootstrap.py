"""Primeiro arranque: valida o token do .env, guarda-o cifrado na BD e renova-o.

Uso: python scripts/bootstrap.py
Depois disto podes apagar IG_ACCESS_TOKEN do .env.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent import TokenStore  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import make_session_factory  # noqa: E402
from app.ig import InstagramClient  # noqa: E402

s = get_settings()
if not s.ig_access_token:
    sys.exit("Define IG_ACCESS_TOKEN no .env (gerado no App Dashboard da Meta).")

store = TokenStore(make_session_factory(s.database_url), s)
ig = InstagramClient(lambda: s.ig_access_token, s.ig_user_id, s.ig_api_version)
me = ig.me()
print(f"Token válido para @{me.get('username')} (user_id {me.get('user_id')})")
if s.ig_user_id and str(me.get("user_id")) != s.ig_user_id:
    print(f"ATENÇÃO: IG_USER_ID no .env ({s.ig_user_id}) difere do da API ({me.get('user_id')})")
if not s.ig_user_id:
    print(f"Põe no .env: IG_USER_ID={me.get('user_id')}")

try:
    token, expires_in = ig.refresh_token()
    print(f"Token renovado; expira em {expires_in // 86400} dias")
except Exception as e:  # token com menos de 24 h ainda não pode ser renovado
    print(f"Não foi possível renovar já ({e}); guardo o atual.")
    token, expires_in = s.ig_access_token, None

store.save(token, expires_in)
print("Token guardado cifrado na base de dados.")
