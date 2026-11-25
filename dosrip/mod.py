"""Режим мода: выгрузка ресурсов для правки и сборка игры с изменёнными ресурсами.

    результат/mod/           <- здесь пользователь правит PNG, WAV, TXT, строки
    результат/mod/manifest.json  (служебный: откуда взят каждый ресурс)
    результат/patched/       <- собранная игра с правками + dosbox.conf
"""

import hashlib
import json
import shutil
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from PIL import Image

from .formats import encode, images
from .formats.encode import FitError
from .util import find_strings, safe_name

IMAGE_TYPES = {"pcx", "bmp", "gif", "raw-vga"}
RAW_EXT = {"midi": "mid", "riff-midi": "rmi", "xmi": "xmi", "cmf": "cmf", "mod": "mod", "s3m": "s3m",
           "iff-image": "lbm", "fli": "fli"}

MOD_README = """РЕЖИМ МОДА dosrip
=================

1. Правьте файлы в этой папке:
   graphics/  — картинки (PNG). Можно перерисовать в любом редакторе.
                Размер должен остаться прежним, либо ровно в 2, 3 или 4 раза больше:
                при сборке картинка будет уменьшена до размера игры.
                Цвета подбираются к палитре игры автоматически. Если сохранить PNG
                как индексированный (256 цветов), его палитра будет записана в игру
                (если формат картинки хранит свою палитру).
                Прозрачные пиксели становятся цветом №0.
   palettes/  — палитры VGA: 16x16 клеток, каждая клетка — один из 256 цветов.
                Меняете цвет клетки — меняется этот цвет во всей игре.
   sound/     — звуки (WAV). Частота и длительность подгонятся под игру:
                длинный звук будет обрезан, короткий — дополнен тишиной.
   text/      — текстовые файлы игры.
   strings/   — строки внутри программ (EXE): «смещение<TAB>текст».
                Меняйте только текст после табуляции. Новая строка не может быть
                длиннее старой. Для русского перевода укажите в settings.json
                "strings_encoding": "cp866" (и нужен шрифт с кириллицей в самой игре).
   raw/       — музыка и прочие ресурсы в исходном формате игры (MIDI, XMI, MOD...).
                Замените файл другим файлом того же формата.

2. Нажмите «Собрать игру с правками» в приложении или выполните:
       dosrip build <папка результата>

3. Готовая игра появится в папке patched/ рядом. Её можно запустить в DOSBox
   (рядом лежит dosbox.conf).

Ограничения: оригинальный движок игры показывает только то, на что был рассчитан
(обычно 320x200 и 256 цветов). Поэтому «улучшение» = перерисовка в том же размере
и палитре. Ресурсы внутри архивов игры должны занимать ровно столько же места,
сколько исходные; если не помещаются, сборка сообщит, что упростить.
Не удаляйте manifest.json. Удалённый файл = оставить оригинал.
"""


def sha1_file(p: Path) -> str:
    return hashlib.sha1(p.read_bytes()).hexdigest()


def _strings_for(data: bytes):
    """Строки, похожие на текст для человека (а не на мусор из кода)."""
    for off, s in find_strings(data, 4):
        letters = sum(c.isalpha() for c in s)
        if letters >= 3 and letters >= len(s) * 0.4:
            yield off, s


# ================================================================ выгрузка

