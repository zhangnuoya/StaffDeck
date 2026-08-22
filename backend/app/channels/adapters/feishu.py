from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from typing import Any, Callable
from urllib.parse import urlparse

import httpx

from app.channels.adapters.base import (
    CHANNEL_TEXT_LIMIT,
    ChannelInboundAttachment,
    register_channel_adapter,
    split_channel_text,
    stream_download_with_limit,
)
from app.channels.crypto import decrypt_channel_secret
from app.channels.markdown_render import (
    has_markdown,
    parse_markdown,
    render_feishu_post,
    split_markdown_by_lines,
)
from app.config import get_settings
from app.db.models import ChannelBinding

FEISHU_API_BASE = "https://open.feishu.cn/open-apis"
TOKEN_REFRESH_SKEW_SECONDS = 300
FEISHU_REACTION_TOKEN = "Get"
_TOKEN_INVALID_CODES = {99991663, 99991664, 99991668}

# 飞书 post 富文本 a 节点要求合法 http(s) URL;本地路径链接(如产物文件
# 绝对路径)会被 API 以 HTTP 400 拒绝,消毒时降级为纯文本。
_FEISHU_ALLOWED_LINK_SCHEMES = {"http", "https"}


def _is_valid_feishu_href(href: object) -> bool:
    raw = str(href or "").strip()
    if not raw:
        return False
    parsed = urlparse(raw)
    return (
        parsed.scheme in _FEISHU_ALLOWED_LINK_SCHEMES
        and bool(parsed.netloc)
    )


def _sanitize_feishu_post_links(node: Any) -> Any:
    """递归把非法 href 的 a 节点降级为 text 节点,其余结构原样保留。"""

    if isinstance(node, dict):
        if node.get("tag") == "a" and not _is_valid_feishu_href(node.get("href")):
            return {
                "tag": "text",
                "text": str(node.get("text") or ""),
                "un_escape": False,
            }
        return {key: _sanitize_feishu_post_links(value) for key, value in node.items()}
    if isinstance(node, list):
        return [_sanitize_feishu_post_links(item) for item in node]
    return node


# ---------------------------------------------------------------------------
# ```echarts 代码块 → 飞书卡片 chart 组件(VChart spec)原生渲染。
# 飞书图表组件不是 ECharts:VChart 用记录数组(data.values)+字段映射
# (xField/yField),需要从 ECharts option 的 xAxis.data + series[].data
# 分离结构转换。仅覆盖常用类型(bar/line/area/pie/scatter),转换不了的
# option 降级为文字提示,不丢消息。
# ---------------------------------------------------------------------------

_ECHARTS_BLOCK_RE = re.compile(r"```echarts[^\S\n]*\n(.*?)```", re.DOTALL)
_MAX_CHART_CARDS_PER_MESSAGE = 5
_VCHART_SUPPORTED_TYPES = {"bar", "line", "area", "pie", "scatter", "radar", "funnel", "gauge"}


def _echarts_option_title(option: dict[str, Any]) -> str:
    title = option.get("title")
    if isinstance(title, str):
        return title.strip()
    if isinstance(title, dict):
        text = title.get("text")
        if isinstance(text, str):
            return text.strip()
    return ""


def _extract_echarts_blocks(text: str) -> tuple[str, list[dict[str, Any]]]:
    """剥离文本中的合法 echarts 代码块;非法块原样保留(照旧当文本发)。"""

    options: list[dict[str, Any]] = []

    def _collect(match: re.Match[str]) -> str:
        try:
            parsed = json.loads(match.group(1).strip())
        except ValueError:
            return match.group(0)
        if isinstance(parsed, dict) and isinstance(parsed.get("series"), list):
            options.append(parsed)
            return ""
        return match.group(0)

    stripped = _ECHARTS_BLOCK_RE.sub(_collect, text)
    return stripped.strip(), options[:_MAX_CHART_CARDS_PER_MESSAGE]


def _echarts_series_value(item: Any) -> Any:
    if isinstance(item, dict):
        return item.get("value")
    return item


