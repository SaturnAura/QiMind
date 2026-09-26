"""DeepSeek 客户端：把事实清单交给大模型，得到人话讲解（带磁盘缓存）。"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Iterator, List, Optional

from .config import DEEPSEEK_BASE_URL, cache_dir, deepseek_api_key, load_settings, scrub_secrets
from .prompts import PROMPT_VERSION, build_messages

logger = logging.getLogger(__name__)


class ExplainerError(RuntimeError):
    """讲解生成失败（缺少 Key、网络异常等）。"""


@dataclass
class Explanation:
    """一次讲解的结果。"""

    text: str
    model: str = ""
    cached: bool = False
    usage: Dict[str, int] = field(default_factory=dict)
    created_at: str = ""
    elapsed: float = 0.0
    error: str = ""

    def to_dict(self) -> Dict[str, object]:
        return {
            "text": self.text,
            "model": self.model,
            "cached": self.cached,
            "usage": self.usage,
            "created_at": self.created_at,
            "elapsed": round(self.elapsed, 2),
            "error": self.error,
        }


class DeepSeekExplainer:
    """调用 DeepSeek 生成讲棋文本。"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        base_url: str = DEEPSEEK_BASE_URL,
    ) -> None:
        settings = load_settings()
        self.api_key = api_key or deepseek_api_key()
        self.model = model or str(settings["model"])
        self.reasoning_effort = reasoning_effort or str(settings["reasoning_effort"])
        self.base_url = base_url
        self._client = None

    @property
    def enabled(self) -> bool:
        """是否配置了 API Key。"""
        return bool(self.api_key)

    def _get_client(self):
        if self._client is None:
            if not self.api_key:
                raise ExplainerError(
                    "缺少 DeepSeek API Key：请设置环境变量 QIMIND_DEEPSEEK_KEY，"
                    "或写入 qimind/data/secrets.json 的 deepseek_api_key 字段。"
                )
            from openai import OpenAI

            self._client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        return self._client

    def explain(
        self,
        fact_sheet: str,
        *,
        played_move: Optional[str] = None,
        is_best: bool = True,
        style: str = "棋友",
        use_cache: bool = True,
    ) -> Explanation:
        """生成讲解文本（非流式）。"""
        messages = build_messages(
            fact_sheet, played_move=played_move, is_best=is_best, style=style
        )
        return self.explain_messages(messages, style=style, use_cache=use_cache)

    def explain_messages(
        self,
        messages: List[Dict[str, str]],
        *,
        style: str = "棋友",
        cache_tag: str = "",
        use_cache: bool = True,
    ) -> Explanation:
        """按给定消息生成文本（非流式），并写入/读取缓存。

        变招讲解、对局报告都复用这里，保证缓存行为一致。
        """
        if cache_tag:
            style = f"{style}|{cache_tag}"
        key = _cache_key(self.model, self.reasoning_effort, style, messages)
        cache_file = cache_dir("explain") / f"{key}.json"

        if use_cache and cache_file.is_file():
            try:
                payload = json.loads(cache_file.read_text(encoding="utf-8"))
                return Explanation(
                    text=payload["text"],
                    model=payload.get("model", self.model),
                    cached=True,
                    usage=payload.get("usage", {}),
                    created_at=payload.get("created_at", ""),
                )
            except (OSError, ValueError, KeyError):
                pass

        client = self._get_client()
        started = time.monotonic()
        try:
            response = client.chat.completions.create(
                model=self.model,
                messages=messages,
                stream=False,
                reasoning_effort=self.reasoning_effort,
                extra_body={"thinking": {"type": "enabled"}},
            )
        except Exception as exc:  # pragma: no cover - 网络异常
            raise ExplainerError(f"调用 DeepSeek 失败：{scrub_secrets(str(exc))}") from exc

        elapsed = time.monotonic() - started
        message = response.choices[0].message
        text = (message.content or "").strip()
        usage: Dict[str, int] = {}
        if getattr(response, "usage", None):
            usage = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }
        explanation = Explanation(
            text=text,
            model=self.model,
            usage=usage,
            created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            elapsed=elapsed,
        )
        if use_cache and text:
            try:
                cache_file.write_text(
                    json.dumps(explanation.to_dict(), ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except OSError:  # pragma: no cover
                pass
        return explanation

    def stream(
        self,
        fact_sheet: str,
        *,
        played_move: Optional[str] = None,
        is_best: bool = True,
        style: str = "棋友",
    ) -> Iterator[str]:
        """流式生成讲解，逐段产出文本增量。"""
        messages = build_messages(
            fact_sheet, played_move=played_move, is_best=is_best, style=style
        )
        client = self._get_client()
        stream = client.chat.completions.create(
            model=self.model,
            messages=messages,
            stream=True,
            reasoning_effort=self.reasoning_effort,
            extra_body={"thinking": {"type": "enabled"}},
        )
        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            content = getattr(delta, "content", None)
            if content:
                yield content


def _cache_key(model: str, effort: str, style: str, messages: List[Dict[str, str]]) -> str:
    digest = hashlib.sha1()
    digest.update(PROMPT_VERSION.encode("utf-8"))
    digest.update(f"|{model}|{effort}|{style}".encode("utf-8"))
    for message in messages:
        digest.update(message["content"].encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()[:24]


_EXPLAINERS: Dict[tuple, DeepSeekExplainer] = {}


def get_explainer(model: Optional[str] = None, reasoning_effort: Optional[str] = None) -> DeepSeekExplainer:
    """返回讲解器实例；同一（模型, 思考强度）组合复用同一个对象。"""
    settings = load_settings()
    model = model or str(settings["model"])
    reasoning_effort = reasoning_effort or str(settings["reasoning_effort"])
    key = (model, reasoning_effort)
    if key not in _EXPLAINERS:
        _EXPLAINERS[key] = DeepSeekExplainer(model=model, reasoning_effort=reasoning_effort)
    return _EXPLAINERS[key]


def reset_explainers() -> None:
    """清空讲解器缓存（在界面上更换 API Key / 模型后调用）。"""
    _EXPLAINERS.clear()


def check_api_key(api_key: Optional[str] = None, base_url: str = DEEPSEEK_BASE_URL) -> Dict[str, object]:
    """测试 API Key 是否可用，返回 ``{ok, models, error}``。"""
    key = (api_key or deepseek_api_key() or "").strip()
    if not key:
        return {"ok": False, "models": [], "error": "还没有填写 API Key"}
    try:
        from openai import OpenAI

        client = OpenAI(api_key=key, base_url=base_url)
        models = [item.id for item in client.models.list().data]
        return {"ok": True, "models": models, "error": ""}
    except Exception as exc:  # pragma: no cover - 网络异常
        return {"ok": False, "models": [], "error": scrub_secrets(str(exc))}


def explain_fact_sheet(
    fact_sheet: str,
    *,
    played_move: Optional[str] = None,
    is_best: bool = True,
    style: str = "棋友",
    use_cache: bool = True,
    fallback: bool = True,
    model: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
) -> Explanation:
    """生成讲解；失败时按 ``fallback`` 决定是抛异常还是返回带 error 的空结果。"""
    return explain_messages(
        build_messages(fact_sheet, played_move=played_move, is_best=is_best, style=style),
        style=style,
        use_cache=use_cache,
        fallback=fallback,
        model=model,
        reasoning_effort=reasoning_effort,
    )


def explain_messages(
    messages: List[Dict[str, str]],
    *,
    style: str = "棋友",
    cache_tag: str = "",
    use_cache: bool = True,
    fallback: bool = True,
    model: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
) -> Explanation:
    """按给定消息生成文本；失败时按 ``fallback`` 决定抛异常还是返回 error 字段。"""
    try:
        return get_explainer(model=model, reasoning_effort=reasoning_effort).explain_messages(
            messages,
            style=style,
            cache_tag=cache_tag,
            use_cache=use_cache,
        )
    except ExplainerError as exc:
        if not fallback:
            raise
        logger.warning("讲解生成失败：%s", exc)
        return Explanation(
            text="",
            error=str(exc),
            created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
