"""Generate images through an Alibaba Cloud Token Plan endpoint using only the stdlib.

The embedded Python has no `openai` or `requests`, and installing packages into it
is off-limits, so this talks to the OpenAI-compatible gateway with urllib.

    python aliyun_image.py --models
    python aliyun_image.py --prompt "..." --out frame.png [--size 1024*1024] [--model ...]

The Token Plan host routes the DashScope-native service paths, not the
OpenAI-compatible `/images/generations` path, so the native protocol is used.
The API key is read from TOKEN_PLAN_API_KEY or, failing that, from
~/.aliyun-tokenplan.key. It is never printed or written to the output.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

KEY_FILE = Path.home() / ".aliyun-tokenplan.key"
DEFAULT_HOST = "https://token-plan.cn-beijing.maas.aliyuncs.com"


def api_key() -> str:
    key = os.environ.get("TOKEN_PLAN_API_KEY", "").strip()
    if not key and KEY_FILE.exists():
        key = KEY_FILE.read_text(encoding="utf-8").strip()
    if not key:
        raise SystemExit(
            f"没有找到密钥。请写入 {KEY_FILE}（只放一行密钥），或设置环境变量 TOKEN_PLAN_API_KEY。"
        )
    return key


def _request(url: str, payload: dict | None = None, timeout: int = 300,
             extra_headers: dict | None = None) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {
        "Authorization": f"Bearer {api_key()}",
        "Content-Type": "application/json",
    }
    headers.update(extra_headers or {})
    request = urllib.request.Request(
        url, data=data, headers=headers, method="POST" if data else "GET"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"HTTP {exc.code} {url}\n{body[:800]}")
    except urllib.error.URLError as exc:
        raise SystemExit(f"网络错误 {url}: {exc.reason}")


def list_models(base: str) -> list[str]:
    payload = _request(f"{base}/compatible-mode/v1/models")
    items = payload.get("data") or payload.get("models") or []
    return [item.get("id") or item.get("name") for item in items if isinstance(item, dict)]


def _first_url(payload: dict) -> str | None:
    """Pull an image URL out of any of the three shapes DashScope returns."""
    output = payload.get("output") or {}
    for result in output.get("results") or []:
        if result.get("url"):
            return result["url"]
    for choice in output.get("choices") or []:
        for part in (choice.get("message") or {}).get("content") or []:
            if isinstance(part, dict) and part.get("image"):
                return part["image"]
    for item in payload.get("data") or []:
        if item.get("url"):
            return item["url"]
    return None


def _save(url_or_b64: str, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    if url_or_b64.startswith("http"):
        with urllib.request.urlopen(url_or_b64, timeout=180) as response:
            out.write_bytes(response.read())
    else:
        out.write_bytes(base64.b64decode(url_or_b64))


def generate_native(host: str, model: str, prompt: str, size: str, out: Path,
                    negative: str = "", reference: Path | None = None,
                    poll_s: int = 5, max_wait_s: int = 600) -> None:
    """Call the DashScope-native image API that the Token Plan gateway actually exposes.

    The OpenAI-compatible `/images/generations` path is not routed on this host
    (it answers `url error` / 404), so the native service paths are used instead.
    Qwen-Image models take `multimodal-generation`; Wan models take `text2image`.
    """
    import time

    if model.startswith("qwen-image"):
        path = "/api/v1/services/aigc/multimodal-generation/generation"
        content: list[dict] = [{"text": prompt}]
        if reference is not None:
            content.append({"image": f"data:image/png;base64,"
                                     f"{base64.b64encode(reference.read_bytes()).decode('ascii')}"})
        body: dict = {
            "model": model,
            "input": {"messages": [{"role": "user", "content": content}]},
            "parameters": {"size": size, "n": 1, "watermark": False},
        }
    else:
        path = "/api/v1/services/aigc/text2image/image-synthesis"
        body = {
            "model": model,
            "input": {"prompt": prompt},
            "parameters": {"size": size, "n": 1, "watermark": False},
        }
        if negative:
            body["input"]["negative_prompt"] = negative

    # Token Plan answers "current user api does not support asynchronous calls"
    # when the async header is present, so this always asks for a sync response
    # and only polls if the gateway still hands back a task id.
    payload = _request(f"{host}{path}", body)
    url = _first_url(payload)
    task_id = (payload.get("output") or {}).get("task_id")
    if not url and task_id:
        waited = 0
        while waited < max_wait_s:
            time.sleep(poll_s)
            waited += poll_s
            status = _request(f"{host}/api/v1/tasks/{task_id}")
            output = status.get("output") or {}
            state = output.get("task_status")
            if state == "SUCCEEDED":
                url = _first_url(status)
                break
            if state in ("FAILED", "CANCELED", "UNKNOWN"):
                raise SystemExit(f"任务 {task_id} 失败: {json.dumps(status)[:600]}")
        if not url:
            raise SystemExit(f"任务 {task_id} 在 {max_wait_s}s 内未完成")
    if not url:
        raise SystemExit(f"响应里没有图片: {json.dumps(payload)[:600]}")
    _save(url, out)
    print(f"saved {out} ({out.stat().st_size} bytes)")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", action="store_true", help="列出该密钥可用的模型")
    parser.add_argument("--host", default=os.environ.get("TOKEN_PLAN_HOST", DEFAULT_HOST))
    parser.add_argument("--model", default="")
    parser.add_argument("--prompt", default="")
    parser.add_argument("--negative", default="")
    parser.add_argument("--size", default="1024*1024")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--reference", type=Path)
    args = parser.parse_args(argv)

    if args.models:
        for name in list_models(args.host):
            print(name)
        return 0
    if not args.prompt or not args.out:
        parser.error("生成图片需要 --prompt 和 --out")
    generate_native(args.host, args.model, args.prompt, args.size, args.out,
                    args.negative, args.reference)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))