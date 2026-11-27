"""Собрать статический сайт для GitHub Pages (или любого хостинга) в папку _site/.

    python tools/build_site.py [папка_назначения]

Копирует site/ и добавляет справочники из bad_checker/data/ — единственного источника данных.
"""
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def build(dest: Path) -> Path:
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(ROOT / "site", dest)
    (dest / "data").mkdir(exist_ok=True)
    for f in (ROOT / "bad_checker" / "data").glob("*.json"):
        shutil.copy2(f, dest / "data" / f.name)
    (dest / ".nojekyll").touch()
    return dest


if __name__ == "__main__":
    out = build(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "_site")
    print(f"Сайт собран: {out}")
