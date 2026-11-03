"""Find the best looping window inside an extracted frame sequence.

    python loop_window.py <frames_dir> [--min N] [--max M]

A generated clip is not a loop: the pose at the end rarely matches the start. This
scans every pair of frames whose distance is inside the plausible cycle range and
keeps the window whose first and last poses agree best, after aligning both by
body centroid so a drifting camera does not count as a pose change.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image


def silhouette(frame: Image.Image, width: int = 160) -> tuple[Image.Image, int]:
    """Boolean body mask at thumbnail size, plus its centroid x."""
    scale = width / frame.width
    small = frame.convert("RGB").resize((width, max(1, round(frame.height * scale))), Image.BILINEAR)
    pixels = small.load()
    mask = Image.new("L", small.size, 0)
    out = mask.load()
    total = weight = 0
    for y in range(small.height):
        for x in range(small.width):
            r, g, b = pixels[x, y]
            if not (g > r + 30 and g > b + 30):  # green backdrop is the only thing to drop
                out[x, y] = 255
                total += 1
                weight += x
    centroid = weight // total if total else small.width // 2
    return mask, centroid


def shifted(mask: Image.Image, dx: int) -> Image.Image:
    moved = Image.new("L", mask.size, 0)
    moved.paste(mask, (dx, 0))
    return moved


def mismatch(a: Image.Image, a_centroid: int, b: Image.Image, b_centroid: int) -> float:
    """Fraction of the union that only one pose covers, after centroid alignment."""
    b_shifted = shifted(b, a_centroid - b_centroid)
    union = 0
    overlap = 0
    pa, pb = a.load(), b_shifted.load()
    for y in range(a.height):
        for x in range(a.width):
            in_a = pa[x, y] > 128
            in_b = pb[x, y] > 128
            if in_a or in_b:
                union += 1
                if in_a and in_b:
                    overlap += 1
    return 1.0 - (overlap / union) if union else 1.0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("frames", type=Path)
    parser.add_argument("--min", type=int, default=10, help="shortest cycle in frames")
    parser.add_argument("--max", type=int, default=24, help="longest cycle in frames")
    parser.add_argument("--top", type=int, default=5, help="how many candidates to print")
    args = parser.parse_args(argv)

    files = sorted(args.frames.glob("*.png"))
    if len(files) < args.min + 1:
        print(f"{args.frames}: only {len(files)} frames", file=sys.stderr)
        return 1

    masks = [silhouette(Image.open(f)) for f in files]
    candidates: list[tuple[float, int, int]] = []
    for start in range(len(files)):
        for length in range(args.min, args.max + 1):
            end = start + length
            if end >= len(files):
                continue
            a, a_c = masks[start]
            b, b_c = masks[end]
            candidates.append((mismatch(a, a_c, b, b_c), start, length))

    candidates.sort()
    print(f"{len(files)} frames, {len(candidates)} candidate windows")
    for score, start, length in candidates[: args.top]:
        print(f"  start={start:2d} length={length:2d}  mismatch={score:.3%}  "
              f"({files[start].name} .. {files[start + length].name})")

    best_score, best_start, best_length = candidates[0]
    print(f"\nBEST start={best_start} length={best_length} frames "
          f"({best_length / 12:.2f}s at 12fps) mismatch={best_score:.3%}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))