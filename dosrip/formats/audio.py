"""Звук и музыка DOS: Creative VOC, WAV, MIDI, XMIDI (Miles), CMF, MOD, S3M."""

import io
import struct
import wave

from ..util import be16, be32, read_varlen, u16, u32, write_varlen

# ---------------------------------------------------------------------- VOC

VOC_MAGIC = b"Creative Voice File\x1a"


def voc_length(data: bytes, off: int):
    try:
        p = off + u16(data, off + 20)
        while True:
            t = data[p]
            if t == 0:
                return p + 1 - off
            size = data[p + 1] | data[p + 2] << 8 | data[p + 3] << 16
            p += 4 + size
            if p > len(data):
                return len(data) - off
    except IndexError:
        return len(data) - off


def voc_to_wav(blob: bytes):
    """Склеивает все PCM-блоки VOC в один WAV. -> bytes или None."""
    p = u16(blob, 20)
    rate, bits, channels = None, 8, 1
    pcm = bytearray()
    while p < len(blob):
        t = blob[p]
        if t == 0 or p + 4 > len(blob):
            break
        size = blob[p + 1] | blob[p + 2] << 8 | blob[p + 3] << 16
        d = blob[p + 4: p + 4 + size]
        if t == 1 and len(d) >= 2:
            if d[1] != 0:  # ADPCM — не поддерживаем
                return None
            if rate is None:
                rate = 1_000_000 // (256 - d[0])
            pcm += d[2:]
        elif t == 2:
            pcm += d
        elif t == 3 and len(d) >= 3:
            pcm += b"\x80" * (u16(d, 0) + 1) * (bits // 8) * channels
        elif t == 8 and len(d) >= 4:
            tc = u16(d, 0)
            channels = 2 if d[3] else 1
            rate = 256_000_000 // (channels * (65536 - tc))
        elif t == 9 and len(d) >= 12:
            rate, bits, channels, codec = u32(d, 0), d[4], d[5], u16(d, 6)
            if codec not in (0, 4):
                return None
            pcm += d[12:]
        p += 4 + size
    if not pcm or not rate:
        return None
    return make_wav(bytes(pcm), rate, bits, channels)


def make_wav(pcm: bytes, rate: int, bits=8, channels=1) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(bits // 8)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


# --------------------------------------------------------------------- MIDI

def midi_length(data: bytes, off: int):
    if be32(data, off + 4) != 6:
        return None
    ntrk = be16(data, off + 10)
    p = off + 14
    for _ in range(ntrk):
        if data[p: p + 4] != b"MTrk":
            return None
        p += 8 + be32(data, p + 4)
    return min(p, len(data)) - off


def _midi_file(tracks, division: int, fmt: int = 0) -> bytes:
    out = b"MThd" + struct.pack(">IHHH", 6, fmt, len(tracks), division)
    for t in tracks:
        out += b"MTrk" + struct.pack(">I", len(t)) + t
    return out


def _track_from_events(events) -> bytes:
    """events: список (абсолютное_время, порядок, байты). Порядок: note-off раньше note-on."""
    events.sort(key=lambda e: (e[0], e[1]))
    out, last = bytearray(), 0
    for t, _o, ev in events:
        out += write_varlen(t - last) + ev
        last = t
    out += write_varlen(0) + b"\xff\x2f\x00"
    return bytes(out)


# -------------------------------------------------------------------- XMIDI

def iff_length(data: bytes, off: int):
    size = be32(data, off + 4)
    end = off + 8 + size + (size & 1)
    # XMI: FORM XDIR сразу за ним идёт CAT  ... XMID
    if data[off + 8: off + 12] == b"XDIR" and data[end: end + 4] == b"CAT ":
        size2 = be32(data, end + 4)
        end += 8 + size2 + (size2 & 1)
    return min(end, len(data)) - off


def _xmi_evnt_chunks(blob: bytes):
    """Рекурсивно обходит IFF и возвращает все чанки EVNT."""
    out = []

    def walk(p, end):
        while p + 8 <= end:
            cid, size = blob[p: p + 4], be32(blob, p + 4)
            if cid in (b"FORM", b"CAT ", b"LIST"):
                walk(p + 12, min(end, p + 8 + size))
            elif cid == b"EVNT":
                out.append(blob[p + 8: p + 8 + size])
            p += 8 + size + (size & 1)

    walk(0, len(blob))
    return out


def _xmi_evnt_to_track(ev: bytes) -> bytes:
    events, p, t, seq = [], 0, 0, 0
    while p < len(ev):
        b = ev[p]
        if b < 0x80:  # интервал: сумма байт < 0x80
            while p < len(ev) and ev[p] < 0x80:
                t += ev[p]
                p += 1
            continue
        p += 1
        hi = b & 0xF0
        seq += 1
        if hi == 0x90:
            note, vel = ev[p], ev[p + 1]
            dur, p = read_varlen(ev, p + 2)
            events.append((t, seq * 2 + 1, bytes([b, note, vel])))
            events.append((t + dur, 0, bytes([0x80 | (b & 0x0F), note, 0])))
        elif hi in (0x80, 0xA0, 0xB0, 0xE0):
            events.append((t, seq * 2 + 1, bytes([b]) + ev[p: p + 2]))
            p += 2
        elif hi in (0xC0, 0xD0):
            events.append((t, seq * 2 + 1, bytes([b, ev[p]])))
            p += 1
        elif b == 0xFF:
            mtype = ev[p]
            ln, q = read_varlen(ev, p + 1)
            data = ev[q: q + ln]
            p = q + ln
            if mtype == 0x2F:
                break
            if mtype != 0x51:  # темп XMI игнорируем: время фиксировано 120 тиков/с
                events.append((t, seq * 2 + 1, bytes([0xFF, mtype]) + write_varlen(ln) + data))
        elif b in (0xF0, 0xF7):
            ln, q = read_varlen(ev, p)
            events.append((t, seq * 2 + 1, bytes([b]) + write_varlen(ln) + ev[q: q + ln]))
            p = q + ln
        else:
            break
    # 120 тиков/с: PPQN 60 при темпе 500000 мкс на четверть
    events.append((0, -1, b"\xff\x51\x03\x07\xa1\x20"))
    return _track_from_events(events)


def xmi_to_midis(blob: bytes):
    """XMI может содержать несколько песен -> список MIDI-файлов."""
    result = []
    for ev in _xmi_evnt_chunks(blob):
        try:
            result.append(_midi_file([_xmi_evnt_to_track(ev)], 60))
        except IndexError:
            continue
    return result


# ---------------------------------------------------------------------- CMF

def cmf_to_midi(blob: bytes):
    """Creative Music File: внутри обычная MIDI-дорожка (инструменты — OPL2)."""
    if len(blob) < 0x14:
        return None
    music_off, tpq, tps = u16(blob, 8), u16(blob, 0x0A), u16(blob, 0x0C)
    if not (0x14 <= music_off < len(blob)) or not tpq or not tps:
        return None
    track = blob[music_off:]
    tempo = 1_000_000 * tpq // tps
    track = b"\x00\xff\x51\x03" + tempo.to_bytes(3, "big") + track
    if not track.endswith(b"\xff\x2f\x00"):
        track += b"\x00\xff\x2f\x00"
    return _midi_file([track], tpq)


# --------------------------------------------------------------- MOD / S3M

MOD_TAGS = {b"M.K.": 4, b"M!K!": 4, b"FLT4": 4, b"4CHN": 4, b"6CHN": 6, b"8CHN": 8, b"FLT8": 8, b"CD81": 8, b"OKTA": 8}


def mod_length(data: bytes, off: int):
    if len(data) - off < 1084:
        return None
    chans = MOD_TAGS.get(data[off + 1080: off + 1084])
    songlen = data[off + 950]
    if not chans or not (1 <= songlen <= 128):
        return None
    npat = max(data[off + 952: off + 952 + 128]) + 1
    samples = sum(be16(data, off + 20 + i * 30 + 22) * 2 for i in range(31))
    return min(1084 + npat * 64 * chans * 4 + samples, len(data) - off)


def s3m_length(data: bytes, off: int):
    try:
        ordnum, insnum, patnum = u16(data, off + 0x20), u16(data, off + 0x22), u16(data, off + 0x24)
        if ordnum > 256 or insnum > 256 or patnum > 256:
            return None
        base = off + 0x60 + ordnum
        end = base + 2 * (insnum + patnum)
        for i in range(insnum):
            ip = off + u16(data, base + 2 * i) * 16
            end = max(end, ip + 0x50)
            if data[ip] == 1:
                memseg = (data[ip + 0x0D] << 16) | u16(data, ip + 0x0E)
                end = max(end, off + memseg * 16 + u32(data, ip + 0x10))
        for i in range(patnum):
            pp = u16(data, base + 2 * (insnum + i)) * 16
            if pp:
                end = max(end, off + pp + u16(data, off + pp))
        return min(end, len(data)) - off
    except (IndexError, struct.error):
        return None
