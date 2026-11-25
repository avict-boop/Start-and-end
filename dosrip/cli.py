import argparse
import sys
from pathlib import Path


def main(argv=None):
    ap = argparse.ArgumentParser(prog="dosrip", description="Восстановление графики, звука, музыки и кода DOS-игры из zip-архива.")
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("run", help="обработать zip с игрой")
    r.add_argument("zip", help="zip-архив с файлами игры")
    r.add_argument("-o", "--out", help="папка результата (по умолчанию <имя>_restored)")
    r.add_argument("--zip-result", action="store_true", help="дополнительно упаковать результат в zip")
    b = sub.add_parser("build", help="собрать игру с правками из папки mod/")
    b.add_argument("result", help="папка результата dosrip run")
    b.add_argument("--zip-result", action="store_true", help="упаковать собранную игру в zip")
    s = sub.add_parser("serve", help="веб-интерфейс: загрузить zip через браузер")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    args = ap.parse_args(argv)

    if args.cmd == "run":
        from .pipeline import UnsafeArchive, process_zip, zip_dir

        out = Path(args.out or Path(args.zip).stem + "_restored")
        try:
            rep = process_zip(args.zip, out)
        except (UnsafeArchive, OSError, ValueError) as e:
            print(f"Ошибка: {e}", file=sys.stderr)
            return 1
        if args.zip_result:
            zip_dir(out, out.with_suffix(".zip"))
        o = rep.outputs
        print(f"\nГотово: {len(o('image'))} изображений, {len(o('animation'))} анимаций, {len(o('sound'))} звуков, "
              f"{len(o('music'))} музыки, {len([f for f in rep.files if f.exe])} программ.")
        print(f"Отчёт: {out / 'report.html'}")
        return 0
    if args.cmd == "build":
        from .mod import build_mod
        from .pipeline import zip_dir

        try:
            res = build_mod(args.result)
        except (OSError, ValueError) as e:
            print(f"Ошибка: {e}", file=sys.stderr)
            return 1
        for it in res.items:
            mark = {"ok": "✓", "warn": "!", "error": "✗"}[it.status]
            print(f" {mark} {it.file}: {it.message}")
        print(f"\nБез изменений: {res.unchanged}. Ошибок: {len(res.errors)}.")
        print(f"Игра собрана: {res.patched_dir}")
        if res.main_exe:
            print(f"Запуск: dosbox -conf \"{res.dosbox_conf}\"")
        if args.zip_result:
            zip_dir(Path(res.patched_dir), Path(res.patched_dir).with_suffix(".zip"))
        return 1 if res.errors else 0
    if args.cmd == "serve":
        from .web import create_app

        create_app().run(host=args.host, port=args.port)
        return 0
    ap.print_help()
    return 0
