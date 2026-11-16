"""Cliente mínimo da Instagram API (Instagram Login, host graph.instagram.com).

Só usa endpoints oficiais. Erros de limite levantam RateLimited; erros
temporários (5xx, timeouts) levantam IGError(transient=True) para a fila voltar
a tentar com backoff.
"""
from __future__ import annotations

import json
import time
from typing import Callable

import httpx

GRAPH_HOST = "https://graph.instagram.com"
RATE_LIMIT_CODES = {4, 17, 32, 613, 80002}
USAGE_KEYS = {"call_count", "total_cputime", "total_time"}


class IGError(Exception):
    def __init__(self, message: str, *, status: int | None = None, code: int | None = None,
                 subcode: int | None = None, transient: bool = False):
        super().__init__(message)
        self.status, self.code, self.subcode, self.transient = status, code, subcode, transient


class RateLimited(IGError):
    pass


def _max_usage_pct(headers: httpx.Headers) -> float:
    """Lê X-App-Usage / X-Business-Use-Case-Usage e devolve a maior percentagem."""
    def walk(obj):
        if isinstance(obj, dict):
            for key, val in obj.items():
                if key in USAGE_KEYS and isinstance(val, (int, float)):
                    yield float(val)
                else:
                    yield from walk(val)
        elif isinstance(obj, list):
            for item in obj:
                yield from walk(item)

    best = 0.0
    for name in ("x-app-usage", "x-business-use-case-usage"):
        raw = headers.get(name)
        if raw:
            try:
                best = max([best, *walk(json.loads(raw))])
            except ValueError:
                pass
    return best


class InstagramClient:
    def __init__(self, token_provider: Callable[[], str], user_id: str, version: str = "v25.0",
                 http: httpx.Client | None = None):
        self._token = token_provider
        self.user_id = user_id
        self.version = version
        self.http = http or httpx.Client(timeout=30)
        self.last_usage_pct = 0.0

    # ---------- núcleo ----------
    def _request(self, method: str, path: str, *, params: dict | None = None,
                 json_body: dict | None = None, versioned: bool = True) -> dict:
        base = f"{GRAPH_HOST}/{self.version}" if versioned else GRAPH_HOST
        params = {**(params or {}), "access_token": self._token()}
        try:
            r = self.http.request(method, f"{base}{path}", params=params, json=json_body)
        except httpx.TransportError as e:
            raise IGError(f"rede: {e}", transient=True) from e

        self.last_usage_pct = _max_usage_pct(r.headers)
        if r.is_success:
            return r.json() if r.content else {}

        try:
            err = r.json().get("error", {})
        except ValueError:
            err = {}
        code, sub = err.get("code"), err.get("error_subcode")
        msg = f"{r.status_code} {err.get('type', '')} {err.get('message', r.text[:200])}".strip()
        if code in RATE_LIMIT_CODES or r.status_code == 429:
            raise RateLimited(msg, status=r.status_code, code=code, subcode=sub, transient=True)
        transient = r.status_code >= 500 or bool(err.get("is_transient"))
        raise IGError(msg, status=r.status_code, code=code, subcode=sub, transient=transient)

    # ---------- conta ----------
    def me(self) -> dict:
        return self._request("GET", "/me", params={"fields": "user_id,username"})

    def refresh_token(self) -> tuple[str, int]:
        """Renova o token de longa duração (60 dias). Devolve (token, expires_in_s)."""
        data = self._request("GET", "/refresh_access_token",
                             params={"grant_type": "ig_refresh_token"}, versioned=False)
        return data["access_token"], int(data.get("expires_in", 0))

    # ---------- media e publicação ----------
    def list_media(self, limit: int = 10) -> list[dict]:
        return self._request("GET", f"/{self.user_id}/media",
                             params={"fields": "id,caption,media_type,permalink,timestamp",
                                     "limit": limit})["data"]

    def get_media(self, media_id: str) -> dict:
        return self._request("GET", f"/{media_id}", params={"fields": "id,caption,permalink"})

    def publishing_limit(self) -> dict:
        data = self._request("GET", f"/{self.user_id}/content_publishing_limit",
                             params={"fields": "quota_usage,config"})["data"]
        return data[0] if data else {}

    def create_container(self, *, media_type: str, url: str | None = None, caption: str | None = None,
                         children: list[str] | None = None, is_carousel_item: bool = False) -> str:
        p: dict = {}
        if media_type == "CAROUSEL":
            p.update(media_type="CAROUSEL", children=",".join(children or []))
        elif media_type == "IMAGE":
            p["image_url"] = url
        elif media_type == "VIDEO":  # só como item de carrossel
            p.update(media_type="VIDEO", video_url=url)
        elif media_type in ("REELS", "STORIES"):
            is_video = (url or "").lower().split("?")[0].endswith((".mp4", ".mov"))
            p["media_type"] = media_type
            p["video_url" if is_video or media_type == "REELS" else "image_url"] = url
        else:
            raise ValueError(f"media_type inválido: {media_type}")
        if is_carousel_item:
            p["is_carousel_item"] = "true"
        elif caption and media_type != "STORIES":
            p["caption"] = caption
        return self._request("POST", f"/{self.user_id}/media", params=p)["id"]

    def container_status(self, container_id: str) -> str:
        return self._request("GET", f"/{container_id}", params={"fields": "status_code"})["status_code"]

    def wait_until_ready(self, container_id: str, timeout_s: int = 300, poll_s: float = 5,
                         sleep: Callable[[float], None] = time.sleep) -> None:
        waited = 0.0
        while True:
            status = self.container_status(container_id)
            if status == "FINISHED":
                return
            if status in ("ERROR", "EXPIRED"):
                raise IGError(f"contentor {container_id} em estado {status}")
            if waited >= timeout_s:
                raise IGError(f"contentor {container_id} não ficou pronto em {timeout_s}s", transient=True)
            sleep(poll_s)
            waited += poll_s

    def publish(self, container_id: str) -> str:
        return self._request("POST", f"/{self.user_id}/media_publish",
                             params={"creation_id": container_id})["id"]

    # ---------- comentários ----------
    def get_comment(self, comment_id: str) -> dict:
        return self._request("GET", f"/{comment_id}",
                             params={"fields": "id,text,username,timestamp,media{id,caption},from"})

    def list_comments(self, media_id: str) -> list[dict]:
        return self._request("GET", f"/{media_id}/comments",
                             params={"fields": "id,text,username,timestamp,replies{username}"})["data"]

    def reply_comment(self, comment_id: str, message: str) -> str:
        return self._request("POST", f"/{comment_id}/replies", params={"message": message})["id"]

    def hide_comment(self, comment_id: str, hide: bool = True) -> None:
        self._request("POST", f"/{comment_id}", params={"hide": "true" if hide else "false"})

    # ---------- mensagens ----------
    def send_message(self, igsid: str, text: str) -> dict:
        return self._request("POST", f"/{self.user_id}/messages",
                             json_body={"recipient": {"id": igsid}, "message": {"text": text}})

    def send_private_reply(self, comment_id: str, text: str) -> dict:
        """Resposta privada (DM) a um comentário."""
        return self._request("POST", f"/{self.user_id}/messages",
                             json_body={"recipient": {"comment_id": comment_id}, "message": {"text": text}})
