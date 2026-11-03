"""Submit an image-to-video job to Alibaba Cloud Model Studio and fetch the result.

    python aliyun_video.py --first-frame ref.png --prompt "..." --out walk.mp4

Video generation is asynchronous on every gateway: the request only creates a
task, and the mp4 has to be polled for. The Token Plan host does accept a base64
data URL for the first frame, which avoids needing a public upload somewhere just
to get a PoC running.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from aliyun_image import DEFAULT_HOST, api_key  # noqa: E402

SUBMIT_PATH = "/api/v1/services/aigc/video-generation/video-synthesis"


def _call(url: str, payload: dict | None = None, timeout: int = 120) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {
        "Authorization": f"Bearer {api_key()}",
        "Content-Type": "application/json",
    }
    # The async header belongs on task creation only; sending it on the polling
    # GET is rejected with "current user api does not support asynchronous calls".
    if data is not None:
        headers["X-DashScope-Async"] = "enable"
    request = urllib.request.Request(url, data=data, headers=headers,
                                     method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"HTTP {exc.code} {url}\n{body[:900]}")
    except urllib.error.URLError as exc:
        raise SystemExit(f"网络错误 {url}: {exc.reason}")


def submit(host: str, model: str, first_frame: Path, prompt: str,
           negative: str, duration: int, resolution: str, ratio: str) -> str:
    encoded = base64.b64encode(first_frame.read_bytes()).decode("ascii")
    payload = {
        "model": model,
        "input": {
            "prompt": prompt,
            "negative_prompt": negative,
            "media": [{"type": "first_frame",
                       "url": f"data:image/png;base64,{encoded}"}],
        },
        "resolution": resolution,
        "duration": duration,
        "ratio": ratio,
        "watermark": False,
    }
    reply = _call(f"{host}{SUBMIT_PATH}", payload)
    task_id = (reply.get("output") or {}).get("task_id")
    if not task_id:
        raise SystemExit(f"没有拿到 task_id: {json.dumps(reply)[:900]}")
    print(f"task_id={task_id}")
    return task_id


def wait(host: str, task_id: str, poll_s: int, max_wait_s: int) -> str:
    waited = 0
    while waited < max_wait_s:
        time.sleep(poll_s)
        waited += poll_s
        status = _call(f"{host}/api/v1/tasks/{task_id}", timeout=60)
        output = status.get("output") or {}
        state = output.get("task_status")
        print(f"  {waited:>4}s {state}")
        if state == "SUCCEEDED":
            video = output.get("video_url") or output.get("video")
            if not video:
                raise SystemExit(f"任务成功但没有视频地址: {json.dumps(status)[:900]}")
            return video
        if state in ("FAILED", "CANCELED", "UNKNOWN"):
            raise SystemExit(f"任务失败: {json.dumps(status)[:900]}")
    raise SystemExit(f"{max_wait_s}s 内未完成")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--first-frame", type=Path)
    parser.add_argument("--prompt")
    parser.add_argument("--negative", default="")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--task-id", default=None,
                        help="skip submission and fetch this task instead")
    parser.add_argument("--model", default="wan3.0-video")
    parser.add_argument("--duration", type=int, default=4)
    parser.add_argument("--resolution", default="720P")
    parser.add_argument("--ratio", default="16:9")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--poll", type=int, default=10)
    parser.add_argument("--max-wait", type=int, default=900)
    parser.add_argument("--submit-only", action="store_true")
    args = parser.parse_args(argv)

    if args.task_id:
        task_id = args.task_id
    else:
        if not args.first_frame or not args.prompt:
            parser.error("--first-frame and --prompt are required unless --task-id is given")
        task_id = submit(args.host, args.model, args.first_frame, args.prompt,
                         args.negative, args.duration, args.resolution, args.ratio)
    if args.submit_only:
        return 0
    url = wait(args.host, task_id, args.poll, args.max_wait)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=300) as response:
        args.out.write_bytes(response.read())
    print(f"saved {args.out} ({args.out.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))