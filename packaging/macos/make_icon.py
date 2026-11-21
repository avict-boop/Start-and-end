"""Рисует иконку приложения: пиксельная дискета на тёмном фоне. -> dosrip.icns и dosrip.png"""

import sys
from pathlib import Path

from PIL import Image, ImageDraw

S = 1024
out = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
out.mkdir(parents=True, exist_ok=True)

img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
# фон в форме macOS (скруглённый квадрат с отступом)
pad = 100
d.rounded_rectangle([pad, pad, S - pad, S - pad], radius=185, fill=(28, 52, 46))
d.rounded_rectangle([pad, pad, S - pad, S - pad], radius=185, outline=(60, 110, 96), width=8)

# дискета 3.5" из «пикселей» 32x32 на сетке 16x16
P = 32
ox, oy = (S - 16 * P) // 2, (S - 16 * P) // 2 + 10
grid = [
    "BBBBBBBBBBBBBBB.",
    "BBBSSSSSSSSSBBBB",
    "BBBSSSSSSHHSBBBB",
    "BBBSSSSSSHHSBBBB",
    "BBBSSSSSSHHSBBBB",
    "BBBSSSSSSSSSBBBB",
    "BBBBBBBBBBBBBBBB",
    "BBLLLLLLLLLLLLBB",
    "BBLWWWWWWWWWWLBB",
    "BBLLLLLLLLLLLLBB",
    "BBLWWWWWWWWWLLBB",
    "BBLLLLLLLLLLLLBB",
    "BBLWWWWWWLLLLLBB",
    "BBLLLLLLLLLLLLBB",
    "BBLLLLLLLLLLLLBB",
    "BBBBBBBBBBBBBBBB",
]
colors = {"B": (40, 40, 44), "S": (180, 186, 194), "H": (40, 40, 44), "L": (240, 236, 222), "W": (124, 196, 173)}
for y, row in enumerate(grid):
    for x, ch in enumerate(row):
        if ch in colors:
            d.rectangle([ox + x * P, oy + y * P, ox + (x + 1) * P - 1, oy + (y + 1) * P - 1], fill=colors[ch])

img.save(out / "dosrip.png")
img.save(out / "dosrip.icns", sizes=[(16, 16), (32, 32), (64, 64), (128, 128), (256, 256), (512, 512), (1024, 1024)])
print(out / "dosrip.icns")
