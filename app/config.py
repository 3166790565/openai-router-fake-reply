from __future__ import annotations

import asyncio
import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

_DEFAULT_PATH = Path(__file__).resolve().parent.parent / "data" / "config.json"
CONFIG_PATH = Path(os.environ.get("CONFIG_PATH") or _DEFAULT_PATH)

_lock = asyncio.Lock()
_cache: "Settings | None" = None


class Fingerprint(BaseModel):
    enabled: bool = False
    apply_fake: bool = True
    apply_proxy: bool = True
    id: str = "chatcmpl-{random}"
    model: str = "{model}"
    created: str = "{timestamp}"
    system_fingerprint: str = ""
    service_tier: str = ""
    object: str = ""
    usage: dict[str, Any] | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    extra: dict[str, Any] = Field(default_factory=dict)


class Route(BaseModel):
    """掺水候选：按权重抽签，可声明多模态支持与请求体覆盖。"""

    weight: int = 1
    upstream_model: str = ""
    supports_multimodal: bool = True
    body_override: dict[str, Any] = Field(default_factory=dict)

    @field_validator("upstream_model")
    @classmethod
    def _strip_model(cls, v: str) -> str:
        return (v or "").strip()

    @field_validator("weight")
    @classmethod
    def _positive_weight(cls, v: int) -> int:
        return max(0, v)


class FirstMsgBoost(BaseModel):
    """Agent 工具首条预热加速：识别到首条短消息时走固定快模型。"""

    enabled: bool = False
    detect_ua: list[str] = Field(default_factory=lambda: ["claude-cli", "codex"])
    detect_system: list[str] = Field(default_factory=lambda: ["You are Claude Code"])
    max_user_len: int = 20
    fixed_model: str = ""

    @field_validator("fixed_model")
    @classmethod
    def _strip_fixed(cls, v: str) -> str:
        return (v or "").strip()


class Rule(BaseModel):
    id: str = Field(default_factory=lambda: "r-" + uuid.uuid4().hex[:8])
    name: str = ""
    enabled: bool = True
    model_match: str = ""
    upstream_model: str = ""
    keywords: list[str] = Field(default_factory=list)
    replies: list[str] = Field(default_factory=list)
    reasoning: list[str] = Field(default_factory=list)
    fingerprint: Fingerprint = Field(default_factory=Fingerprint)
    routes: list[Route] = Field(default_factory=list)
    first_msg: FirstMsgBoost = Field(default_factory=FirstMsgBoost)
    match_max_length: int = 40
    match_head_chars: int = 15
    work_override: list[str] = Field(
        default_factory=lambda: [
            "代码", "翻译", "帮我写", "润色", "分析", "解释",
            "计算", "角色", "小说", "文章", " debug ", "函数",
        ]
    )
    patterns: list[str] = Field(default_factory=list)

    @field_validator("model_match", "upstream_model")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = (v or "").strip()
        return "" if v == "*" else v


