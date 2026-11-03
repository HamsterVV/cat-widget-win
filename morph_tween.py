"""Deterministic in-between frames for an existing sprite sequence.

    python morph_tween.py <out_dir> <frame_a> <frame_b> [<frame_b> ...] [--steps N]

ImageGen cannot be told where a limb should sit: measured against the true
midpoint it overshoots body extension by 10-19% and snaps every pose toward one
canonical gallop. These tweens are computed instead, so the geometry lands
exactly halfway. The silhouette of a tween is the union of its parents, which
keeps it fully opaque (the widget window keys a single colour and cannot show
translucency) while every pixel both parents cover takes their average colour.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

from PIL import Image

MIN_ALPHA = 128  # below this a pixel is backdrop for the widget's colour key


def _load_frames(path: Path) -> list[Image.Image]:
    files = sorted(path.glob("*.png")) if path.is_dir() else [path]
    return [Image.open(f).convert("RGBA") for f in files]


def _pairwise_aligned(frames: list[Image.Image]) -> list[Image.Image]:
    """Register a whole cycle at once so every tween shares one body anchor."""
    align = importlib.util.spec_from_file_location("cw", Path(__file__).with_name("cat_widget.py"))
    module = importlib.util.module_from_spec(align)
    align.loader.exec_module(module)
    return module._align_frames(frames)


def _canvas(frames: list[Image.Image]) -> tuple[int, int]:
    return max(f.width for f in frames), max(f.height for f in frames)


def _stamp(frame: Image.Image, size: tuple[int, int]) -> Image.Image:
    sheet = Image.new("RGBA", size, (0, 0, 0, 0))
    sheet.paste(frame, ((size[0] - frame.width) // 2, size[1] - frame.height), frame)
    return sheet


def tween(a: Image.Image, b: Image.Image, t: float) -> Image.Image:
    """Blend two equally sized RGBA frames, keeping the result fully opaque."""
    size = (a.width, a.height)
    pa, pb = list(a.getdata()), list(b.getdata())
    out = []
    for (r1, g1, b1, a1), (r2, g2, b2, a2) in zip(pa, pb):
        if a1 < MIN_ALPHA and a2 < MIN_ALPHA:
            out.append((0, 0, 0, 0))
            continue
        w1 = (1.0 - t) * (a1 if a1 >= MIN_ALPHA else 0)
        w2 = t * (a2 if a2 >= MIN_ALPHA else 0)
        total = w1 + w2
        if total <= 0:  # one parent covers this pixel only faintly
            out.append((r1, g1, b1, 255) if a1 >= MIN_ALPHA else (r2, g2, b2, 255))
            continue
        out.append((
            round((r1 * w1 + r2 * w2) / total),
            round((g1 * w1 + g2 * w2) / total),
            round((b1 * w1 + b2 * w2) / total),
            255 if max(a1, a2) >= 220 else max(a1, a2),
        ))
    image = Image.new("RGBA", size)
    image.putdata(out)
    return image


def build(frames: list[Image.Image], steps: int) -> list[Image.Image]:
    """Interleave `steps` tweens after every frame of a looping cycle."""
    aligned = _pairwise_aligned(frames)
    size = _canvas(aligned)
    stamps = [_stamp(f, size) for f in aligned]
    result: list[Image.Image] = []
    for index, frame in enumerate(stamps):
        result.append(frame)
        nxt = stamps[(index + 1) % len(stamps)]
        for step in range(1, steps + 1):
            result.append(tween(frame, nxt, step / (steps + 1)))
    return result


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out_dir", type=Path)
    parser.add_argument("frames", type=Path, help="directory of source frames")
    parser.add_argument("--steps", type=int, default=1, help="tweens inserted after each frame")
    parser.add_argument("--prefix", default="f")
    parser.add_argument("--mirror-to", type=Path, default=None)
    args = parser.parse_args(argv)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    if args.mirror_to:
        args.mirror_to.mkdir(parents=True, exist_ok=True)
    sources = _load_frames(args.frames)
    cycle = build(sources, args.steps)
    for number, frame in enumerate(cycle, 1):
        name = f"{args.prefix}_{number:02d}.png"
        frame.crop(frame.getbbox() or (0, 0, frame.width, frame.height)).save(args.out_dir / name)
        if args.mirror_to:
            flipped = frame.transpose(Image.FLIP_LEFT_RIGHT)
            flipped.crop(flipped.getbbox() or (0, 0, flipped.width, flipped.height)).save(
                args.mirror_to / name
            )
    print(f"{len(sources)} source frames -> {len(cycle)} frames in {args.out_dir}")


if __name__ == "__main__":
    main(sys.argv[1:])
