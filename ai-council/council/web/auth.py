"""Вход по паролю для веб-интерфейса (HTTP Basic) с защитой от перебора.

Включается переменными окружения WEB_PASSWORD (и необязательно WEB_USER, по умолчанию admin).
Без WEB_PASSWORD интерфейс открыт — так безопасно только на localhost.
Пароль передаётся в каждом запросе, поэтому снаружи сервер нужно открывать только по HTTPS.
"""
from __future__ import annotations

import base64
import hmac
import time
from typing import Any, Awaitable, Callable

Scope = dict[str, Any]
ASGIApp = Callable[[Scope, Callable, Callable], Awaitable[None]]

OPEN_PATHS = {"/healthz"}


class BasicAuth:
    def __init__(
        self,
        app: ASGIApp,
        *,
        user: str,
        password: str,
        trust_proxy: bool = False,
        max_failures: int = 10,
        lock_seconds: float = 900,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.app = app
        self.user = user.encode()
        self.password = password.encode()
        self.trust_proxy = trust_proxy
        self.max_failures = max_failures
        self.lock_seconds = lock_seconds
        self.clock = clock
        self.failures: dict[str, tuple[int, float]] = {}  # ip -> (неудачных попыток, время первой)

    def _client_ip(self, scope: Scope) -> str:
        headers = dict(scope.get("headers") or [])
        if self.trust_proxy and b"x-forwarded-for" in headers:
            return headers[b"x-forwarded-for"].decode("latin-1").split(",")[0].strip()
        client = scope.get("client")
        return client[0] if client else "?"

    def _check(self, scope: Scope) -> bool:
        headers = dict(scope.get("headers") or [])
        raw = headers.get(b"authorization", b"")
        if not raw.lower().startswith(b"basic "):
            return False
        try:
            user, _, password = base64.b64decode(raw[6:].strip()).partition(b":")
        except ValueError:
            return False
        ok_user = hmac.compare_digest(user, self.user)
        ok_pass = hmac.compare_digest(password, self.password)
        return ok_user and ok_pass

    async def __call__(self, scope: Scope, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http" or scope.get("path") in OPEN_PATHS:
            await self.app(scope, receive, send)
            return
        ip = self._client_ip(scope)
        now = self.clock()
        count, first = self.failures.get(ip, (0, now))
        if now - first > self.lock_seconds:
            count, first = 0, now
        if count >= self.max_failures:
            await _respond(send, 429, "Слишком много неудачных попыток входа. Подождите 15 минут.",
                           [(b"retry-after", str(int(self.lock_seconds - (now - first)) + 1).encode())])
            return
        if self._check(scope):
            self.failures.pop(ip, None)
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        if b"authorization" in headers:  # пустой запрос без пароля — не попытка подбора
            self.failures[ip] = (count + 1, first)
        await _respond(send, 401, "Нужен логин и пароль.",
                       [(b"www-authenticate", 'Basic realm="AI Council", charset="UTF-8"'.encode())])


async def _respond(send: Callable, status: int, text: str, extra: list[tuple[bytes, bytes]]) -> None:
    body = text.encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                            (b"content-length", str(len(body)).encode()), *extra]})
    await send({"type": "http.response.body", "body": body})
