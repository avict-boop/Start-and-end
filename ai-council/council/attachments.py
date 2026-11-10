"""Вложения к запросу: картинки и документы.

Картинки уменьшаются (экономия токенов) и передаются моделям, которые умеют смотреть на изображения;
для остальных одна из «зрячих» моделей заранее описывает картинки текстом.
Из документов (PDF, Word, Excel, текст) извлекается текст — он добавляется к запросу.
"""
from __future__ import annotations

import base64
import csv
import io
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

MAX_FILES = 10
MAX_FILE_BYTES = 20 * 1024 * 1024        # как у Telegram Bot API
MAX_DOC_CHARS = 60_000                    # на один документ
MAX_TOTAL_DOC_CHARS = 150_000             # на все документы запроса
IMAGE_MAX_SIDE = 1600                     # пикселей по длинной стороне
IMAGE_TOKENS = 1100                       # грубая оценка «стоимости» одной картинки в токенах

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
TEXT_EXT = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".xml", ".yaml", ".yml", ".log", ".html", ".htm",
            ".py", ".js", ".ts", ".sql", ".ini", ".cfg", ".toml"}
DOC_EXT = {".pdf", ".docx", ".xlsx", ".xlsm"} | TEXT_EXT
ACCEPT = sorted(IMAGE_EXT | DOC_EXT)


class AttachmentError(ValueError):
    """Файл нельзя приложить (тип, размер, повреждён)."""


@dataclass
class Attachment:
    name: str                      # имя для людей (исходное имя файла)
    kind: str                      # image | document
    file: str                      # имя файла на диске (в папке вложений)
    size: int
    mime: str = ""
    text: str = ""                 # извлечённый текст (для документов)
    chars: int = 0                 # сколько символов текста в исходнике
    truncated: bool = False
    pages: int | None = None
    width: int | None = None
    height: int | None = None
    warning: str = ""
    image_b64: str = field(default="", repr=False)  # уменьшенная картинка (JPEG/PNG) для моделей
    raw: bytes = field(default=b"", repr=False)       # исходный файл (сохраняется в архив)

    def meta(self) -> dict[str, Any]:
        """Описание без содержимого — для стенограммы, архива, интерфейса."""
        d = {k: v for k, v in asdict(self).items() if k not in ("image_b64", "text", "raw")}
        return d

    @property
    def data_url(self) -> str:
        if not self.image_b64:
            return ""
        mime = "image/png" if self.image_b64.startswith("iVBOR") else "image/jpeg"
        return f"data:{mime};base64,{self.image_b64}"

    @property
    def tokens(self) -> int:
        return IMAGE_TOKENS if self.kind == "image" else max(1, len(self.text) // 3)


def safe_name(name: str) -> str:
    name = Path(str(name or "file")).name
    stem, ext = Path(name).stem, Path(name).suffix.lower()
    stem = re.sub(r"[^\w\-. ]+", "_", stem, flags=re.U).strip(" ._")[:80] or "file"
    return f"{stem}{ext}"


def human_size(n: int) -> str:
    for unit in ("Б", "КБ", "МБ"):
        if n < 1024 or unit == "МБ":
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} Б"


# ------------------------------------------------------------------ разбор файлов


def _image(data: bytes) -> tuple[str, int, int, str]:
    """Проверяет и уменьшает картинку. Возвращает (base64, ширина, высота, mime)."""
    from PIL import Image, ImageOps

    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as e:  # noqa: BLE001 — любой мусор вместо картинки
        raise AttachmentError("файл не похож на картинку или повреждён") from e
    img = ImageOps.exif_transpose(img)  # фото с телефона — правильная ориентация
    if getattr(img, "is_animated", False):
        img.seek(0)
    img.thumbnail((IMAGE_MAX_SIDE, IMAGE_MAX_SIDE))
    out = io.BytesIO()
    has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
    if has_alpha:
        img.save(out, "PNG", optimize=True)
        mime = "image/png"
    else:
        img.convert("RGB").save(out, "JPEG", quality=85, optimize=True)
        mime = "image/jpeg"
    return base64.b64encode(out.getvalue()).decode("ascii"), img.width, img.height, mime


