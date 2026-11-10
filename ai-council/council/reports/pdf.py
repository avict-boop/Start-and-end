"""Отчёт в PDF (из HTML через WeasyPrint)."""
from __future__ import annotations

import logging

from .html import render_html
from .view import View

log = logging.getLogger("council.reports")

try:  # WeasyPrint нужны системные библиотеки (Pango); в Docker-образе они есть
    import weasyprint
    PDF_AVAILABLE = True
    PDF_ERROR = ""
except (ImportError, OSError) as e:  # pragma: no cover - зависит от системы
    weasyprint = None
    PDF_AVAILABLE = False
    PDF_ERROR = str(e)


def _no_external(url: str, *args, **kwargs):
    """Отчёт не должен ничего загружать из сети (ответ модели может содержать ссылки на картинки)."""
    if url.startswith("data:"):
        return weasyprint.default_url_fetcher(url, *args, **kwargs)
    raise ValueError(f"внешние ресурсы в отчёте запрещены: {url[:80]}")


def render_pdf(view: View, summary: bool = False) -> bytes:
    if not PDF_AVAILABLE:
        raise RuntimeError(f"PDF недоступен: не установлен WeasyPrint или системные библиотеки ({PDF_ERROR})")
    html = render_html(view, summary=summary)
    return weasyprint.HTML(string=html, url_fetcher=_no_external).write_pdf()
