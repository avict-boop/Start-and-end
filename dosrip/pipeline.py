"""Главный конвейер: zip с игрой -> папка с графикой, звуком, музыкой, кодом и отчётом."""

import hashlib
import json
import os
import shutil
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath

from . import __version__
from .carve import carve
from .exe.disasm import Disassembly
from .exe.mz import looks_packed, parse_le_summary, parse_mz
from .exe.unpack import unpack
from .formats import audio, images
from .util import entropy, find_strings, looks_like_text, safe_name

MAX_TOTAL_UNPACKED = 2 * 1024 ** 3
MAX_FILES = 20000
TEXT_EXT = {".txt", ".doc", ".nfo", ".diz", ".bat", ".cfg", ".ini", ".me", ".1st", ".faq", ".hlp", ".asc"}
EXE_EXT = {".exe", ".ovl", ".ovr", ".com", ".bin", ".drv", ".dat"}


@dataclass
class Output:
    kind: str  # image, animation, sound, music, code, text, palette
    path: str  # относительно папки результата
    source: str
    offset: int
    size: int
    fmt: str
    note: str = ""
    width: int = 0
    height: int = 0


@dataclass
class FileReport:
    path: str
    size: int
    entropy: float
    outputs: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    exe: dict = field(default_factory=dict)


@dataclass
class Report:
    game: str
    version: str = __version__
    files: list = field(default_factory=list)
    duplicates: int = 0

    def outputs(self, kind=None):
        return [o for f in self.files for o in f.outputs if kind is None or o.kind == kind]


class UnsafeArchive(Exception):
    pass


def safe_extract(zip_path, dest: Path):
    dest.mkdir(parents=True, exist_ok=True)
    total = 0
    with zipfile.ZipFile(zip_path) as z:
        infos = [i for i in z.infolist() if not i.is_dir()]
        if len(infos) > MAX_FILES:
            raise UnsafeArchive(f"слишком много файлов в архиве ({len(infos)})")
        for info in infos:
            name = PurePosixPath(info.filename.replace("\\", "/"))
            if name.is_absolute() or ".." in name.parts:
                raise UnsafeArchive(f"опасный путь в архиве: {info.filename}")
            total += info.file_size
            if total > MAX_TOTAL_UNPACKED:
                raise UnsafeArchive("архив слишком большой после распаковки")
            target = dest.joinpath(*name.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)