def export_mod(out: Path, report) -> Path:
    mod = out / "mod"
    if mod.exists():
        shutil.rmtree(mod)
    mod.mkdir(parents=True)
    (mod / "README.txt").write_text(MOD_README, encoding="utf-8")
    (mod / "settings.json").write_text(json.dumps({"strings_encoding": "cp437"}, indent=2), encoding="utf-8")
    entries, seen = [], set()

    def add(kind_dir, name, data_or_src, entry):
        d = mod / kind_dir
        d.mkdir(parents=True, exist_ok=True)
        dest = d / name
        if isinstance(data_or_src, Path):
            shutil.copyfile(data_or_src, dest)
        else:
            dest.write_bytes(data_or_src)
        entry["file"] = f"{kind_dir}/{name}"
        entry["hash"] = sha1_file(dest)
        entries.append(entry)

    for f in report.files:
        src_file = out / "original" / f.path
        for o in f.outputs:
            key = (o.source, o.offset, o.fmt)
            if key in seen:
                continue
            base = {"type": o.fmt, "source": o.source, "offset": o.offset, "length": o.size,
                    "copies": report.copies.get(o.sha1, []) if o.sha1 else []}
            name = Path(o.path).name
            if o.kind == "image" and o.fmt in IMAGE_TYPES:
                seen.add(key)
                add("graphics", name, out / o.path, {**base, "width": o.width, "height": o.height,
                                                    "palette_source": o.extra.get("palette_source", "")})
            elif o.kind == "palette":
                seen.add(key)
                add("palettes", name, out / o.path, base)
            elif o.kind == "sound" and o.path.endswith(".wav") and o.fmt in ("voc", "wav"):
                seen.add(key)
                add("sound", name, out / o.path, base)
            elif o.kind == "text":
                seen.add(key)
                raw = src_file.read_bytes()
                add("text", name, out / o.path, {**base, "encoding": o.extra.get("encoding", "cp437"),
                                                "crlf": b"\r\n" in raw, "eof": raw.endswith(b"\x1a")})
            elif o.fmt in RAW_EXT or o.kind in ("image", "animation", "music"):
                seen.add(key)
                blob = src_file.read_bytes()[o.offset: o.offset + o.size]
                stem = f"{safe_name(o.source)}@{o.offset:06X}"
                add("raw", f"{stem}.{RAW_EXT.get(o.fmt, o.fmt)}", blob, base)

        # строки из программ
        if f.exe and "LE" not in f.exe.get("формат", "") and "+" not in f.exe.get("формат", ""):
            code_dir = out / "code" / safe_name(f.path)
            unpacked = code_dir / "unpacked.exe"
            exe_bytes = unpacked.read_bytes() if unpacked.exists() else src_file.read_bytes()
            lines = [f"# {f.path}: строки программы. Формат: смещение<TAB>текст. Меняйте только текст.",
                     "# Новая строка не длиннее старой; '$' в конце строк DOS сохраняется автоматически."]
            count = 0
            for off, s in _strings_for(exe_bytes):
                lines.append(f"{off:06X}\t{s}")
                count += 1
            if count:
                add("strings", safe_name(f.path) + ".tsv", "\n".join(lines).encode("utf-8") + b"\n",
                    {"type": "strings", "source": f.path, "offset": 0, "length": 0, "copies": [],
                     "target": "unpacked" if unpacked.exists() else "original",
                     "unpacked": str(unpacked.relative_to(out)) if unpacked.exists() else ""})

    for i, e in enumerate(entries):
        e["id"] = i
    (mod / "manifest.json").write_text(json.dumps({"version": 1, "entries": entries}, ensure_ascii=False,
                                                  indent=1), encoding="utf-8")
    return mod


# ================================================================== сборка

@dataclass
class BuildItem:
    file: str
    status: str  # ok, warn, error
    message: str = ""


@dataclass
class BuildResult:
    patched_dir: str
    items: list = field(default_factory=list)
    unchanged: int = 0
    dosbox_conf: str = ""
    main_exe: str = ""

    @property
    def errors(self):
        return [i for i in self.items if i.status == "error"]


