"""Интерфейс загрузки zip -> отчёт и архив с результатом.

Работает и как веб-сервер (`dosrip serve`), и внутри окна macOS-приложения
(desktop=True: результаты сохраняются в ~/Documents/dosrip, есть кнопка «Показать в Finder»).
"""

import re
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from flask import Flask, abort, redirect, request, send_from_directory, url_for

from .pipeline import UnsafeArchive, process_zip, zip_dir
from .util import safe_name

JOB_RE = re.compile(r"^[A-Za-z0-9._-]{1,120}$")

PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>dosrip</title>
<style>
:root{--bg:#f6f5f1;--fg:#1d1d1b;--muted:#6b6a64;--card:#fff;--line:#d9d6cc;--accent:#2f6f5e;--hl:#e6f1ed}
@media (prefers-color-scheme:dark){:root{--bg:#161614;--fg:#ecebe6;--muted:#9d9b93;--card:#21211e;--line:#3a3934;--accent:#7cc4ad;--hl:#1e2d28}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 -apple-system,system-ui,sans-serif;-webkit-user-select:none;user-select:none}
main{max-width:620px;margin:0 auto;padding:48px 16px}
h1{font-size:28px;margin:0 0 4px}.muted{color:var(--muted)}
#drop{margin-top:24px;border:2px dashed var(--line);border-radius:14px;background:var(--card);padding:48px 20px;text-align:center;cursor:pointer;transition:.15s}
#drop.over{border-color:var(--accent);background:var(--hl)}
#drop b{display:block;font-size:18px;margin-bottom:6px}
button{background:var(--accent);color:#fff;border:0;border-radius:8px;padding:9px 18px;font-size:15px;cursor:pointer;margin-top:14px}
.err{color:#c0392b;margin-top:16px}
#busy{display:none;margin-top:24px;text-align:center}
.spin{width:34px;height:34px;border:3px solid var(--line);border-top-color:var(--accent);border-radius:50%;margin:0 auto 12px;animation:r 1s linear infinite}
@keyframes r{to{transform:rotate(360deg)}}
ul{padding-left:18px}li{margin:2px 0}a{color:var(--accent)}
</style></head><body><main>
<h1>dosrip</h1>
<div class="muted">Восстановление графики, звука, музыки и кода старых DOS-игр</div>
{error}
<form id="f" method="post" enctype="multipart/form-data" action="/upload">
<input id="file" type="file" name="game" accept=".zip,application/zip" hidden>
<div id="drop"><b>Перетащите сюда zip с игрой</b><span class="muted">или нажмите, чтобы выбрать файл</span></div>
</form>
<div id="busy"><div class="spin"></div><b id="busyname"></b><div class="muted">Идёт разбор. Большая игра может обрабатываться пару минут.</div></div>
{recent}
<script>
const drop=document.getElementById('drop'),file=document.getElementById('file'),form=document.getElementById('f');
function go(){if(!file.files.length)return;if(!/\\.zip$/i.test(file.files[0].name)){alert('Нужен файл .zip');return}
 form.style.display='none';document.getElementById('busy').style.display='block';
 document.getElementById('busyname').textContent=file.files[0].name;form.submit();}
drop.onclick=()=>file.click();file.onchange=go;
['dragenter','dragover'].forEach(e=>document.addEventListener(e,ev=>{ev.preventDefault();drop.classList.add('over')}));
['dragleave','drop'].forEach(e=>document.addEventListener(e,ev=>{ev.preventDefault();drop.classList.remove('over')}));
document.addEventListener('drop',ev=>{if(ev.dataTransfer.files.length){file.files=ev.dataTransfer.files;go()}});
</script></main></body></html>"""


def _job_name(filename: str) -> str:
    stem = safe_name(Path(filename).stem)[:60] or "game"
    return f"{stem}-{uuid.uuid4().hex[:6]}"


def reveal_in_finder(path: Path):
    if sys.platform == "darwin":
        subprocess.run(["open", str(path)], check=False)


def create_app(work_dir=None, desktop=False):
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 ** 3
    root = Path(work_dir or tempfile.mkdtemp(prefix="dosrip_"))
    root.mkdir(parents=True, exist_ok=True)

    def job_dir(job):
        if not JOB_RE.match(job) or job.startswith("."):
            abort(404)
        d = root / job
        if not d.is_dir():
            abort(404)
        return d

    def page(error=""):
        recent = ""
        if desktop:
            jobs = sorted((d for d in root.iterdir() if (d / "result" / "report.html").is_file()),
                          key=lambda d: d.stat().st_mtime, reverse=True)[:8]
            if jobs:
                items = "".join(f"<li><a href='/job/{d.name}/report.html'>{d.name}</a></li>" for d in jobs)
                recent = f"<h3>Недавние</h3><ul>{items}</ul><p class='muted'>Все результаты лежат в {root}</p>"
        err = f"<p class='err'>{error}</p>" if error else ""
        return PAGE.replace("{error}", err).replace("{recent}", recent)

    @app.get("/")
    def index():
        return page()

    @app.post("/upload")
    def upload():
        f = request.files.get("game")
        if not f or not f.filename.lower().endswith(".zip"):
            return page("Нужен файл .zip"), 400
        job = _job_name(f.filename)
        jd = root / job
        jd.mkdir(parents=True)
        zpath = jd / "game.zip"
        f.save(zpath)
        out = jd / "result"
        try:
            process_zip(zpath, out, log=lambda *_: None, name=Path(f.filename).stem)
            if not desktop:
                zip_dir(out, jd / "result.zip")
        except (UnsafeArchive, OSError, ValueError) as e:
            return page(f"Ошибка: {e}"), 400
        finally:
            zpath.unlink(missing_ok=True)
        return redirect(url_for("files", job=job, path="report.html"))

    @app.get("/job/<job>/download")
    def download(job):
        jd = job_dir(job)
        if not (jd / "result.zip").exists():
            zip_dir(jd / "result", jd / "result.zip")
        return send_from_directory(jd, "result.zip", as_attachment=True, download_name=f"{job}.zip")

    @app.post("/job/<job>/reveal")
    def reveal(job):
        reveal_in_finder(job_dir(job) / "result")
        return "", 204

    @app.get("/job/<job>/<path:path>")
    def files(job, path):
        result = job_dir(job) / "result"
        if path == "report.html":
            report = result / "report.html"
            if not report.is_file():
                abort(404)
            if desktop:
                bar = (f"<main><p><button onclick=\"fetch('/job/{job}/reveal',{{method:'POST'}})\">"
                       f"Показать в Finder</button> &nbsp; <a href='/'>Разобрать другую игру</a></p>"
                       f"<p class='muted'>Папка: {result}</p>")
            else:
                bar = (f"<main><p><a href='/job/{job}/download'>Скачать всё одним zip</a> · "
                       f"<a href='/'>Загрузить другую игру</a></p>")
            return report.read_text(encoding="utf-8").replace("<main>", bar, 1)
        return send_from_directory(result, path)

    return app
