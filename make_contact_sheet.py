"""Temporary contact-sheet builder for the visual gate.

Renders a cycle at display height, aligned by cat_widget._align_frames, on a
mid-grey sheet with frame indices. Also reports translucency, since a
transparentkey window leaks any semi-opaque pixel onto the desktop.
"""
import sys
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cat_widget as cw

DISPLAY_H = 180
PAD = 8
LABEL = 18


def main(directory, out):
    paths = sorted(Path(directory).glob("*.png"))
    raw = [Image.open(p).convert("RGBA") for p in paths]

    # translucency audit on the raw (unscaled) frames
    print("translucency audit (raw frames):")
    worst = 0.0
    for p, img in zip(paths, raw):
        a = img.split()[3]
        hist = a.histogram()
        opaque = hist[255]
        semi = sum(hist[1:255])
        empty = hist[0]
        pct = semi / max(1, opaque + semi)
        worst = max(worst, pct)
        print(f"  {p.name:<14} opaque={opaque:>8}  semi={semi:>7} ({pct:.3%})  empty={empty}")

    aligned = cw._align_frames(list(raw))
    scale = DISPLAY_H / aligned[0].height
    tiles = [im.resize((max(1, round(im.width * scale)), DISPLAY_H), Image.LANCZOS)
             for im in aligned]

    cell_w = max(t.width for t in tiles)
    sheet_w = PAD + (cell_w + PAD) * len(tiles)
    sheet = Image.new("RGB", (sheet_w, PAD + LABEL + DISPLAY_H + PAD), (128, 128, 128))
    draw = ImageDraw.Draw(sheet)
    x = PAD
    ground_y = PAD + LABEL + DISPLAY_H - 2
    draw.line([(0, ground_y), (sheet_w, ground_y)], fill=(255, 255, 0), width=1)
    for i, tile in enumerate(tiles):
        draw.text((x, PAD), str(i + 1), fill=(255, 255, 255))
        bg = Image.new("RGBA", (cell_w, DISPLAY_H), (128, 128, 128, 255))
        bg.paste(tile, (0, 0), tile)
        sheet.paste(bg.convert("RGB"), (x, PAD + LABEL))
        x += cell_w + PAD

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out)
    print(f"\nworst semi-opaque share: {worst:.3%}")
    print(f"sheet {sheet.size} -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1], sys.argv[2]))