def _new_blob(e, edited: Path, orig: bytes, whole: bool, patched: Path):
    """-> (новые байты, предупреждение)."""
    t = e["type"]
    if t == "pcx":
        img0, _ = images.decode_pcx(orig)
        pal = img0.getpalette()
        idx, own = encode.to_indices(Image.open(edited), img0.width, img0.height, pal)
        return encode.encode_pcx(orig, idx, own, exact_length=None if whole else len(orig)), ""
    if t == "bmp":
        img0 = images.pil_open(orig)
        if img0.mode != "P":
            raise FitError("перекодирование поддерживается только для 256-цветных BMP")
        idx, own = encode.to_indices(Image.open(edited), img0.width, img0.height, img0.getpalette())
        return encode.patch_bmp(orig, idx, own), ""
    if t == "gif":
        img0 = images.pil_open(orig)
        pal = img0.convert("P").getpalette()
        idx, own = encode.to_indices(Image.open(edited), img0.width, img0.height, pal)
        return _fit_raw(encode.encode_gif(idx, img0.width, img0.height, own or pal), len(orig), whole), ""
    if t == "raw-vga":
        warn = ""
        embedded = None
        if len(orig) == 64768:
            embedded = "end" if images.is_vga_palette(orig[-768:]) else "start"
            pal_bytes = orig[-768:] if embedded == "end" else orig[:768]
            pal = images.vga_palette_to_rgb(pal_bytes)
        else:
            psrc = e.get("palette_source")
            pal_file = patched / psrc if psrc else None
            pal = (images.vga_palette_to_rgb(pal_file.read_bytes()) if pal_file and pal_file.exists()
                   else images.DEFAULT_PALETTE)
        idx, own = encode.to_indices(Image.open(edited), 320, 200, pal)
        new_pal = encode.rgb_to_vga6(own) if own else None
        if own and not embedded:
            warn = "палитра из PNG не записана: этот экран использует общую палитру игры (правьте её в palettes/)"
        if embedded == "end":
            return idx + (new_pal or orig[-768:]), warn
        if embedded == "start":
            return (new_pal or orig[:768]) + idx, warn
        return idx, warn
    if t == "vga-palette":
        return encode.rgb_to_vga6(encode.read_swatch(Image.open(edited))), ""
    if t == "voc":
        return encode.patch_voc(orig, edited.read_bytes())
    if t == "wav":
        return encode.patch_wav(orig, edited.read_bytes())
    if t == "text":
        return encode.encode_text(edited.read_text(encoding="utf-8"), e["encoding"], e["crlf"], e["eof"]), ""
    return _fit_raw(edited.read_bytes(), len(orig), whole), ""


def _fit_raw(blob: bytes, length: int, whole: bool) -> bytes:
    if whole or len(blob) == length:
        return blob
    if len(blob) > length:
        raise FitError(f"новый файл {len(blob)} байт, а место в игре только {length} байт")
    return blob + bytes(length - len(blob))


def _write_at(patched: Path, source: str, offset: int, length: int, blob: bytes):
    target = patched / source
    data = bytearray(target.read_bytes())
    whole = offset == 0 and length == len(data)
    if whole:
        target.write_bytes(blob)
        return
    if len(blob) != length:
        raise FitError(f"{source}: ресурс должен занимать ровно {length} байт, получилось {len(blob)}")
    data[offset: offset + length] = blob
    target.write_bytes(bytes(data))


