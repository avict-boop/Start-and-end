"""Разбор DOS MZ EXE: заголовок, релокации, оверлей, упаковщики, компилятор."""

import struct
from dataclasses import dataclass, field

from ..util import entropy, u16, u32


@dataclass
class MZ:
    data: bytes
    header_size: int
    image: bytes
    relocs: list  # [(seg, off)]
    cs: int
    ip: int
    ss: int
    sp: int
    minalloc: int
    maxalloc: int
    overlay: bytes
    new_exe: str = ""  # NE/LE/LX/PE если есть
    new_exe_offset: int = 0
    packer: str = ""
    compiler: list = field(default_factory=list)
    extender: str = ""

    @property
    def entry_offset(self) -> int:
        return self.cs * 16 + self.ip

    def reloc_linear(self):
        return sorted(s * 16 + o for s, o in self.relocs)


def parse_mz(data: bytes):
    if len(data) < 0x1C or data[:2] not in (b"MZ", b"ZM"):
        return None
    (cblp, cp, crlc, cparhdr, minalloc, maxalloc, ss, sp, _csum, ip, cs, lfarlc, _ovno) = struct.unpack_from(
        "<13H", data, 2
    )
    total = cp * 512 - ((512 - cblp) if cblp else 0)
    total = min(max(total, cparhdr * 16), len(data))
    hsize = cparhdr * 16
    if hsize > total:
        return None
    relocs = []
    for i in range(crlc):
        p = lfarlc + i * 4
        if p + 4 > len(data):
            break
        off, seg = struct.unpack_from("<HH", data, p)
        relocs.append((seg, off))
    mz = MZ(
        data=data, header_size=hsize, image=data[hsize:total], relocs=relocs, cs=cs, ip=ip, ss=ss, sp=sp,
        minalloc=minalloc, maxalloc=maxalloc, overlay=data[total:],
    )
    if lfarlc >= 0x40 and len(data) >= 0x40:
        ne = u32(data, 0x3C)
        if 0x40 <= ne < len(data) - 4 and data[ne: ne + 2] in (b"NE", b"LE", b"LX", b"PE"):
            mz.new_exe, mz.new_exe_offset = data[ne: ne + 2].decode(), ne
    if not mz.new_exe:  # DOS/4GW: LE часто идёт сразу после MZ-заглушки
        p = data.find(b"LE\x00\x00", 0, 0x40000)
        if p > 0 and data.find(b"DOS/4G") != -1:
            mz.new_exe, mz.new_exe_offset = "LE", p
    mz.packer = detect_packer(mz)
    mz.compiler = detect_compiler(data)
    mz.extender = detect_extender(data)
    return mz


def detect_packer(mz: MZ) -> str:
    d = mz.data
    if d[0x1C:0x20] in (b"LZ09", b"LZ91"):
        return "LZEXE " + ("0.90" if d[0x1C:0x20] == b"LZ09" else "0.91")
    if b"PKLITE" in d[:0x60] or b"PKlite" in d[:0x60]:
        return "PKLITE"
    if b"Packed file is corrupt" in mz.image[-0x200:] or b"Packed file is corrupt" in mz.image:
        return "Microsoft EXEPACK"
    if b"UPX!" in d[:0x400]:
        return "UPX"
    if b"diet" in d[:0x40].lower() or b"dlz" in d[0x1C:0x24]:
        return "DIET"
    if b"WWPACK" in d[:0x200] or b"WWP" in d[0x1C:0x22]:
        return "WWPACK"
    if b"aPack" in d[:0x400] or b"aPA" in d[:0x40]:
        return "aPACK"
    if b"Copyright (c) 1990 by Microsoft" in d[:0x400] and b"EXEPACK" in d:
        return "Microsoft EXEPACK"
    return ""


