"""Обратное преобразование: отредактированные PNG/WAV/TXT -> исходные форматы игры.

Главное правило: если ресурс лежит внутри другого файла (архив .DAT, EXE), новый
ресурс должен занять ровно столько же байт, сколько старый, иначе сдвинутся все
остальные данные и игра сломается.
"""

import io
import struct
import wave

from PIL import Image

from ..util import u16, u32


class FitError(Exception):
    """Новый ресурс не помещается на место старого."""


# ----------------------------------------------------------- изображения

def palette_image(pal: list) -> Image.Image:
    p = Image.new("P", (1, 1))
    p.putpalette((list(pal) + [0] * 768)[:768])
    return p


def to_indices(img: Image.Image, width: int, height: int, pal: list):
    """Картинку пользователя -> индексы палитры игры (bytes длиной width*height).

    * Можно рисовать в 2x/3x/4x размере — картинка будет уменьшена.
    * Индексированный PNG (режим P) берётся как есть, вместе со своей палитрой.
    * Полноцветный PNG подбирается к палитре игры; прозрачные пиксели -> индекс 0.
    Возвращает (индексы, палитра_из_png_или_None)."""
    w, h = img.size
    if (w, h) != (width, height):
        if w % width or h % height or w // width != h // height:
            raise FitError(f"размер {w}x{h}, а нужен {width}x{height} (или кратный: "
                           f"{width * 2}x{height * 2}, {width * 4}x{height * 4})")
        resample = Image.NEAREST if img.mode == "P" else Image.BOX
        if img.mode not in ("P", "RGB", "RGBA", "L"):
            img = img.convert("RGBA")
        img = img.resize((width, height), resample)
    if img.mode == "P":
        own = img.getpalette() or []
        return img.tobytes(), (own + [0] * 768)[:768]
    rgba = img.convert("RGBA").tobytes()
    colors = [tuple(pal[i * 3: i * 3 + 3]) for i in range(len(pal) // 3)]
    cache = {}
    idx = bytearray(len(rgba) // 4)
    for i in range(len(idx)):
        r, g, b, a = rgba[i * 4: i * 4 + 4]
        if a < 128:
            continue  # прозрачный -> цвет 0
        key = (r, g, b)
        j = cache.get(key)
        if j is None:
            j = min(range(len(colors)), key=lambda k: (colors[k][0] - r) ** 2 * 3 + (colors[k][1] - g) ** 2 * 4
                    + (colors[k][2] - b) ** 2 * 2)
            cache[key] = j
        idx[i] = j
    return bytes(idx), None


def _pcx_tokens(row: bytes):
    """RLE одной строки PCX -> список [значение, количество]."""
    tokens, i = [], 0
    while i < len(row):
        v, n = row[i], 1
        while i + n < len(row) and row[i + n] == v and n < 63:
            n += 1
        tokens.append([v, n, False])  # False = короткая форма, если возможна
        i += n
    return tokens


def _tok_size(t):
    v, n, forced = t
    return 1 if (n == 1 and v < 0xC0 and not forced) else 2


def encode_pcx(original: bytes, indices: bytes, palette=None, exact_length=None) -> bytes:
    """Перекодирует 8-битный PCX с заголовком оригинала. exact_length — подогнать размер точно."""
    hdr = bytearray(original[:128])
    if hdr[3] != 8 or hdr[65] != 1:
        raise FitError("перекодирование поддерживается только для 256-цветных PCX")
    w = u16(hdr, 8) - u16(hdr, 4) + 1
    h = u16(hdr, 10) - u16(hdr, 6) + 1
    bpl = u16(hdr, 66)
    tokens = []
    for y in range(h):
        row = indices[y * w: (y + 1) * w] + bytes(bpl - w)
        tokens.extend(_pcx_tokens(row))

    from .images import decode_pcx
    _img, orig_len = decode_pcx(original)
    has_pal = orig_len >= 128 + 769 and original[orig_len - 769] == 0x0C
    tail = b""
    if has_pal:
        pal = palette if palette is not None else list(original[orig_len - 768: orig_len])
        tail = b"\x0c" + bytes(pal[:768])

    size = 128 + sum(_tok_size(t) for t in tokens) + len(tail)
    if exact_length is not None:
        if size > exact_length:
            raise FitError(f"после сжатия картинка занимает {size} байт, а место только {exact_length}. "
                           "Упростите рисунок: меньше мелких деталей и шума, больше однотонных участков.")
        need = exact_length - size
        while need > 0:
            progressed = False
            # 1) одиночный пиксель в длинной форме: +1 байт
            for t in tokens:
                if need and _tok_size(t) == 1:
                    t[2] = True
                    need -= 1
                    progressed = True
            if not need:
                break
            # 2) отщепляем пиксель от серии (прибавка 0..2 байта)
            for i, t in enumerate(tokens):
                if t[1] < 2:
                    continue
                before = _tok_size(t)
                single = [t[0], 1, False]
                t[1] -= 1
                delta = _tok_size(t) + _tok_size(single) - before
                if delta > need:
                    t[1] += 1
                    continue
                tokens.insert(i + 1, single)
                need -= delta
                progressed = True
                break
            if not progressed:
                break
        if need:
            raise FitError("не удалось подогнать размер PCX точно")
    out = bytearray(hdr)
    for v, n, forced in tokens:
        if n == 1 and v < 0xC0 and not forced:
            out.append(v)
        else:
            out += bytes([0xC0 | n, v])
    return bytes(out) + tail


def patch_bmp(original: bytes, indices: bytes, palette=None) -> bytes:
    """Подменяет пиксели 8-битного несжатого BMP, заголовок и размер не меняются."""
    out = bytearray(original)
    pixoff, hsize = u32(out, 10), u32(out, 14)
    w, h = struct.unpack_from("<ii", out, 18)
    bpp, comp = u16(out, 28), u32(out, 30)
    if bpp != 8 or comp != 0:
        raise FitError("перекодирование поддерживается только для 256-цветных несжатых BMP")
    stride = (w + 3) & ~3
    for y in range(abs(h)):
        dst_y = abs(h) - 1 - y if h > 0 else y
        p = pixoff + dst_y * stride
        out[p: p + w] = indices[y * w: (y + 1) * w]
    if palette is not None:
        pal_off = 14 + hsize
        for i in range(256):
            if pal_off + i * 4 + 4 > pixoff:
                break
            r, g, b = palette[i * 3: i * 3 + 3]
            out[pal_off + i * 4: pal_off + i * 4 + 3] = bytes([b, g, r])
    return bytes(out)


def encode_gif(indices: bytes, width: int, height: int, pal: list) -> bytes:
    img = Image.frombytes("P", (width, height), indices)
    img.putpalette(pal)
    buf = io.BytesIO()
    img.save(buf, "GIF")
    return buf.getvalue()


def rgb_to_vga6(pal: list) -> bytes:
    return bytes(min(63, v >> 2) for v in pal[:768])


def read_swatch(img: Image.Image) -> list:
    """Палитра из PNG-образца 16x16 клеток (как его выгружает dosrip)."""
    img = img.convert("RGB")
    cw, ch = img.width / 16, img.height / 16
    pal = []
    for i in range(256):
        x, y = int((i % 16 + 0.5) * cw), int((i // 16 + 0.5) * ch)
        pal.extend(img.getpixel((x, y)))
    return pal


# ----------------------------------------------------------------- звук

def read_wav_mono(blob: bytes):
    """WAV пользователя -> (отсчёты float -1..1, частота)."""
    with wave.open(io.BytesIO(blob)) as w:
        ch, sw, rate, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        raw = w.readframes(n)
    samples = []
    step = ch * sw
    for i in range(0, len(raw) - step + 1, step):
        acc = 0.0
        for c in range(ch):
            b = raw[i + c * sw: i + (c + 1) * sw]
            if sw == 1:
                acc += (b[0] - 128) / 128
            else:
                acc += int.from_bytes(b, "little", signed=True) / (1 << (8 * sw - 1))
        samples.append(acc / ch)
    return samples, rate


def resample(samples, src_rate: int, dst_rate: int):
    if src_rate == dst_rate or not samples:
        return samples
    n = max(1, int(len(samples) * dst_rate / src_rate))
    ratio = src_rate / dst_rate
    out = []
    last = len(samples) - 1
    for i in range(n):
        x = i * ratio
        j = int(x)
        f = x - j
        a = samples[min(j, last)]
        b = samples[min(j + 1, last)]
        out.append(a + (b - a) * f)
    return out


def pcm_bytes(samples, bits: int, channels: int, length: int) -> bytes:
    """Отсчёты -> PCM ровно `length` байт (тишина в конце или обрезка)."""
    out = bytearray()
    for s in samples:
        s = max(-1.0, min(1.0, s))
        if bits == 8:
            frame = bytes([int(round(s * 127)) + 128])
        else:
            frame = int(round(s * 32767)).to_bytes(2, "little", signed=True)
        out += frame * channels
        if len(out) >= length:
            break
    silence = b"\x80" if bits == 8 else b"\x00"
    return bytes(out[:length]) + silence * (length - min(len(out), length))


def patch_voc(original: bytes, wav: bytes):
    """Заполняет PCM-блоки оригинального VOC новым звуком (частота и длина сохраняются).
    -> (новые байты, сообщение)."""
    segments, rate, bits, ch = [], None, 8, 1
    p = u16(original, 20)
    while p + 4 <= len(original) and original[p] != 0:
        t = original[p]
        size = original[p + 1] | original[p + 2] << 8 | original[p + 3] << 16
        d = p + 4
        if t == 1:
            if original[d + 1] != 0:
                raise FitError("VOC со сжатием ADPCM не поддерживается")
            rate = rate or 1_000_000 // (256 - original[d])
            segments.append((d + 2, size - 2))
        elif t == 2:
            segments.append((d, size))
        elif t == 8:
            ch = 2 if original[d + 3] else 1
            rate = 256_000_000 // (ch * (65536 - u16(original, d)))
        elif t == 9:
            rate, bits, ch, codec = u32(original, d), original[d + 4], original[d + 5], u16(original, d + 6)
            if codec not in (0, 4):
                raise FitError("сжатый VOC не поддерживается")
            segments.append((d + 12, size - 12))
        p = d + size
    if not segments or not rate:
        raise FitError("в VOC нет звуковых данных")
    capacity = sum(n for _o, n in segments)
    samples, src_rate = read_wav_mono(wav)
    samples = resample(samples, src_rate, rate)
    pcm = pcm_bytes(samples, bits, ch, capacity)
    out = bytearray(original)
    pos = 0
    for o, n in segments:
        out[o: o + n] = pcm[pos: pos + n]
        pos += n
    return bytes(out), _fit_message(len(samples), capacity // (bits // 8 * ch), rate)


def patch_wav(original: bytes, wav: bytes):
    """Заполняет чанк data оригинального WAV новым звуком в формате оригинала."""
    p, fmt, data = 12, None, None
    while p + 8 <= len(original):
        cid, size = original[p: p + 4], u32(original, p + 4)
        if cid == b"fmt ":
            fmt = original[p + 8: p + 8 + size]
        elif cid == b"data":
            data = (p + 8, min(size, len(original) - p - 8))
        p += 8 + size + (size & 1)
    if not fmt or not data or u16(fmt, 0) != 1:
        raise FitError("поддерживаются только несжатые WAV (PCM)")
    ch, rate, bits = u16(fmt, 2), u32(fmt, 4), u16(fmt, 14)
    if bits not in (8, 16):
        raise FitError(f"WAV {bits} бит не поддерживается")
    samples, src_rate = read_wav_mono(wav)
    samples = resample(samples, src_rate, rate)
    o, n = data
    out = bytearray(original)
    out[o: o + n] = pcm_bytes(samples, bits, ch, n)
    return bytes(out), _fit_message(len(samples), n // (bits // 8 * ch), rate)


def _fit_message(have: int, room: int, rate: int) -> str:
    if have > room:
        return f"звук обрезан до {room / rate:.2f} с (место в игре ограничено)"
    if have < room:
        return f"дополнен тишиной до {room / rate:.2f} с"
    return ""


# ---------------------------------------------------------------- текст

def encode_text(text: str, encoding: str, crlf: bool, eof: bool) -> bytes:
    text = text.replace("\r\n", "\n")
    if crlf:
        text = text.replace("\n", "\r\n")
    try:
        data = text.encode(encoding)
    except UnicodeEncodeError as e:
        bad = text[e.start: e.end]
        raise FitError(f"символ «{bad}» нельзя записать в кодировке {encoding.upper()}") from None
    return data + (b"\x1a" if eof else b"")


def fit_string(original: bytes, new: str, encoding: str, terminator: int):
    """Строка внутри EXE: новая не длиннее старой, остаток заполняется."""
    try:
        data = new.encode(encoding)
    except UnicodeEncodeError as e:
        raise FitError(f"символ «{new[e.start:e.end]}» нельзя записать в кодировке {encoding.upper()}") from None
    if original.endswith(b"$") and not data.endswith(b"$"):
        data += b"$"
    if len(data) > len(original):
        raise FitError(f"строка длиннее исходной: {len(data)} байт при максимуме {len(original)}")
    pad = len(original) - len(data)
    if terminator == 0:
        return data + b"\x00" * pad
    if data.endswith(b"$"):
        return data[:-1] + b" " * pad + b"$"
    return data + b" " * pad
