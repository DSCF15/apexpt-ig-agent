"""Bot do Telegram em modo polling: para testar em local, sem HTTPS nem webhook.

Uso: python scripts/telegram_poll.py

Apaga o webhook do bot (se houver) e vai buscar as mensagens/botões ao Telegram.
Sem TELEGRAM_CHAT_ID no .env, mostra o chat.id de quem escrever ao bot, para o copiares.
Em produção usa-se o webhook (/webhook/telegram) e este script não corre.
"""
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import get_settings  # noqa: E402
from app.telegram import TelegramNotifier  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("telegram_poll")


def main() -> None:
    s = get_settings()
    if not s.telegram_bot_token:
        sys.exit("Falta TELEGRAM_BOT_TOKEN no .env (cria o bot com o @BotFather).")
    tg = TelegramNotifier(s.telegram_bot_token, s.telegram_chat_id)
    tg.delete_webhook()

    if not s.telegram_chat_id:
        print("Falta TELEGRAM_CHAT_ID. Manda uma mensagem ao bot no Telegram...")
        offset = None
        while True:
            for u in tg.get_updates(offset):
                offset = u["update_id"] + 1
                chat = (u.get("message") or {}).get("chat") or {}
                if chat.get("id"):
                    print(f"\nTELEGRAM_CHAT_ID={chat['id']}\n\nCopia esta linha para o .env e corre outra vez.")
                    tg.get_updates(offset, timeout_s=0)  # marca como lida
                    return

    from app.main import handle_telegram_update  # import tardio: precisa do .env completo

    log.info("Bot à escuta (polling). Ctrl+C para parar.")
    offset = None
    while True:
        try:
            updates = tg.get_updates(offset)
        except Exception:
            log.exception("falha no getUpdates; nova tentativa em 5 s")
            time.sleep(5)
            continue
        for u in updates:
            offset = u["update_id"] + 1
            try:
                log.info("update %s → %s", u["update_id"], handle_telegram_update(u))
            except Exception:
                log.exception("falha a tratar o update %s", u["update_id"])


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
