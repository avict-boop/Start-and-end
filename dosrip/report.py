"""HTML-отчёт: галерея графики, проигрыватели звука, ссылки на музыку и код."""

from html import escape
from urllib.parse import quote

CSS = """
:root{--bg:#f6f5f1;--fg:#1d1d1b;--muted:#6b6a64;--card:#fff;--line:#e3e1da;--accent:#2f6f5e}
@media (prefers-color-scheme:dark){:root{--bg:#161614;--fg:#ecebe6;--muted:#9d9b93;--card:#21211e;--line:#34332f;--accent:#7cc4ad}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px 64px}h1{font-size:26px;margin:0 0 4px}
h2{font-size:18px;margin:36px 0 12px;padding-bottom:6px;border-bottom:1px solid var(--line)}
.muted{color:var(--muted)}.stats{display:flex;flex-wrap:wrap;gap:10px;margin:16px 0}
.stat{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 14px;min-width:120px}
.stat b{display:block;font-size:22px}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:12px}
figure{margin:0;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px}
figure img{width:100%;height:130px;object-fit:contain;image-rendering:pixelated;background:repeating-conic-gradient(#8882 0 25%,transparent 0 50%) 0 0/16px 16px}
figcaption{font-size:12px;color:var(--muted);word-break:break-all}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:8px;overflow:hidden}
td,th{text-align:left;padding:6px 10px;border-bottom:1px solid var(--line);vertical-align:top;font-size:14px}
th{color:var(--muted);font-weight:500}a{color:var(--accent)}audio{width:100%;max-width:320px;height:32px}
code{font-size:13px}.exe{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 14px;margin-bottom:12px}
.exe h3{margin:0 0 8px;font-size:16px}.exe dl{display:grid;grid-template-columns:max-content 1fr;gap:2px 14px;margin:0;font-size:14px}
.exe dt{color:var(--muted)}.exe dd{margin:0}
button{background:var(--accent);color:#fff;border:0;border-radius:8px;padding:8px 16px;font-size:14px;cursor:pointer}
"""


def _a(path, text=None):
    return f'<a href="{quote(path)}">{escape(text or path.split("/")[-1])}</a>'


def write_html(report, dest):
    outs = report.outputs
    images = outs("image")
    anims = outs("animation")
    sounds = outs("sound")
    music = outs("music")
    palettes = outs("palette")
    texts = outs("text")
    exes = [f for f in report.files if f.exe]
    unknown = [f for f in report.files if not f.outputs and f.notes]

    h = [f"<!doctype html><html lang='ru'><head><meta charset='utf-8'>"
         f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
         f"<title>{escape(report.game)} — dosrip</title><style>{CSS}</style></head><body><main>",
         f"<h1>{escape(report.game)}</h1><div class='muted'>Отчёт dosrip {escape(report.version)}</div>",
         "<div class='stats'>"]
    for n, label in [(len(report.files), "файлов в архиве"), (len(images), "изображений"), (len(anims), "анимаций"),
                     (len(sounds), "звуков"), (len(music), "музыки"), (len(exes), "программ"),
                     (len(texts), "текстов"), (len(unknown), "не распознано")]:
        h.append(f"<div class='stat'><b>{n}</b>{label}</div>")
    h.append("</div>")

    def gallery(title, items):
        if not items:
            return
        h.append(f"<h2>{title}</h2><div class='grid'>")
        for o in items:
            size = f"{o.width}×{o.height} · " if o.width else ""
            h.append(f"<figure><a href='{quote(o.path)}'><img loading='lazy' src='{quote(o.path)}' alt=''></a>"
                     f"<figcaption>{size}{escape(o.source)} @ {o.offset:#x}"
                     f"{'<br>' + escape(o.note) if o.note else ''}</figcaption></figure>")
        h.append("</div>")

    gallery("Графика", images)
    gallery("Анимации", anims)
    gallery("Палитры", palettes)

    if sounds:
        h.append("<h2>Звук</h2><table><tr><th>Файл</th><th>Источник</th><th>Прослушать</th></tr>")
        for o in sounds:
            player = f"<audio controls preload='none' src='{quote(o.path)}'></audio>" if o.path.endswith(".wav") else ""
            h.append(f"<tr><td>{_a(o.path)}<div class='muted'>{escape(o.note)}</div></td>"
                     f"<td>{escape(o.source)} @ {o.offset:#x}</td><td>{player}</td></tr>")
        h.append("</table>")

    if music:
        h.append("<h2>Музыка</h2><p class='muted'>MIDI открывается в любом плеере MIDI, "
                 "MOD/S3M — в VLC или OpenMPT.</p><table><tr><th>Файл</th><th>Источник</th><th>Примечание</th></tr>")
        for o in music:
            h.append(f"<tr><td>{_a(o.path)}</td><td>{escape(o.source)} @ {o.offset:#x}</td><td>{escape(o.note)}</td></tr>")
        h.append("</table>")

    if exes:
        h.append("<h2>Код</h2>")
        for f in exes:
            h.append(f"<div class='exe'><h3>{escape(f.path)}</h3><dl>")
            for k, v in f.exe.items():
                h.append(f"<dt>{escape(str(k))}</dt><dd>{escape(str(v))}</dd>")
            links = " · ".join(_a(o.path, o.note or None) for o in f.outputs if o.kind == "code")
            h.append(f"</dl><p>{links}</p>")
            for n in f.notes:
                h.append(f"<p class='muted'>{escape(n)}</p>")
            h.append("</div>")

    if texts:
        h.append("<h2>Тексты</h2><ul>")
        for o in texts:
            h.append(f"<li>{_a(o.path, o.source)}</li>")
        h.append("</ul>")

    if unknown:
        h.append("<h2>Не распознано</h2><table><tr><th>Файл</th><th>Размер</th><th>Энтропия</th><th>Почему</th></tr>")
        for f in unknown:
            h.append(f"<tr><td>{_a('original/' + f.path, f.path)}</td><td>{f.size:,}</td>"
                     f"<td>{f.entropy}</td><td>{escape('; '.join(f.notes))}</td></tr>")
        h.append("</table><p class='muted'>Энтропия около 8 означает сжатые данные. Для таких файлов нужен "
                 "плагин под формат конкретной игры.</p>")

    h.append("</main></body></html>")
    dest.write_text("\n".join(h), encoding="utf-8")
