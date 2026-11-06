"""从模型供应商的上游接口读取模型元数据。

上下文窗口不能只依赖本地配置或硬编码：不同模型、同一模型的不同版本可能完全不
一样。启动时请求 OpenAI 兼容的 ``/models``（必要时再请求 ``/models/{model}``），
从返回的模型条目中提取 ``context_window`` 等字段。供应商没有暴露该字段时返回
``None`` 和可读错误，由 UI 明确显示“未知”，而不是拿本地默认值冒充上游数据。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from .config import LLMSettings

DEFAULT_MODEL_INFO_TIMEOUT = 10.0
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024

_CONTEXT_KEYS = (
    "context_window",
    "context_length",
    "max_context_length",
    "max_context_window",
    "max_model_len",
    "max_input_tokens",
    "input_token_limit",
    "context_size",
    "n_ctx",
    "n_ctx_train",
)
_OUTPUT_KEYS = (
    "max_output_tokens",
    "max_completion_tokens",
    "max_tokens",
)


class _FetchError(Exception):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True, slots=True)
class ModelMetadata:
    """启动时从上游读取到的模型信息。"""

    model: str
    context_window: int | None = None
    max_output_tokens: int | None = None
    display_name: str = ""
    endpoint: str = ""
    error: str = field(default="", repr=False)

    @property
    def available(self) -> bool:
        return self.context_window is not None

    @property
    def source(self) -> str:
        if self.context_window is not None and self.endpoint:
            return "上游"
        return "未知"


def _normalize_url(url: str) -> str:
    """主机名大小写不敏感；统一小写可避免部分 TLS 栈的主机名校验差异。"""

    parts = urlsplit(url)
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, "")
    )


def _model_list_urls(base_url: str) -> list[str]:
    base = _normalize_url(base_url.rstrip("/"))
    parts = urlsplit(base)
    path = parts.path.rstrip("/")
    urls = [
        urlunsplit((parts.scheme, parts.netloc, f"{path}/models", "", "")),
    ]
    if path.endswith("/v1"):
        root = path[: -len("/v1")].rstrip("/")
        urls.append(urlunsplit((parts.scheme, parts.netloc, f"{root}/models", "", "")))
    else:
        urls.append(
            urlunsplit((parts.scheme, parts.netloc, f"{path}/v1/models", "", ""))
        )
    unique: list[str] = []
    for url in urls:
        if url not in unique:
            unique.append(url)
    return unique


def _read_json(url: str, api_key: str, timeout: float) -> Any:
    request = Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": "xAgent/0.1 model-metadata",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        raise _FetchError(f"HTTP {exc.code} {exc.reason}", status=exc.code) from exc
    except URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise _FetchError(f"无法连接上游：{reason}") from exc
    except (TimeoutError, OSError) as exc:
        raise _FetchError(f"请求上游失败：{exc}") from exc

    if len(raw) > _MAX_RESPONSE_BYTES:
        raise _FetchError("上游响应过大")
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except (ValueError, UnicodeError) as exc:
        raise _FetchError("上游返回的不是合法 JSON") from exc


def _as_positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        number = int(value)
        return number if number > 0 else None
    if isinstance(value, str):
        text = value.strip().replace(",", "").replace("_", "")
        if text.endswith(("k", "K")):
            text = text[:-1]
            try:
                number = int(float(text) * 1024)
            except ValueError:
                return None
            return number if number > 0 else None
        try:
            number = int(float(text))
        except ValueError:
            return None
        return number if number > 0 else None
    return None


def _find_int(data: Any, keys: tuple[str, ...], depth: int = 0) -> int | None:
    """先查当前层的精确字段，再有限递归到 provider / details 等嵌套对象。"""

    if depth > 3:
        return None
    if isinstance(data, dict):
        for key in keys:
            value = _as_positive_int(data.get(key))
            if value is not None:
                return value
        for value in data.values():
            if isinstance(value, (dict, list)):
                found = _find_int(value, keys, depth + 1)
                if found is not None:
                    return found
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                found = _find_int(item, keys, depth + 1)
                if found is not None:
                    return found
    return None


def _model_entries(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("data", "models", "result"):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            return [value]
    return [data]


def _select_model_entry(data: Any, model: str) -> dict[str, Any] | None:
    entries = _model_entries(data)
    target = model.casefold()
    for entry in entries:
        identifiers = (
            entry.get("id"),
            entry.get("model"),
            entry.get("name"),
            entry.get("display_name"),
        )
        if any(
            isinstance(identifier, str) and identifier.casefold() == target
            for identifier in identifiers
        ):
            return entry
    if len(entries) == 1:
        return entries[0]
    return None


def _metadata_from_entry(
    model: str,
    entry: dict[str, Any],
    endpoint: str,
) -> ModelMetadata:
    context_window = _find_int(entry, _CONTEXT_KEYS)
    max_output = _find_int(entry, _OUTPUT_KEYS)
    name = entry.get("name") or entry.get("display_name") or ""
    if context_window is None:
        return ModelMetadata(
            model=model,
            max_output_tokens=max_output,
            display_name=str(name),
            endpoint=endpoint,
            error="上游模型信息没有返回 context_window / context_length 字段",
        )
    return ModelMetadata(
        model=model,
        context_window=context_window,
        max_output_tokens=max_output,
        display_name=str(name),
        endpoint=endpoint,
    )


def discover_model_metadata(
    settings: LLMSettings,
    *,
    timeout: float = DEFAULT_MODEL_INFO_TIMEOUT,
) -> ModelMetadata:
    """同步查询上游模型列表；失败时返回带 ``error`` 的元数据，不抛异常。"""

    model = settings.model
    errors: list[str] = []
    list_urls = _model_list_urls(settings.base_url)

    for url in list_urls:
        try:
            data = _read_json(url, settings.api_key, timeout)
        except _FetchError as exc:
            errors.append(f"{url}：{exc}")
            # 鉴权失败或网络不可达时换路径也不会成功，避免启动等待多个超时。
            if exc.status in {401, 403} or exc.status is None:
                break
            continue

        entry = _select_model_entry(data, model)
        if entry is not None:
            metadata = _metadata_from_entry(model, entry, url)
            if metadata.context_window is not None:
                return metadata
            errors.append(metadata.error or "上游未返回 context_window")

        detail_url = f"{url.rstrip('/')}/{quote(model, safe='')}"
        try:
            detail = _read_json(detail_url, settings.api_key, timeout)
        except _FetchError as exc:
            errors.append(f"{detail_url}：{exc}")
            continue
        detail_entry = _select_model_entry(detail, model)
        if detail_entry is not None:
            metadata = _metadata_from_entry(model, detail_entry, detail_url)
            if metadata.context_window is not None:
                return metadata
            errors.append(metadata.error or "上游未返回 context_window")
        # 列表接口已经可访问，说明地址是正确的；不再尝试其他候选地址。
        break

    message = "；".join(dict.fromkeys(errors))
    return ModelMetadata(
        model=model,
        error=message or "上游没有返回可用的模型信息",
    )


__all__ = [
    "DEFAULT_MODEL_INFO_TIMEOUT",
    "ModelMetadata",
    "discover_model_metadata",
]
