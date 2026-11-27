"""Распознавание текста этикетки с фотографии (Tesseract OCR).

Требуются: системный пакет tesseract-ocr с русским языком (tesseract-ocr-rus)
и Python-пакеты pytesseract, pillow.
"""
from __future__ import annotations

import io
import re
from pathlib import Path

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}


# Языки распознавания: используются те, что установлены (tesseract-ocr-deu, -fra, …)
PREFERRED_LANGS = ("rus", "eng", "deu", "fra", "spa", "ita")
OCR_LANGS = "rus+eng"


class OCRUnavailable(RuntimeError):
    pass


def _deps():
    try:
        import pytesseract
        from PIL import Image, ImageOps
    except ImportError as e:  # pragma: no cover
        raise OCRUnavailable(
            "Для распознавания фото установите: pip install pytesseract pillow "
            "и системный пакет tesseract-ocr-rus") from e
    try:
        langs = set(pytesseract.get_languages(config=""))
    except Exception as e:  # pragma: no cover
        raise OCRUnavailable("Не найден исполняемый файл tesseract (apt install tesseract-ocr tesseract-ocr-rus)") from e
    if "rus" not in langs:
        raise OCRUnavailable("В Tesseract нет русского языка (apt install tesseract-ocr-rus)")
    global OCR_LANGS
    OCR_LANGS = "+".join(l for l in PREFERRED_LANGS if l in langs)
    return pytesseract, Image, ImageOps


def _preprocess(img, Image, ImageOps):
    img = ImageOps.exif_transpose(img).convert("L")
    # Мелкий шрифт этикеток распознаётся лучше после увеличения
    if max(img.size) < 2000:
        k = 2000 / max(img.size)
        img = img.resize((int(img.width * k), int(img.height * k)), Image.LANCZOS)
    img = ImageOps.autocontrast(img)
    return img


def clean_ocr_text(text: str) -> str:
    text = text.replace("­", "")
    text = re.sub(r"-\n(?=[а-яa-z])", "", text)        # переносы слов
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def image_to_text(source: str | Path | bytes) -> str:
    pytesseract, Image, ImageOps = _deps()
    img = Image.open(io.BytesIO(source) if isinstance(source, bytes) else source)
    img = _preprocess(img, Image, ImageOps)
    text = pytesseract.image_to_string(img, lang=OCR_LANGS, config="--psm 6")
    return clean_ocr_text(text)


def is_image(path: str | Path) -> bool:
    return Path(path).suffix.lower() in IMAGE_SUFFIXES
