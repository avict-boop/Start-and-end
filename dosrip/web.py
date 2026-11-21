"""Простой веб-интерфейс: страница загрузки zip -> отчёт и архив с результатом."""

import tempfile
import uuid
from pathlib import Path

from flask import Flask, abort, redirect, request, send_from_directory, url_for

from .pipeline import UnsafeArchive, process_zip, zip_dir

PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>dosrip</title>
<style>:root{--bg:#f6f5f1;--fg:#1d1d1b;--card:#fff;--line:#e3e1da;--accent:#2f6f5e}
@media (prefers-color-scheme:dark){:root{--bg:#161614;--fg:#ecebe6;--card:#21211e;--line:#34332f;--accent:#7cc4ad}}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif}
main{max-width:560px;margin:10vh auto;padding:0 16px}form{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:20px}
button{margin-top:14px;background:var(--accent);color:#fff;border:0;border-radius:6px;padding:10px 18px;font-size:15px;cursor:pointer}
.err{color:#c0392b}</style></head><body><main><h1>dosrip</h1>
<p>Загрузите zip со всеми файлами DOS-игры. Программа достанет графику, звук, музыку и разберёт код.</p>
{error}<form method="post" enctype="multipart/form-data" action="/upload">
<input type="file" name="game" accept=".zip" required><br><button>Восстановить</button></form>
<p style="opacity:.7;font-size:14px">Обработка большой игры может занять пару минут.</p></main></body></html>"""


def create_app(work_dir=None):
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 1024 ** 3
    root = Path(work_dir or tempfile.mkdtemp(prefix="dosrip_"))

    @app.get("/")
    def index():
        return PAGE.replace("{error}", "")

    @app.post("/upload")
    def upload():
        f = request.files.get("game")
        if not f or not f.filename.lower().endswith(".zip"):
            return PAGE.replace("{error}", "<p class='err'>Нужен файл .zip</p>"), 400
        job = uuid.uuid4().hex[:12]
        jd = root / job
        jd.mkdir(parents=True)
        zpath = jd / "game.zip"
        f.save(zpath)
        out = jd / "result"
        try:
            process_zip(zpath, out, log=lambda *_: None, name=Path(f.filename).stem)
        except (UnsafeArchive, OSError, ValueError) as e:
            return PAGE.replace("{error}", f"<p class='err'>Ошибка: {e}</p>"), 400
        zip_dir(out, jd / "result.zip")
        return redirect(url_for("files", job=job, path="report.html"))

    @app.get("/job/<job>/download")
    def download(job):
        if not job.isalnum():
            abort(404)
        return send_from_directory(root / job, "result.zip", as_attachment=True, download_name="restored.zip")

    @app.get("/job/<job>/<path:path>")
    def files(job, path):
        if not job.isalnum():
            abort(404)
        result = root / job / "result"
        if path == "report.html":
            report = result / "report.html"
            if not report.is_file():
                abort(404)
            links = (f"<main><p><a href='/job/{job}/download'>Скачать всё одним zip</a> · "
                     f"<a href='/'>Загрузить другую игру</a></p>")
            return report.read_text(encoding="utf-8").replace("<main>", links, 1)
        return send_from_directory(result, path)

    return app
