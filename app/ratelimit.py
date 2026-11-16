"""Limites próprios (bem abaixo dos da Meta) e interruptor global, em Redis."""
from __future__ import annotations

import time

import redis

PAUSE_KEY = "agent:paused"


class Limiter:
    def __init__(self, r: redis.Redis):
        self.r = r

    def allow(self, action: str, limit: int, window_s: int) -> bool:
        """Janela fixa: no máximo `limit` ações `action` por `window_s` segundos."""
        bucket = int(time.time() // window_s)
        key = f"rl:{action}:{bucket}"
        pipe = self.r.pipeline()
        pipe.incr(key)
        pipe.expire(key, window_s + 5)
        count = pipe.execute()[0]
        if count > limit:
            self.r.decr(key)
            return False
        return True

    def once(self, key: str, ttl_s: int) -> bool:
        """True só na primeira vez que `key` é visto dentro de ttl_s."""
        return bool(self.r.set(f"once:{key}", 1, nx=True, ex=ttl_s))

    # ---------- kill switch ----------
    def is_paused(self) -> bool:
        return self.r.exists(PAUSE_KEY) == 1

    def pause(self) -> None:
        self.r.set(PAUSE_KEY, 1)

    def resume(self) -> None:
        self.r.delete(PAUSE_KEY)
