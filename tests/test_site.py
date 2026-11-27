import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from bad_checker.web.server import Handler

ROOT = Path(__file__).parent.parent


@pytest.fixture(scope="module")
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def get(url):
    try:
        with urllib.request.urlopen(url) as r:
            return r.status, r.headers.get("Content-Type"), r.read()
    except urllib.error.HTTPError as e:
        return e.code, None, b""


@pytest.mark.parametrize("path,ctype", [
    ("/", "text/html"), ("/js/engine.js", "text/javascript"), ("/data/rules.json", "application/json"),
    ("/manifest.webmanifest", "application/manifest+json"), ("/icons/icon-192.png", "image/png"),
])
def test_static_files(server, path, ctype):
    status, ct, _ = get(server + path)
    assert status == 200 and ct.startswith(ctype)


@pytest.mark.parametrize("path", ["/../README.md", "/data/../analyzer.py", "/%2e%2e/README.md", "/nope.html"])
def test_no_path_traversal(server, path):
    req = urllib.request.Request(server + path)
    try:
        urllib.request.urlopen(req)
        raise AssertionError("должно быть 404")
    except urllib.error.HTTPError as e:
        assert e.code == 404


def test_api_analyze(server):
    body = json.dumps({"composition_text": "Цинк 10 мг", "units_per_day": 3}).encode()
    req = urllib.request.Request(server + "/api/analyze", data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        res = json.loads(r.read())
    assert res["nutrients"][0]["ratio_upper"] == 1.2


def test_build_site(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("build_site", ROOT / "tools" / "build_site.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    out = mod.build(tmp_path / "_site")
    for f in ["index.html", "js/engine.js", "js/app.js", "sw.js", "data/nutrients.json", "data/rules.json", ".nojekyll"]:
        assert (out / f).exists(), f


def test_service_worker_caches_existing_files():
    import re
    sw = (ROOT / "site" / "sw.js").read_text(encoding="utf-8")
    shell = re.search(r"SHELL = \[(.*?)\]", sw, re.S).group(1)
    for f in re.findall(r'"([^"]+)"', shell):
        if f in ("./",):
            continue
        base = ROOT / "bad_checker" if f.startswith("data/") else ROOT / "site"
        assert (base / f).exists(), f
