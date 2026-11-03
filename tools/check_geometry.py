"""Temporary geometry gate: adjacent-frame centroid jitter and torso-depth delta.

Uses check_frames.stats() so torso_depth means exactly what the quantitative
gate means, and cat_widget._align_frames for the centroid anchor.
"""
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cat_widget as cw
import check_frames as cf

MAX_CENTROID_PX = 6.0
MAX_DEPTH_DELTA = 0.12


def main(directory):
    paths = sorted(Path(directory).glob("*.png"))
    raw = [Image.open(p).convert("RGBA") for p in paths]
    aligned = cw._align_frames(list(raw))

    print(f"dir={directory}  frames={len(paths)}  aligned_size={aligned[0].size}")
    print(f"{'frame':<14}{'centroid':>10}{'d_cent':>9}{'depth':>8}{'d_depth%':>10}"
          f"{'ground_y':>10}{'back_top':>10}{'fill':>7}")
    prev = None
    fails = []
    rows = []
    for p, img in zip(paths, aligned):
        mask = cw._alpha_mask(img)
        box = mask.getbbox()
        c = cw._torso_centroid(mask, box)
        st = cf.stats(img)
        d = st["torso_depth"]
        # ground line = lowest opaque row of the central 30-70% column band;
        # a jump here is a visible teleport at the paws.
        # NB PIL getbbox() right/lower are EXCLUSIVE.
        px = mask.load()
        cols = range(box[0] + int((box[2]-box[0])*0.30), box[0] + int((box[2]-box[0])*0.70))
        bottoms = []
        for x in cols:
            col = [y for y in range(box[1], box[3]) if px[x, y]]
            if col:
                bottoms.append(max(col))
        ground = max(bottoms) if bottoms else 0
        back_top = box[1]
        dc = dd = float("nan")
        if prev is not None:
            dc = abs(c - prev[0])
            dd = abs(d - prev[1]) / prev[1]
            if dc > MAX_CENTROID_PX:
                fails.append(f"{p.name}: centroid jitter {dc:.1f}px > {MAX_CENTROID_PX}")
            if dd > MAX_DEPTH_DELTA:
                fails.append(f"{p.name}: torso_depth delta {dd:.1%} > {MAX_DEPTH_DELTA:.0%}")
        print(f"{p.name:<14}{c:>10.1f}{dc:>9.1f}{d:>8}{dd*100 if prev is not None else 0:>10.1f}"
              f"{ground:>10}{back_top:>10}{st['fill']:>7}")
        rows.append({"file": p.name, "centroid": c, "torso_depth": d,
                     "ground": ground, "back_top": back_top, "fill": st["fill"]})
        prev = (c, d)

    d_cent = [r["centroid"] - rows[0]["centroid"] for r in rows]
    print("\nalignment shift vs frame 1 (px):", [f"{v:+.1f}" for v in d_cent])
    print("ground-line spread (px):", f"{max(r['ground'] for r in rows) - min(r['ground'] for r in rows):.0f}")
    print("back-top spread (px):", f"{max(r['back_top'] for r in rows) - min(r['back_top'] for r in rows):.0f}")
    if fails:
        print("\nGEOMETRY FAIL")
        for f in fails:
            print("  -", f)
        return 1
    print("\nGEOMETRY PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