def _pdf_text(data: bytes) -> tuple[str, int]:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception as e:  # noqa: BLE001
                raise AttachmentError("PDF защищён паролем") from e
        parts = []
        for i, page in enumerate(reader.pages, 1):
            t = (page.extract_text() or "").strip()
            if t:
                parts.append(f"[страница {i}]\n{t}")
        return "\n\n".join(parts), len(reader.pages)
    except AttachmentError:
        raise
    except Exception as e:  # noqa: BLE001
        raise AttachmentError(f"не удалось прочитать PDF: {e.__class__.__name__}") from e


def _docx_text(data: bytes) -> str:
    from docx import Document

    try:
        doc = Document(io.BytesIO(data))
    except Exception as e:  # noqa: BLE001
        raise AttachmentError("не удалось прочитать документ Word (нужен .docx)") from e
    parts: list[str] = []
    body = doc.element.body
    for child in body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            text = "".join(t.text or "" for t in child.iter() if t.tag.endswith("}t"))
            style = child.find(".//{*}pStyle")
            sval = style.get("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}val", "") if style is not None else ""
            if text.strip():
                if sval.lower().startswith("heading"):
                    text = "#" * min(6, int(re.sub(r"\D", "", sval) or 1)) + " " + text
                elif "list" in sval.lower():
                    text = "- " + text
                parts.append(text)
        elif tag == "tbl":
            rows = []
            for tr in child.iter("{*}tr"):
                cells = ["".join(t.text or "" for t in tc.iter() if t.tag.endswith("}t")).replace("|", "/").strip()
                         for tc in tr.iter("{*}tc")]
                rows.append("| " + " | ".join(cells) + " |")
            if rows:
                ncol = rows[0].count("|") - 1
                rows.insert(1, "|" + "---|" * max(ncol, 1))
                parts.append("\n".join(rows))
    return "\n\n".join(parts)


def _xlsx_text(data: bytes) -> str:
    from openpyxl import load_workbook

    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as e:  # noqa: BLE001
        raise AttachmentError("не удалось прочитать таблицу Excel (нужен .xlsx)") from e
    parts = []
    for ws in wb.worksheets:
        rows = []
        for row in ws.iter_rows(values_only=True):
            if row is None or all(v is None or str(v).strip() == "" for v in row):
                continue
            rows.append(" | ".join("" if v is None else str(v).replace("\n", " ") for v in row))
            if len(rows) >= 2000:
                rows.append("… (строки дальше не показаны)")
                break
        if rows:
            parts.append(f"[лист «{ws.title}»]\n" + "\n".join(rows))
    wb.close()
    return "\n\n".join(parts)


def _plain_text(data: bytes, ext: str) -> str:
    for enc in ("utf-8-sig", "cp1251", "latin-1"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if ext in (".html", ".htm"):
        text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"[ \t]+", " ", text)
    if ext == ".json":
        try:
            text = json.dumps(json.loads(text), ensure_ascii=False, indent=1)
        except ValueError:
            pass
    if ext in (".csv", ".tsv"):
        try:
            rows = list(csv.reader(io.StringIO(text), delimiter="\t" if ext == ".tsv" else ","))[:3000]
            text = "\n".join(" | ".join(r) for r in rows)
        except csv.Error:
            pass
    return text


def load_attachment(data: bytes, name: str, file: str = "") -> Attachment:
    """Разбирает загруженный файл. Бросает AttachmentError, если файл приложить нельзя."""
    name = safe_name(name)
    ext = Path(name).suffix.lower()
    if len(data) > MAX_FILE_BYTES:
        raise AttachmentError(f"«{name}» больше {MAX_FILE_BYTES // 1024 // 1024} МБ")
    if not data:
        raise AttachmentError(f"«{name}» пустой")
    att = Attachment(name=name, kind="document", file=file or name, size=len(data), raw=data)
    if ext in IMAGE_EXT:
        att.kind = "image"
        att.image_b64, att.width, att.height, att.mime = _image(data)
        return att
    if ext not in DOC_EXT:
        raise AttachmentError(f"«{name}»: такой тип файла не поддерживается. Можно: картинки (JPG, PNG, WebP), "
                              "PDF, Word (.docx), Excel (.xlsx), текстовые файлы (TXT, MD, CSV, JSON)")
    if ext == ".pdf":
        att.mime = "application/pdf"
        text, att.pages = _pdf_text(data)
        if not text.strip():
            att.warning = ("в PDF нет текстового слоя (похоже на скан) — приложите страницы картинками, "
                           "чтобы модели их увидели")
    elif ext == ".docx":
        att.mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        text = _docx_text(data)
    elif ext in (".xlsx", ".xlsm"):
        att.mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        text = _xlsx_text(data)
    else:
        att.mime = "text/plain"
        text = _plain_text(data, ext)
    text = text.replace("\x00", "").strip()
    att.chars = len(text)
    if len(text) > MAX_DOC_CHARS:
        text = text[:MAX_DOC_CHARS]
        att.truncated = True
    att.text = text
    if not text and not att.warning:
        att.warning = "в файле не найден текст"
    return att


