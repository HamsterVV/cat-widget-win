"""Measure candidate sprite frames against a reference set.

    python check_frames.py <candidate_dir> [--reference <dir_or_png>] [--json]

Prints one row per frame, then a verdict. A candidate passes when its body
proportions land inside the reference spread and its keyed edges carry no
backdrop colour, so an overnight generation run can reject its own bad frames
instead of trusting a visual impression.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageFilter

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ingest_frames as ingest  # noqa: E402


def stats(image: Image.Image) -> dict:
    """Body geometry and fur colour of one RGBA frame."""
    alpha = image.split()[3]
    pixels = image.convert("RGB").load()
    ap = alpha.load()
    xs: list[int] = []
    ys: list[int] = []
    total = [0, 0, 0]
    count = 0
    for y in range(image.height):
        for x in range(image.width):
            if ap[x, y] > 200:
                xs.append(x)
                ys.append(y)
                r, g, b = pixels[x, y]
                total[0] += r
                total[1] += g
                total[2] += b
                count += 1
    if not xs:
        return {"empty": True}
    left, right, top, bottom = min(xs), max(xs), min(ys), max(ys)
    width = right - left + 1
    height = bottom - top + 1
    depths = []
    for x in range(left + int(width * 0.30), left + int(width * 0.70)):
        column = [y for y in range(top, bottom + 1) if ap[x, y] > 200]
        if column:
            depths.append(max(column) - min(column) + 1)
    return {
        "aspect": round(width / height, 3),
        "torso_depth": round(sum(depths) / len(depths)) if depths else 0,
        "fill": round(count / (width * height), 3),
        "mean": [round(v / count) for v in total],
    }


def partial_alpha(image: Image.Image) -> float:
    """Share of visible pixels that are not fully opaque.

    The window keys on an exact colour, so any pixel below full opacity blends
    the desktop through the sprite instead of antialiasing.
    """
    hist = image.split()[3].histogram()
    visible = sum(hist[1:])
    return sum(hist[1:255]) / visible if visible else 0.0


def detached_haze(image: Image.Image, halo_px: int = 4) -> float:
    """Share of pixels that are translucent *and* nowhere near the body.

    Edge anti-aliasing is legitimately partial but always hugs the silhouette,
    so it cannot be the signal. A backdrop left over by a keying threshold that
    was too high forms a translucent field across the whole frame, and that is
    what this catches: measured over the 9 known-good production frames it is
    0.00%, over the rejected staging batch 24-42%.
    """
    alpha = image.split()[3]
    hist = alpha.histogram()
    visible = sum(hist[1:])
    if not visible:
        return 0.0
    opaque = alpha.point(lambda v: 255 if v == 255 else 0)
    near_body = opaque.filter(ImageFilter.MaxFilter(2 * halo_px + 1))
    translucent = alpha.point(lambda v: 255 if 0 < v < 255 else 0)
    ring = ImageChops.subtract(translucent, near_body)
    return sum(ring.histogram()[1:]) / visible


def rim_magenta(image: Image.Image) -> float:
    """Share of edge pixels still tinted with a magenta backdrop."""
    alpha = image.split()[3]
    pixels = image.convert("RGB").load()
    ring = ImageChops.subtract(alpha.filter(ImageFilter.MaxFilter(3)), alpha)
    rp = ring.load()
    magenta = total = 0
    for y in range(image.height):
        for x in range(image.width):
            if rp[x, y] > 128:
                total += 1
                r, g, b = pixels[x, y]
                if r > 150 and g < r - 40 and b > g + 20:
                    magenta += 1
    return magenta / total if total else 0.0


def _spread(values: list[float]) -> tuple[float, float]:
    low, high = min(values), max(values)
    pad = max((high - low) * 0.35, high * 0.12)
    return low - pad, high + pad


def load_frames(directory: Path) -> list[Image.Image]:
    return [Image.open(f).convert("RGBA") for f in sorted(directory.glob("*.png"))]


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--reference", type=Path, default=None)
    parser.add_argument("--max-rim", type=float, default=0.05)
    parser.add_argument("--max-partial", type=float, default=0.25,
                        help="largest overall share of pixels allowed below full opacity")
    parser.add_argument("--max-haze", type=float, default=0.01,
                        help="largest share of translucent pixels sitting away from the body")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    frames = load_frames(args.candidate)
    if not frames:
        print(f"{args.candidate}: no PNG frames", file=sys.stderr)
        raise SystemExit(2)

    bands: dict[str, tuple[float, float]] = {}
    if args.reference:
        references = load_frames(args.reference) if args.reference.is_dir() else [
            Image.open(args.reference).convert("RGBA")
        ]
        reference_stats = [stats(f) for f in references]
        for key in ("aspect", "torso_depth", "fill"):
            bands[key] = _spread([r[key] for r in reference_stats if not r.get("empty")])

    rows = []
    for frame, path in zip(frames, sorted(args.candidate.glob("*.png"))):
        row = stats(frame)
        row["file"] = path.name
        if row.get("empty"):
            row["verdict"] = "FAIL empty"
        else:
            row["rim"] = round(rim_magenta(frame), 4)
            row["partial"] = round(partial_alpha(frame), 4)
            row["haze"] = round(detached_haze(frame), 4)
            problems = []
            if row["rim"] > args.max_rim:
                problems.append(f"rim {row['rim']:.0%}")
            if row["haze"] > args.max_haze:
                problems.append(f"detached haze {row['haze']:.1%} > {args.max_haze:.0%}")
            if row["partial"] > args.max_partial:
                problems.append(f"partial alpha {row['partial']:.1%} > {args.max_partial:.0%}")
            for key, (low, high) in bands.items():
                if not low <= row[key] <= high:
                    problems.append(f"{key} {row[key]} outside [{low:.2f},{high:.2f}]")
            row["verdict"] = "PASS" if not problems else "FAIL " + "; ".join(problems)
        rows.append(row)

    if args.json:
        print(json.dumps({"bands": {k: list(v) for k, v in bands.items()}, "frames": rows}, indent=2))
    else:
        for row in rows:
            print(f"{row['file']:<18} {row['verdict']}")
        passed = sum(1 for r in rows if r["verdict"] == "PASS")
        print(f"{passed}/{len(rows)} pass")
    raise SystemExit(0 if all(r["verdict"] == "PASS" for r in rows) else 1)


if __name__ == "__main__":
    main(sys.argv[1:])
