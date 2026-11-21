"""Приложение на телефоне: описание, служебный работник, вход с запоминанием устройства, уведомления (Web Push)."""
from __future__ import annotations

import asyncio
import json
import struct

import httpx
import yaml
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi.testclient import TestClient

from council.web.app import create_app
from council.web.push import Push, _hkdf, b64u, encrypt, unb64u


def _cfg(tmp_path):
    cfg = {"chair": "a", "runs_dir": str(tmp_path / "runs"), "members": [{"name": n, "model": f"m/{n}"} for n in "ab"]}
    (tmp_path / "c.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return tmp_path / "c.yaml"


def _device():
    """Ключи «телефона»: как их создаёт браузер при подписке."""
    k = ec.generate_private_key(ec.SECP256R1())
    pub = k.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return k, b64u(pub), b64u(b"0123456789abcdef")


def _decrypt(body: bytes, ua_key, p256dh: str, auth: str) -> bytes:
    """Расшифровка так, как это делает браузер (RFC 8291)."""
    salt, rs, idlen = body[:16], struct.unpack("!I", body[16:20])[0], body[20]
    as_pub = body[21:21 + idlen]
    assert rs == 4096
    shared = ua_key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_pub))
    ikm = _hkdf(unb64u(auth), b"WebPush: info\x00" + unb64u(p256dh) + as_pub, shared, 32)
    cek = _hkdf(salt, b"Content-Encoding: aes128gcm\x00", ikm, 16)
    nonce = _hkdf(salt, b"Content-Encoding: nonce\x00", ikm, 12)
    plain = AESGCM(cek).decrypt(nonce, body[21 + idlen:], None)
    assert plain.endswith(b"\x02")
    return plain[:-1]


def test_push_encrypt_roundtrip():
    k, p256dh, auth = _device()
    msg = json.dumps({"title": "Совет вынес решение"}, ensure_ascii=False).encode()
    assert _decrypt(encrypt(msg, p256dh, auth), k, p256dh, auth) == msg


def test_push_send_signs_and_drops_gone(tmp_path):
    p = Push(tmp_path / "push", contact="mailto:a@b.c")
    k, p256dh, auth = _device()
    p.add({"endpoint": "https://push.example/a", "keys": {"p256dh": p256dh, "auth": auth}}, "iPhone")
    p.add({"endpoint": "https://push.example/gone", "keys": {"p256dh": p256dh, "auth": auth}})
    p.add({"endpoint": "https://push.example/a", "keys": {"p256dh": p256dh, "auth": auth}}, "iPhone")  # повтор — не дубль
    assert len(p.subs()) == 2
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(410 if req.url.path == "/gone" else 201)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await p.send({"title": "T", "body": "B", "tag": "x"}, client=c)

    r = asyncio.run(go())
    assert r == {"sent": 1, "failed": 1}
    assert [s["endpoint"] for s in p.subs()] == ["https://push.example/a"]  # отписавшееся устройство удалено
    req = seen[0]
    assert req.headers["content-encoding"] == "aes128gcm" and req.headers["ttl"]
    assert json.loads(_decrypt(req.content, k, p256dh, auth)) == {"title": "T", "body": "B", "tag": "x"}
    # подпись VAPID проверяется открытым ключом сервера
    auth_h = req.headers["authorization"]
    token = auth_h.split("t=")[1].split(",")[0]
    head, claims, sig = token.split(".")
    assert json.loads(unb64u(claims))["aud"] == "https://push.example"
    raw = unb64u(sig)
    pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), unb64u(p.public_key()))
    pub.verify(encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
               f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))
    assert Push(tmp_path / "push").public_key() == p.public_key()  # ключ сервера сохраняется


def test_manifest_sw_and_push_api(tmp_path):
    app = create_app(_cfg(tmp_path), mock_delay=0, password="")
    with TestClient(app) as c:
        m = c.get("/manifest.webmanifest").json()
        assert m["display"] == "standalone" and m["start_url"].startswith("/")
        assert {i["purpose"] for i in m["icons"]} == {"any", "maskable"}
        for i in m["icons"]:
            assert c.get(i["src"]).status_code == 200
        sw = c.get("/sw.js")
        assert sw.status_code == 200 and "__VERSION__" not in sw.text and "__SHELL__" not in sw.text
        shell = json.loads(sw.text.split("const SHELL = ")[1].split(";\n")[0])
        for u in shell:
            assert c.get(u).status_code == 200, u
        page = c.get("/").text
        assert 'rel="manifest"' in page and "/static/pwa.js?v=" in page and "/static/selection.js?v=" in page
        key = c.get("/api/push/key").json()
        assert len(unb64u(key["key"])) == 65 and key["devices"] == 0
        _, p256dh, auth = _device()
        assert c.post("/api/push/subscribe", json={"subscription": {"endpoint": "http://bad", "keys": {}}}).status_code == 400
        r = c.post("/api/push/subscribe", json={"subscription": {"endpoint": "https://push.example/x",
                                                                 "keys": {"p256dh": p256dh, "auth": auth}}, "label": "Android"})
        assert r.json()["devices"] == 1
        assert c.post("/api/push/unsubscribe", json={"endpoint": "https://push.example/x"}).json()["ok"] is True
        assert c.get("/api/push/key").json()["devices"] == 0


def test_login_page_remembers_device(tmp_path, monkeypatch):
    monkeypatch.setenv("WEB_USER", "boss")
    app = create_app(_cfg(tmp_path), mock_delay=0, password="s3cret-pass")
    with TestClient(app, follow_redirects=False) as c:
        # браузер без входа — на страницу входа, а не системное окно; программа — по-прежнему 401 Basic
        r = c.get("/", headers={"Accept": "text/html"})
        assert r.status_code == 303 and r.headers["location"] == "/login"
        assert c.get("/api/config").status_code == 401
        # описание приложения и значки доступны без пароля (их читает система при установке)
        assert c.get("/manifest.webmanifest").status_code == 200
        assert c.get("/static/icons/icon-192.png").status_code == 200
        assert "Войти" in c.get("/login").text
        bad = c.post("/login", data={"user": "boss", "password": "nope"})
        assert bad.status_code == 401 and "Неверный" in bad.text and "council_auth" not in bad.headers.get("set-cookie", "")
        ok = c.post("/login", data={"user": "boss", "password": "s3cret-pass"})
        assert ok.status_code == 303 and "Max-Age=15552000" in ok.headers["set-cookie"] and "HttpOnly" in ok.headers["set-cookie"]
        assert c.get("/api/config").status_code == 200  # дальше — по запомненному входу
        out = c.get("/logout")
        assert out.status_code == 303 and "Max-Age=0" in out.headers["set-cookie"]
        c.cookies.clear()
        assert c.get("/api/config").status_code == 401
        c.cookies.set("council_auth", "9999999999.deadbeef")  # подделка не проходит
        assert c.get("/api/config").status_code == 401
    # смена пароля — запомненные входы больше не действуют
    app2 = create_app(_cfg(tmp_path), mock_delay=0, password="new-pass-123")
    with TestClient(app2, follow_redirects=False) as c2:
        c2.cookies.set("council_auth", ok.headers["set-cookie"].split(";")[0].split("=", 1)[1])
        assert c2.get("/api/config").status_code == 401
