"""Фирменное оформление отчётов: название, логотип, цвет, текст колонтитула (раздел branding в config.yaml)."""
from __future__ import annotations

import base64
import io
import logging
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("council.reports")

DEFAULT_TITLE = "Совет нейросетей"
DEFAULT_COLOR = "#4f46e5"  # индиго логотипа


@dataclass
class Brand:
    title: str = DEFAULT_TITLE        # в шапке и колонтитулах
    color: str = DEFAULT_COLOR        # акцентный цвет (заголовки, метки, полосы)
    footer: str = ""                  # дополнительный текст колонтитула, например «Конфиденциально»
    logo_png: bytes = b""             # логотип, приведённый к PNG (не больше 600 px)

    @property
    def logo_data_url(self) -> str:
        return "data:image/png;base64," + base64.b64encode(self.logo_png).decode() if self.logo_png else ""

    @property
    def footer_text(self) -> str:
        return " · ".join(x for x in (self.title, self.footer) if x)

    @property
    def rgb(self) -> tuple[int, int, int]:
        h = self.color.lstrip("#")
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


_current = Brand()


def current() -> Brand:
    return _current


def set_brand(cfg_branding, base_dir: str | Path) -> Brand:
    """Применяет раздел branding из конфигурации. Ошибки (нет файла логотипа и т. п.) не ломают отчёты."""
    global _current
    b = Brand()
    if cfg_branding is not None:
        b.title = (getattr(cfg_branding, "title", "") or "").strip() or DEFAULT_TITLE
        color = (getattr(cfg_branding, "color", "") or "").strip()
        if re.fullmatch(r"#?[0-9a-fA-F]{6}", color):
            b.color = "#" + color.lstrip("#").lower()
        b.footer = (getattr(cfg_branding, "footer", "") or "").strip()
        logo = (getattr(cfg_branding, "logo", "") or "").strip()
        if logo:
            p = Path(logo)
            p = p if p.is_absolute() else Path(base_dir) / p
            try:
                from PIL import Image
                img = Image.open(p)
                img.load()
                img.thumbnail((600, 600))
                out = io.BytesIO()
                img.convert("RGBA").save(out, "PNG", optimize=True)
                b.logo_png = out.getvalue()
            except Exception as e:  # noqa: BLE001
                log.warning("логотип %s не загружен: %s", p, e)
    _current = b
    return b
