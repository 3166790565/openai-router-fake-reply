from __future__ import annotations

import asyncio
import base64
import json
import os
import random
import re
import secrets
import time
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

from . import config, fingerprint, request_log, tokenizer
from .config import FirstMsgBoost, Route, Rule, Settings

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="OpenAI 兼容路由", docs_url=None, redoc_url=None)


@app.on_event("startup")
async def _startup():
    request_log.start_cleanup_task()


# ---------------------------------------------------------------- auth

def _auth_ok(header: str | None) -> bool:
    user = os.environ.get("WEBUI_USER", "")
    pwd = os.environ.get("WEBUI_PASS", "")
    if not (user and pwd):
        return True
    if not header or not header.lower().startswith("basic "):
        return False
    try:
        raw = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
        u, _, p = raw.partition(":")
    except Exception:
        return False
    return secrets.compare_digest(u, user) and secrets.compare_digest(p, pwd)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    exempt = path.startswith("/v1") or path == "/chat/completions" or path == "/models" or path == "/healthz"
    if not exempt and not _auth_ok(request.headers.get("authorization")):
        return Response(
            status_code=401,
            content="Unauthorized",
            headers={"WWW-Authenticate": 'Basic realm="proxy"'},
        )
    return await call_next(request)


# ---------------------------------------------------------------- webui

@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/api/config")
async def get_config():
    return config.get().model_dump()


@app.put("/api/config")
async def put_config(settings: Settings):
    saved = await config.save(settings)
    return saved.model_dump()


@app.post("/api/config/reset")
async def reset_config():
    saved = await config.reset()
    return saved.model_dump()


# ---------------------------------------------------------------- matching

def pick_rule(model: str, settings: Settings) -> Rule | None:
    m = (model or "").lower()
    for rule in settings.rules:
        if not rule.enabled:
            continue
        if rule.model_match == "" or rule.model_match.lower() in m:
            return rule
    return None


def last_user_text(messages: Any) -> str:
    if not isinstance(messages, list):
        return ""
    for msg in reversed(messages):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
        return ""
    return ""


def hit_keyword(text: str, rule: Rule) -> tuple[bool, str]:
    """判断消息是否命中假回复。返回 (是否命中, 命中方式)。

    命中方式: "pattern" | "keyword" | ""（未命中）
    闸门顺序：工作词黑名单（硬闸门，最高优先）→ 长度 → 正则整句模板 → 关键词句首。
    任务词命中直接放行走上游，正则/关键词都碰不到，防止"帮我写，你是谁?"被劫持。
    """
    t = (text or "").strip()
    if not t:
        return False, ""
    # 1. 工作词黑名单：命中任一 → 强制走上游（最高优先级）
    tl = t.lower()
    if any(w.strip() and w.lower() in tl for w in rule.work_override):
        return False, "work_override"
    # 2. 长度闸门
    if len(t) > rule.match_max_length:
        return False, "length"
    # 3. 正则整句模板：匹配整句形状的身份询问
    for pat in rule.patterns:
        try:
            if re.search(pat, t, re.IGNORECASE):
                return True, "pattern"
        except re.error:
            continue
    # 4. 关键词：限制在句首前 N 字内
    head = tl[: rule.match_head_chars]
    if any(k.strip() and k.strip().lower() in head for k in rule.keywords):
        return True, "keyword"
    return False, ""


def _has_multimodal(messages: Any) -> bool:
    """检测请求是否含图片/多模态内容。"""
    if not isinstance(messages, list):
        return False
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") != "text":
                    return True
    return False