def _echarts_option_to_vchart(option: dict[str, Any]) -> dict[str, Any] | None:
    series = option.get("series")
    if not isinstance(series, list) or not series:
        return None
    normalized = [s for s in series if isinstance(s, dict)]
    if len(normalized) != len(series) or not normalized:
        return None
    chart_type = str(normalized[0].get("type") or "").lower()
    if chart_type not in _VCHART_SUPPORTED_TYPES:
        return None
    if any(str(s.get("type") or "").lower() != chart_type for s in normalized):
        return None

    spec: dict[str, Any] = {"type": chart_type}
    title = _echarts_option_title(option)
    if title:
        spec["title"] = {"text": title}

    if chart_type in ("pie", "funnel"):
        # ECharts 玫瑰图不是独立类型:pie + roseType。VChart 用 type=rose。
        rose = bool(str(normalized[0].get("roseType") or "").strip())
        spec["type"] = "rose" if (chart_type == "pie" and rose) else chart_type
        values = []
        for item in normalized[0].get("data") or []:
            if isinstance(item, dict):
                values.append(
                    {
                        "name": str(item.get("name") or item.get("value") or ""),
                        "value": _echarts_series_value(item),
                    }
                )
            else:
                values.append({"name": str(item), "value": item})
        if not values:
            return None
        spec["data"] = {"values": values}
        spec["categoryField"] = "name"
        spec["valueField"] = "value"
        spec["legends"] = {"visible": True}
        return spec

    if chart_type == "gauge":
        first_data = normalized[0].get("data") or []
        gauge_value: Any = None
        if first_data and isinstance(first_data[0], dict):
            gauge_value = first_data[0].get("value")
        elif first_data:
            gauge_value = first_data[0]
        if gauge_value is None:
            return None
        spec["data"] = {"values": [{"value": gauge_value}]}
        spec["valueField"] = "value"
        return spec

    if chart_type == "radar":
        # ECharts radar:radar.indicator 给维度名,series[].data 是
        # [{value: [多维数值], name}] 或裸数组;VChart 用记录数组展开。
        radar = option.get("radar")
        if isinstance(radar, list) and radar and isinstance(radar[0], dict):
            radar = radar[0]
        indicators: list[str] = []
        if isinstance(radar, dict):
            for item in radar.get("indicator") or []:
                if isinstance(item, dict) and str(item.get("name") or "").strip():
                    indicators.append(str(item["name"]).strip())
        if not indicators:
            return None
        entries: list[tuple[str, list[Any]]] = []
        for s in normalized:
            for item in s.get("data") or []:
                if isinstance(item, dict) and isinstance(item.get("value"), list):
                    entries.append((str(item.get("name") or "系列"), list(item["value"])))
                elif isinstance(item, list):
                    entries.append(("系列", list(item)))
        if not entries:
            return None
        multi_radar = len(entries) > 1
        values: list[dict[str, Any]] = []
        for entry_name, entry_values in entries:
            for position, value in enumerate(entry_values):
                row: dict[str, Any] = {
                    "dimension": (
                        indicators[position]
                        if position < len(indicators)
                        else f"维度{position + 1}"
                    ),
                    "value": value,
                }
                if multi_radar:
                    row["series"] = entry_name
                values.append(row)
        spec["data"] = {"values": values}
        spec["categoryField"] = "dimension"
        spec["valueField"] = "value"
        if multi_radar:
            spec["seriesField"] = "series"
            spec["legends"] = {"visible": True}
        return spec

    if chart_type == "scatter":
        values = []
        for s in normalized:
            for item in s.get("data") or []:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    values.append({"x": item[0], "y": item[1]})
        if not values:
            return None
        spec["data"] = {"values": values}
        spec["xField"] = "x"
        spec["yField"] = "y"
        return spec

    xaxis = option.get("xAxis")
    if isinstance(xaxis, list) and xaxis:
        xaxis = xaxis[0] if isinstance(xaxis[0], dict) else None
    categories = [str(c) for c in (xaxis.get("data") or [])] if isinstance(xaxis, dict) else []
    if not categories:
        return None

    multi_series = len(normalized) > 1
    values: list[dict[str, Any]] = []
    for s in normalized:
        name = str(s.get("name") or "系列")
        data = s.get("data") or []
        for position, item in enumerate(data):
            row: dict[str, Any] = {
                "x": categories[position] if position < len(categories) else str(position),
                "y": _echarts_series_value(item),
            }
            if multi_series:
                row["series"] = name
            values.append(row)
    if not values:
        return None
    spec["data"] = {"values": values}
    spec["xField"] = "x"
    spec["yField"] = "y"
    if multi_series:
        spec["seriesField"] = "series"
        spec["legends"] = {"visible": True}
    return spec


