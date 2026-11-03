"""Create a restorable snapshot of the working pet version.

Snapshot contains code, config, runtime frames and logs; it deliberately leaves
out python-embed (stock embedded CPython), raw/staging footage, and scratch
screenshots.

    python snapshot.py v1.0-walk-petwin "walk + layered window verified"

Restore = unzip over the project root (python-embed stays as-is).
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent   # project root
OUT_DIR = ROOT / "versions"

SKIP_DIRS = {"__pycache__", "python-embed", "versions"}
SKIP_UNDER = {
    "assets/frames_raw",
    "assets/frames_staging",
    "assets/frames_unused",
}
ROOT_GLOBS = ("*.py", "*.json", "*.bat")
INCLUDE_DIRS = ("pet", "assets/frames", "tools", "logs")
EXTRA_DOCS = {"assets/frames_staging/review/REPORT.md": "docs/REPORT.md"}


def wanted(path: Path) -> bool:
    rel = path.relative_to(ROOT).as_posix()
    if path.suffix.lower() in {".png", ".mp4"} and "/" not in rel:
        return False
    if path.is_dir():
        if path.name in SKIP_DIRS:
            return False
        return not any(rel == d or rel.startswith(d + "/") for d in SKIP_UNDER)
    return True


def collect() -> list[tuple[Path, str]]:
    items: list[tuple[Path, str]] = []
    for pattern in ROOT_GLOBS:
        for path in sorted(ROOT.glob(pattern)):
            if path.is_file():
                items.append((path, path.name))
    for name in INCLUDE_DIRS:
        base = ROOT / name
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_file() and wanted(path):
                items.append((path, path.relative_to(ROOT).as_posix()))
    for src, arc in EXTRA_DOCS.items():
        path = ROOT / src
        if path.is_file():
            items.append((path, arc))
    return items


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    label = sys.argv[1] if len(sys.argv) > 1 else "snapshot"
    note = sys.argv[2] if len(sys.argv) > 2 else ""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = f"{label}-{stamp}"
    OUT_DIR.mkdir(exist_ok=True)
    archive = OUT_DIR / f"{name}.zip"

    items = collect()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        for path, arc in items:
            bundle.write(path, arc)

    with zipfile.ZipFile(archive) as bundle:
        bad = bundle.testzip()
        entries = len(bundle.namelist())
    if bad:
        print(f"FAIL integrity check at {bad}")
        return 1

    key_files = [p for p in ("cat_widget.py", "pet/app.py", "pet/window.py", "actions.json", "settings.json")]
    lines = [
        f"# {name}",
        "",
        f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 说明：{note or '（无）'}",
        f"- 归档：`versions/{archive.name}`（{archive.stat().st_size / 1048576:.1f} MB，{entries} 个文件，CRC 校验通过）",
        "- 包含：根目录 *.py / *.json / *.bat、assets/frames、assets/frames_backup、tools、logs、docs/REPORT.md",
        "- 不含：python-embed（原版嵌入式 CPython，未改动）、frames_raw、frames_staging、frames_unused、临时截图",
        "",
        "## 关键文件 SHA-256",
        "",
        "| 文件 | SHA-256 |",
        "| --- | --- |",
    ]
    for name_ in key_files:
        path = ROOT / name_
        lines.append(f"| {name_} | `{sha256(path)}` |")
    lines += [
        "",
        "## 恢复",
        "",
        "1. 退出正在运行的宠物（任务管理器结束 `pythonw.exe`）。",
        f"2. 将 `versions/{archive.name}` 解压覆盖到项目根目录。",
        "3. `python-embed` 不要动；若丢失，重新解压 Python 3.12 嵌入式包并补回 Pillow、Tk。",
        "4. 双击 `start.bat`，`logs/cat.log` 应出现 `layered pet window at 1.00x scaling`。",
    ]
    manifest = OUT_DIR / f"{name}.md"
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(json.dumps({
        "archive": str(archive),
        "manifest": str(manifest),
        "files": entries,
        "mb": round(archive.stat().st_size / 1048576, 1),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())