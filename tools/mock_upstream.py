"""本地验证用的假上游：回显收到的 model，返回 OpenAI 风格 JSON / SSE。

用法: python tools/mock_upstream.py [port]
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
import uuid

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 9000


def _headers() -> dict:
    return {
        "x-request-id": "req_" + uuid.uuid4().hex[:24],
        "openai-processing-ms": "123",
        "openai-version": "2020-10-01",
        "x-upstream-model": "MOCK",
    }


@app.get("/v1/models")
@app.get("/models")
async def models():
    """回显一个模型列表，用于测试 /models 中转。"""
    return JSONResponse(
        {
            "object": "list",
            "data": [
                {"id": "deepseek-chat", "object": "model", "created": 1710000000, "owned_by": "deepseek"},
                {"id": "deepseek-v4.1-flash", "object": "model", "created": 1720000000, "owned_by": "deepseek"},
                {"id": "glm-5.3", "object": "model", "created": 1730000000, "owned_by": "zhipu"},
            ],
        },
        headers=_headers(),
    )


@app.post("/v1/chat/completions")
@app.post("/chat/completions")
async def chat(request: Request):
    body = await request.json()
    model = body.get("model", "unknown")
    print(f"[mock] model={model} stream={body.get('stream')} "
          f"auth={request.headers.get('authorization')}", flush=True)
    headers = _headers()
    headers["x-upstream-model"] = model
    created = int(time.time())
    cid = "chatcmpl-mock" + uuid.uuid4().hex[:12]

    if not body.get("stream"):
        return JSONResponse(
            {
                "id": cid,
                "object": "chat.completion",
                "created": created,
                "model": model,
                "system_fingerprint": "fp_mock_12345",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": f"上游真实回复，model={model}"},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 5, "completion_tokens": 8, "total_tokens": 13},
            },
            headers=headers,
        )

    async def gen():
        # 故意把事件拆到多个分片，验证代理的行缓冲
        chunks = []
        base = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model}
        chunks.append(json.dumps({**base, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]}))
        for word in ["上游", "流式", "回复"]:
            chunks.append(json.dumps({**base, "choices": [{"index": 0, "delta": {"content": word}, "finish_reason": None}]}))
        chunks.append(json.dumps({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}))

        for c in chunks:
            payload = ("data: " + c + "\n\n").encode()
            mid = len(payload) // 2
            yield payload[:mid]
            await asyncio.sleep(0.05)
            yield payload[mid:]
            await asyncio.sleep(0.05)
        yield b"data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream", headers=headers)


if __name__ == "__main__":
    print(f"mock upstream on http://127.0.0.1:{PORT}/v1", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