class Extractor:
    def __init__(self, out: Path, log=print):
        self.out = out
        self.log = log
        self.seen = set()
        self.duplicates = 0
        self.palette = None
        self.palette_source = ""

    def _write(self, sub: str, name: str, blob: bytes) -> str:
        d = self.out / sub
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_bytes(blob)
        return f"{sub}/{name}"

    def _save_img(self, sub, name, img) -> tuple:
        d = self.out / sub
        d.mkdir(parents=True, exist_ok=True)
        img.save(d / name)
        return f"{sub}/{name}", img.width, img.height

    def _dup(self, blob: bytes) -> bool:
        h = hashlib.sha1(blob).digest()
        if h in self.seen:
            self.duplicates += 1
            return True
        self.seen.add(h)
        return False

    # ------------------------------------------------------------ ресурсы
    def convert_hit(self, rep: FileReport, data: bytes, off: int, length: int, fmt: str):
        blob = data[off: off + length]
        if self._dup(blob):
            return
        base = f"{safe_name(rep.path)}@{off:06X}"
        O = lambda kind, path, note="", w=0, h=0: rep.outputs.append(  # noqa: E731
            Output(kind, path, rep.path, off, length, fmt, note, w, h))
        try:
            if fmt == "pcx":
                img, _ = images.decode_pcx(blob)
                O("image", *self._save_img("graphics", base + ".png", img)[:1], "", img.width, img.height)
            elif fmt in ("bmp", "gif"):
                img = images.pil_open(blob)
                if img is None:
                    return
                if getattr(img, "n_frames", 1) > 1:
                    O("animation", self._write("animations", base + ".gif", blob), f"{img.n_frames} кадров")
                path, w, h = self._save_img("graphics", base + ".png", img.convert("RGBA") if img.mode == "P" and "transparency" in img.info else img)
                O("image", path, "", w, h)
            elif fmt == "iff-image":
                img = images.decode_iff_image(blob)
                if img:
                    path, w, h = self._save_img("graphics", base + ".png", img)
                    O("image", path, "Deluxe Paint LBM/ILBM", w, h)
            elif fmt == "fli":
                r = images.fli_to_gif(blob)
                if r:
                    first, gif = r
                    O("animation", self._write("animations", base + ".gif", gif), "FLI/FLC -> GIF",
                      first.width, first.height)
                    self._write("animations", base + (".flc" if blob[4] == 0x12 else ".fli"), blob)
            elif fmt == "voc":
                wav = audio.voc_to_wav(blob)
                if wav:
                    O("sound", self._write("sound", base + ".wav", wav), "Creative VOC -> WAV")
                else:
                    O("sound", self._write("sound", base + ".voc", blob), "VOC со сжатием ADPCM, сохранён как есть")
            elif fmt == "wav":
                O("sound", self._write("sound", base + ".wav", blob))
            elif fmt == "riff-midi":
                O("music", self._write("music", base + ".rmi", blob), "RIFF MIDI")
            elif fmt == "midi":
                O("music", self._write("music", base + ".mid", blob))
            elif fmt == "xmi":
                self._write("music", base + ".xmi", blob)
                for i, mid in enumerate(audio.xmi_to_midis(blob)):
                    O("music", self._write("music", f"{base}_{i + 1}.mid", mid), "XMIDI (Miles) -> MIDI")
            elif fmt == "cmf":
                self._write("music", base + ".cmf", blob)
                mid = audio.cmf_to_midi(blob)
                if mid:
                    O("music", self._write("music", base + ".mid", mid), "CMF -> MIDI (инструменты OPL2 приблизительно)")
            elif fmt in ("mod", "s3m"):
                O("music", self._write("music", f"{base}.{fmt}", blob), "трекерная музыка")
        except Exception as e:  # noqa: BLE001 - повреждённые данные не должны ронять весь процесс
            rep.notes.append(f"{fmt} @ {off:#x}: не удалось преобразовать ({e})")

    def whole_file(self, rep: FileReport, data: bytes, ext: str):
        name = safe_name(rep.path)
        if images.is_vga_palette(data):
            pal = images.vga_palette_to_rgb(data)
            if self.palette is None:
                self.palette, self.palette_source = pal, rep.path
            path, w, h = self._save_img("graphics/palettes", name + ".png", images.palette_swatch(pal))
            rep.outputs.append(Output("palette", path, rep.path, 0, len(data), "vga-palette", "палитра VGA", w, h))
            return True
        screen = images.guess_raw_screen(data)
        if screen and entropy(data) < 7.9:
            pixels, pal = screen
            note = "экран VGA 320x200 со своей палитрой"
            if pal is None:
                pal = self.palette or images.DEFAULT_PALETTE
                note = (f"экран VGA 320x200, палитра из {self.palette_source}" if self.palette
                        else "экран VGA 320x200, палитра приблизительная")
            if not self._dup(data):
                path, w, h = self._save_img("graphics", name + ".png", images.raw_vga_screen(pixels, pal))
                rep.outputs.append(Output("image", path, rep.path, 0, len(data), "raw-vga", note, w, h))
            return True
        if looks_like_text(data) and (ext in TEXT_EXT or len(data) < 200_000):
            text = data.replace(b"\x1a", b"").decode("cp437")
            path = self._write("text", name + ".txt", text.encode("utf-8"))
            rep.outputs.append(Output("text", path, rep.path, 0, len(data), "text", "CP437 -> UTF-8"))
            return True
        return False

    # ---------------------------------------------------------------- код
    def code(self, rep: FileReport, data: bytes, ext: str):
        sub = f"code/{safe_name(rep.path)}"
        d = self.out / sub
        info = {}
        if data[:2] in (b"MZ", b"ZM"):
            mz = parse_mz(data)
            if not mz:
                return
            info.update({
                "формат": "DOS MZ EXE" + (f" + {mz.new_exe}" if mz.new_exe else ""),
                "точка входа": f"{mz.cs:04X}:{mz.ip:04X}",
                "размер образа": len(mz.image),
                "релокаций": len(mz.relocs),
                "оверлей (данные после EXE)": len(mz.overlay),
                "упаковщик": mz.packer or "не обнаружен",
                "компилятор": ", ".join(mz.compiler) or "не определён",
            })
            if mz.extender:
                info["DOS-расширитель"] = mz.extender
            d.mkdir(parents=True, exist_ok=True)
            if mz.new_exe in ("LE", "LX"):
                info.update({f"LE: {k}": v for k, v in parse_le_summary(data, mz.new_exe_offset).items()})
                rep.notes.append("32-битная программа защищённого режима (LE). Код сохранён в разобранном виде "
                                 "не будет; откройте файл в Ghidra или IDA (загрузчик LE/LX).")
            elif mz.new_exe:
                rep.notes.append(f"Файл формата {mz.new_exe} (Windows/OS2), анализируется только DOS-заглушка.")
            if not mz.new_exe and looks_packed(mz):
                self.log(f"  распаковка {rep.path} в эмуляторе...")
                res = unpack(data)
                if res:
                    self._write(sub, "unpacked.exe", res.exe)
                    info["распаковано"] = (f"да, слоёв: {res.steps}, исходная точка входа "
                                           f"{res.oep[0]:04X}:{res.oep[1]:04X}, релокаций: {res.relocs}")
                    rep.outputs.append(Output("code", f"{sub}/unpacked.exe", rep.path, 0, len(res.exe), "exe",
                                              "распакованный EXE"))
                    if not mz.packer:
                        info["упаковщик"] = "неизвестный (снят эмуляцией)"
                    mz = parse_mz(res.exe)
                else:
                    info["распаковано"] = "нет (не удалось найти исходную точку входа)"
            image, cs, ip, relocs = mz.image, mz.cs, mz.ip, mz.reloc_linear()
            if mz.new_exe in ("LE", "LX", "PE"):
                image = b""
        else:  # .COM: загружается по смещению 100h
            info.update({"формат": "DOS COM", "точка входа": "0000:0100", "размер": len(data)})
            d.mkdir(parents=True, exist_ok=True)
            image, cs, ip, relocs = bytes(0x100) + data, 0, 0x100, []

        if image:
            dis = Disassembly(image, cs, ip, relocs)
            (d / "listing.asm").write_text(dis.listing(), encoding="utf-8")
            (d / "callgraph.txt").write_text(dis.call_graph(), encoding="utf-8")
            info["функций найдено"] = len(dis.functions)
            info["разобрано кода"] = f"{dis.coverage * 100:.1f}% образа"
            rep.outputs.append(Output("code", f"{sub}/listing.asm", rep.path, 0, len(image), "asm",
                                      f"ассемблер, {len(dis.functions)} функций"))
            rep.outputs.append(Output("code", f"{sub}/callgraph.txt", rep.path, 0, 0, "txt", "граф вызовов"))
        strings = "\n".join(f"{o:06X}  {s}" for o, s in find_strings(data, 5))
        (d / "strings.txt").write_text(strings, encoding="utf-8")
        rep.outputs.append(Output("code", f"{sub}/strings.txt", rep.path, 0, 0, "txt", "строки из файла"))
        (d / "info.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
        rep.exe = info

    # ------------------------------------------------------------ файл
    def process_file(self, rel: str, data: bytes) -> FileReport:
        ext = os.path.splitext(rel)[1].lower()
        rep = FileReport(rel, len(data), round(entropy(data[:1 << 20]), 2))
        is_code = data[:2] in (b"MZ", b"ZM") or ext == ".com"
        if is_code:
            try:
                self.code(rep, data, ext)
            except Exception as e:  # noqa: BLE001
                rep.notes.append(f"анализ кода прерван: {e}")
        elif self.whole_file(rep, data, ext):
            return rep
        for h in carve(data):
            if h.offset == 0 and h.length == len(data) and h.fmt == "pcx" and is_code:
                continue
            self.convert_hit(rep, data, h.offset, h.length, h.fmt)
        if not rep.outputs and not rep.notes:
            e = rep.entropy
            if e > 7.5:
                rep.notes.append("данные сжаты или зашифрованы собственным форматом игры — нужен отдельный плагин")
            elif e < 1:
                rep.notes.append("почти пустой файл")
            else:
                rep.notes.append("формат не распознан (возможно, уровни, спрайты без заголовка или настройки)")
        return rep


def process_zip(zip_path, out_dir, log=print, name=None) -> Report:
    out = Path(out_dir)
    if out.exists():
        shutil.rmtree(out)
    original = out / "original"
    safe_extract(zip_path, original)
    files = sorted(p for p in original.rglob("*") if p.is_file())
    ex = Extractor(out, log)
    report = Report(game=name or Path(zip_path).stem)
    # сначала ищем палитры, чтобы раскрашивать сырые картинки
    ordered = sorted(files, key=lambda p: (p.stat().st_size != 768, str(p)))
    for p in ordered:
        rel = p.relative_to(original).as_posix()
        log(f"[{len(report.files) + 1}/{len(files)}] {rel}")
        data = p.read_bytes()
        if data[:4] == b"PK\x03\x04" and zipfile.is_zipfile(p):
            fr = FileReport(rel, len(data), round(entropy(data[:1 << 20]), 2))
            fr.notes.append("вложенный zip-архив — распакуйте его и запустите dosrip отдельно")
            report.files.append(fr)
            continue
        report.files.append(ex.process_file(rel, data))
    report.files.sort(key=lambda f: f.path)
    report.duplicates = ex.duplicates
    (out / "report.json").write_text(json.dumps(asdict(report), ensure_ascii=False, indent=2), encoding="utf-8")
    from .report import write_html

    write_html(report, out / "report.html")
    return report


def zip_dir(src: Path, dest_zip: Path):
    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(src.rglob("*")):
            if p.is_file():
                z.write(p, p.relative_to(src).as_posix())
