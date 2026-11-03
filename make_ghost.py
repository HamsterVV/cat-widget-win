"""Render an aligned 50/50 double-exposure of two frames for ImageGen to resolve."""
import sys, os, importlib.util
from pathlib import Path
from PIL import Image

sys.path.insert(0, os.getcwd())
_spec = importlib.util.spec_from_file_location("cw", "cat_widget.py")
cw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cw)

BACKDROP = (243, 14, 140)


def ghost(left: Path, right: Path, out: Path, canvas=(2560, 1080), subject_h=900) -> None:
    pair = cw._align_frames([Image.open(left).convert("RGBA"), Image.open(right).convert("RGBA")])
    w = max(f.width for f in pair)
    sheet = Image.new("RGBA", (w, pair[0].height), (0, 0, 0, 0))
    for f in pair:
        sheet.paste(f, ((w - f.width) // 2, 0), f)
    mid = Image.blend(
        Image.new("RGBA", sheet.size, (0, 0, 0, 0)), sheet, 0.5
    )
    image = Image.alpha_composite(
        Image.new("RGBA", sheet.size, (*BACKDROP, 255)), mid
    ).convert("RGB")
    scale = min(subject_h / image.height, (canvas[0] - 80) / image.width, (canvas[1] - 80) / image.height)
    image = image.resize((round(image.width * scale), round(image.height * scale)), Image.LANCZOS)
    board = Image.new("RGB", canvas, BACKDROP)
    board.paste(image, ((canvas[0] - image.width) // 2, (canvas[1] - image.height) // 2))
    board.save(out)
    print(f"{out.name} {image.width}x{image.height}")


if __name__ == "__main__":
    ghost(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))
