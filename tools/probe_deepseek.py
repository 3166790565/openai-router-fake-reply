"""对上游真实请求 5 次“你是谁”类问题，输出回复与指纹，供填入项目预设。

用法:
    DS_BASE_URL=http://host:3000/ DS_API_KEY=sk-xxx python tools/probe_deepseek.py [model]

Key 只从环境变量读取，不写入任何文件。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

import httpx

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE_URL = os.environ.get("DS_BASE_URL", "").strip()
API_KEY = os.environ.get("DS_API_KEY", "").strip()
MODEL = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("DS_MODEL", "deepseek-v4.1-flash")
OUT = sys.argv[2] if len(sys.argv) > 2 else ""

PROMPTS = [
    "你是谁？",
    "请介绍一下你自己，你是谁开发的？",
    "你好，你是谁？你是什么模型？",
    "你是谁呀？",
    "你能告诉我你是谁、你的模型名称是什么吗？",
]


def build_url(base: str, path: str) -> str:
    base = base.rstrip("/")
    p = path.lstrip("/")
    if p.startswith("v1/"):
        p = p[3:]
    return f"{base}/{p}" if base.endswith("/v1") else f"{base}/v1/{p}"


async def main() -> int:
    if not BASE_URL or not API_KEY:
        print("缺少 DS_BASE_URL 或 DS_API_KEY", file=sys.stderr)
        return 2
    url = build_url(BASE_URL, "chat/completions")
    print(f"POST {url}\nmodel={MODEL}\n", file=sys.stderr)

    results = []
    async with httpx.AsyncClient(timeout=120) as client:
        for i, prompt in enumerate(PROMPTS, 1):
            body = {
                "model": MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
            }
            try:
                resp = await client.post(
                    url, json=body,
                    headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
                )
            except Exception as exc:
                print(f"[{i}] 请求失败: {exc}", file=sys.stderr)
                continue
            if resp.status_code >= 400:
                print(f"[{i}] HTTP {resp.status_code}: {resp.text[:300]}", file=sys.stderr)
                continue
            try:
                data = json.loads(resp.content.decode("utf-8"))
            except Exception:
                print(f"[{i}] 非 JSON: {resp.text[:300]}", file=sys.stderr)
                continue

            content = ""
            try:
                content = data["choices"][0]["message"]["content"]
            except Exception:
                pass
            headers = {k.lower(): v for k, v in resp.headers.items()}
            results.append({
                "prompt": prompt,
                "content": content,
                "id": data.get("id"),
                "model": data.get("model"),
                "object": data.get("object"),
                "created": data.get("created"),
                "system_fingerprint": data.get("system_fingerprint"),
                "service_tier": data.get("service_tier"),
                "usage": data.get("usage"),
                "headers": {
                    k: headers.get(k) for k in (
                        "x-request-id", "openai-processing-ms", "openai-version",
                        "openai-organization", "openai-project", "cf-ray",
                        "server", "x-envoy-upstream-service-time",
                    ) if headers.get(k)
                },
            })
            print(f"[{i}] {prompt}\n    -> {content[:300]}\n", file=sys.stderr)
            await asyncio.sleep(0.5)

    text = json.dumps(results, ensure_ascii=False, indent=2)
    if OUT:
        with open(OUT, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"written: {OUT}", file=sys.stderr)
    else:
        print(text)
    return 0 if results else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