def _build_feishu_chart_card(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": "2.0",
        "body": {"elements": [{"tag": "chart", "chart_spec": spec}]},
    }
_PERMANENT_MESSAGE_CODES = {
    230001,  # invalid request/target
    230002,  # bot is not in the chat
    230006,  # message cannot be replied to
    230011,  # bot has no permission in the chat
}


class FeishuSendError(RuntimeError):
    retryable = True


class FeishuPermanentError(FeishuSendError):
    retryable = False


class FeishuTransientError(FeishuSendError):
    retryable = True


class FeishuTokenProvider:
    def __init__(self, *, client_factory: Callable[[], httpx.Client] | None = None):
        self._client_factory = client_factory or (lambda: httpx.Client(timeout=10.0))
        self._cache: dict[tuple[str, str, int], tuple[str, float]] = {}
        self._lock = threading.Lock()
        self._key_locks: dict[tuple[str, str, int], threading.Lock] = {}

    @staticmethod
    def _key(binding: ChannelBinding) -> tuple[str, str, int]:
        return (
            str(binding.external_account_key or ""),
            str(binding.provider_tenant_key or ""),
            binding.config_revision,
        )

    def invalidate(self, binding: ChannelBinding, *, expected_token: str | None = None) -> bool:
        with self._lock:
            key = self._key(binding)
            cached = self._cache.get(key)
            if expected_token is not None and cached and cached[0] != expected_token:
                return False
            self._cache.pop(key, None)
            return True

    def get(self, binding: ChannelBinding, *, force_refresh: bool = False) -> str:
        key = self._key(binding)
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(key)
            if cached and not force_refresh and cached[1] > now:
                return cached[0]
            observed_token = cached[0] if cached else None
            key_lock = self._key_locks.setdefault(key, threading.Lock())
        with key_lock:
            with self._lock:
                cached = self._cache.get(key)
                if cached and cached[1] > time.monotonic() and (
                    not force_refresh or cached[0] != observed_token
                ):
                    return cached[0]
            config = dict(binding.config_json or {})
            app_id = str(config.get("app_id") or "").strip()
            if not app_id or not binding.credentials_enc:
                raise FeishuPermanentError("飞书绑定缺少应用凭证")
            app_secret = decrypt_channel_secret(binding.credentials_enc)
            try:
                with self._client_factory() as client:
                    response = client.post(
                        f"{FEISHU_API_BASE}/auth/v3/tenant_access_token/internal",
                        json={"app_id": app_id, "app_secret": app_secret},
                    )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                raise FeishuTransientError("飞书 token 请求暂时失败") from exc
            try:
                data = response.json()
            except ValueError as exc:
                raise FeishuTransientError("飞书 token 响应格式无效") from exc
            if response.status_code >= 500 or response.status_code == 429:
                raise FeishuTransientError("飞书 token 服务暂时不可用")
            if response.status_code >= 400 or int(data.get("code", -1)) != 0:
                raise FeishuPermanentError("飞书应用凭证无效或无权限")
            token = str(data.get("tenant_access_token") or "").strip()
            expires_in = int(data.get("expire") or 0)
            if not token or expires_in <= 0:
                raise FeishuTransientError("飞书 token 响应缺少必要字段")
            valid_for = max(1, expires_in - TOKEN_REFRESH_SKEW_SECONDS)
            with self._lock:
                self._cache[key] = (token, time.monotonic() + valid_for)
            return token