def _apply_strings(e, edited: Path, out: Path, patched: Path, settings: dict):
    enc = settings.get("strings_encoding", "cp437")
    target = patched / e["source"]
    base = (out / e["unpacked"]).read_bytes() if e["target"] == "unpacked" else target.read_bytes()
    original = dict(_strings_for(base))
    data = bytearray(base)
    changed, problems = 0, []
    for n, line in enumerate(edited.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.startswith("#") or "\t" not in line:
            continue
        off_s, text = line.split("\t", 1)
        try:
            off = int(off_s, 16)
        except ValueError:
            problems.append(f"строка {n}: неверное смещение «{off_s}»")
            continue
        old = original.get(off)
        if old is None:
            problems.append(f"строка {n}: по смещению {off_s} нет исходной строки")
            continue
        if text == old:
            continue
        old_b = old.encode("ascii")
        term = data[off + len(old_b)] if off + len(old_b) < len(data) else 0
        try:
            data[off: off + len(old_b)] = encode.fit_string(old_b, text, enc, term)
            changed += 1
        except FitError as ex:
            problems.append(f"строка {n} ({off_s}): {ex}")
    if changed:
        target.write_bytes(bytes(data))
    msg = f"изменено строк: {changed}"
    if e["target"] == "unpacked" and changed:
        msg += "; программа записана в распакованном виде"
    return changed, problems, msg


def _guess_main_exe(patched: Path) -> str:
    skip = ("install", "setup", "setsound", "sndsetup", "config", "uninst", "readme")
    best = None
    for p in patched.rglob("*"):
        if p.suffix.lower() not in (".exe", ".com", ".bat") or any(k in p.stem.lower() for k in skip):
            continue
        score = (-len(p.relative_to(patched).parts), p.suffix.lower() == ".exe", p.stat().st_size)
        if best is None or score > best[0]:
            best = (score, p)
    return best[1].relative_to(patched).as_posix() if best else ""


def write_dosbox_conf(patched: Path, main_exe: str) -> Path:
    exe = Path(main_exe)
    lines = ["[sdl]", "fullscreen=false", "", "[cpu]", "cycles=auto", "", "[autoexec]",
             f'mount c "{patched}"', "c:"]
    if exe.parent.as_posix() not in ("", "."):
        lines.append("cd \\" + "\\".join(exe.parent.parts))
    if main_exe:
        lines.append(exe.name)
    conf = patched / "dosbox.conf"
    conf.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return conf


def build_mod(out, log=print) -> BuildResult:
    out = Path(out)
    mod = out / "mod"
    manifest = json.loads((mod / "manifest.json").read_text(encoding="utf-8"))
    try:
        settings = json.loads((mod / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        settings = {}
    patched = out / "patched"
    if patched.exists():
        shutil.rmtree(patched)
    shutil.copytree(out / "original", patched)
    res = BuildResult(str(patched))

    order = sorted(manifest["entries"], key=lambda e: (e["type"] != "vga-palette", e["id"]))
    for e in order:
        edited = mod / e["file"]
        if not edited.exists():
            continue
        if sha1_file(edited) == e["hash"]:
            res.unchanged += 1
            continue
        log(f"  {e['file']}")
        try:
            if e["type"] == "strings":
                changed, problems, msg = _apply_strings(e, edited, out, patched, settings)
                for p in problems:
                    res.items.append(BuildItem(e["file"], "error", p))
                if changed or not problems:
                    res.items.append(BuildItem(e["file"], "ok", msg))
                continue
            src = patched / e["source"]
            full = (out / "original" / e["source"]).read_bytes()
            orig = full[e["offset"]: e["offset"] + e["length"]]
            whole = e["offset"] == 0 and e["length"] == len(full)
            blob, warn = _new_blob(e, edited, orig, whole, patched)
            _write_at(patched, e["source"], e["offset"], e["length"], blob)
            copies = 0
            for csrc, coff, clen in e.get("copies", []):
                try:
                    _write_at(patched, csrc, coff, clen, blob)
                    copies += 1
                except FitError as ex:
                    res.items.append(BuildItem(e["file"], "warn", f"копия в {csrc}: {ex}"))
            msg = f"записано в {src.relative_to(patched)}" + (f" и ещё {copies} копий" if copies else "")
            res.items.append(BuildItem(e["file"], "warn" if warn else "ok", msg + (f". {warn}" if warn else "")))
        except FitError as ex:
            res.items.append(BuildItem(e["file"], "error", str(ex)))
        except Exception as ex:  # noqa: BLE001 - битый файл пользователя не должен ронять сборку
            res.items.append(BuildItem(e["file"], "error", f"не удалось обработать файл: {ex}"))

    res.main_exe = _guess_main_exe(patched)
    res.dosbox_conf = str(write_dosbox_conf(patched, res.main_exe))
    (out / "build_report.json").write_text(json.dumps(asdict(res), ensure_ascii=False, indent=2), encoding="utf-8")
    return res


# ================================================================== DOSBox

DOSBOX_APPS = ["DOSBox Staging.app", "dosbox-staging.app", "dosbox-x.app", "DOSBox-X.app", "DOSBox.app"]


def find_dosbox():
    """-> команда запуска (список) без аргументов или None."""
    if sys.platform == "darwin":
        for root in (Path("/Applications"), Path.home() / "Applications"):
            for name in DOSBOX_APPS:
                if (root / name).exists():
                    return ["open", "-a", str(root / name), "--args"]
    for exe in ("dosbox-staging", "dosbox-x", "dosbox"):
        found = shutil.which(exe)
        if found:
            return [found]
    return None
