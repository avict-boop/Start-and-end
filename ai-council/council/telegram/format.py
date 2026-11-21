"""Перевод Markdown ответов моделей в HTML-разметку Telegram и разбиение длинных сообщений."""
from __future__ import annotations

import html
import re

LIMIT = 4000  # с запасом от 4096


def esc(s: object) -> str:
    return html.escape(str(s if s is not None else ""), quote=False)


def md_to_tg(text: str) -> str:
    """Безопасно: сначала экранируем всё, затем добавляем только парные теги."""
    text = str(text or "").replace("\r\n", "\n")
    holders: list[str] = []

    def hold(s: str) -> str:
        holders.append(s)
        return f"\x00{len(holders) - 1}\x00"

    text = re.sub(r"```[^\n]*\n(.*?)```", lambda m: hold(f"<pre>{esc(m.group(1).rstrip())}</pre>"), text, flags=re.S)
    text = re.sub(r"`([^`\n]+)`", lambda m: hold(f"<code>{esc(m.group(1))}</code>"), text)
    text = tables_to_lists(text)
    text = esc(text)
    text = re.sub(r"^\s{0,3}#{1,6}\s+(.+)$", r"<b>\1</b>", text, flags=re.M)
    text = re.sub(r"^(\s*)[-*+]\s+", r"\1• ", text, flags=re.M)
    text = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__(?=\S)(.+?)(?<=\S)__", r"<b>\1</b>", text)
    text = re.sub(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)", r'<a href="\2">\1</a>', text)
    text = re.sub(r"\x00(\d+)\x00", lambda m: holders[int(m.group(1))], text)
    return text.strip()


_ROW = re.compile(r"^\s*\|.*\|\s*$")
_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def tables_to_lists(text: str) -> str:
    """В Telegram нет таблиц: строки Markdown-таблицы превращаются в пункты «• первая ячейка — заголовок: значение; …»."""
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    cells = lambda line: [c.strip() for c in line.strip().strip("|").split("|")]  # noqa: E731
    while i < len(lines):
        if _ROW.match(lines[i]) and i + 1 < len(lines) and _SEP.match(lines[i + 1]):
            head = cells(lines[i])
            i += 2
            while i < len(lines) and _ROW.match(lines[i]):
                row = cells(lines[i])
                first, rest = (row[0] if row else ""), row[1:]
                pairs = [f"{h}: {v}" if h else v for h, v in zip(head[1:], rest) if v]
                out.append(f"- **{first}**" + (" — " + "; ".join(pairs) if pairs else ""))
                i += 1
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


def strip_tags(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s))


def split_message(text: str, limit: int = LIMIT) -> list[str]:
    """Режет по абзацам/строкам, не разрывая теги. Слишком длинный кусок превращается в простой текст."""
    chunks: list[str] = []
    cur = ""
    for block in re.split(r"(\n\n+)", text):
        if len(cur) + len(block) <= limit:
            cur += block
            continue
        if cur.strip():
            chunks.append(cur.strip())
        cur = ""
        if len(block) <= limit:
            cur = block
            continue
        for line in block.split("\n"):
            if len(cur) + len(line) + 1 <= limit:
                cur += line + "\n"
                continue
            if cur.strip():
                chunks.append(cur.strip())
            cur = ""
            if len(line) > limit:
                plain = strip_tags(line)
                step = limit // 2  # экранирование может удлинить текст
                chunks += [esc(plain[i:i + step]) for i in range(0, len(plain), step)]
            else:
                cur = line + "\n"
    if cur.strip():
        chunks.append(cur.strip())
    return chunks or [""]
