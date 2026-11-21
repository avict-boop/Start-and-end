"""Вход по паролю для веб-интерфейса с защитой от перебора.

Включается переменными окружения WEB_PASSWORD (и необязательно WEB_USER, по умолчанию admin).
Без WEB_PASSWORD интерфейс открыт — так безопасно только на localhost.

Два способа: страница входа в браузере (запоминает устройство на полгода — так работает приложение на телефоне,
которому системное окно пароля неудобно) и HTTP Basic (для программ и старых закладок).
Смена пароля делает недействительными все запомненные входы. Снаружи — только по HTTPS.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import re
import time
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs

Scope = dict[str, Any]
ASGIApp = Callable[[Scope, Callable, Callable], Awaitable[None]]

OPEN_PATHS = {"/healthz", "/manifest.webmanifest"}  # описание приложения браузер читает без пароля
OPEN_PREFIXES = ("/static/icons/", "/static/brand/")  # значки — тоже
# Ключ «только чтение» (READ_TOKEN в .env): для разбора заседаний снаружи — список, ход, журнал, отчёты, диагностика.
# Только GET и только эти адреса: запустить заседание, поменять настройки или потратить деньги с ним нельзя.
READ_PATHS = re.compile(r"^/api/(sessions(/[^/]+(/(events|diagnostics|report|costs|request|selection\.xlsx|thoughts))?)?"
                        r"|diagnostics|stats|paused|system)$")
COOKIE = "council_auth"
COOKIE_DAYS = 180


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
        read_token: str = "",
    ) -> None:
        self.app = app
        self.read_token = read_token.encode() if len(read_token) >= 24 else b""  # короткий ключ — не включаем
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

    def _secret(self) -> bytes:
        return hashlib.sha256(b"council-login\0" + self.user + b"\0" + self.password).digest()

    def make_token(self, now: float | None = None) -> str:
        exp = str(int((now or time.time()) + COOKIE_DAYS * 86400))
        return f"{exp}.{hmac.new(self._secret(), exp.encode(), hashlib.sha256).hexdigest()}"

    def _cookie_ok(self, scope: Scope) -> bool:
        headers = dict(scope.get("headers") or [])
        for part in headers.get(b"cookie", b"").decode("latin-1").split(";"):
            name, _, value = part.strip().partition("=")
            if name != COOKIE:
                continue
            exp, _, sig = value.partition(".")
            good = hmac.new(self._secret(), exp.encode(), hashlib.sha256).hexdigest()
            if exp.isdigit() and int(exp) > time.time() and hmac.compare_digest(sig, good):
                return True
        return False

    def _https(self, scope: Scope) -> bool:
        headers = dict(scope.get("headers") or [])
        if self.trust_proxy and headers.get(b"x-forwarded-proto", b"").lower() == b"https":
            return True
        return scope.get("scheme") == "https"

    def _cookie_header(self, value: str, max_age: int, scope: Scope) -> tuple[bytes, bytes]:
        return (b"set-cookie", (f"{COOKIE}={value}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Lax"
                                + ("; Secure" if self._https(scope) else "")).encode())

    def _check(self, scope: Scope) -> bool:
        if self._cookie_ok(scope):
            return True
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
        path = scope.get("path") or ""
        if scope["type"] != "http" or path in OPEN_PATHS or path.startswith(OPEN_PREFIXES):
            await self.app(scope, receive, send)
            return
        if path == "/logout":
            await _respond(send, 303, "", [(b"location", b"/login"), self._cookie_header("", 0, scope)])
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
        if path == "/login":
            await self._login(scope, receive, send, ip, count, first)
            return
        if self.read_token:
            headers = dict(scope.get("headers") or [])
            raw = headers.get(b"authorization", b"")
            if raw.lower().startswith(b"bearer "):
                if not hmac.compare_digest(raw[7:].strip(), self.read_token):
                    self.failures[ip] = (count + 1, first)
                    await _respond(send, 401, "Неверный ключ.", [])
                    return
                if scope.get("method") not in ("GET", "HEAD") or not READ_PATHS.match(path):
                    await _respond(send, 403, "Ключ только для чтения: эта операция с ним недоступна.", [])
                    return
                self.failures.pop(ip, None)
                await self.app(scope, receive, send)
                return
        if self._check(scope):
            self.failures.pop(ip, None)
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        if b"authorization" in headers:  # пустой запрос без пароля — не попытка подбора
            self.failures[ip] = (count + 1, first)
        if scope.get("method") == "GET" and b"text/html" in headers.get(b"accept", b"") and b"authorization" not in headers:
            # браузер открывает страницу — показываем свою форму входа, а не системное окно
            await _respond(send, 303, "", [(b"location", b"/login")])
            return
        await _respond(send, 401, "Нужен логин и пароль.",
                       [(b"www-authenticate", 'Basic realm="AI Council", charset="UTF-8"'.encode())])

    async def _login(self, scope: Scope, receive: Callable, send: Callable, ip: str, count: int, first: float) -> None:
        if scope.get("method") != "POST":
            await _html(send, 200, login_page())
            return
        body = b""
        while True:
            msg = await receive()
            body += msg.get("body", b"")
            if not msg.get("more_body") or len(body) > 8192:
                break
        form = parse_qs(body.decode("utf-8", "replace"))
        user = (form.get("user") or [""])[0].encode()
        password = (form.get("password") or [""])[0].encode()
        if hmac.compare_digest(user, self.user) and hmac.compare_digest(password, self.password):
            self.failures.pop(ip, None)
            await _respond(send, 303, "", [(b"location", b"/"),
                                           self._cookie_header(self.make_token(), COOKIE_DAYS * 86400, scope)])
            return
        self.failures[ip] = (count + 1, first)
        await _html(send, 401, login_page("Неверный логин или пароль.", (form.get("user") or [""])[0]))


async def _html(send: Callable, status: int, page: str) -> None:
    body = page.encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"text/html; charset=utf-8"), (b"content-length", str(len(body)).encode()),
                            (b"cache-control", b"no-store")]})
    await send({"type": "http.response.body", "body": body})


def login_page(error: str = "", user: str = "") -> str:
    err = f'<p class="err" role="alert">{html.escape(error)}</p>' if error else ""
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="light dark"><meta name="theme-color" content="#f7f8fb">
<link rel="manifest" href="/manifest.webmanifest"><link rel="apple-touch-icon" href="/static/icons/apple-touch-icon.png">
<meta name="apple-mobile-web-app-capable" content="yes"><meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Совет">
<title>Вход — Совет нейросетей</title><style>
:root {{ --bg:#f7f8fb; --surface:#fff; --text:#18233d; --muted:#586379; --line:#c9d0dd; --accent:#4f46e5; --bad:#b8283a; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#0f1729; --surface:#18233d; --text:#e8ecf5; --muted:#9aa6bf; --line:#3e4d71;
  --accent:#818cf8; --bad:#ff8a96; }} }}
* {{ box-sizing: border-box; }}
body {{ margin:0; min-height:100vh; min-height:100dvh; display:grid; place-items:center; background:var(--bg); color:var(--text);
  font: 16px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; padding: 16px env(safe-area-inset-right) 16px env(safe-area-inset-left); }}
form {{ width:100%; max-width:360px; background:var(--surface); border:1px solid var(--line); border-radius:18px; padding:28px 24px;
  box-shadow: 0 12px 40px rgb(24 35 61 / .1); }}
.brand {{ display:flex; gap:10px; align-items:center; font-weight:700; font-size:1.1rem; margin-bottom:18px; }}
.brand img {{ width:40px; height:40px; border-radius:10px; }}
label {{ display:block; font-weight:600; font-size:.9rem; margin:12px 0 4px; }}
input {{ width:100%; font:inherit; font-size:16px; padding:12px; border-radius:12px; border:1px solid var(--line); background:var(--bg); color:var(--text); }}
input:focus {{ outline:2px solid var(--accent); outline-offset:1px; }}
button {{ width:100%; margin-top:20px; min-height:50px; border:0; border-radius:14px; background:var(--accent); color:#fff; font:inherit; font-weight:650; font-size:1.05rem; }}
.err {{ color:var(--bad); font-weight:600; margin:10px 0 0; }}
.hint {{ color:var(--muted); font-size:.85rem; margin:14px 0 0; }}
</style></head><body>
<form method="post" action="/login">
  <div class="brand"><img src="/static/icons/icon-192.png" alt="">Совет нейросетей</div>
  <label for="u">Логин</label><input id="u" name="user" autocomplete="username" autocapitalize="none" value="{html.escape(user)}" required>
  <label for="p">Пароль</label><input id="p" name="password" type="password" autocomplete="current-password" required {"autofocus" if user else ""}>
  {err}
  <button type="submit">Войти</button>
  <p class="hint">Устройство запомнится на полгода — пароль больше не спросим.</p>
</form></body></html>"""


async def _respond(send: Callable, status: int, text: str, extra: list[tuple[bytes, bytes]]) -> None:
    body = text.encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                            (b"content-length", str(len(body)).encode()), *extra]})
    await send({"type": "http.response.body", "body": body})