def _is_agent_first_msg(request: Request, body: dict, fm: FirstMsgBoost) -> bool:
    """检测是否为 Agent 工具的首条预热消息。"""
    if not fm.enabled or not fm.fixed_model:
        return False
    # UA 识别
    ua = (request.headers.get("user-agent") or "").lower()
    ua_hit = any(s.strip() and s.lower() in ua for s in fm.detect_ua)
    # system prompt 识别
    msgs = body.get("messages") or []
    sys_text = ""
    for m in msgs:
        if isinstance(m, dict) and m.get("role") == "system":
            c = m.get("content")
            sys_text = c if isinstance(c, str) else ""
            break
    sys_hit = any(s.strip() and s in sys_text for s in fm.detect_system)
    # 首条 + 短消息判定
    user_msgs = [m for m in msgs if isinstance(m, dict) and m.get("role") == "user"]
    if len(user_msgs) != 1:
        return False
    user_text = last_user_text(msgs)
    if len(user_text) > fm.max_user_len:
        return False
    return ua_hit or sys_hit


def _pick_route(rule: Rule, client_model: str, has_multimodal: bool) -> tuple[Route, list[str]]:
    """按权重抽一条掺水候选。返回 (选中候选, 因多模态被排除的模型列表)。"""
    routes = [r for r in rule.routes if r.weight > 0]
    if not routes:
        return Route(weight=1, upstream_model=rule.upstream_model), []
    excluded: list[str] = []
    if has_multimodal:
        compat = [r for r in routes if r.supports_multimodal]
        excluded = [r.upstream_model for r in routes if not r.supports_multimodal and r.upstream_model]
        routes = compat if compat else [Route(weight=1, upstream_model=client_model)]
    picked = random.choices(routes, weights=[r.weight for r in routes], k=1)[0]
    return picked, excluded


def decide(model: str, message: str, settings: Settings) -> dict:
    rule = pick_rule(model, settings)
    if rule is None:
        return {"rule": None, "branch": "proxy", "reply": None, "fingerprint": False}
    hit, hit_via = hit_keyword(message, rule)
    fp_on = rule.fingerprint.enabled and (
        rule.fingerprint.apply_fake if hit else rule.fingerprint.apply_proxy
    )
    result = {
        "rule": {"id": rule.id, "name": rule.name},
        "branch": "fake" if hit else "proxy",
        "reply": None if not hit else _pick_reply(rule),
        "fingerprint": bool(fp_on),
        "upstream_model": None if hit else (rule.upstream_model or None),
        "hit_via": hit_via,
    }
    if rule.routes:
        result["routes"] = [r.model_dump() for r in rule.routes]
    if rule.first_msg.enabled:
        result["first_msg"] = {"enabled": True, "fixed_model": rule.first_msg.fixed_model}
    return result


def _pick_reply(rule: Rule) -> str:
    import random

    replies = [r for r in rule.replies if r.strip()]
    return random.choice(replies) if replies else "……"


def _pick_reasoning(rule: Rule) -> str:
    import random

    items = [r for r in rule.reasoning if r.strip()]
    return random.choice(items) if items else ""


@app.post("/api/match")
async def match(payload: dict):
    settings = config.get()
    model = str(payload.get("model") or "")
    message = str(payload.get("message") or "")
    return decide(model, message, settings)


@app.post("/api/fingerprint/capture")
async def capture(payload: dict):
    settings = config.get()
    try:
        return await fingerprint.capture(
            str(payload.get("base_url") or settings.upstream_base_url),
            str(payload.get("api_key") or ""),
            str(payload.get("model") or ""),
            str(payload.get("prompt") or ""),
            float(payload.get("timeout") or settings.capture_timeout),
        )
    except fingerprint.CaptureError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ---------------------------------------------------------------- request logs

@app.get("/api/logs")
async def get_logs(
    date: str = "",
    branch: str = "",
    rule: str = "",
    page: int = 1,
    size: int = 50,
):
    return await request_log.read(date=date, branch=branch, rule=rule, page=page, size=size)


@app.get("/api/logs/dates")
async def get_log_dates():
    return {"dates": await request_log.list_dates()}


@app.post("/api/logs/clear")
async def clear_logs():
    count = await request_log.clear_all()
    return {"cleared": count}


# ---------------------------------------------------------------- helpers

