"""Поиск известных форматов внутри любых файлов (архивов .DAT/.RES, EXE и т.д.)."""

import re
from dataclasses import dataclass

from .formats import audio, images
from .util import u16


@dataclass
class Hit:
    offset: int
    length: int
    fmt: str  # pcx, bmp, gif, iff-image, fli, voc, wav, midi, xmi, cmf, mod, s3m


def _riff(data, o):
    if data[o + 8: o + 12] not in (b"WAVE", b"RMID"):
        return None
    n = int.from_bytes(data[o + 4: o + 8], "little") + 8
    return (min(n, len(data) - o), "wav" if data[o + 8: o + 12] == b"WAVE" else "riff-midi") if n > 44 else None


def _form(data, o):
    kind = data[o + 8: o + 12]
    if kind in (b"ILBM", b"PBM "):
        return audio.iff_length(data, o), "iff-image"
    if kind in (b"XDIR", b"XMID"):
        return audio.iff_length(data, o), "xmi"
    return None


def _cmf(data, o):
    if not (0x14 <= u16(data, o + 8) < len(data) - o):
        return None
    # длина в формате не записана; берём до конца файла или до следующей сигнатуры CTMF
    nxt = data.find(b"CTMF", o + 4)
    return (nxt if nxt != -1 else len(data)) - o, "cmf"


# (регулярное выражение, смещение сигнатуры от начала ресурса, обработчик)
SIGNATURES = [
    (re.compile(re.escape(audio.VOC_MAGIC)), 0, lambda d, o: (audio.voc_length(d, o), "voc")),
    (re.compile(b"RIFF"), 0, _riff),
    (re.compile(b"MThd"), 0, lambda d, o: (n, "midi") if (n := audio.midi_length(d, o)) else None),
    (re.compile(b"FORM"), 0, _form),
    (re.compile(b"CTMF"), 0, _cmf),
    (re.compile(b"GIF8[79]a"), 0, lambda d, o: (n, "gif") if (n := images.gif_length(d, o)) else None),
    (re.compile(b"BM"), 0, lambda d, o: (n, "bmp") if (n := images.bmp_length(d, o)) else None),
    (re.compile(b"\x0a[\x00\x02-\x05]\x01[\x01\x02\x04\x08]"), 0, None),  # PCX: проверяется декодированием
    (re.compile(b"[\x11\x12]\xaf"), 4, lambda d, o: (n, "fli") if (n := images.fli_length(d, o)) else None),
    (re.compile(b"|".join(re.escape(t) for t in audio.MOD_TAGS)), 1080,
     lambda d, o: (n, "mod") if (n := audio.mod_length(d, o)) else None),
    (re.compile(b"SCRM"), 44, lambda d, o: (n, "s3m") if (n := audio.s3m_length(d, o)) else None),
]


def _pcx(data, o):
    if len(data) - o < 128 or data[o + 64] != 0:
        return None
    res = images.decode_pcx(data, o)
    if not res:
        return None
    img, n = res
    if img.width * img.height < 16:
        return None
    return n, "pcx"


def carve(data: bytes, limit_per_format: int = 2000):
    hits, covered_until = [], 0
    candidates = []
    for rx, back, handler in SIGNATURES:
        handler = handler or _pcx
        count = 0
        for m in rx.finditer(data):
            o = m.start() - back
            if o < 0:
                continue
            try:
                r = handler(data, o)
            except (IndexError, ValueError, OverflowError, Exception):  # noqa: BLE001 - мусорные данные
                r = None
            if r and r[0] and r[0] > 0:
                candidates.append(Hit(o, r[0], r[1]))
                count += 1
                if count >= limit_per_format:
                    break
    # убираем находки, вложенные в уже найденный ресурс (например, "BM" внутри звука)
    for h in sorted(candidates, key=lambda h: (h.offset, -h.length)):
        if h.offset < covered_until:
            continue
        hits.append(h)
        covered_until = h.offset + h.length
    return hits
