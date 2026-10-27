"""DeepSeek tokenizer 精确计数。

本地加载官方 tokenizer.json（随镜像打包），不联网、不产生 API 费用。

prompt 偏移实测模型（对照真实上游 usage，覆盖 1~6 条消息、有无 system）：
    无 system:  base = 35 + n     (n=1→36, n=3→38, n=5→40)
    有 system:  base = 27 + n     (n=2→29, n=4→31, n=6→33)
    prompt_tokens = encode(全部消息文本) + base

completion_tokens = encode(reasoning + content) + 1
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from tokenizers import Tokenizer

TOKENIZER_PATH = Path(__file__).resolve().parent / "data" / "deepseek_tokenizer.json"

# prompt 偏移：base = intercept + n
INTERCEPT_NO_SYSTEM = 35
INTERCEPT_WITH_SYSTEM = 27
COMPLETION_OFFSET = 1


@lru_cache(maxsize=1)
def _tokenizer() -> Tokenizer:
    return Tokenizer.from_file(str(TOKENIZER_PATH))


def count(text: str) -> int:
    if not text:
        return 0
    return len(_tokenizer().encode(text).ids)


def prompt_tokens(text: str, message_count: int = 1, has_system: bool = False) -> int:
    n = max(1, message_count)
    intercept = INTERCEPT_WITH_SYSTEM if has_system else INTERCEPT_NO_SYSTEM
    return count(text) + intercept + n


def completion_tokens(reasoning: str, content: str) -> int:
    return count((reasoning or "") + (content or "")) + COMPLETION_OFFSET
