from __future__ import annotations

import json
import random
import re
import secrets
import string
import time
import uuid
from typing import Any

import httpx

_RAND = string.ascii_letters + string.digits
_HEX = string.hexdigits.lower()[:16]
_PLACEHOLDER = re.compile(r"\{([A-Za-z_]+(?::\d+)?)\}")


class CaptureError(Exception):
    pass


def _random(n: int = 24) -> str:
    return "".join(secrets.choice(_RAND) for _ in range(n))


def _hex(n: int = 32) -> str:
    return "".join(secrets.choice(_HEX) for _ in range(n))


def new_id() -> str:
    return "chatcmpl-" + _random(24)


def render(value: Any, model: str = "") -> Any:
    """Recursively render template placeholders in strings/dicts/lists."""
    if isinstance(value, str):
        def repl(m: re.Match) -> str:
            key = m.group(1)
            if key == "random":
                return _random(24)
            if key.startswith("random:"):
                try:
                    n = max(1, min(int(key.split(":", 1)[1]), 128))
                except ValueError:
                    return m.group(0)
                return _random(n)
            if key == "uuid":
                return str(uuid.uuid4())
            if key == "hex":
                return _hex(32)
            if key.startswith("hex:"):
                try:
                    n = max(1, min(int(key.split(":", 1)[1]), 128))
                except ValueError:
                    return m.group(0)
                return _hex(n)
            if key == "timestamp":
                return str(int(time.time()))
            if key == "model":
                return model
            if key == "ms":
                return str(random.randint(60, 900))
            return m.group(0)

        return _PLACEHOLDER.sub(repl, value)
    if isinstance(value, dict):
        return {k: render(v, model) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, model) for v in value]
    return value


def materialize(fp, model: str) -> dict:
    """Turn a Fingerprint config into a concrete per-request fingerprint."""
    if fp is None:
        return {}
    mat: dict[str, Any] = {}
    if fp.id:
        mat["id"] = render(fp.id, model)
    if fp.created:
        created = render(fp.created, model)
        try:
            mat["created"] = int(created)
        except (TypeError, ValueError):
            mat["created"] = int(time.time())
    if fp.model:
        mat["model"] = render(fp.model, model)
    if fp.system_fingerprint:
        mat["system_fingerprint"] = render(fp.system_fingerprint, model)
    if fp.service_tier:
        mat["service_tier"] = render(fp.service_tier, model)
    if fp.object:
        mat["object"] = render(fp.object, model)
    if fp.usage:
        mat["usage"] = render(fp.usage, model)
    if fp.extra:
        mat["extra"] = render(fp.extra, model)
    if fp.headers:
        mat["headers"] = {k: render(v, model) for k, v in fp.headers.items()}
    return mat


_TOP_FIELDS = ("id", "object", "created", "model", "system_fingerprint", "service_tier")


def apply_json(obj: dict, mat: dict) -> dict:
    for key in _TOP_FIELDS:
        if key in mat:
            obj[key] = mat[key]
    if mat.get("usage"):
        obj["usage"] = mat["usage"]
    if mat.get("extra"):
        obj.update(mat["extra"])
    return obj


def apply_chunk(obj: dict, mat: dict) -> dict:
    for key in _TOP_FIELDS:
        if key in mat:
            obj[key] = mat[key]
    if "usage" in mat and "usage" in obj:
        obj["usage"] = mat["usage"]
    if mat.get("extra"):
        obj.update(mat["extra"])
    return obj


def apply_headers(upstream_headers, mat: dict) -> dict:
    skip = {
        "content-length", "content-encoding", "transfer-encoding",
        "connection", "keep-alive", "trailers", "upgrade", "date", "server",
    }
    headers: dict[str, str] = {}
    for key, value in upstream_headers.multi_items():
        low = key.lower()
        if low in skip:
            continue
        for existing in list(headers):
            if existing.lower() == low:
                del headers[existing]
        headers[key] = value
    for key, value in (mat.get("headers") or {}).items():
        headers = {k: v for k, v in headers.items() if k.lower() != key.lower()}
        headers[key] = str(value)
    return headers


def rewrite_sse_line(line: bytes, mat: dict) -> bytes:
    has_cr = line.endswith(b"\r")
    raw = line[:-1] if has_cr else line
    stripped = raw.lstrip()
    if not stripped.startswith(b"data:"):
        return line
    payload = stripped[5:].strip()
    if not payload or payload == b"[DONE]":
        return line
    try:
        obj = json.loads(payload)
    except Exception:
        return line
    if not isinstance(obj, dict):
        return line
    apply_chunk(obj, mat)
    new_line = b"data: " + json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()
    return new_line + (b"\r" if has_cr else b"")


