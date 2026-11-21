"""Настольное приложение: окно macOS (WebKit через pywebview) с локальным сервером внутри."""

import io
import socket
import sys
import threading
import zipfile
from pathlib import Path

from werkzeug.serving import make_server

from . import __version__
from .web import create_app

APP_NAME = "dosrip"


def results_dir() -> Path:
    return Path.home() / "Documents" / APP_NAME


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def selftest() -> int:
    """Проверка собранного приложения без окна: все нативные библиотеки на месте и конвейер работает."""
    import tempfile

    from capstone import CS_ARCH_X86, CS_MODE_16, Cs
    from PIL import Image
    from unicorn import UC_ARCH_X86, UC_MODE_16, Uc

    from .exe.mz import build_mz
    from .exe.unpack import unpack
    from .pipeline import process_zip

    Uc(UC_ARCH_X86, UC_MODE_16)
    assert next(Cs(CS_ARCH_X86, CS_MODE_16).disasm(b"\xcd\x21", 0)).mnemonic == "int"
    prog = bytes.fromhex("b8004ccd21")  # mov ax,4C00h; int 21h
    exe = build_mz(prog, [], 0, 0, 0x10, 0x100, minalloc=0x20)
    assert unpack(exe) is None  # обычная программа — распаковывать нечего, эмулятор отработал
    buf = io.BytesIO()
    Image.new("P", (8, 8)).save(buf, "PCX")
    z = io.BytesIO()
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("GAME/PIC.PCX", buf.getvalue())
        zf.writestr("GAME/GAME.EXE", exe)
    with tempfile.TemporaryDirectory() as td:
        zp = Path(td) / "t.zip"
        zp.write_bytes(z.getvalue())
        rep = process_zip(zp, Path(td) / "out", log=lambda *_: None)
        assert len(rep.outputs("image")) == 1, rep
        assert (Path(td) / "out/code/GAME__GAME.EXE/listing.asm").exists()
    print(f"dosrip {__version__} selftest OK")
    return 0


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()
    import webview  # pywebview: на macOS использует системный WKWebView

    app = create_app(results_dir(), desktop=True)
    port = _free_port()
    server = make_server("127.0.0.1", port, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    webview.create_window(APP_NAME, f"http://127.0.0.1:{port}/", width=1100, height=820, min_size=(640, 480))
    webview.start()
    server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
