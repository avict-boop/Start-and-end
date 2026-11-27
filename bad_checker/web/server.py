"""Веб-интерфейс анализатора БАД (только стандартная библиотека Python).

Запуск:  python -m bad_checker.web  [--host 127.0.0.1] [--port 8000]
"""
from __future__ import annotations

import argparse
import base64
import json
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..analyzer import analyze
from ..ocr import OCRUnavailable, image_to_text
from ..parser import product_from_text
from ..report import to_dict, to_markdown

STATIC = Path(__file__).parent / "static"
MAX_BODY = 15 * 1024 * 1024  # 15 МБ — фото этикетки с запасом


def ocr_status() -> dict:
    try:
        from ..ocr import _deps
        _deps()
        return {"available": True}
    except OCRUnavailable as e:
        return {"available": False, "reason": str(e)}


def handle_analyze(payload: dict) -> dict:
    ocr_text = ""
    image = payload.get("image")
    if image:
        m = re.match(r"data:image/[\w.+-]+;base64,(.*)", image, re.S)
        raw = base64.b64decode(m.group(1) if m else image)
        ocr_text = image_to_text(raw)

    composition = (payload.get("composition_text") or "").strip()
    label = (payload.get("label_text") or "").strip()
    if ocr_text:
        composition = f"{composition}\n{ocr_text}".strip()
        label = f"{label}\n{ocr_text}".strip()

    try:
        per_day = float(str(payload.get("units_per_day") or 1).replace(",", "."))
    except ValueError:
        per_day = 0
    product = product_from_text(composition, name=(payload.get("name") or "Без названия").strip(),
                                units_per_day=per_day, label_text=label)
    product.amounts_per_unit = bool(payload.get("amounts_per_unit", True))
    report = analyze(product)
    result = to_dict(report)
    result["ocr_text"] = ocr_text
    result["markdown"] = to_markdown(report)
    return result


class Handler(BaseHTTPRequestHandler):
    server_version = "BADChecker/0.2"

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, data: dict) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/", "/index.html"):
            self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/api/health":
            self._json(200, {"ok": True, "ocr": ocr_status()})
        else:
            self._json(404, {"error": "Не найдено"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/api/analyze":
            return self._json(404, {"error": "Не найдено"})
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            return self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE if length else 400,
                              {"error": "Пустой запрос или файл больше 15 МБ"})
        try:
            payload = json.loads(self.rfile.read(length))
            self._json(200, handle_analyze(payload))
        except OCRUnavailable as e:
            self._json(503, {"error": str(e)})
        except (ValueError, KeyError) as e:
            self._json(400, {"error": f"Некорректные данные: {e}"})

    def log_message(self, fmt, *args):  # тише в консоли
        print(f"[web] {self.address_string()} {fmt % args}")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="bad_checker.web", description="Веб-интерфейс анализатора БАД")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args(argv)
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Анализатор БАД: http://{args.host}:{args.port}  (Ctrl+C — остановить)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass

