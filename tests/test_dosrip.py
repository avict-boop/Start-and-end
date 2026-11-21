import io
import json
import wave
import zipfile

import pytest

from dosrip.carve import carve
from dosrip.exe.disasm import Disassembly
from dosrip.exe.mz import parse_mz
from dosrip.exe.unpack import unpack
from dosrip.formats import audio, images
from dosrip.pipeline import UnsafeArchive, process_zip

from fixtures import PROGRAM, PROGRAM_RELOCS, game_zip, gif_bytes, midi_bytes, original_exe, packed_exe, pcx_bytes, \
    voc_bytes, xmi_bytes


def test_parse_mz():
    mz = parse_mz(original_exe())
    assert mz.image == PROGRAM
    assert mz.reloc_linear() == PROGRAM_RELOCS
    assert (mz.cs, mz.ip) == (0, 0)


def test_unpack_restores_original_program():
    r = unpack(packed_exe())
    assert r is not None and r.oep == (0, 0)
    mz = parse_mz(r.exe)
    assert mz.reloc_linear() == PROGRAM_RELOCS
    assert mz.image == PROGRAM[: len(mz.image)]
    assert not any(PROGRAM[len(mz.image):])  # отрезаны только хвостовые нули
    assert (mz.ss, mz.sp) == (0x12, 0x100)


def test_unpack_refuses_plain_program():
    assert unpack(original_exe()) is None


def test_disassembly_follows_far_calls():
    mz = parse_mz(original_exe())
    dis = Disassembly(mz.image, mz.cs, mz.ip, mz.reloc_linear())
    assert (0, 0x16) in dis.functions
    text = dis.listing()
    assert "lcall   sub_0000_0016" in text
    assert "вывод строки $" in text and "выход из программы" in text
    assert "релокация" in text


def test_carve_finds_everything_in_archive():
    junk = bytes(range(256)) * 4
    data = junk + pcx_bytes() + junk + voc_bytes() + junk + midi_bytes() + gif_bytes() + xmi_bytes()
    fmts = [h.fmt for h in carve(data)]
    assert fmts == ["pcx", "voc", "midi", "gif", "xmi"]


def test_pcx_roundtrip():
    img, n = images.decode_pcx(pcx_bytes())
    assert img.size == (32, 20) and n == len(pcx_bytes())
    assert img.getpixel((3, 2)) == (3 * 8 + 2) % 256


def test_voc_to_wav():
    w = wave.open(io.BytesIO(audio.voc_to_wav(voc_bytes(4000))))
    assert w.getnframes() == 4000 and abs(w.getframerate() - 11025) < 300


def test_xmi_to_midi_note_offs():
    (mid,) = audio.xmi_to_midis(xmi_bytes())
    assert mid.startswith(b"MThd")
    track = mid[22:]
    assert track.count(b"\x80\x3c\x00") == 1 and track.count(b"\x80\x40\x00") == 1


def test_full_pipeline(tmp_path):
    z = tmp_path / "game.zip"
    z.write_bytes(game_zip())
    rep = process_zip(z, tmp_path / "out", log=lambda *_: None)
    kinds = {k: len(rep.outputs(k)) for k in ("image", "sound", "music", "palette", "text")}
    assert kinds == {"image": 3, "sound": 1, "music": 2, "palette": 1, "text": 1}
    packed = next(f for f in rep.files if f.path.endswith("PACKED.EXE"))
    assert packed.exe["распаковано"].startswith("да")
    assert (tmp_path / "out/code/GAME__PACKED.EXE/unpacked.exe").exists()
    assert "sub_0000_0016" in (tmp_path / "out/code/GAME__PACKED.EXE/listing.asm").read_text()
    assert json.loads((tmp_path / "out/report.json").read_text())["files"]
    assert "Графика" in (tmp_path / "out/report.html").read_text()


def test_rejects_path_traversal(tmp_path):
    z = tmp_path / "evil.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("../escape.txt", "x")
    with pytest.raises(UnsafeArchive):
        process_zip(z, tmp_path / "out", log=lambda *_: None)


def test_web_upload(tmp_path):
    from dosrip.web import create_app

    client = create_app(tmp_path).test_client()
    assert "Перетащите сюда zip" in client.get("/").get_data(as_text=True)
    resp = client.post("/upload", data={"game": (io.BytesIO(game_zip()), "mygame.zip")},
                       content_type="multipart/form-data")
    assert resp.status_code == 302
    report = client.get(resp.headers["Location"])
    html = report.get_data(as_text=True)
    assert "mygame" in html and "Скачать всё одним zip" in html
    job = resp.headers["Location"].split("/")[2]
    dl = client.get(f"/job/{job}/download")
    assert zipfile.ZipFile(io.BytesIO(dl.data)).namelist()


def test_desktop_mode(tmp_path):
    from dosrip.web import create_app

    client = create_app(tmp_path, desktop=True).test_client()
    resp = client.post("/upload", data={"game": (io.BytesIO(game_zip()), "My Game.zip")},
                       content_type="multipart/form-data")
    assert resp.status_code == 302
    job = resp.headers["Location"].split("/")[2]
    assert job.startswith("My_Game-")
    assert (tmp_path / job / "result" / "report.html").exists()
    assert not (tmp_path / job / "game.zip").exists()  # загруженный zip не хранится
    html = client.get(resp.headers["Location"]).get_data(as_text=True)
    assert "Показать в Finder" in html
    assert client.post(f"/job/{job}/reveal").status_code == 204
    assert job in client.get("/").get_data(as_text=True)  # в списке «Недавние»
    assert client.get("/job/..%2F..%2Fetc/report.html").status_code == 404


def test_desktop_selftest():
    from dosrip.desktop import selftest

    assert selftest() == 0
