import asyncio
import io

import pytest
from PIL import Image

from council.attachments import (MAX_DOC_CHARS, AttachmentError, assign_files, limit_total, load_attachment,
                                 supports_images)
from council.config import CouncilConfig
from council.estimate import estimate, vision_map
from council.mock import MockClient
from council.session import Council


def png(w=3000, h=2000, color="orange") -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (w, h), color).save(b, "PNG")
    return b.getvalue()


def docx_bytes() -> bytes:
    import docx
    d = docx.Document()
    d.add_heading("Бриф", 1)
    d.add_paragraph("Бюджет 1 млн ₽")
    t = d.add_table(2, 2)
    t.cell(0, 0).text, t.cell(0, 1).text, t.cell(1, 0).text, t.cell(1, 1).text = "Месяц", "Сумма", "Январь", "10"
    b = io.BytesIO()
    d.save(b)
    return b.getvalue()


def test_parse_types_and_limits():
    img = load_attachment(png(), "фото.png")
    assert img.kind == "image" and max(img.width, img.height) == 1600 and img.data_url.startswith("data:image/")
    doc = load_attachment(docx_bytes(), "бриф.docx")
    assert doc.kind == "document" and "# Бриф" in doc.text and "| Январь | 10 |" in doc.text
    import openpyxl
    wb = openpyxl.Workbook()
    wb.active.append(["a", 1])
    xb = io.BytesIO()
    wb.save(xb)
    assert "a | 1" in load_attachment(xb.getvalue(), "t.xlsx").text
    import weasyprint
    pdf = load_attachment(weasyprint.HTML(string="<p>Договор №7</p>").write_pdf(), "d.pdf")
    assert pdf.pages == 1 and "Договор №7" in pdf.text
    big = load_attachment(("я" * (MAX_DOC_CHARS + 10)).encode(), "big.txt")
    assert big.truncated and len(big.text) == MAX_DOC_CHARS
    for name, data in [("x.exe", b"MZ"), ("broken.jpg", b"nope"), ("empty.txt", b"")]:
        with pytest.raises(AttachmentError):
            load_attachment(data, name)
    atts = [load_attachment(("б" * 60_000).encode(), f"{i}.txt") for i in range(3)]
    assert limit_total(atts) and sum(len(a.text) for a in atts) == 150_000
    assign_files(atts)
    assert [a.file for a in atts] == ["01-0.txt", "02-1.txt", "03-2.txt"]
    assert "raw" not in atts[0].meta() and "text" not in atts[0].meta()


def test_supports_images():
    assert supports_images({"architecture": {"input_modalities": ["text", "image"]}}) is True
    assert supports_images({"architecture": {"modality": "text->text"}}) is False
    assert supports_images(None) is None


class Recorder(MockClient):
    def __init__(self):
        super().__init__(delay=0)
        self.msgs = []

    async def complete(self, model, messages, **kw):
        self.msgs.append((model, (kw.get("meta") or {}).get("stage"), messages[-1]["content"]))
        return await super().complete(model, messages, **kw)


def test_models_get_images_or_descriptions():
    cfg = CouncilConfig.model_validate({"chair": "a", "members": [{"name": n, "model": f"m/{n}"} for n in "abc"]})
    client = Recorder()
    atts = [load_attachment(png(), "график.png"), load_attachment(docx_bytes(), "бриф.docx")]
    assign_files(atts)
    vision = {"m/a": True, "m/b": True, "m/c": False}
    res = asyncio.run(Council(cfg, client, vision=vision).run("Вопрос?", mode="quick", criteria=["x"], attachments=atts))
    by = {(m, st): c for m, st, c in client.msgs}
    # описание картинок сделал председатель (он видит), один раз
    assert [m for m, st, _ in client.msgs if st == "describe"] == ["m/a"]
    assert res.image_notes_by == "a" and "график.png" in res.image_notes
    seen = by[("m/a", "proposal")]
    assert isinstance(seen, list) and seen[1]["type"] == "image_url" and "Бюджет 1 млн" in seen[0]["text"]
    blind = by[("m/c", "proposal")]
    assert isinstance(blind, str) and "cannot see" in blind and "таблица с цифрами" in blind and "Бюджет 1 млн" in blind
    assert isinstance(by[("m/a", "vote")], str) and "Бюджет 1 млн" not in by[("m/a", "vote")]  # голосование без вложений
    assert [a["name"] for a in res.to_dict()["attachments"]] == ["график.png", "бриф.docx"]


def test_no_description_when_everyone_sees():
    cfg = CouncilConfig.model_validate({"chair": "a", "members": [{"name": n, "model": f"m/{n}"} for n in "ab"]})
    client = Recorder()
    asyncio.run(Council(cfg, client).run("Q?", mode="quick", criteria=["x"], attachments=[load_attachment(png(), "p.png")]))
    assert not [1 for _, st, _ in client.msgs if st == "describe"]


def test_estimate_counts_attachments():
    cfg = CouncilConfig.model_validate({"chair": "a", "members": [{"name": "a", "model": "m/a"},
                                                                   {"name": "b", "model": "deepseek/deepseek-chat"}]})
    vision = vision_map(cfg, None)
    assert vision == {"m/a": None, "deepseek/deepseek-chat": False}
    plain = estimate(cfg, {}, mode="quick", rounds=1)
    rich = estimate(cfg, {}, mode="quick", rounds=1, doc_tokens=5000, images=2, vision=vision)
    assert rich.calls == plain.calls + 1  # + описание картинок
    assert all(r.prompt_tokens > p.prompt_tokens for r, p in zip(rich.members, plain.members))