def validate_feishu_credentials(
    app_id: str,
    app_secret: str,
    *,
    client_factory: Callable[[], httpx.Client] | None = None,
) -> dict[str, str]:
    factory = client_factory or (lambda: httpx.Client(timeout=10.0))
    try:
        with factory() as client:
            token_response = client.post(
                f"{FEISHU_API_BASE}/auth/v3/tenant_access_token/internal",
                json={"app_id": app_id, "app_secret": app_secret},
            )
            token_data = token_response.json()
            token = str(token_data.get("tenant_access_token") or "").strip()
            if token_response.status_code == 429 or token_response.status_code >= 500:
                raise FeishuTransientError("飞书 token 服务暂时不可用")
            if (
                token_response.status_code >= 400
                or int(token_data.get("code", -1)) != 0
                or not token
            ):
                raise FeishuPermanentError("飞书应用凭证无效或无权限")
            bot_response = client.get(
                f"{FEISHU_API_BASE}/bot/v3/info/",
                headers={"Authorization": f"Bearer {token}"},
            )
            bot_data = bot_response.json()
    except FeishuSendError:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        raise FeishuTransientError("无法验证飞书应用凭证") from exc
    if bot_response.status_code == 429 or bot_response.status_code >= 500:
        raise FeishuTransientError("飞书机器人信息服务暂时不可用")
    if bot_response.status_code >= 400 or int(bot_data.get("code", -1)) != 0:
        raise FeishuPermanentError("无法读取飞书机器人信息，请检查应用权限")
    bot = bot_data.get("bot") or {}
    open_id = str(bot.get("open_id") or "").strip()
    if not open_id:
        raise FeishuPermanentError("飞书机器人信息缺少 open_id")
    return {"bot_open_id": open_id, "bot_name": str(bot.get("app_name") or "").strip()}


