"""Полный цикл мода: выгрузить -> отредактировать -> собрать -> игра работает с новыми ресурсами."""

import io
import json
import random
import wave

import pytest
from PIL import Image

from dosrip.formats import audio, images
from dosrip.mod import build_mod
from dosrip.pipeline import process_zip

from dosrun import run_exe
from fixtures import game_zip

PCX_PNG = "mod/graphics/GAME__RESOURCE.DAT@000BB8.png"
PCX_OFF = 0xBB8


@pytest.fixture()
def game(tmp_path):
    z = tmp_path / "game.zip"
    z.write_bytes(game_zip())
    out = tmp_path / "out"
    process_zip(z, out, log=lambda *_: None)
    return out


def _orig(out, name):
    return (out / "original/GAME" / name).read_bytes()


def _patched(out, name):
    return (out / "patched/GAME" / name).read_bytes()


def _items(res):
    return {i.file: i for i in res.items}


def test_no_edits_gives_identical_game(game):
    res = build_mod(game, log=lambda *_: None)
    assert res.items == [] and res.unchanged > 0
    for name in ("RESOURCE.DAT", "GAME.EXE", "PACKED.EXE", "TITLE.PAL", "TITLE.SCR", "README.TXT"):
        assert _patched(game, name) == _orig(game, name)
    assert "PACKED.EXE" in (game / "patched/dosbox.conf").read_text()


def test_redrawn_pcx_inside_archive(game):
    png = game / PCX_PNG
    img = Image.open(png)
    assert img.mode == "P"
    for x in range(32):  # рисуем линию цветом 200
        img.putpixel((x, 5), 200)
    img.save(png)
    res = build_mod(game, log=lambda *_: None)
    assert _items(res)["graphics/GAME__RESOURCE.DAT@000BB8.png"].status == "ok"
    orig, new = _orig(game, "RESOURCE.DAT"), _patched(game, "RESOURCE.DAT")
    assert len(new) == len(orig)  # архив не сдвинулся
    got, n = images.decode_pcx(new, PCX_OFF)
    assert got.tobytes() == img.tobytes()
    # остальные ресурсы архива не тронуты
    assert new[:PCX_OFF] == orig[:PCX_OFF] and new[PCX_OFF + n:] == orig[PCX_OFF + n:]


def test_redraw_at_4x_in_full_color(game):
    png = game / PCX_PNG
    small = Image.open(png)
    big = small.convert("RGB").resize((128, 80), Image.NEAREST)  # художник рисует в 4x и в RGB
    big.save(png)
    build_mod(game, log=lambda *_: None)
    got, _ = images.decode_pcx(_patched(game, "RESOURCE.DAT"), PCX_OFF)
    # те же цвета палитры: картинка после уменьшения и подбора цветов совпадает с исходной по RGB
    assert got.convert("RGB").tobytes() == small.convert("RGB").tobytes()


def test_too_detailed_image_is_rejected_safely(game):
    png = game / PCX_PNG
    img = Image.open(png)
    rnd = random.Random(5)
    img.putdata([rnd.randrange(0xC0, 256) for _ in range(32 * 20)])  # шум из «тяжёлых» цветов не сжимается
    img.save(png)
    res = build_mod(game, log=lambda *_: None)
    item = _items(res)["graphics/GAME__RESOURCE.DAT@000BB8.png"]
    assert item.status == "error" and "Упростите рисунок" in item.message
    assert _patched(game, "RESOURCE.DAT") == _orig(game, "RESOURCE.DAT")


def test_wrong_size_image(game):
    Image.new("RGB", (50, 50)).save(game / PCX_PNG)
    item = _items(build_mod(game, log=lambda *_: None))["graphics/GAME__RESOURCE.DAT@000BB8.png"]
    assert item.status == "error" and "32x20" in item.message