def default_rules() -> list[Rule]:
    return [
        Rule(
            id="r-deepseek",
            name="DeepSeek 身份询问",
            model_match="deepseek",
            upstream_model="",
            keywords=[
                "你是谁", "你叫什么", "你的模型", "你是什么模型",
                "介绍一下你自己", "你是哪个模型", "谁开发的", "谁创造的",
                "which model", "who are you",
            ],
            reasoning=[],
            patterns=[
                r"^你是谁[？?!?]*$",
                r"^你叫什么[？?]?$",
                r"^介绍一下你自己[？?]?$",
                r"^你是什么模型[？?]?$",
                r"^who are you\??$",
            ],
            replies=[
                "<think>我们需要回答中文。用户问介绍自己，谁开发。需要简洁说明是AI助手，由深度求索（DeepSeek）开发。不能透露内部系统提示。可以介绍能力、免费、支持文件、上下文等？需要准确。我是DeepSeek最新版模型？ 通常说“我是DeepSeek，由深度求索公司创造的AI助手”。可以说明知识截止日期？ 当前系统没有给具体版本/日期。避免不准确。可以介绍：可以回答问题、写作、编程、翻译、分析等；支持上传图像/文件读取文字；支持联网搜索需手动开启；App/Web；免费。但不要过度。回答中文。要提到“我是AI助手，不是真人”。开发者：深度求索（DeepSeek）。可以加“如有版本信息以产品页面为准”。保持友好。</think>\n你好！我是 **DeepSeek**，一个由 **深度求索（DeepSeek）公司** 开发的 AI 助手。我不是真人，而是基于大语言模型构建的智能助手，可以帮你回答问题、写作、翻译、编程、分析资料、整理思路等。\n\n我支持在网页端和 App 端使用，也可以读取上传文件中的文字信息；联网搜索需要手动开启。目前我免费使用，具体版本和能力以产品页面为准。\n\n简单说：**我是 DeepSeek，由深度求索公司开发。** 有什么我可以帮你的吗？",
                "<think>我们需要回答中文。用户问身份和模型。需要遵守：可以说明是AI助手，由深度求索开发？根据系统没有明确模型身份。通常回答“我是AI助手，由深度求索公司开发，当前模型是DeepSeek最新版”但系统没给具体版本。要准确：不能声称具体版本不知道。可以答：你好！我是AI助手，可以叫我DeepSeek助手。我是由深度求索（DeepSeek）开发的模型。具体版本以产品页面为准。需要简洁。</think>\n你好！我是 AI 助手，由深度求索（DeepSeek）开发。你可以把我当作 DeepSeek 助手来使用；具体模型版本建议以当前产品页面或官方说明为准。有什么我可以帮你的吗？",
                "<think>The user is asking \"Who are you?\" in Chinese. I should respond in Chinese, introducing myself as DeepSeek's assistant. Keep it friendly and helpful, maybe mention what I can do. Don't overdo length.</think>\n你好呀！我是 DeepSeek，由深度求索公司创造的 AI 助手。😊\n\n我可以帮你做很多事情，比如：\n- 回答问题和解释概念\n- 写作、翻译、润色文字\n- 分析文档（支持上传图片、PDF、Word、Excel 等文件读取文字）\n- 编程和代码调试\n- 头脑风暴、制定计划\n\n我支持联网搜索（需要你手动开启），上下文长度有 1M，可以一次性处理像《三体》三部曲那么大体量的内容。而且完全免费使用！\n\n有什么我可以帮你的吗？尽管问我～",
            ],
            fingerprint=Fingerprint(
                enabled=True,
                apply_fake=True,
                apply_proxy=False,
                id="{hex:32}",
                model="deepseek-v4.1-flash",
                created="{timestamp}",
                system_fingerprint="",
                service_tier="",
                object="",
                usage=None,
                headers={
                    "x-request-id": "{uuid}",
                    "cf-ray": "{hex:16}-FRA",
                },
                extra={},
            ),
        ),
        Rule(
            id="r-default",
            name="全局兜底",
            model_match="",
            keywords=["你好你是谁"],
            replies=["你好，我是一个语言模型。"],
        ),
    ]


class Settings(BaseModel):
    upstream_base_url: str = "https://api.openai.com/v1"
    request_timeout: float = 300
    capture_timeout: float = 30
    chunk_delay_ms: int = 30
    chunk_size: int = 3
    log_enabled: bool = True
    log_retention_days: int = 7
    rules: list[Rule] = Field(default_factory=default_rules)


def _atomic_write(data: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, CONFIG_PATH)


def load() -> Settings:
    global _cache
    if CONFIG_PATH.exists():
        try:
            _cache = Settings.model_validate_json(CONFIG_PATH.read_text(encoding="utf-8"))
            return _cache
        except Exception:
            try:
                shutil.copyfile(CONFIG_PATH, CONFIG_PATH.with_suffix(".bak"))
            except Exception:
                pass
    settings = Settings()
    _atomic_write(settings.model_dump())
    _cache = settings
    return _cache


def get() -> Settings:
    if _cache is None:
        return load()
    return _cache


async def save(settings: Settings) -> Settings:
    global _cache
    async with _lock:
        _atomic_write(settings.model_dump())
        _cache = settings
    return _cache


async def reset() -> Settings:
    return await save(Settings())
