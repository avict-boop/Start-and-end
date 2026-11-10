"""Расшифровка голоса: декодирование любого аудио в WAV и распознавание моделью OpenRouter с аудиовходом.

Голосовые Telegram (OGG/Opus), запись браузера (WebM/Opus в Chrome, MP4/AAC в Safari), MP3 и т. п.
декодируются библиотекой PyAV (собственные кодеки внутри пакета, системный ffmpeg не нужен).
"""
from __future__ import annotations

import base64
import io
import wave

MAX_AUDIO_SECONDS = 300
MAX_AUDIO_BYTES = 20 * 1024 * 1024
RATE = 16_000

PROMPT = ("Transcribe this audio recording verbatim, in the language it is spoken in. "
          "Keep the speaker's wording; add punctuation and paragraph breaks. "
          "Return ONLY the transcript text, without any comments. If there is no speech, return an empty string.")


class TranscribeError(RuntimeError):
    pass


def to_wav(data: bytes) -> tuple[bytes, float]:
    """Любое аудио → WAV 16 кГц моно. Возвращает (wav, длительность в секундах)."""
    if not data:
        raise TranscribeError("пустая запись")
    if len(data) > MAX_AUDIO_BYTES:
        raise TranscribeError("запись больше 20 МБ")
    try:
        import av
    except ImportError as e:  # pragma: no cover
        raise TranscribeError("не установлен пакет av (pip install av)") from e
    pcm = bytearray()
    try:
        with av.open(io.BytesIO(data)) as container:
            stream = next((s for s in container.streams if s.type == "audio"), None)
            if stream is None:
                raise TranscribeError("в файле нет звуковой дорожки")
            resampler = av.AudioResampler(format="s16", layout="mono", rate=RATE)
            for frame in container.decode(stream):
                for out in resampler.resample(frame):
                    pcm += bytes(out.planes[0])[: out.samples * 2]
                if len(pcm) > MAX_AUDIO_SECONDS * RATE * 2:
                    raise TranscribeError(f"запись длиннее {MAX_AUDIO_SECONDS // 60} минут")
            for out in resampler.resample(None):
                pcm += bytes(out.planes[0])[: out.samples * 2]
    except TranscribeError:
        raise
    except Exception as e:  # noqa: BLE001 — повреждённый или неподдерживаемый файл
        raise TranscribeError(f"не удалось прочитать аудио ({e.__class__.__name__})") from e
    seconds = len(pcm) / (RATE * 2)
    if seconds < 0.3:
        raise TranscribeError("запись слишком короткая")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(bytes(pcm))
    return buf.getvalue(), seconds


async def transcribe(client, model: str, data: bytes) -> tuple[str, float, float]:
    """Распознаёт речь. client — OpenRouterClient или MockClient. Возвращает (текст, секунды, стоимость $)."""
    import asyncio

    wav, seconds = await asyncio.to_thread(to_wav, data)
    messages = [{"role": "user", "content": [
        {"type": "text", "text": PROMPT},
        {"type": "input_audio", "input_audio": {"data": base64.b64encode(wav).decode("ascii"), "format": "wav"}},
    ]}]
    comp = await client.complete(model, messages, temperature=0, max_tokens=4000, reasoning_effort=None,
                                 meta={"stage": "transcribe", "seconds": seconds})
    text = comp.text.strip().strip('"').strip()
    if not text:
        raise TranscribeError("речь не распознана — попробуйте записать ещё раз")
    return text, seconds, comp.cost