HOP_HEADERS = {
    "host", "content-length", "connection", "keep-alive", "transfer-encoding",
    "upgrade", "trailers", "proxy-authorization", "proxy-authenticate",
}
RESP_DROP = {
    "content-length", "transfer-encoding", "connection", "keep-alive",
    "trailers", "upgrade", "date", "server",
}


def _forward_headers(request: Request) -> dict[str, str]:
    headers = {}
    for key, value in request.headers.items():
        if key.lower() in HOP_HEADERS:
            continue
        headers[key] = value
    return headers


def _upstream_headers(resp: httpx.Response, rewrite: bool) -> dict[str, str]:
    headers = {}
    drop = RESP_DROP | ({"content-encoding"} if rewrite else set())
    for key, value in resp.headers.multi_items():
        if key.lower() in drop:
            continue
        headers[key] = value
    return headers


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": "proxy_error", "code": status}},
    )


# ---------------------------------------------------------------- fake reply

def _fake_meta(mat: dict, client_model: str, obj_type: str) -> dict:
    meta = {
        "id": mat.get("id") or fingerprint.new_id(),
        "object": mat.get("object") or obj_type,
        "created": mat.get("created") or int(time.time()),
        "model": mat.get("model") or client_model,
    }
    if "system_fingerprint" in mat:
        meta["system_fingerprint"] = mat["system_fingerprint"]
    if "service_tier" in mat:
        meta["service_tier"] = mat["service_tier"]
    if mat.get("extra"):
        meta.update(mat["extra"])
    return meta


def _all_messages_text(messages: Any) -> str:
    if not isinstance(messages, list):
        return ""
    parts: list[str] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            parts.append(
                "".join(
                    p.get("text", "")
                    for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                )
            )
    return "\n".join(parts)


_THINK_RE = re.compile(r"<think>(.*?)</think>\s*", re.S)


def _split_think(reply: str) -> tuple[str, str]:
    """把 <think>...</think> 拆成 (reasoning, content)。无标签则 reasoning 为空。"""
    m = _THINK_RE.search(reply or "")
    if not m:
        return "", reply or ""
    return m.group(1).strip(), (reply[: m.start()] + reply[m.end() :]).strip()


def _usage(
    prompt_text: str,
    message_count: int,
    has_system: bool,
    reasoning: str,
    reply: str,
) -> dict:
    prompt = tokenizer.prompt_tokens(prompt_text, message_count, has_system)
    completion = tokenizer.completion_tokens(reasoning, reply)
    reasoning_tokens = tokenizer.count(reasoning) if reasoning else 0
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "completion_tokens_details": {"reasoning_tokens": reasoning_tokens},
        "prompt_tokens_details": {"cached_tokens": 0},
    }


