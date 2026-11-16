"""Aprovação humana e alertas via Telegram Bot API (só httpx, sem SDK)."""
from __future__ import annotations

import html
import logging
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger(__name__)
LISBON = ZoneInfo("Europe/Lisbon")


class Notifier:
    """Interface mínima usada pelo agente; a versão nula serve para testes/dev."""

    def send_draft(self, draft) -> int | None:  # devolve message_id
        log.info("rascunho #%s para aprovação: %s", draft.id, draft.reply_text)
        return None

    def send_post(self, post) -> int | None:  # devolve message_id
        log.info("post #%s para aprovação: %s", post.id, post.caption[:80])
        return None

    def alert(self, text: str) -> None:
        log.warning("ALERTA: %s", text)

    def edit_status(self, message_id: int, text: str) -> None:
        pass


class TelegramNotifier(Notifier):
    def __init__(self, bot_token: str, chat_id: str, http: httpx.Client | None = None):
        self.base = f"https://api.telegram.org/bot{bot_token}"
        self.chat_id = chat_id
        self.http = http or httpx.Client(timeout=15)

    def _call(self, method: str, payload: dict) -> dict:
        r = self.http.post(f"{self.base}/{method}", json=payload)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method}: {data}")
        return data["result"]

    def send_draft(self, draft) -> int | None:
        where = "Comentário" if draft.kind == "comment" else "DM"
        problems = "".join(f"\n⚠️ {html.escape(p)}" for p in (draft.problems or []))
        text = (
            f"<b>{where} de @{html.escape(draft.username or '?')}</b> · {html.escape(draft.intent or '?')}\n"
            f"<blockquote>{html.escape(draft.source_text[:800])}</blockquote>\n"
            f"<b>Resposta proposta:</b>\n{html.escape(draft.reply_text or '(sem rascunho)')}{problems}\n\n"
            f"<i>Para editar: responde a esta mensagem com o texto final.</i>"
        )
        buttons = [[
            {"text": "✅ Publicar", "callback_data": f"approve:{draft.id}"},
            {"text": "🗑 Descartar", "callback_data": f"discard:{draft.id}"},
        ]]
        if draft.kind == "comment":
            buttons[0].append({"text": "🙈 Esconder", "callback_data": f"hide:{draft.id}"})
        res = self._call("sendMessage", {
            "chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
            "reply_markup": {"inline_keyboard": buttons},
        })
        return res["message_id"]

    def send_post(self, post) -> int | None:
        when = post.publish_at.astimezone(LISBON).strftime("%d/%m/%Y %H:%M")
        links = "\n".join(f'<a href="{html.escape(u)}">ficheiro {i}</a>'
                          for i, u in enumerate(post.media_urls, 1))
        text = (
            f"<b>Post #{post.id} para aprovar</b> · {html.escape(post.media_type)}\n"
            f"🕒 {when} (Lisboa)\n{links}\n\n"
            f"<b>Legenda:</b>\n{html.escape((post.caption or '(sem legenda)')[:3000])}\n\n"
            f"<i>Para mudar a legenda: responde a esta mensagem com o texto final (fica aprovado).</i>"
        )
        res = self._call("sendMessage", {
            "chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
            "reply_markup": {"inline_keyboard": [[
                {"text": "✅ Aprovar", "callback_data": f"post_ok:{post.id}"},
                {"text": "❌ Rejeitar", "callback_data": f"post_no:{post.id}"},
            ]]},
        })
        return res["message_id"]

    def alert(self, text: str) -> None:
        try:
            self._call("sendMessage", {"chat_id": self.chat_id, "text": f"⚠️ {text}"})
        except Exception:  # alertas nunca devem rebentar o fluxo principal
            log.exception("falha a enviar alerta")

    def edit_status(self, message_id: int, text: str) -> None:
        try:
            self._call("editMessageReplyMarkup", {"chat_id": self.chat_id, "message_id": message_id,
                                                  "reply_markup": {"inline_keyboard": []}})
            self._call("sendMessage", {"chat_id": self.chat_id, "text": text,
                                       "reply_to_message_id": message_id})
        except Exception:
            log.exception("falha a atualizar mensagem")

    def answer_callback(self, callback_id: str, text: str) -> None:
        try:
            self._call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})
        except Exception:
            log.exception("falha a responder ao callback")

    # ---------- modo polling (local, sem HTTPS) ----------
    def delete_webhook(self) -> None:
        self._call("deleteWebhook", {"drop_pending_updates": False})

    def get_updates(self, offset: int | None, timeout_s: int = 30) -> list[dict]:
        r = self.http.post(f"{self.base}/getUpdates",
                           json={"offset": offset, "timeout": timeout_s,
                                 "allowed_updates": ["message", "callback_query"]},
                           timeout=timeout_s + 10)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram getUpdates: {data}")
        return data["result"]