def looks_packed(mz: MZ) -> bool:
    """Известная сигнатура, либо точка входа в хвосте образа (типично для распаковщиков),
    либо сжатые данные. Окончательно проверяется эмуляцией."""
    if mz.packer:
        return True
    if mz.new_exe or not mz.image or mz.entry_offset >= len(mz.image):
        return False
    tail = len(mz.image) - mz.entry_offset
    if mz.entry_offset >= len(mz.image) // 2 and tail < 8192 and len(mz.relocs) < 4:
        return True
    return entropy(mz.image[: max(0, mz.entry_offset)][:65536]) > 7.3


_COMPILERS = [
    (b"Turbo Pascal", "Borland Turbo Pascal"),
    (b"Borland C++", "Borland C++"),
    (b"Turbo C++", "Borland Turbo C++"),
    (b"Turbo-C", "Borland Turbo C"),
    (b"Turbo C", "Borland Turbo C"),
    (b"MS Run-Time Library", "Microsoft C"),
    (b"Microsoft C", "Microsoft C"),
    (b"WATCOM C", "Watcom C/C++"),
    (b"Watcom C", "Watcom C/C++"),
    (b"Zortech", "Zortech C++"),
    (b"QuickBASIC", "Microsoft QuickBASIC"),
    (b"BASCOM", "Microsoft BASIC compiler"),
    (b"Clipper", "Clipper"),
    (b"MetaWare", "MetaWare High C"),
    (b"DJGPP", "DJGPP (GCC)"),
]

_EXTENDERS = [
    (b"DOS/4GW", "DOS/4GW (32-битный защищённый режим)"),
    (b"DOS/4G", "DOS/4G (32-битный защищённый режим)"),
    (b"PMODE/W", "PMODE/W (32-битный защищённый режим)"),
    (b"DOS32A", "DOS/32A (32-битный защищённый режим)"),
    (b"CauseWay", "CauseWay (32-битный защищённый режим)"),
    (b"Phar Lap", "Phar Lap (защищённый режим)"),
    (b"go32", "DJGPP go32 (32-битный защищённый режим)"),
]


def detect_compiler(data: bytes):
    found = []
    for sig, name in _COMPILERS:
        if sig in data and name not in found:
            found.append(name)
    if b"Runtime error " in data and b" at " in data and "Borland Turbo Pascal" not in found:
        found.append("Borland Turbo Pascal (по сообщению Runtime error)")
    return found


def detect_extender(data: bytes) -> str:
    for sig, name in _EXTENDERS:
        if sig in data:
            return name
    return ""


def build_mz(image: bytes, relocs_linear, cs, ip, ss, sp, minalloc=0, maxalloc=0xFFFF) -> bytes:
    """Собирает корректный MZ EXE из образа и списка релокаций (линейные смещения)."""
    nrel = len(relocs_linear)
    hdr_len = 0x1C + 4 + 4 * nrel
    cparhdr = (hdr_len + 15) // 16
    hdr_bytes = cparhdr * 16
    total = hdr_bytes + len(image)
    cblp, cp = total % 512, (total + 511) // 512
    header = bytearray(hdr_bytes)
    struct.pack_into(
        "<2s13H", header, 0, b"MZ", cblp, cp, nrel, cparhdr, minalloc, maxalloc, ss & 0xFFFF, sp & 0xFFFF, 0,
        ip & 0xFFFF, cs & 0xFFFF, 0x20, 0,
    )
    for i, lin in enumerate(sorted(relocs_linear)):
        seg, off = lin >> 4, lin & 0xF
        struct.pack_into("<HH", header, 0x20 + 4 * i, off, seg)
    return bytes(header) + image


def parse_le_summary(data: bytes, off: int) -> dict:
    """Минимальная информация о LE-файле (DOS/4GW)."""
    try:
        return {
            "cpu": {1: "286", 2: "386", 3: "486", 4: "586"}.get(u16(data, off + 8), "?"),
            "objects": u32(data, off + 0x44),
            "pages": u32(data, off + 0x14),
            "page_size": u32(data, off + 0x28),
            "entry_object": u32(data, off + 0x18),
            "entry_eip": u32(data, off + 0x1C),
        }
    except struct.error:
        return {}