def build_url(base: str, path: str) -> str:
    base = (base or "").rstrip("/")
    p = (path or "").lstrip("/")
    if p.startswith("v1/"):
        p = p[3:]
    if base.endswith("/v1"):
        return f"{base}/{p}"
    return f"{base}/v1/{p}"


_CAPTURE_HEADERS = {
    "x-request-id", "openai-processing-ms", "openai-version", "openai-organization",
    "openai-project", "cf-ray", "x-envoy-upstream-service-time",
}


def _templ_id(value: str) -> str:
    value = value or ""
    if re.fullmatch(r"[0-9a-f]{16,64}", value):
        return f"{{hex:{len(value)}}}"
    if "-" in value:
        prefix, tail = value.rsplit("-", 1)
        if 0 < len(tail) <= 64:
            return f"{prefix}-{{random:{len(tail)}}}"
    return "{random}"


def _templ_token(value: str) -> str:
    value = value or ""
    if re.fullmatch(r"[0-9a-f]{16,64}", value):
        return f"{{hex:{len(value)}}}"
    if re.fullmatch(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
        value,
    ):
        return "{uuid}"
    m = re.match(r"^([A-Za-z][A-Za-z_-]*[_-])([A-Za-z0-9]+)$", value)
    if m:
        return f"{m.group(1)}{{random:{min(len(m.group(2)), 64)}}}"
    return "{random}"


def _templ_cf_ray(value: str) -> str:
    m = re.match(r"^([0-9a-f]{16,32})-([A-Z]{2,5})$", value or "")
    if m:
        return f"{{hex:{len(m.group(1))}}}-{m.group(2)}"
    return _templ_token(value)


async def capture(base_url: str, api_key: str, model: str, prompt: str, timeout: float) -> dict:
    if not (base_url or "").strip():
        raise CaptureError("base_url 不能为空")
    if not (model or "").strip():
        raise CaptureError("model 不能为空")
    url = build_url(base_url, "chat/completions")
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt or "hi"}],
        "max_tokens": 1,
        "stream": False,
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=payload, headers=headers)
    except Exception as exc:
        raise CaptureError(f"请求上游失败: {exc}") from exc
    if resp.status_code >= 400:
        raise CaptureError(f"上游返回 {resp.status_code}: {resp.text[:500]}")
    try:
        data = json.loads(resp.content.decode("utf-8"))
    except Exception as exc:
        raise CaptureError(f"上游返回非 JSON: {resp.text[:300]}") from exc
    if not isinstance(data, dict):
        raise CaptureError("上游返回 JSON 结构异常")

    captured_headers: dict[str, str] = {}
    for key, value in resp.headers.multi_items():
        low = key.lower()
        if low in _CAPTURE_HEADERS and low not in captured_headers:
            captured_headers[low] = value

    fp_headers: dict[str, str] = {}
    for key, value in captured_headers.items():
        if key == "openai-processing-ms":
            fp_headers[key] = "{ms}"
        elif key == "cf-ray":
            fp_headers[key] = _templ_cf_ray(value)
        elif key in ("x-request-id",):
            fp_headers[key] = _templ_token(value)
        else:
            fp_headers[key] = value

    usage = data.get("usage") if isinstance(data.get("usage"), dict) else None
    extra = {
        k: v for k, v in data.items()
        if k not in {"id", "object", "created", "model", "choices", "usage",
                     "system_fingerprint", "service_tier", "messages"}
    }
    raw_id = str(data.get("id") or "")
    raw_model = str(data.get("model") or "")
    fp = {
        "enabled": True,
        "apply_fake": True,
        "apply_proxy": True,
        "id": _templ_id(raw_id),
        "model": raw_model or "{model}",
        "created": "{timestamp}",
        "system_fingerprint": str(data.get("system_fingerprint") or ""),
        "service_tier": str(data.get("service_tier") or ""),
        "object": str(data.get("object") or ""),
        # usage 不采集：本次采样用的是 max_tokens=1，数值没有代表性。
        # 假回复的 usage 由本地 tokenizer 按实际内容计算。
        "usage": None,
        "headers": fp_headers,
        "extra": extra,
    }
    return {
        "fingerprint": fp,
        "captured_headers": captured_headers,
        "captured": {
            "id": raw_id,
            "model": raw_model,
            "object": data.get("object"),
            "created": data.get("created"),
            "system_fingerprint": data.get("system_fingerprint"),
            "service_tier": data.get("service_tier"),
            "usage_sample": usage,
        },
        "sample": json.dumps(data, ensure_ascii=False)[:2000],
    }
