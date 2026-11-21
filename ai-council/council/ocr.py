"""Распознавание сканов: PDF без текстового слоя и картинки (сертификаты, лицензии, страницы годовых отчётов).

Работает через программы Tesseract (распознавание) и pdftoppm из poppler-utils (страницы PDF в картинки) — они
ставятся в Docker-образ. Нет программ на сервере — ничего не делает (возвращает пустой текст), остальное работает
как раньше. Модели не нужны, денег не стоит; страница — несколько секунд процессора.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

log = logging.getLogger("council.ocr")
PAGE_TIMEOUT = 90  # одна страница — не дольше, с
DPI = 200          # мелкий китайский текст сертификатов на 150 dpi распознаётся хуже


def available() -> bool:
    return bool(shutil.which("tesseract"))


def can_split_pdf() -> bool:
    return bool(shutil.which("pdftoppm"))


def _tesseract(image: Path, langs: str) -> str:
    try:
        r = subprocess.run(["tesseract", str(image), "stdout", "-l", langs, "--psm", "3"], capture_output=True,
                           timeout=PAGE_TIMEOUT, check=False, env={**os.environ, "OMP_THREAD_LIMIT": "1"})
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("распознавание %s не удалось: %r", image.name, e)
        return ""
    if r.returncode != 0:
        err = r.stderr.decode("utf-8", "ignore")[:200]
        # нет языка (например, китайского) — пробуем без него, чем вовсе без текста
        if "Failed loading language" in err and "+" in langs:
            return _tesseract(image, "eng")
        log.warning("tesseract %s: %s", image.name, err)
        return ""
    return r.stdout.decode("utf-8", "ignore")


# письменность (по определению Tesseract OSD) → языки распознавания; английский — всегда (номера, единицы, бренды)
SCRIPT_LANGS = {
    "Han": "chi_sim+chi_tra+eng", "Japanese": "jpn+eng", "Katakana": "jpn+eng", "Hiragana": "jpn+eng",
    "Hangul": "kor+eng", "Cyrillic": "rus+ukr+eng", "Latin": "eng+deu+fra+spa+ita+por+tur+pol+vie+nld",
    "Arabic": "ara+eng", "Hebrew": "heb+eng", "Devanagari": "hin+eng", "Thai": "tha+eng", "Greek": "ell+eng",
}
AUTO_FALLBACK = "chi_sim+eng+rus"


def installed_langs() -> set[str]:
    try:
        r = subprocess.run(["tesseract", "--list-langs"], capture_output=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return set()
    return {x.strip() for x in r.stdout.decode("utf-8", "ignore").splitlines()[1:] if x.strip()}


_LANGS: set[str] | None = None


def _only_installed(langs: str) -> str:
    """Из списка языков — только установленные (не хватает пакета — Tesseract иначе не запустится вовсе)."""
    global _LANGS
    if _LANGS is None:
        _LANGS = installed_langs()
    keep = [x for x in langs.split("+") if not _LANGS or x in _LANGS]
    return "+".join(keep) or "eng"


def detect_langs(image: Path) -> str:
    """Письменность страницы (OSD) → языки. Не определилась — запасной набор."""
    try:
        r = subprocess.run(["tesseract", str(image), "stdout", "--psm", "0"], capture_output=True, timeout=60,
                           check=False, env={**os.environ, "OMP_THREAD_LIMIT": "1"})
        out = r.stdout.decode("utf-8", "ignore") + r.stderr.decode("utf-8", "ignore")
    except (OSError, subprocess.TimeoutExpired):
        return _only_installed(AUTO_FALLBACK)
    import re
    m = re.search(r"Script:\s*(\w+)", out)
    return _only_installed(SCRIPT_LANGS.get(m.group(1), AUTO_FALLBACK) if m else AUTO_FALLBACK)


def _langs_for(image: Path, langs: str) -> str:
    return detect_langs(image) if langs in ("", "auto") else _only_installed(langs)


def image_text(data: bytes, langs: str = "auto", suffix: str = ".png") -> str:
    """Текст картинки (фото сертификата, скриншот). Нет tesseract — пусто."""
    if not data or not available():
        return ""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / f"img{suffix if suffix.startswith('.') else '.' + suffix}"
        p.write_bytes(data)
        return _tesseract(p, _langs_for(p, langs)).strip()


def pdf_text(data: bytes, *, langs: str = "auto", pages: int = 20, parallel: int = 4,
             first: int = 1) -> str:
    """Текст скана PDF: страницы first…first+pages-1 в картинки (pdftoppm), каждая — в Tesseract, несколько сразу.
    Возвращает «[страница N]\\nтекст» по порядку. Нет программ — пусто."""
    if not data or not available() or not can_split_pdf():
        return ""
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "doc.pdf"
        src.write_bytes(data)
        try:
            subprocess.run(["pdftoppm", "-r", str(DPI), "-gray", "-png", "-f", str(first), "-l",
                            str(first + pages - 1), str(src), str(Path(d) / "p")],
                           capture_output=True, timeout=PAGE_TIMEOUT * 2, check=False)
        except (OSError, subprocess.TimeoutExpired) as e:
            log.warning("страницы PDF не разобраны на картинки: %r", e)
            return ""
        imgs = sorted(Path(d).glob("p-*.png"), key=lambda f: int(f.stem.rsplit("-", 1)[-1]))
        if not imgs:
            return ""
        # письменность — по первой странице (у документа она одна), языки — для всех страниц
        use = _langs_for(imgs[0], langs)
        with ThreadPoolExecutor(max_workers=max(1, parallel)) as ex:
            texts = list(ex.map(lambda f: _tesseract(f, use), imgs))
    out = []
    for f, t in zip(imgs, texts):
        t = t.strip()
        if t:
            out.append(f"[страница {int(f.stem.rsplit('-', 1)[-1])}]\n{t}")
    return "\n\n".join(out)


def looks_scanned(text: str, pages: int) -> bool:
    """Текстового слоя почти нет: меньше ~60 знаков на страницу — скорее всего скан."""
    return len((text or "").strip()) < max(200, 60 * max(1, pages))
