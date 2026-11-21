"""Графические форматы DOS: PCX, BMP, GIF, ILBM/PBM (Deluxe Paint), FLI/FLC,
сырые экраны VGA 320x200 и палитры VGA."""

import io

from PIL import Image

from ..util import be16, be32, u16, u32


def _default_palette() -> list:
    """Приблизительная палитра по умолчанию: EGA-16, градации серого, куб 6x6x6."""
    ega = [
        (0, 0, 0), (0, 0, 170), (0, 170, 0), (0, 170, 170),
        (170, 0, 0), (170, 0, 170), (170, 85, 0), (170, 170, 170),
        (85, 85, 85), (85, 85, 255), (85, 255, 85), (85, 255, 255),
        (255, 85, 85), (255, 85, 255), (255, 255, 85), (255, 255, 255),
    ]
    grays = [(i * 17,) * 3 for i in range(16)]
    cube = [(r * 51, g * 51, b * 51) for r in range(6) for g in range(6) for b in range(6)]
    pal = ega + grays + cube
    pal += [(0, 0, 0)] * (256 - len(pal))
    return [c for rgb in pal for c in rgb]


DEFAULT_PALETTE = _default_palette()


# ---------------------------------------------------------------- палитры VGA

def is_vga_palette(data: bytes) -> bool:
    """768 байт с 6-битными значениями (0..63) — типичная палитра VGA."""
    if len(data) != 768 or max(data) > 63:
        return False
    return len(set(data)) > 8


def vga_palette_to_rgb(data: bytes) -> list:
    return [(v << 2) | (v >> 4) for v in data[:768]]


def palette_swatch(pal: list) -> Image.Image:
    img = Image.new("P", (16, 16))
    img.putdata(list(range(256)))
    img.putpalette(pal)
    return img.resize((256, 256), Image.NEAREST)


def raw_vga_screen(pixels: bytes, pal: list, width=320, height=200) -> Image.Image:
    img = Image.frombytes("P", (width, height), pixels[: width * height])
    img.putpalette(pal)
    return img


def guess_raw_screen(data: bytes):
    """Файл 64000 байт = экран 320x200; 64768 = экран + палитра (в начале или в конце).
    Возвращает (пиксели, палитра_или_None) либо None."""
    if len(data) == 64000:
        return data, None
    if len(data) == 64768:
        if is_vga_palette(data[-768:]):
            return data[:64000], vga_palette_to_rgb(data[-768:])
        if is_vga_palette(data[:768]):
            return data[768:], vga_palette_to_rgb(data[:768])
    return None


# ------------------------------------------------------------------------ PCX