def assign_files(atts: list[Attachment]) -> None:
    """Уникальные имена файлов для сохранения: 01-имя.pdf, 02-имя.jpg…"""
    for i, a in enumerate(atts, 1):
        a.file = f"{i:02d}-{safe_name(a.name)}"


def save_files(atts: list[Attachment], folder: Path) -> None:
    if not atts:
        return
    folder.mkdir(parents=True, exist_ok=True)
    for a in atts:
        if a.raw:
            (folder / a.file).write_bytes(a.raw)


def load_from_dir(folder: Path, metas: list[dict[str, Any]]) -> list[Attachment]:
    """Восстанавливает вложения из сохранённых файлов (например, для пересборки отчётов)."""
    out = []
    for m in metas:
        p = folder / m.get("file", "")
        if p.is_file():
            try:
                out.append(load_attachment(p.read_bytes(), m.get("name") or p.name, m.get("file", p.name)))
            except AttachmentError:
                continue
    return out


def limit_total(atts: list[Attachment]) -> list[str]:
    """Обрезает документы, если вместе они больше MAX_TOTAL_DOC_CHARS. Возвращает предупреждения."""
    warnings = []
    total = 0
    for a in atts:
        if a.kind != "document":
            continue
        room = MAX_TOTAL_DOC_CHARS - total
        if len(a.text) > room:
            a.text = a.text[:max(0, room)]
            a.truncated = True
            warnings.append(f"«{a.name}» обрезан: общий объём документов больше {MAX_TOTAL_DOC_CHARS:,} символов"
                            .replace(",", " "))
        total += len(a.text)
    return warnings


# ------------------------------------------------------------------ для промптов


def documents_block(atts: list[Attachment]) -> str:
    docs = [a for a in atts if a.kind == "document" and a.text]
    if not docs:
        return ""
    parts = ["Attached documents from the user (treat them as source data for the question):"]
    for a in docs:
        note = " (truncated)" if a.truncated else ""
        parts.append(f"\n===== Document: {a.name}{note} =====\n{a.text}\n===== End of {a.name} =====")
    return "\n".join(parts)


def images_note(atts: list[Attachment], descriptions: dict[str, str] | None, can_see: bool) -> str:
    imgs = [a for a in atts if a.kind == "image"]
    if not imgs:
        return ""
    names = ", ".join(a.name for a in imgs)
    if can_see:
        return f"The user attached {len(imgs)} image(s), shown below in order: {names}. Use them as source data."
    if descriptions:
        body = "\n".join(f"- {n}: {descriptions.get(n, '(no description)')}" for n in (a.name for a in imgs))
        return ("The user attached image(s) that you cannot see. Another council member described them for you:\n"
                + body)
    return f"The user attached image(s) you cannot see ({names}); no description is available."


def image_parts(atts: list[Attachment]) -> list[dict[str, Any]]:
    return [{"type": "image_url", "image_url": {"url": a.data_url}} for a in atts if a.kind == "image" and a.data_url]


def supports_images(model_info: dict[str, Any] | None) -> bool | None:
    """По записи из каталога OpenRouter: умеет ли модель принимать картинки (None — неизвестно)."""
    if not model_info:
        return None
    arch = model_info.get("architecture") or {}
    mods = arch.get("input_modalities")
    if isinstance(mods, list):
        return "image" in mods
    modality = str(arch.get("modality") or "")
    if "->" in modality:
        return "image" in modality.split("->", 1)[0]
    return None
