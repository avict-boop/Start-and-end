"""Синтетическая «DOS-игра» для тестов: EXE (обычный и упакованный) и ресурсы разных форматов.

Байты программ получены из tests/asm/*.asm через `nasm -f bin`.
"""

import io
import struct
import zipfile

from PIL import Image

from dosrip.exe.mz import build_mz
from dosrip.formats.audio import VOC_MAGIC
from dosrip.util import write_varlen

PROGRAM = bytes.fromhex(
    "b810008ed8ba0000b409cd219a16000000b8004ccd21b402b221cd21cb"
) + bytes(0x100 - 29) + b"Hello from DOS$" + bytes(0x120 - 0x100 - 15)
PROGRAM_RELOCS = [0x0001, 0x000F]
PROGRAM_SS, PROGRAM_SP = 0x12, 0x100

STUB = bytes.fromhex(
    "eb0c0000000000000000000000008cc083c01089c58ed831f62e8b0e020080345a46e2fa2e8b0e0400bb5e00e3132e8b"
    "372e8b7f0201ef8edf012c83c30449ebeb2e012e08002ea10a0001e8fa8ed02e8b260c00fb8cc08ed82eff2e0600"
)


def original_exe() -> bytes:
    return build_mz(PROGRAM, PROGRAM_RELOCS, 0, 0, PROGRAM_SS, PROGRAM_SP, minalloc=0x20)


def packed_exe() -> bytes:
    """Упаковывает PROGRAM игрушечным упаковщиком из tests/asm/stub.asm."""
    body = bytes(b ^ 0x5A for b in PROGRAM)
    body += bytes(-len(body) % 16)
    stub = bytearray(STUB)
    struct.pack_into("<6H", stub, 2, len(PROGRAM), len(PROGRAM_RELOCS), 0, 0, PROGRAM_SS, PROGRAM_SP)
    for r in PROGRAM_RELOCS:
        stub += struct.pack("<HH", r & 0xF, r >> 4)
    image = body + bytes(stub)
    stub_seg = len(body) // 16
    return build_mz(image, [], stub_seg, 0, stub_seg + 0x10, 0x100, minalloc=0x30)


def _palette6():
    return bytes(min(63, (i * 3 + c * 20) % 64) for i in range(256) for c in range(3))


def pcx_bytes():
    img = Image.new("P", (32, 20))
    img.putpalette([(i * 7) % 256 for i in range(768)])
    img.putdata([(x * 8 + y) % 256 for y in range(20) for x in range(32)])
    buf = io.BytesIO()
    img.save(buf, "PCX")
    return buf.getvalue()


def gif_bytes():
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (200, 30, 30)).save(buf, "GIF")
    return buf.getvalue()


def voc_bytes(n=4000):
    pcm = bytes(128 + (40 if (i // 20) % 2 else -40) for i in range(n))
    sr = 256 - 1_000_000 // 11025
    block = bytes([1]) + (len(pcm) + 2).to_bytes(3, "little") + bytes([sr, 0]) + pcm
    return VOC_MAGIC + struct.pack("<HHH", 26, 0x010A, 0x1129) + block + b"\x00"


def midi_bytes():
    track = b"\x00\x90\x3c\x40\x60\x80\x3c\x00\x00\xff\x2f\x00"
    return b"MThd" + struct.pack(">IHHH", 6, 0, 1, 96) + b"MTrk" + struct.pack(">I", len(track)) + track


def xmi_bytes():
    # нота C4 длительностью 60 тиков, пауза 120, нота E4, конец
    evnt = b"\x90\x3c\x64" + write_varlen(60) + b"\x78" + b"\x90\x40\x64" + write_varlen(30) + b"\xff\x2f\x00"
    evnt_chunk = b"EVNT" + struct.pack(">I", len(evnt)) + evnt + (b"\0" if len(evnt) & 1 else b"")
    form = b"FORM" + struct.pack(">I", 4 + len(evnt_chunk)) + b"XMID" + evnt_chunk
    cat = b"CAT " + struct.pack(">I", 4 + len(form)) + b"XMID" + form
    info = b"INFO" + struct.pack(">I", 2) + b"\x01\x00"
    xdir = b"FORM" + struct.pack(">I", 4 + len(info)) + b"XDIR" + info
    return xdir + cat


def game_zip() -> bytes:
    junk = bytes((i * 37 + 11) % 251 for i in range(3000))
    archive = junk + pcx_bytes() + junk + voc_bytes() + junk + midi_bytes() + gif_bytes() + junk + xmi_bytes() + junk
    pal = _palette6()
    screen = bytes((x // 5 + y // 4) % 64 + 32 for y in range(200) for x in range(320))
    files = {
        "GAME/GAME.EXE": original_exe(),
        "GAME/PACKED.EXE": packed_exe(),
        "GAME/RESOURCE.DAT": archive,
        "GAME/TITLE.PAL": pal,
        "GAME/TITLE.SCR": screen,
        "GAME/README.TXT": b"Welcome to the game!\r\nPress F1 for help.\r\n",
        "GAME/LEVELS.BIN": __import__("random").Random(1).randbytes(5000),
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()