def _fake_reply(request: Request, body: dict, rule: Rule, client_model: str) -> Response:
    settings = config.get()
    reply = _pick_reply(rule)
    inline_reasoning, reply = _split_think(reply)
    reasoning = inline_reasoning or _pick_reasoning(rule)
    stream = bool(body.get("stream"))
    fp = rule.fingerprint
    mat = (
        fingerprint.materialize(fp, client_model)
        if fp.enabled and fp.apply_fake
        else {}
    )
    headers = {"Cache-Control": "no-cache"}
    for key, value in (mat.get("headers") or {}).items():
        headers[key] = str(value)
    messages = body.get("messages")
    prompt_text = _all_messages_text(messages)
    message_count = len(messages) if isinstance(messages, list) else 1
    has_system = isinstance(messages, list) and any(
        isinstance(m, dict) and m.get("role") == "system" for m in messages
    )
    usage = mat.get("usage") or _usage(
        prompt_text, message_count, has_system, reasoning, reply
    )

    if not stream:
        meta = _fake_meta(mat, client_model, "chat.completion")
        message = {"role": "assistant", "content": reply}
        if reasoning:
            message["reasoning_content"] = reasoning
        payload = {
            **meta,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "logprobs": None,
                    "finish_reason": "stop",
                }
            ],
            "usage": usage,
        }
        headers["Content-Type"] = "application/json"
        return JSONResponse(payload, headers=headers)

    delay = max(0, settings.chunk_delay_ms) / 1000
    size = max(1, settings.chunk_size)
    include_usage = bool(
        isinstance(body.get("stream_options"), dict)
        and body["stream_options"].get("include_usage")
    )
    meta = _fake_meta(mat, client_model, "chat.completion.chunk")

    def sse(obj: dict) -> bytes:
        return b"data: " + json.dumps(obj, ensure_ascii=False).encode() + b"\n\n"

    async def gen():
        first = {**meta, "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]}
        yield sse(first)
        if reasoning:
            for i in range(0, len(reasoning), size):
                if delay:
                    await asyncio.sleep(delay)
                chunk = {
                    **meta,
                    "choices": [{"index": 0, "delta": {"reasoning_content": reasoning[i : i + size]}, "finish_reason": None}],
                }
                yield sse(chunk)
        for i in range(0, len(reply), size):
            if delay:
                await asyncio.sleep(delay)
            piece = reply[i : i + size]
            chunk = {
                **meta,
                "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}],
            }
            yield sse(chunk)
        final = {**meta, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
        if include_usage:
            final["usage"] = None
        yield sse(final)
        if include_usage:
            yield sse({**meta, "choices": [], "usage": usage})
        yield b"data: [DONE]\n\n"

    headers["Content-Type"] = "text/event-stream"
    headers["X-Accel-Buffering"] = "no"
    return StreamingResponse(gen(), headers=headers)


# ---------------------------------------------------------------- proxy

async def _proxy(
    request: Request,
    path: str,
    raw: bytes,
    rule: Rule | None,
    client_model: str,
    upstream_base_url: str | None = None,
    upstream_api_key: str | None = None,
) -> Response:
    settings = config.get()
    base_url = upstream_base_url or settings.upstream_base_url
    url = fingerprint.build_url(base_url, path)
    fp = rule.fingerprint if rule else None
    rewrite = bool(fp and fp.enabled and fp.apply_proxy)
    mat = fingerprint.materialize(fp, client_model) if rewrite else {}

    fwd_headers = _forward_headers(request)
    # 独立上游时用配置的 key 覆盖客户端透传的 Authorization
    if upstream_api_key:
        fwd_headers = {k: v for k, v in fwd_headers.items() if k.lower() != "authorization"}
        fwd_headers["Authorization"] = f"Bearer {upstream_api_key}"

    client = httpx.AsyncClient(timeout=settings.request_timeout)
    try:
        req = client.build_request(
            request.method, url, headers=fwd_headers, content=raw
        )
        upstream = await client.send(req, stream=True)
    except Exception as exc:
        await client.aclose()
        return _error(502, f"上游请求失败: {exc}")

    headers = (
        fingerprint.apply_headers(upstream.headers, mat)
        if rewrite
        else _upstream_headers(upstream, rewrite=False)
    )

    content_type = upstream.headers.get("content-type", "").lower()
    status = upstream.status_code

    async def gen():
        buf = b""
        try:
            if not rewrite or status >= 400 or not (
                "text/event-stream" in content_type or "application/json" in content_type
            ):
                async for chunk in upstream.aiter_bytes():
                    yield chunk
                return

            if "text/event-stream" in content_type:
                async for chunk in upstream.aiter_bytes():
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        yield fingerprint.rewrite_sse_line(line, mat) + b"\n"
                if buf:
                    yield fingerprint.rewrite_sse_line(buf, mat)
            else:
                data = await upstream.aread()
                try:
                    obj = json.loads(data)
                except Exception:
                    yield data
                    return
                if isinstance(obj, dict):
                    fingerprint.apply_json(obj, mat)
                    yield json.dumps(obj, ensure_ascii=False).encode()
                else:
                    yield data
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(gen(), status_code=status, headers=headers)


@app.post("/v1/chat/completions")
@app.post("/chat/completions")
async def chat_completions(request: Request):
    t0 = time.time()
    raw = await request.body()
    settings = config.get()
    try:
        body = json.loads(raw) if raw else {}
    except Exception:
        body = None
    if not isinstance(body, dict):
        return await _proxy(request, "chat/completions", raw, None, "")

    client_model = str(body.get("model") or "")
    user_text = last_user_text(body.get("messages"))
    has_mm = _has_multimodal(body.get("messages"))
    is_stream = bool(body.get("stream"))
    rule = pick_rule(client_model, settings)

    # 日志基础字段
    log_entry: dict[str, Any] = {
        "model": client_model,
        "user_msg_snippet": user_text,
        "has_multimodal": has_mm,
        "stream": is_stream,
    }

    if rule is not None:
        log_entry["rule_id"] = rule.id
        log_entry["rule_name"] = rule.name

        # 1. 假回复分支
        hit, hit_via = hit_keyword(user_text, rule)
        if hit:
            log_entry["branch"] = "fake"
            log_entry["fake_hit"] = True
            log_entry["fake_via"] = hit_via
            reply_text = _pick_reply(rule)
            log_entry["fake_reply_snippet"] = reply_text
            resp = _fake_reply(request, body, rule, client_model)
            log_entry["status"] = resp.status_code
            log_entry["duration_ms"] = int((time.time() - t0) * 1000)
            asyncio.create_task(request_log.write(log_entry))
            return resp

        # 2. Agent 首条加速分支
        if _is_agent_first_msg(request, body, rule.first_msg):
            fm = rule.first_msg
            log_entry["branch"] = "first_msg"
            log_entry["first_msg_boost"] = True
            log_entry["first_msg_model"] = fm.fixed_model
            body["model"] = fm.fixed_model
            raw = json.dumps(body, ensure_ascii=False).encode()
            log_entry["upstream_model"] = fm.fixed_model
            if fm.upstream_base_url:
                log_entry["first_msg_base_url"] = fm.upstream_base_url
            resp = await _proxy(
                request, "chat/completions", raw, rule, client_model,
                upstream_base_url=fm.upstream_base_url or None,
                upstream_api_key=fm.api_key or None,
            )
            log_entry["status"] = resp.status_code
            log_entry["duration_ms"] = int((time.time() - t0) * 1000)
            asyncio.create_task(request_log.write(log_entry))
            return resp

        # 3. 掺水路由分支
        route, mm_excluded = _pick_route(rule, client_model, has_mm)
        if route.upstream_model and route.upstream_model != client_model:
            log_entry["branch"] = "route"
            log_entry["route_picked"] = route.upstream_model
            log_entry["route_weight"] = route.weight
            if mm_excluded:
                log_entry["route_multimodal_excluded"] = mm_excluded
            body["model"] = route.upstream_model
        else:
            log_entry["branch"] = "proxy"
            log_entry["route_picked"] = ""
        if route.body_override:
            body.update(route.body_override)
        if route.upstream_model or route.body_override:
            raw = json.dumps(body, ensure_ascii=False).encode()
        log_entry["upstream_model"] = body.get("model", client_model)
    else:
        log_entry["branch"] = "proxy"
        log_entry["upstream_model"] = client_model

    resp = await _proxy(request, "chat/completions", raw, rule, client_model)
    log_entry["status"] = resp.status_code
    log_entry["duration_ms"] = int((time.time() - t0) * 1000)
    asyncio.create_task(request_log.write(log_entry))
    return resp


@app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def passthrough(path: str, request: Request):
    raw = await request.body()
    return await _proxy(request, f"/v1/{path}", raw, None, "")


@app.api_route("/models", methods=["GET"])
@app.api_route("/v1/models", methods=["GET"])
async def models_proxy(request: Request):
    """中转上游 /v1/models 接口，支持带/不带 /v1 前缀。"""
    raw = await request.body()
    return await _proxy(request, "models", raw, None, "")