def test_palette_and_raw_screen(game):
    # меняем цвет №40 палитры на чисто красный
    sw = Image.open(game / "mod/palettes/GAME__TITLE.PAL.png").convert("RGB")
    cx, cy = (40 % 16) * 16 + 8, (40 // 16) * 16 + 8
    for dx in range(-4, 4):
        for dy in range(-4, 4):
            sw.putpixel((cx + dx, cy + dy), (252, 0, 0))
    sw.save(game / "mod/palettes/GAME__TITLE.PAL.png")
    # и на экране рисуем пиксель этим красным цветом
    scr = Image.open(game / "mod/graphics/GAME__TITLE.SCR.png").convert("RGB")
    scr.putpixel((0, 0), (252, 0, 0))
    scr.save(game / "mod/graphics/GAME__TITLE.SCR.png")
    res = build_mod(game, log=lambda *_: None)
    assert not res.errors, res.items
    pal = _patched(game, "TITLE.PAL")
    assert pal[40 * 3: 40 * 3 + 3] == bytes([63, 0, 0])
    screen = _patched(game, "TITLE.SCR")
    assert len(screen) == 64000 and screen[0] == 40  # подобран по новой палитре


def test_replace_sound(game):
    wav_path = game / "mod/sound/GAME__RESOURCE.DAT@001E11.wav"
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:  # 16 бит стерео 22 кГц, длиннее оригинала
        w.setnchannels(2), w.setsampwidth(2), w.setframerate(22050)
        w.writeframes(b"".join((((i // 30) % 2) * 20000 - 10000).to_bytes(2, "little", signed=True) * 2
                               for i in range(22050)))
    wav_path.write_bytes(buf.getvalue())
    res = build_mod(game, log=lambda *_: None)
    item = _items(res)["sound/GAME__RESOURCE.DAT@001E11.wav"]
    assert item.status == "warn" and "обрезан" in item.message
    orig, new = _orig(game, "RESOURCE.DAT"), _patched(game, "RESOURCE.DAT")
    assert len(new) == len(orig)
    voc_len = audio.voc_length(new, 0x1E11)
    assert voc_len == audio.voc_length(orig, 0x1E11)
    old_wav = audio.voc_to_wav(orig[0x1E11:0x1E11 + voc_len])
    new_wav = audio.voc_to_wav(new[0x1E11:0x1E11 + voc_len])
    assert len(new_wav) == len(old_wav) and new_wav != old_wav


def test_edit_text_file(game):
    t = game / "mod/text/GAME__README.TXT.txt"
    t.write_text("Welcome to the MODDED game!\nHave fun.\n", encoding="utf-8")
    build_mod(game, log=lambda *_: None)
    assert _patched(game, "README.TXT") == b"Welcome to the MODDED game!\r\nHave fun.\r\n"


def test_strings_translation_runs_in_dos(game):
    """Перевод строк в обычном и в упакованном EXE — и программы реально работают с новым текстом."""
    settings = game / "mod/settings.json"
    settings.write_text(json.dumps({"strings_encoding": "cp866"}), encoding="utf-8")
    for exe in ("GAME__GAME.EXE.tsv", "GAME__PACKED.EXE.tsv"):
        p = game / "mod/strings" / exe
        p.write_text(p.read_text(encoding="utf-8").replace("Hello from DOS$", "Привет из DOS"), encoding="utf-8")
    res = build_mod(game, log=lambda *_: None)
    assert not res.errors, res.items
    assert run_exe(_patched(game, "GAME.EXE"), "cp866") == "Привет из DOS!"
    assert run_exe(_patched(game, "PACKED.EXE"), "cp866") == "Привет из DOS!"
    assert len(_patched(game, "GAME.EXE")) == len(_orig(game, "GAME.EXE"))


def test_too_long_string(game):
    p = game / "mod/strings/GAME__GAME.EXE.tsv"
    p.write_text(p.read_text(encoding="utf-8").replace("Hello from DOS$", "This text is far too long$"),
                 encoding="utf-8")
    res = build_mod(game, log=lambda *_: None)
    assert res.errors and "длиннее исходной" in res.errors[0].message
    assert run_exe(_patched(game, "GAME.EXE")) == "Hello from DOS!"


def test_replace_raw_music(game):
    raw = game / "mod/raw/GAME__RESOURCE.DAT@00398A.mid"
    orig_len = len(raw.read_bytes())
    track = b"\x00\x90\x40\x40\x30\x80\x40\x00\x00\xff\x2f\x00"
    new_mid = b"MThd\x00\x00\x00\x06\x00\x00\x00\x01\x00\x60MTrk" + len(track).to_bytes(4, "big") + track
    assert len(new_mid) <= orig_len
    raw.write_bytes(new_mid)
    build_mod(game, log=lambda *_: None)
    new = _patched(game, "RESOURCE.DAT")
    assert new[0x398A:0x398A + len(new_mid)] == new_mid
    assert len(new) == len(_orig(game, "RESOURCE.DAT"))


def test_web_build_flow(tmp_path):
    from dosrip.web import create_app

    client = create_app(tmp_path, desktop=True).test_client()
    loc = client.post("/upload", data={"game": (io.BytesIO(game_zip()), "g.zip")},
                      content_type="multipart/form-data").headers["Location"]
    job = loc.split("/")[2]
    assert "Собрать игру с правками" in client.get(loc).get_data(as_text=True)
    t = tmp_path / job / "result/mod/text/GAME__README.TXT.txt"
    t.write_text("Changed!\n", encoding="utf-8")
    html = client.post(f"/job/{job}/build").get_data(as_text=True)
    assert "Игра собрана" in html and "README" in html
    dl = client.get(f"/job/{job}/download/patched")
    import zipfile
    z = zipfile.ZipFile(io.BytesIO(dl.data))
    assert z.read("GAME/README.TXT") == b"Changed!\r\n"
    assert client.post(f"/job/{job}/reveal/mod").status_code == 204
