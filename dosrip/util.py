import math
import re
import struct
from collections import Counter

u16 = lambda b, o: struct.unpack_from("<H", b, o)[0]
u32 = lambda b, o: struct.unpack_from("<I", b, o)[0]
be16 = lambda b, o: struct.unpack_from(">H", b, o)[0]
be32 = lambda b, o: struct.unpack_from(">I", b, o)[0]


def entropy(data: bytes) -> float:
    if not data:
        return 0.0
    n = len(data)
    return -sum(c / n * math.log2(c / n) for c in Counter(data).values())


def read_varlen(data: bytes, pos: int):
    """Стандартное MIDI-число переменной длины -> (значение, новая позиция)."""
    value = 0
    while True:
        b = data[pos]
        pos += 1
        value = (value << 7) | (b & 0x7F)
        if not b & 0x80:
            return value, pos


def write_varlen(value: int) -> bytes:
    out = [value & 0x7F]
    value >>= 7
    while value:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    return bytes(reversed(out))


def safe_name(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", path.replace("/", "__")).strip("_") or "file"


_PRINTABLE = set(range(0x20, 0x7F)) | {9, 10, 13} | set(range(0x80, 0x100))


def looks_like_text(data: bytes) -> bool:
    if not data or b"\x00" in data[:4096]:
        return False
    sample = data[:8192]
    good = sum(1 for b in sample if b in _PRINTABLE or b == 0x1A)
    ascii_letters = sum(1 for b in sample if 0x41 <= b <= 0x7A)
    return good / len(sample) > 0.97 and ascii_letters / len(sample) > 0.3


def find_strings(data: bytes, min_len: int = 5):
    for m in re.finditer(rb"[\x20-\x7e]{%d,}" % min_len, data):
        yield m.start(), m.group().decode("ascii")


def guess_dos_encoding(data: bytes) -> str:
    """CP866 (русские DOS-игры) или CP437 (остальные): по доле байт кириллицы CP866."""
    high = [b for b in data if b >= 0x80]
    if len(high) < 20:
        return "cp437"
    cyr = sum(1 for b in high if b <= 0xAF or 0xE0 <= b <= 0xF1)
    return "cp866" if cyr / len(high) > 0.7 else "cp437"