class FeishuAdapter:
    # 飞书 reaction 会返回远端 reaction_id，重复挂会产生第二个表情，因此重试前必须
    # 先用 find_own_reaction() 回查，不能直接重发。
    reaction_attach_idempotent = False
    reaction_token = FEISHU_REACTION_TOKEN

    def __init__(
        self,
        *,
        token_provider: FeishuTokenProvider | None = None,
        client_factory: Callable[[], httpx.Client] | None = None,
    ):
        self._client_factory = client_factory or (lambda: httpx.Client(timeout=15.0))
        self._tokens = token_provider or FeishuTokenProvider(client_factory=self._client_factory)

    def normalize(self, raw: dict[str, Any]):
        return None

    @staticmethod
    def _uuid(idempotency_key: str, chunk_index: int) -> str:
        return hashlib.sha256(
            f"{idempotency_key}:{chunk_index}".encode("utf-8")
        ).hexdigest()[:40]

    def _request(
        self,
        binding: ChannelBinding,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None,
        body: dict[str, Any] | None,
    ) -> dict[str, Any]:
        force_refresh = False
        for attempt in range(2):
            token = self._tokens.get(binding, force_refresh=force_refresh)
            force_refresh = False
            try:
                with self._client_factory() as client:
                    request_kwargs = {
                        "params": params,
                        "headers": {"Authorization": f"Bearer {token}"},
                    }
                    if method == "POST":
                        response = client.post(url, json=body, **request_kwargs)
                    elif method == "GET":
                        response = client.get(url, **request_kwargs)
                    elif method == "PATCH":
                        response = client.patch(url, json=body, **request_kwargs)
                    else:
                        response = client.delete(url, **request_kwargs)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                raise FeishuTransientError("飞书消息发送暂时失败") from exc
            if response.status_code == 401 and attempt == 0:
                force_refresh = self._tokens.invalidate(binding, expected_token=token)
                continue
            if response.status_code == 429 or response.status_code >= 500:
                raise FeishuTransientError("飞书消息服务暂时不可用")
            if method == "DELETE" and response.status_code == 404:
                return {}
            try:
                data = response.json()
            except ValueError as exc:
                raise FeishuTransientError("飞书消息响应格式无效") from exc
            if response.status_code >= 400:
                raise FeishuPermanentError(f"飞书拒绝消息请求 HTTP {response.status_code}")
            code = int(data.get("code", -1))
            if code in _TOKEN_INVALID_CODES and attempt == 0:
                force_refresh = self._tokens.invalidate(binding, expected_token=token)
                continue
            if code in _TOKEN_INVALID_CODES:
                raise FeishuPermanentError("飞书 token 刷新后仍无效")
            if code in _PERMANENT_MESSAGE_CODES:
                raise FeishuPermanentError(
                    f"飞书拒绝消息请求 code={code}"
                )
            if code != 0:
                raise FeishuTransientError(f"飞书消息服务返回错误 code={code}")
            return data
        raise FeishuPermanentError("飞书 token 刷新后仍无效")

    def _post(
        self,
        binding: ChannelBinding,
        url: str,
        *,
        params: dict[str, str] | None,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        return self._request(binding, "POST", url, params=params, body=body)

    def add_reaction(
        self,
        binding: ChannelBinding,
        target: dict[str, Any],
        emoji_type: str = FEISHU_REACTION_TOKEN,
    ) -> str:
        message_id = str((target or {}).get("message_id") or "").strip()
        emoji_type = str(emoji_type or "").strip()
        if not message_id or not emoji_type:
            raise FeishuPermanentError("飞书 reaction 参数无效")
        data = self._post(
            binding,
            f"{FEISHU_API_BASE}/im/v1/messages/{message_id}/reactions",
            params=None,
            body={"reaction_type": {"emoji_type": emoji_type}},
        )
        reaction_id = str((data.get("data") or {}).get("reaction_id") or "").strip()
        if not reaction_id:
            raise FeishuTransientError("飞书 reaction 响应缺少 reaction_id")
        return reaction_id

    def find_own_reaction(
        self,
        binding: ChannelBinding,
        target: dict[str, Any],
        emoji_type: str = FEISHU_REACTION_TOKEN,
    ) -> str | None:
        message_id = str((target or {}).get("message_id") or "").strip()
        emoji_type = str(emoji_type or "").strip()
        app_id = str((binding.config_json or {}).get("app_id") or "").strip()
        if not message_id or not emoji_type or not app_id:
            raise FeishuPermanentError("飞书 reaction 查询参数无效")
        page_token = ""
        for _ in range(20):
            params = {"reaction_type": emoji_type, "page_size": "50"}
            if page_token:
                params["page_token"] = page_token
            data = self._request(
                binding,
                "GET",
                f"{FEISHU_API_BASE}/im/v1/messages/{message_id}/reactions",
                params=params,
                body=None,
            )
            payload = data.get("data") or {}
            for item in payload.get("items") or []:
                operator = item.get("operator") or {}
                reaction_type = item.get("reaction_type") or {}
                if (
                    operator.get("operator_type") == "app"
                    and str(operator.get("operator_id") or "") == app_id
                    and str(reaction_type.get("emoji_type") or "") == emoji_type
                ):
                    reaction_id = str(item.get("reaction_id") or "").strip()
                    if reaction_id:
                        return reaction_id
            if not payload.get("has_more"):
                return None
            page_token = str(payload.get("page_token") or "").strip()
            if not page_token:
                raise FeishuTransientError("飞书 reaction 分页响应无效")
        raise FeishuTransientError("飞书 reaction 分页数量异常")

    def remove_reaction(
        self,
        binding: ChannelBinding,
        target: dict[str, Any],
        reaction_id: str,
    ) -> None:
        message_id = str(target.get("message_id") or "").strip()
        reaction_id = str(reaction_id or "").strip()
        if not message_id or not reaction_id:
            raise FeishuPermanentError("飞书 reaction 清理参数无效")
        self._request(
            binding,
            "DELETE",
            f"{FEISHU_API_BASE}/im/v1/messages/{message_id}/reactions/{reaction_id}",
            params=None,
            body=None,
        )

    def download_media(
        self,
        binding: ChannelBinding,
        attachment: ChannelInboundAttachment,
        *,
        max_bytes: int = 0,
    ) -> bytes:
        """飞书附件下载:im/v1/messages/{message_id}/resources/{file_key}?type=image|file。

        download_params 中需含 file_key / type / message_id。
        下载返回二进制流,不走 _request(那个解析 JSON),直接使用 response.content。
        复用 FeishuTokenProvider 的 token 刷新重试模式(401 -> invalidate -> 重试一次)。
        max_bytes > 0 时流式读取,超过上限立即中止并抛 ValueError。
        """
        file_key = str(attachment.download_params.get("file_key") or attachment.media_id)
        media_type = str(attachment.download_params.get("type") or "").strip()
        message_id = str(attachment.download_params.get("message_id") or "").strip()
        if not file_key or not media_type or not message_id:
            raise FeishuPermanentError(
                f"飞书附件下载参数缺失: file_key={file_key} type={media_type} message_id={message_id}"
            )
        url = f"{FEISHU_API_BASE}/im/v1/messages/{message_id}/resources/{file_key}"
        force_refresh = False
        for attempt in range(2):
            token = self._tokens.get(binding, force_refresh=force_refresh)
            force_refresh = False
            try:
                with self._client_factory() as client:
                    if max_bytes > 0:
                        status, data = stream_download_with_limit(
                            client, "GET", url,
                            params={"type": media_type},
                            headers={"Authorization": f"Bearer {token}"},
                            max_bytes=max_bytes,
                        )
                        if status == 401 and attempt == 0:
                            force_refresh = self._tokens.invalidate(binding, expected_token=token)
                            continue
                        if status == 429 or status >= 500:
                            raise FeishuTransientError("飞书附件下载服务暂时不可用")
                        if status >= 400:
                            raise FeishuPermanentError(f"飞书拒绝附件下载 HTTP {status}")
                        return data
                    response = client.get(
                        url,
                        params={"type": media_type},
                        headers={"Authorization": f"Bearer {token}"},
                    )
            except ValueError:
                raise
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                raise FeishuTransientError("飞书附件下载暂时失败") from exc
            if response.status_code == 401 and attempt == 0:
                force_refresh = self._tokens.invalidate(binding, expected_token=token)
                continue
            if response.status_code == 429 or response.status_code >= 500:
                raise FeishuTransientError("飞书附件下载服务暂时不可用")
            if response.status_code >= 400:
                raise FeishuPermanentError(f"飞书拒绝附件下载 HTTP {response.status_code}")
            return response.content
        raise FeishuPermanentError("飞书 token 刷新后仍无法下载附件")

    def create_card(
        self,
        binding: ChannelBinding,
        target: dict[str, Any],
        card_json: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> str:
        """发送一张交互式卡片，返回 message_id。

        复用 send() 的目标解析逻辑：有 message_id 走 reply，否则按 receive_id 投递。
        idempotency_key 生成稳定 uuid，保证重试不重复发卡。
        """
        key = str(idempotency_key or "").strip()
        if not key:
            raise FeishuPermanentError("飞书卡片创建缺少幂等键")
        message_id = str(target.get("message_id") or "").strip()
        receive_id = str(target.get("receive_id") or "").strip()
        receive_id_type = str(target.get("receive_id_type") or "").strip()
        if not message_id and (not receive_id or not receive_id_type):
            raise FeishuPermanentError("飞书卡片投递目标无效")
        body: dict[str, Any] = {
            "msg_type": "interactive",
            "content": json.dumps(card_json, ensure_ascii=False),
            "uuid": self._uuid(key, 0),
        }
        if message_id:
            body["reply_in_thread"] = bool(target.get("reply_in_thread"))
            data = self._post(
                binding,
                f"{FEISHU_API_BASE}/im/v1/messages/{message_id}/reply",
                params=None,
                body=body,
            )
        else:
            body["receive_id"] = receive_id
            data = self._post(
                binding,
                f"{FEISHU_API_BASE}/im/v1/messages",
                params={"receive_id_type": receive_id_type},
                body=body,
            )
        created_id = str((data.get("data") or {}).get("message_id") or "").strip()
        if not created_id:
            raise FeishuTransientError("飞书卡片创建响应缺少 message_id")
        return created_id

    def update_card(
        self,
        binding: ChannelBinding,
        message_id: str,
        card_json: dict[str, Any],
    ) -> None:
        """PATCH 更新已发送卡片的 content。"""
        message_id = str(message_id or "").strip()
        if not message_id:
            raise FeishuPermanentError("飞书卡片更新缺少 message_id")
        self._request(
            binding,
            "PATCH",
            f"{FEISHU_API_BASE}/im/v1/messages/{message_id}",
            params=None,
            body={"content": json.dumps(card_json, ensure_ascii=False)},
        )

    def resolve_open_id_by_mobile_or_email(
        self,
        binding: ChannelBinding,
        *,
        mobile: str | None = None,
        email: str | None = None,
    ) -> str | None:
        """飞书通讯录反查 open_id(方案 B):POST /contactv3/users/batch_get_id。

        需要应用具备 contact:user.id:readonly 或 contact:user.base:readonly 权限。
        传入手机号或邮箱,返回首个命中用户的 open_id;未命中或无权限返回 None。
        """
        mobiles = [str(mobile).strip()] if mobile and str(mobile).strip() else []
        emails = [str(email).strip()] if email and str(email).strip() else []
        if not mobiles and not emails:
            return None
        body: dict[str, Any] = {}
        if mobiles:
            body["mobiles"] = mobiles
        if emails:
            body["emails"] = emails
        try:
            data = self._request(
                binding,
                "POST",
                f"{FEISHU_API_BASE}/contact/v3/users/batch_get_id",
                params={"user_id_type": "open_id"},
                body=body,
            )
        except Exception:
            return None
        user_list = (data.get("data") or {}).get("user_list") or []
        for item in user_list:
            open_id = str(item.get("user_id") or "").strip()
            if open_id:
                return open_id
        return None

    def get_user_name(
        self,
        binding: ChannelBinding,
        open_id: str,
    ) -> str | None:
        """通过 open_id 查询飞书用户真实姓名: GET /contact/v3/users/{open_id}。

        需要应用具备 contact:user.base:readonly 权限。
        返回 name 字段;无权限或未命中返回 None。
        """
        open_id = str(open_id or "").strip()
        if not open_id or open_id.startswith("group:"):
            return None
        try:
            data = self._request(
                binding,
                "GET",
                f"{FEISHU_API_BASE}/contact/v3/users/{open_id}",
                params={"user_id_type": "open_id"},
                body=None,
            )
        except Exception:
            return None
        user = (data.get("data") or {}).get("user") or {}
        name = str(user.get("name") or "").strip()
        return name or None

    def send(
        self,
        binding: ChannelBinding,
        target: dict[str, Any],
        text: str,
        *,
        idempotency_key: str | None = None,
    ) -> str | None:
        key = str(idempotency_key or "").strip()
        if not key:
            raise FeishuPermanentError("飞书投递缺少幂等键")
        # echarts 代码块剥离后转飞书卡片 chart 组件;转换失败的块降级为文字行。
        chart_options: list[dict[str, Any]] = []
        if get_settings().channel_feishu_chart_cards_enabled:
            text, chart_options = _extract_echarts_blocks(text)
        vchart_specs: list[dict[str, Any]] = []
        fallback_lines: list[str] = []
        for chart_option in chart_options:
            spec = _echarts_option_to_vchart(chart_option)
            if spec is not None:
                vchart_specs.append(spec)
            else:
                fallback_lines.append(
                    f"📊 图表：{_echarts_option_title(chart_option) or '（未命名）'}"
                    "（请在网页端查看交互版）"
                )
        if fallback_lines:
            text = "\n\n".join(part for part in (text.strip(), "\n".join(fallback_lines)) if part)
        if not str(text or "").strip() and not vchart_specs:
            raise FeishuPermanentError("飞书投递文本不能为空")
        message_id = str(target.get("message_id") or "").strip()
        receive_id = str(target.get("receive_id") or "").strip()
        receive_id_type = str(target.get("receive_id_type") or "").strip()
        if not message_id and (not receive_id or not receive_id_type):
            raise FeishuPermanentError("飞书投递目标无效")
        rich_enabled = bool(get_settings().channel_rich_render_enabled)
        use_rich = rich_enabled and has_markdown(text)
        if not str(text or "").strip():
            chunks = []
        elif use_rich:
            chunks = split_markdown_by_lines(text, CHANNEL_TEXT_LIMIT)
            if not chunks:
                chunks = [text]
        else:
            chunks = split_channel_text(text)
        # 发送单元序列:文字 chunks 在前、图表卡片在后(正文先到图后到)。
        # 全单元统一编号 uuid,重试时飞书按 uuid 去重,与纯文本 chunk 语义一致。
        units: list[tuple[str, Any]] = [("text", chunk) for chunk in chunks]
        units.extend(("card", spec) for spec in vchart_specs)
        # 透传 _post 返回的飞书 message_id,供 outbox 回写 handoff.notify_message_id
        # (阶段 4 关联回复)。多 chunk 时取最后一条(即完整消息的末段)的 message_id。
        created_message_id: str | None = None
        for index, (unit_kind, payload) in enumerate(units):
            if unit_kind == "card":
                body: dict[str, Any] = {
                    "msg_type": "interactive",
                    "content": json.dumps(
                        _build_feishu_chart_card(payload), ensure_ascii=False
                    ),
                    "uuid": self._uuid(key, index),
                }
            elif use_rich:
                post_content = _sanitize_feishu_post_links(render_feishu_post(parse_markdown(payload)))
                body = {
                    "msg_type": "post",
                    "content": json.dumps(post_content, ensure_ascii=False),
                    "uuid": self._uuid(key, index),
                }
            else:
                body = {
                    "msg_type": "text",
                    "content": json.dumps({"text": payload}, ensure_ascii=False),
                    "uuid": self._uuid(key, index),
                }
            if message_id:
                body["reply_in_thread"] = bool(target.get("reply_in_thread"))
                data = self._post(
                    binding,
                    f"{FEISHU_API_BASE}/im/v1/messages/{message_id}/reply",
                    params=None,
                    body=body,
                )
            else:
                body["receive_id"] = receive_id
                data = self._post(
                    binding,
                    f"{FEISHU_API_BASE}/im/v1/messages",
                    params={"receive_id_type": receive_id_type},
                    body=body,
                )
            created_message_id = str((data.get("data") or {}).get("message_id") or "").strip() or None
        return created_message_id

    def upload_file(
        self,
        binding: ChannelBinding,
        *,
        filename: str,
        data: bytes,
    ) -> str:
        """上传文件到飞书拿 file_key(POST /im/v1/files,multipart)。

        独立于 _request(JSON only);token 获取/401 刷新/错误映射保持同构。
        """

        name = filename or "artifact"
        url = f"{FEISHU_API_BASE}/im/v1/files"
        force_refresh = False
        for attempt in range(2):
            token = self._tokens.get(binding, force_refresh=force_refresh)
            force_refresh = False
            try:
                with self._client_factory() as client:
                    response = client.post(
                        url,
                        headers={"Authorization": f"Bearer {token}"},
                        data={"file_type": "stream", "file_name": name},
                        files={"file": (name, data)},
                    )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                raise FeishuTransientError("飞书文件上传暂时失败") from exc
            if response.status_code == 401 and attempt == 0:
                force_refresh = self._tokens.invalidate(binding, expected_token=token)
                continue
            if response.status_code == 429 or response.status_code >= 500:
                raise FeishuTransientError("飞书文件服务暂时不可用")
            try:
                payload = response.json()
            except ValueError as exc:
                raise FeishuTransientError("飞书文件上传响应格式无效") from exc
            if response.status_code >= 400:
                raise FeishuPermanentError(f"飞书拒绝文件上传 HTTP {response.status_code}")
            code = int(payload.get("code", -1))
            if code in _TOKEN_INVALID_CODES and attempt == 0:
                force_refresh = self._tokens.invalidate(binding, expected_token=token)
                continue
            if code != 0:
                raise FeishuPermanentError(f"飞书文件上传失败 code={code}")
            file_key = str(
                ((payload.get("data") or {}).get("file_key") or "").strip()
            )
            if not file_key:
                raise FeishuPermanentError("飞书文件上传未返回 file_key")
            return file_key
        raise FeishuPermanentError("飞书文件上传鉴权失败")

    def send_file(
        self,
        binding: ChannelBinding,
        target: dict[str, Any],
        *,
        file_key: str,
        idempotency_key: str | None = None,
    ) -> str | None:
        """按 msg_type=file 发送文件消息;target 解析与 send() 一致。"""

        key = str(idempotency_key or "").strip()
        if not str(file_key or "").strip():
            raise FeishuPermanentError("飞书文件投递缺少 file_key")
        if not key:
            raise FeishuPermanentError("飞书文件投递缺少幂等键")
        message_id = str(target.get("message_id") or "").strip()
        receive_id = str(target.get("receive_id") or "").strip()
        receive_id_type = str(target.get("receive_id_type") or "").strip()
        if not message_id and (not receive_id or not receive_id_type):
            raise FeishuPermanentError("飞书投递目标无效")
        body: dict[str, Any] = {
            "msg_type": "file",
            "content": json.dumps({"file_key": file_key}, ensure_ascii=False),
            "uuid": self._uuid(key, 0),
        }
        if message_id:
            body["reply_in_thread"] = bool(target.get("reply_in_thread"))
            data = self._post(
                binding,
                f"{FEISHU_API_BASE}/im/v1/messages/{message_id}/reply",
                params=None,
                body=body,
            )
        else:
            body["receive_id"] = receive_id
            data = self._post(
                binding,
                f"{FEISHU_API_BASE}/im/v1/messages",
                params={"receive_id_type": receive_id_type},
                body=body,
            )
        return str((data.get("data") or {}).get("message_id") or "").strip() or None

    def start_ingress(self, binding_id: str) -> None:
        from app.channels import get_feishu_process_manager

        get_feishu_process_manager().ensure_binding(binding_id)

    def stop_ingress(self, binding_id: str) -> None:
        from app.channels import get_feishu_process_manager

        get_feishu_process_manager().stop_binding(binding_id)


register_channel_adapter("feishu", FeishuAdapter())
