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
    """Отчёт не должен ничего загружать из сети (ответ модели может содержать ссылки на картинки).

    Для WeasyPrint < 70: функция-загрузчик, пропускающая только встроенные data:-картинки.
    """
    if url.startswith("data:"):
        return weasyprint.default_url_fetcher(url, *args, **kwargs)
    raise ValueError(f"внешние ресурсы в отчёте запрещены: {url[:80]}")


def _fetcher():
    """WeasyPrint ≥ 70 ждёт объект URLFetcher; разрешаем только data: (картинки вложений внутри отчёта)."""
    cls = getattr(getattr(weasyprint, "urls", None), "URLFetcher", None)
    if cls is not None:
        return cls(allowed_protocols=["data"], timeout=5)
    return _no_external


def render_pdf(view: View, summary: bool = False) -> bytes:
    if not PDF_AVAILABLE:
        raise RuntimeError(f"PDF недоступен: не установлен WeasyPrint или системные библиотеки ({PDF_ERROR})")
    html = render_html(view, summary=summary)
    return weasyprint.HTML(string=html, url_fetcher=_fetcher()).write_pdf()
