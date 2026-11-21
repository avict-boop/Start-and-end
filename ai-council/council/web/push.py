"""Уведомления на телефон (Web Push): заседание закончилось, ждёт проверки или встало на паузу.

Работает, даже когда приложение закрыто: сообщение доставляет служба браузера (Google, Apple, Mozilla).
Ключи сервера (VAPID) создаются один раз и хранятся рядом с архивом; подписки устройств — там же.
Шифрование — RFC 8291 (aes128gcm), подпись — RFC 8292 (ES256); всё на библиотеке cryptography.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import struct
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

log = logging.getLogger("council.push")

MAX_SUBS = 50


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _point(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def _hkdf(salt: bytes, info: bytes, ikm: bytes, length: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


def encrypt(payload: bytes, p256dh: str, auth: str, *, salt: bytes | None = None,
            server_key: ec.EllipticCurvePrivateKey | None = None) -> bytes:
    """Тело сообщения для устройства (RFC 8291, aes128gcm, одна запись)."""
    ua_pub = unb64u(p256dh)
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_pub)
    as_key = server_key or ec.generate_private_key(ec.SECP256R1())
    as_pub = _point(as_key.public_key())
    shared = as_key.exchange(ec.ECDH(), ua_key)
    ikm = _hkdf(unb64u(auth), b"WebPush: info\x00" + ua_pub + as_pub, shared, 32)
    salt = salt or os.urandom(16)
    cek = _hkdf(salt, b"Content-Encoding: aes128gcm\x00", ikm, 16)
    nonce = _hkdf(salt, b"Content-Encoding: nonce\x00", ikm, 12)
    body = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    return salt + struct.pack("!IB", 4096, len(as_pub)) + as_pub + body


class Push:
    def __init__(self, folder: Path, contact: str = "") -> None:
        self.folder = Path(folder)
        self.contact = contact or os.environ.get("PUSH_CONTACT", "") or (
            f"https://{os.environ['DOMAIN']}" if os.environ.get("DOMAIN") else "mailto:council@localhost.localdomain")
        self._lock = threading.Lock()
        self._key: ec.EllipticCurvePrivateKey | None = None

    # ------------------------------------------------------------------ ключи и подписки
    @property
    def key(self) -> ec.EllipticCurvePrivateKey:
        if self._key is None:
            path = self.folder / "vapid.pem"
            with self._lock:
                if path.exists():
                    k = serialization.load_pem_private_key(path.read_bytes(), password=None)
                    assert isinstance(k, ec.EllipticCurvePrivateKey)
                    self._key = k
                else:
                    self.folder.mkdir(parents=True, exist_ok=True)
                    self._key = ec.generate_private_key(ec.SECP256R1())
                    path.write_bytes(self._key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                             serialization.NoEncryption()))
                    os.chmod(path, 0o600)
        return self._key

    def public_key(self) -> str:
        return b64u(_point(self.key.public_key()))

    def _subs_path(self) -> Path:
        return self.folder / "subscriptions.json"

    def subs(self) -> list[dict[str, Any]]:
        try:
            return json.loads(self._subs_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []

    def _save(self, subs: list[dict[str, Any]]) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        tmp = self._subs_path().with_suffix(".tmp")
        tmp.write_text(json.dumps(subs, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self._subs_path())

    def add(self, sub: dict[str, Any], label: str = "") -> None:
        ep = str(sub.get("endpoint") or "")
        keys = sub.get("keys") or {}
        if not ep.startswith("https://") or not keys.get("p256dh") or not keys.get("auth"):
            raise ValueError("неполная подписка на уведомления")
        with self._lock:
            subs = [s for s in self.subs() if s["endpoint"] != ep]
            subs.append({"endpoint": ep, "keys": {"p256dh": keys["p256dh"], "auth": keys["auth"]},
                         "label": label[:80], "added": int(time.time())})
            self._save(subs[-MAX_SUBS:])

    def remove(self, endpoint: str) -> bool:
        with self._lock:
            subs = self.subs()
            left = [s for s in subs if s["endpoint"] != endpoint]
            if len(left) != len(subs):
                self._save(left)
            return len(left) != len(subs)

    # ------------------------------------------------------------------ отправка
    def _vapid(self, endpoint: str) -> str:
        u = urlsplit(endpoint)
        head = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
        claims = b64u(json.dumps({"aud": f"{u.scheme}://{u.netloc}", "exp": int(time.time()) + 12 * 3600,
                                  "sub": self.contact}, separators=(",", ":")).encode())
        r, s = decode_dss_signature(self.key.sign(f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256())))
        sig = b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
        return f"vapid t={head}.{claims}.{sig}, k={self.public_key()}"

    async def send(self, message: dict[str, Any], *, client: httpx.AsyncClient | None = None,
                   only: str | None = None) -> dict[str, int]:
        """Разослать всем подписанным устройствам (или одному — only=endpoint). Устаревшие подписки удаляются."""
        subs = [s for s in self.subs() if only is None or s["endpoint"] == only]
        if not subs:
            return {"sent": 0, "failed": 0}
        data = json.dumps(message, ensure_ascii=False).encode()[:3500]
        own = client is None
        client = client or httpx.AsyncClient(timeout=15)
        sent = failed = 0
        try:
            for s in subs:
                try:
                    body = encrypt(data, s["keys"]["p256dh"], s["keys"]["auth"])
                    r = await client.post(s["endpoint"], content=body, headers={
                        "Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream", "TTL": "86400",
                        "Urgency": "high" if message.get("urgent") else "normal", "Authorization": self._vapid(s["endpoint"])})
                    if r.status_code in (404, 410):  # устройство отписалось или приложение удалено
                        self.remove(s["endpoint"])
                        failed += 1
                    elif r.status_code >= 400:
                        log.warning("уведомление не принято (%s): %s", r.status_code, r.text[:200])
                        failed += 1
                    else:
                        sent += 1
                except Exception as e:  # noqa: BLE001 — уведомление не должно мешать заседанию
                    log.warning("уведомление не отправлено: %s", e)
                    failed += 1
        finally:
            if own:
                await client.aclose()
        return {"sent": sent, "failed": failed}