def decode_pcx(data: bytes, off: int = 0):
    """-> (Image, длина в байтах) или None."""
    if len(data) - off < 128:
        return None
    h = data[off: off + 128]
    if h[0] != 0x0A or h[1] not in (0, 2, 3, 4, 5) or h[2] != 1 or h[3] not in (1, 2, 4, 8):
        return None
    bpp = h[3]
    xmin, ymin, xmax, ymax = u16(h, 4), u16(h, 6), u16(h, 8), u16(h, 10)
    planes, bpl = h[65], u16(h, 66)
    w, hgt = xmax - xmin + 1, ymax - ymin + 1
    if not (1 <= w <= 2048 and 1 <= hgt <= 2048 and 1 <= planes <= 4):
        return None
    if bpl < (w * bpp + 7) // 8 or bpl > w * bpp + 16:
        return None
    total = bpl * planes * hgt
    out = bytearray()
    p = off + 128
    while len(out) < total:
        if p >= len(data):
            return None
        b = data[p]
        p += 1
        if b >= 0xC0:
            if p >= len(data):
                return None
            out += bytes([data[p]]) * (b & 0x3F)
            p += 1
        else:
            out.append(b)
    out = out[:total]

    if bpp == 8 and planes == 1:
        pal = None
        if p + 769 <= len(data) and data[p] == 0x0C:
            pal = list(data[p + 1: p + 769])
            p += 769
        img = Image.new("P", (w, hgt))
        img.putdata([out[y * bpl + x] for y in range(hgt) for x in range(w)])
        img.putpalette(pal or DEFAULT_PALETTE)
        return img, p - off
    if bpp == 8 and planes == 3:
        img = Image.new("RGB", (w, hgt))
        px = []
        for y in range(hgt):
            row = y * bpl * 3
            for x in range(w):
                px.append((out[row + x], out[row + bpl + x], out[row + 2 * bpl + x]))
        img.putdata(px)
        return img, p - off
    if bpp == 1 or (bpp in (2, 4) and planes == 1):
        # планарные/упакованные режимы EGA/CGA
        pal16 = list(h[16:64])
        idx = []
        for y in range(hgt):
            for x in range(w):
                v = 0
                for pl in range(planes):
                    row = (y * planes + pl) * bpl
                    if bpp == 1:
                        bit = (out[row + x // 8] >> (7 - x % 8)) & 1
                        v |= bit << pl
                    else:
                        per = 8 // bpp
                        byte = out[row + x // per]
                        v |= ((byte >> ((per - 1 - x % per) * bpp)) & ((1 << bpp) - 1)) << (pl * bpp)
                idx.append(v)
        img = Image.new("P", (w, hgt))
        img.putdata(idx)
        if planes * bpp == 1:
            pal = [0, 0, 0, 255, 255, 255]
        elif any(pal16):
            pal = pal16
        else:
            pal = DEFAULT_PALETTE[:48]
        img.putpalette(pal + [0] * (768 - len(pal)))
        return img, p - off
    return None


# ------------------------------------------------------------------- BMP, GIF

def bmp_length(data: bytes, off: int):
    if len(data) - off < 30:
        return None
    size, res, pixoff, hsize = u32(data, off + 2), u32(data, off + 6), u32(data, off + 10), u32(data, off + 14)
    if res != 0 or hsize not in (12, 40, 56, 108, 124) or not (26 <= pixoff < size):
        return None
    if size > len(data) - off or size < 32:
        return None
    return size


def gif_length(data: bytes, off: int):
    try:
        p = off + 13
        flags = data[off + 10]
        if flags & 0x80:
            p += 3 * (2 << (flags & 7))
        while True:
            b = data[p]
            if b == 0x3B:
                return p + 1 - off
            if b == 0x21:  # extension
                p += 2
            elif b == 0x2C:  # image descriptor
                f = data[p + 9]
                p += 10
                if f & 0x80:
                    p += 3 * (2 << (f & 7))
                p += 1  # LZW min code size
            else:
                return None
            while True:  # sub-blocks
                n = data[p]
                p += 1 + n
                if n == 0:
                    break
    except IndexError:
        return None


def pil_open(blob: bytes):
    try:
        img = Image.open(io.BytesIO(blob))
        img.load()
        return img
    except Exception:
        return None


# --------------------------------------------------------- ILBM / PBM (IFF)

def _byterun1(src: bytes, p: int, n: int):
    out = bytearray()
    while len(out) < n and p < len(src):
        c = src[p]
        p += 1
        if c < 128:
            out += src[p: p + c + 1]
            p += c + 1
        elif c > 128:
            out += bytes([src[p]]) * (257 - c)
            p += 1
    return bytes(out[:n]), p


def decode_iff_image(blob: bytes):
    """FORM ILBM (планарный Amiga/DPaint) или FORM PBM (VGA DPaint .LBM)."""
    if blob[:4] != b"FORM" or blob[8:12] not in (b"ILBM", b"PBM "):
        return None
    chunky = blob[8:12] == b"PBM "
    p, end = 12, min(len(blob), 8 + be32(blob, 4))
    bmhd = cmap = body = None
    while p + 8 <= end:
        cid, size = blob[p: p + 4], be32(blob, p + 4)
        data = blob[p + 8: p + 8 + size]
        if cid == b"BMHD":
            bmhd = data
        elif cid == b"CMAP":
            cmap = data
        elif cid == b"BODY":
            body = data
        p += 8 + size + (size & 1)
    if not bmhd or body is None:
        return None
    w, h, nplanes, masking, comp = be16(bmhd, 0), be16(bmhd, 2), bmhd[8], bmhd[9], bmhd[10]
    if not (0 < w <= 4096 and 0 < h <= 4096):
        return None
    pal = list(cmap[:768]) if cmap else DEFAULT_PALETTE
    pal += [0] * (768 - len(pal))
    q = 0
    idx = bytearray()
    if chunky:
        rowlen = w + (w & 1)
        for _ in range(h):
            row, q = _byterun1(body, q, rowlen) if comp else (body[q: q + rowlen], q + rowlen)
            idx += row[:w].ljust(w, b"\0")
    else:
        rowbytes = ((w + 15) // 16) * 2
        planes_total = nplanes + (1 if masking == 1 else 0)
        for _ in range(h):
            planes = []
            for _pl in range(planes_total):
                row, q = _byterun1(body, q, rowbytes) if comp else (body[q: q + rowbytes], q + rowbytes)
                planes.append(row.ljust(rowbytes, b"\0"))
            for x in range(w):
                v = 0
                for pl in range(min(nplanes, 8)):
                    v |= ((planes[pl][x >> 3] >> (7 - (x & 7))) & 1) << pl
                idx.append(v)
    if nplanes == 24:
        return None
    img = Image.frombytes("P", (w, h), bytes(idx))
    img.putpalette(pal)
    return img


# ------------------------------------------------------------------ FLI/FLC

def fli_length(data: bytes, off: int):
    if len(data) - off < 128:
        return None
    size, magic, frames, w, h = u32(data, off), u16(data, off + 4), u16(data, off + 6), u16(data, off + 8), u16(data, off + 10)
    if magic not in (0xAF11, 0xAF12) or not (0 < frames < 10000) or not (0 < w <= 1280 and 0 < h <= 1024):
        return None
    if size < 128 or size > len(data) - off:
        return None
    return size


def fli_to_gif(blob: bytes, max_frames=400):
    """-> (первый кадр, байты анимированного GIF) или None."""
    img = pil_open(blob)
    if img is None:
        return None
    frames, durations = [], []
    try:
        for i in range(max_frames):
            img.seek(i)
            frames.append(img.convert("RGB").copy())
            durations.append(max(20, img.info.get("duration", 70)))
    except EOFError:
        pass
    if not frames:
        return None
    buf = io.BytesIO()
    frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:], duration=durations, loop=0)
    return frames[0], buf.getvalue()
