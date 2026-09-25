"""Provider-neutral chat completion clients."""

from __future__ import annotations

import http.client
import functools
import ipaddress
import json
import secrets
import socket
import ssl
import struct
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Iterator, Protocol, Sequence
from urllib.parse import urlsplit

import certifi


DEEPSEEK_PRESET = {
    "provider": "openai_compatible",
    "wire_api": "chat_completions",
    "base_url": "https://api.deepseek.com/v1/chat/completions",
    # DeepSeek's current account-visible 4.1 Flash endpoint is exposed under
    # the short ``deepseek-flash`` id.  Keep both roles on that live model so
    # a preset reset does not revive the retired Pro/old Flash names.
    "quality_model": "deepseek-flash",
    "fast_model": "deepseek-flash",
    "dns_mode": "domestic",
    "dns_servers": ["223.5.5.5", "119.29.29.29"],
}

ANTHROPIC_PRESET = {
    "provider": "anthropic",
    "wire_api": "messages",
    "base_url": "https://api.anthropic.com/v1",
    "quality_model": "claude-opus-5",
    "fast_model": "claude-haiku-4-5-20251001",
    "reasoning_effort": "medium",
    "dns_mode": "proxy",
    "dns_servers": ["223.5.5.5", "119.29.29.29"],
}

DNS_MODES = {"domestic", "system", "proxy"}
WIRE_APIS = {"chat_completions", "responses", "messages"}
DEFAULT_DOMESTIC_DNS_SERVERS = ("223.5.5.5", "119.29.29.29")
_SYNTHETIC_IPV4_RANGES = (
    ipaddress.ip_network("28.0.0.0/8"),
    ipaddress.ip_network("198.18.0.0/15"),
)


class CancellationToken(Protocol):
    def is_cancelled(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class CallbackCancellationToken:
    """Adapt a durable-job cancellation callback to the Provider protocol."""

    callback: Callable[[], bool]

    def is_cancelled(self) -> bool:
        return bool(self.callback())


class AIProviderError(RuntimeError):
    """A sanitized provider failure suitable for API error mapping."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        attempts: int = 0,
        duration_ms: int = 0,
        error_class: str | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.attempts = max(0, int(attempts))
        self.duration_ms = max(0, int(duration_ms))
        self.error_class = str(error_class or code).strip().casefold()


class DNSResolutionError(ConnectionError):
    """Raised when the selected application DNS strategy has no usable answer."""


class TLSConfigurationError(ConnectionError):
    """Raised when the bundled certificate authority store cannot be loaded."""


@dataclass(frozen=True)
class ChatResponse:
    content: str
    model: str
    request_id: str | None = None
    usage: dict | None = None
    finish_reason: str | None = None
    duration_ms: int = 0
    attempts: int = 1


@dataclass(frozen=True)
class ChatStreamEvent:
    """One provider-neutral event from a streamed chat completion."""

    kind: str
    text: str = ""
    response: ChatResponse | None = None
    error: AIProviderError | None = None


_RESPONSE_CHUNK_BYTES = 64 * 1024


def _iter_sse_records(
    response,
    *,
    cancellation: CancellationToken | None,
    maximum_bytes: int,
) -> Iterator[tuple[str, str]]:
    """Parse one SSE response without retaining raw provider events."""

    event_name = "message"
    data_lines: list[str] = []
    consumed = 0
    while True:
        if cancellation and cancellation.is_cancelled():
            raise AIProviderError(
                "CANCELLED", "AI 请求已取消", error_class="cancelled"
            )
        raw = response.readline()
        if not raw:
            if data_lines:
                yield event_name, "\n".join(data_lines)
            return
        if not isinstance(raw, (bytes, bytearray)):
            raise AIProviderError(
                "INVALID_RESPONSE",
                "AI 流式响应格式异常，请稍后重试",
                retryable=True,
                error_class="invalid_stream",
            )
        consumed += len(raw)
        if consumed > maximum_bytes:
            raise AIProviderError(
                "INVALID_RESPONSE",
                "AI 流式响应超过长度上限",
                retryable=True,
                error_class="response_too_large",
            )
        try:
            line = bytes(raw).decode("utf-8").rstrip("\r\n")
        except UnicodeDecodeError as exc:
            raise AIProviderError(
                "INVALID_RESPONSE",
                "AI 流式响应编码异常，请稍后重试",
                retryable=True,
                error_class="invalid_stream_encoding",
            ) from exc
        if not line:
            if data_lines:
                yield event_name, "\n".join(data_lines)
            event_name = "message"
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if field == "event":
            event_name = value or "message"
        elif field == "data":
            data_lines.append(value)


def validate_dns_mode(value: object) -> str:
    mode = str(value or "domestic").strip().casefold()
    if mode not in DNS_MODES:
        raise ValueError("DNS 解析模式无效")
    return mode


def validate_wire_api(value: object) -> str:
    wire_api = str(value or "chat_completions").strip().casefold()
    if wire_api not in WIRE_APIS:
        raise ValueError("AI 通信协议无效")
    return wire_api


def validate_dns_servers(value: object) -> tuple[str, ...]:
    if value is None:
        values = list(DEFAULT_DOMESTIC_DNS_SERVERS)
    elif isinstance(value, str):
        values = [item.strip() for item in value.replace("；", ",").split(",")]
    elif isinstance(value, (list, tuple)):
        values = [str(item).strip() for item in value]
    else:
        raise ValueError("DNS 服务器必须是逗号分隔的 IPv4 地址")
    values = [item for item in values if item]
    if not 1 <= len(values) <= 3:
        raise ValueError("请配置 1 至 3 个 DNS 服务器")
    normalized: list[str] = []
    for item in values:
        try:
            address = ipaddress.ip_address(item)
        except ValueError as exc:
            raise ValueError("DNS 服务器必须是有效 IPv4 地址") from exc
        if address.version != 4 or address.is_unspecified or address.is_multicast:
            raise ValueError("DNS 服务器必须是有效 IPv4 地址")
        rendered = str(address)
        if rendered not in normalized:
            normalized.append(rendered)
    if not normalized:
        raise ValueError("至少需要一个 DNS 服务器")
    return tuple(normalized)


def _explicit_loopback_host(hostname: str) -> bool:
    if hostname.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _public_provider_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return address.is_global and not any(
        address.version == network.version and address in network
        for network in _SYNTHETIC_IPV4_RANGES
    )


def _validate_resolved_addresses(hostname: str, addresses: Sequence[str]) -> str:
    """Return one pinned address only when the full DNS answer is trustworthy."""

    parsed = []
    try:
        parsed = [ipaddress.ip_address(value) for value in addresses]
    except ValueError as exc:
        raise DNSResolutionError("AI service resolved to an invalid address") from exc
    if not parsed:
        raise DNSResolutionError("AI service hostname has no usable address")
    if _explicit_loopback_host(hostname):
        if not all(address.is_loopback for address in parsed):
            raise DNSResolutionError("localhost resolved outside the loopback network")
    elif not all(_public_provider_address(address) for address in parsed):
        raise DNSResolutionError("AI service resolved to a non-public address")
    return str(parsed[0])


def _resolve_system_addresses(hostname: str, port: int) -> list[str]:
    try:
        records = socket.getaddrinfo(
            hostname, port, type=socket.SOCK_STREAM
        )
    except OSError as exc:
        raise DNSResolutionError("system DNS returned no usable address") from exc
    values: list[str] = []
    for record in records:
        address = str(record[4][0])
        if address not in values:
            values.append(address)
    return values


def _dns_name(hostname: str) -> bytes:
    labels = hostname.rstrip(".").split(".")
    encoded = bytearray()
    for label in labels:
        raw = label.encode("idna")
        if not raw or len(raw) > 63:
            raise ValueError("AI 服务主机名无效")
        encoded.append(len(raw))
        encoded.extend(raw)
    encoded.append(0)
    return bytes(encoded)


def _skip_dns_name(packet: bytes, offset: int) -> int:
    while True:
        if offset >= len(packet):
            raise ValueError("DNS 响应截断")
        length = packet[offset]
        if length & 0xC0 == 0xC0:
            if offset + 1 >= len(packet):
                raise ValueError("DNS 响应截断")
            return offset + 2
        if length == 0:
            return offset + 1
        if length & 0xC0 or offset + 1 + length > len(packet):
            raise ValueError("DNS 响应名称无效")
        offset += 1 + length


def _parse_dns_ipv4(packet: bytes, transaction_id: int) -> list[str]:
    if len(packet) < 12:
        raise ValueError("DNS 响应截断")
    received_id, flags, questions, answers, authorities, additional = struct.unpack(
        "!HHHHHH", packet[:12]
    )
    if received_id != transaction_id or not flags & 0x8000:
        raise ValueError("DNS 响应不匹配")
    if flags & 0x0200:
        raise ValueError("DNS 响应需要 TCP，当前服务器不可用")
    if flags & 0x000F:
        raise ValueError("DNS 服务器未返回有效记录")
    offset = 12
    for _ in range(questions):
        offset = _skip_dns_name(packet, offset)
        if offset + 4 > len(packet):
            raise ValueError("DNS 响应截断")
        offset += 4
    resolved: list[str] = []
    for _ in range(answers + authorities + additional):
        offset = _skip_dns_name(packet, offset)
        if offset + 10 > len(packet):
            raise ValueError("DNS 响应截断")
        record_type, record_class, _ttl, length = struct.unpack(
            "!HHIH", packet[offset : offset + 10]
        )
        offset += 10
        if offset + length > len(packet):
            raise ValueError("DNS 响应截断")
        if record_type == 1 and record_class == 1 and length == 4:
            address = str(ipaddress.ip_address(packet[offset : offset + 4]))
            parsed = ipaddress.ip_address(address)
            if not any(parsed in network for network in _SYNTHETIC_IPV4_RANGES):
                resolved.append(address)
        offset += length
    return resolved


def resolve_ipv4_with_servers(
    hostname: str,
    dns_servers: tuple[str, ...],
    *,
    timeout: float = 2.0,
) -> str:
    if hostname.casefold() == "localhost":
        return "127.0.0.1"
    try:
        return str(ipaddress.ip_address(hostname))
    except ValueError:
        pass
    query_name = _dns_name(hostname)
    for server in dns_servers:
        transaction_id = secrets.randbelow(65536)
        packet = struct.pack("!HHHHHH", transaction_id, 0x0100, 1, 0, 0, 0)
        packet += query_name + struct.pack("!HH", 1, 1)
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
                client.settimeout(timeout)
                client.sendto(packet, (server, 53))
                response, source = client.recvfrom(4096)
            if source[0] != server:
                continue
            addresses = _parse_dns_ipv4(response, transaction_id)
            if addresses:
                return addresses[0]
        except (OSError, ValueError):
            continue
    raise DNSResolutionError("configured DNS resolvers returned no usable address")


class _ResolvedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, *, resolved_ip: str, **kwargs):
        self._resolved_ip = resolved_ip
        super().__init__(host, **kwargs)

    def connect(self):
        self.sock = self._create_connection(
            (self._resolved_ip, self.port),
            self.timeout,
            self.source_address,
        )
        if self._tunnel_host:
            self._tunnel()
        server_hostname = self._tunnel_host or self.host
        self.sock = self._context.wrap_socket(
            self.sock,
            server_hostname=server_hostname,
        )


def _verified_ssl_context() -> ssl.SSLContext:
    """Use the bundled CA file instead of enumerating the Windows cert store."""

    try:
        return ssl.create_default_context(cafile=certifi.where())
    except (OSError, ssl.SSLError) as exc:
        raise TLSConfigurationError("bundled CA certificate store is unavailable") from exc


class _ResolvedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, resolved_ip: str, *, context: ssl.SSLContext):
        super().__init__(context=context)
        self._resolved_ip = resolved_ip

    def https_open(self, request):
        connection = functools.partial(
            _ResolvedHTTPSConnection,
            resolved_ip=self._resolved_ip,
        )
        return self.do_open(
            connection,
            request,
            context=self._context,
            check_hostname=self._check_hostname,
        )


class _ResolvedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host, *, resolved_ip: str, **kwargs):
        self._resolved_ip = resolved_ip
        super().__init__(host, **kwargs)

    def connect(self):
        self.sock = self._create_connection(
            (self._resolved_ip, self.port),
            self.timeout,
            self.source_address,
        )
        if self._tunnel_host:
            self._tunnel()


class _ResolvedHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, resolved_ip: str):
        super().__init__()
        self._resolved_ip = resolved_ip

    def http_open(self, request):
        connection = functools.partial(
            _ResolvedHTTPConnection,
            resolved_ip=self._resolved_ip,
        )
        return self.do_open(connection, request)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def direct_urlopen(
    request,
    *,
    timeout: float,
    dns_mode: str = "domestic",
    dns_servers: object = None,
):
    """Open one AI request directly with a user-selectable DNS strategy."""
    mode = validate_dns_mode(dns_mode)
    servers = validate_dns_servers(dns_servers)
    parsed = urlsplit(request.full_url)
    if mode == "proxy":
        # Explicit opt-in for Windows proxy/TUN stacks such as Clash Fake-IP.
        # URL validation still blocks literal private-network targets.
        handlers: list[object] = [_NoRedirectHandler()]
        if parsed.scheme == "https":
            handlers.append(
                urllib.request.HTTPSHandler(context=_verified_ssl_context())
            )
        elif parsed.scheme != "http":
            raise DNSResolutionError("AI service protocol is invalid")
        opener = urllib.request.build_opener(*handlers)
        return opener.open(request, timeout=timeout)
    hostname = parsed.hostname
    if not hostname:
        raise DNSResolutionError("AI service hostname is missing")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise DNSResolutionError("AI service port is invalid") from exc
    if mode == "domestic":
        addresses = [resolve_ipv4_with_servers(hostname, servers)]
    else:
        addresses = _resolve_system_addresses(hostname, port)
    resolved_ip = _validate_resolved_addresses(hostname, addresses)
    handlers: list[object] = [
        urllib.request.ProxyHandler({}),
        _NoRedirectHandler(),
    ]
    if parsed.scheme == "https":
        handlers.append(
            _ResolvedHTTPSHandler(
                resolved_ip,
                context=_verified_ssl_context(),
            )
        )
    elif parsed.scheme == "http":
        handlers.append(_ResolvedHTTPHandler(resolved_ip))
    else:
        raise DNSResolutionError("AI service protocol is invalid")
    opener = urllib.request.build_opener(*handlers)
    return opener.open(request, timeout=timeout)


def _read_response_body(response, *, maximum_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(_RESPONSE_CHUNK_BYTES)
        if not chunk:
            return b"".join(chunks)
        if not isinstance(chunk, bytes):
            raise AIProviderError(
                "INVALID_RESPONSE",
                "AI 服务返回格式异常，请稍后重试",
                error_class="invalid_response",
            )
        total += len(chunk)
        if total > maximum_bytes:
            raise AIProviderError(
                "INVALID_RESPONSE",
                "AI 服务响应超过安全长度",
                retryable=True,
                error_class="response_too_large",
            )
        chunks.append(chunk)


def _close_response_safely(response: object | None) -> None:
    """Close an HTTP response without changing the request's outcome."""

    if response is None:
        return
    close = getattr(response, "close", None)
    if not callable(close):
        return
    try:
        close()
    except Exception:
        # Closing is best-effort here: it must never replace a provider error
        # or turn an otherwise valid response into a failed business request.
        pass


def _network_error_class(error: BaseException) -> str:
    candidate = error.reason if isinstance(error, urllib.error.URLError) else error
    if isinstance(candidate, DNSResolutionError):
        return "dns_resolution"
    if isinstance(candidate, (TLSConfigurationError, ssl.SSLError)):
        return "tls"
    if isinstance(candidate, http.client.IncompleteRead):
        return "incomplete_read"
    if isinstance(candidate, http.client.RemoteDisconnected):
        return "remote_disconnected"
    if isinstance(candidate, (TimeoutError, socket.timeout)):
        return "timeout"
    if isinstance(candidate, ConnectionResetError):
        return "connection_reset"
    if isinstance(candidate, ConnectionAbortedError):
        return "connection_aborted"
    if isinstance(candidate, BrokenPipeError):
        return "broken_pipe"
    if isinstance(candidate, ConnectionError):
        return "connection_error"
    if isinstance(candidate, http.client.HTTPException):
        return "http_connection"
    return "url_error"


class AIProvider(Protocol):
    def list_models(self, *, timeout: float = 30) -> list[dict]: ...

    def stream_complete(
        self,
        messages: Sequence[dict],
        *,
        model: str,
        temperature: float,
        timeout: float,
        max_attempts: int,
        max_tokens: int | None = None,
        thinking: dict | None = None,
        reasoning_effort: str | None = None,
        response_format: dict | None = None,
        task_policy: str | None = None,
        cancellation: CancellationToken | None = None,
    ) -> Iterator[ChatStreamEvent]: ...

    def complete(
        self,
        messages: Sequence[dict],
        *,
        model: str,
        temperature: float,
        timeout: float,
        max_attempts: int,
        max_tokens: int | None = None,
        thinking: dict | None = None,
        reasoning_effort: str | None = None,
        response_format: dict | None = None,
        task_policy: str | None = None,
        cancellation: CancellationToken | None = None,
    ) -> ChatResponse: ...


def validate_provider_url(value: object) -> str:
    url = str(value or "").strip()
    parsed = urlsplit(url)
    hostname = parsed.hostname or ""
    loopback = _explicit_loopback_host(hostname)
    if (
        parsed.scheme not in ({"https"} if not loopback else {"http", "https"})
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("AI 服务地址必须是 HTTPS URL（本机服务可使用 HTTP）")
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None and not (literal.is_loopback or _public_provider_address(literal)):
        raise ValueError("AI 服务地址不能指向非公开网络")
    return url


def openai_chat_completions_url(value: object) -> str:
    """Resolve an OpenAI-compatible base URL to the chat-completions endpoint."""

    url = validate_provider_url(value)
    parsed = urlsplit(url)
    path = parsed.path.rstrip("/")
    if path.endswith("/chat/completions"):
        return url
    if not path:
        path = "/v1"
    return parsed._replace(path=f"{path}/chat/completions").geturl()


def openai_responses_url(value: object) -> str:
    """Resolve an OpenAI-compatible base URL to the Responses endpoint."""

    url = validate_provider_url(value)
    parsed = urlsplit(url)
    path = parsed.path.rstrip("/")
    for suffix in ("/chat/completions", "/responses"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    if not path:
        path = "/v1"
    return parsed._replace(path=f"{path}/responses").geturl()


def openai_responses_input_tokens_url(value: object) -> str:
    """Resolve a Responses base URL to its non-generating token-count endpoint."""

    responses = openai_responses_url(value)
    parsed = urlsplit(responses)
    return parsed._replace(path=f"{parsed.path.rstrip('/')}/input_tokens").geturl()


def provider_models_url(value: object) -> str:
    """Resolve a provider base or completion URL to its models endpoint."""

    url = validate_provider_url(value)
    parsed = urlsplit(url)
    path = parsed.path.rstrip("/")
    for suffix in ("/chat/completions", "/responses", "/messages"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    if not path:
        path = "/v1"
    return parsed._replace(path=f"{path}/models").geturl()


def anthropic_messages_url(value: object) -> str:
    """Resolve an Anthropic base URL to the Messages endpoint."""

    url = validate_provider_url(value)
    parsed = urlsplit(url)
    path = parsed.path.rstrip("/")
    if path.endswith("/messages"):
        return url
    if not path:
        path = "/v1"
    return parsed._replace(path=f"{path}/messages").geturl()


def _responses_output_text(body: object) -> str:
    if not isinstance(body, dict):
        return ""
    output_text = body.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text
    parts: list[str] = []
    output = body.get("output")
    if not isinstance(output, list):
        return ""
    for item in output:
        if not isinstance(item, dict) or not isinstance(item.get("content"), list):
            continue
        for content in item["content"]:
            if (
                isinstance(content, dict)
                and content.get("type") == "output_text"
                and isinstance(content.get("text"), str)
            ):
                parts.append(content["text"])
    return "".join(parts)


def _responses_usage(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    usage = dict(value)
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    if isinstance(input_tokens, int) and not isinstance(input_tokens, bool):
        usage.setdefault("prompt_tokens", input_tokens)
    if isinstance(output_tokens, int) and not isinstance(output_tokens, bool):
        usage.setdefault("completion_tokens", output_tokens)
    input_details = usage.get("input_tokens_details")
    if isinstance(input_details, dict):
        cached_tokens = input_details.get("cached_tokens")
        if isinstance(cached_tokens, int) and not isinstance(cached_tokens, bool):
            usage.setdefault("cached_tokens", cached_tokens)
    output_details = usage.get("output_tokens_details")
    if isinstance(output_details, dict):
        usage.setdefault("completion_tokens_details", dict(output_details))
        reasoning_tokens = output_details.get("reasoning_tokens")
        if isinstance(reasoning_tokens, int) and not isinstance(reasoning_tokens, bool):
            usage.setdefault("reasoning_tokens", reasoning_tokens)
    return usage


def _is_reasoning_model(model: str, reasoning_effort: str | None) -> bool:
    if str(reasoning_effort or "").strip():
        return True
    normalized = str(model or "").strip().casefold()
    return normalized.startswith(("gpt-5", "o1", "o3", "o4"))


def _responses_request_body(
    messages: Sequence[dict],
    *,
    model: str,
    temperature: float,
    stream: bool,
    max_tokens: int | None,
    reasoning_effort: str | None,
    response_format: dict | None,
) -> dict[str, object]:
    request_body: dict[str, object] = {
        "model": model,
        "input": list(messages),
        "store": False,
    }
    if stream:
        request_body["stream"] = True
    if not _is_reasoning_model(model, reasoning_effort):
        request_body["temperature"] = temperature
    if max_tokens is not None:
        request_body["max_output_tokens"] = max_tokens
    if reasoning_effort is not None:
        request_body["reasoning"] = {"effort": str(reasoning_effort)}
    if response_format is not None:
        request_body["text"] = {"format": dict(response_format)}
    return request_body


def _validated_model_items(body: object) -> list[dict]:
    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        raise AIProviderError(
            "INVALID_RESPONSE",
            "AI 服务返回的模型列表格式异常",
            error_class="invalid_model_list",
        )
    models: list[dict] = []
    seen: set[str] = set()
    for item in body["data"][:1000]:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or "").strip()
        if (
            not model_id
            or len(model_id) > 200
            or "\r" in model_id
            or "\n" in model_id
            or model_id in seen
        ):
            continue
        seen.add(model_id)
        display_name = str(item.get("display_name") or model_id).strip()
        if not display_name or len(display_name) > 200:
            display_name = model_id
        models.append(
            {
                "id": model_id,
                "display_name": display_name,
                "created_at": str(item.get("created_at") or "")[:40],
            }
        )
    return models


class OpenAICompatibleProvider:
    """Minimal OpenAI-compatible chat completions provider."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        urlopen: Callable | None = None,
        sleep: Callable[[float], None] = time.sleep,
        dns_mode: str = "domestic",
        dns_servers: object = None,
        max_response_bytes: int = 8 * 1024 * 1024,
    ):
        try:
            self.base_url = validate_provider_url(base_url)
            self.wire_api = validate_wire_api(
                getattr(self, "wire_api", "chat_completions")
            )
            if self.wire_api == "chat_completions":
                self._request_url = openai_chat_completions_url(self.base_url)
            elif self.wire_api == "responses":
                self._request_url = openai_responses_url(self.base_url)
            else:
                raise ValueError("OpenAI 兼容 Provider 不支持该通信协议")
            self._models_url = provider_models_url(self.base_url)
        except ValueError as exc:
            raise AIProviderError("CONFIGURATION", str(exc)) from exc
        self._api_key = str(api_key or "").strip()
        if not self._api_key:
            raise AIProviderError("CONFIGURATION", "未配置 AI API 密钥")
        try:
            self._dns_mode = validate_dns_mode(dns_mode)
            self._dns_servers = validate_dns_servers(dns_servers)
        except ValueError as exc:
            raise AIProviderError("CONFIGURATION", str(exc)) from exc
        self._urlopen = urlopen or functools.partial(
            direct_urlopen,
            dns_mode=self._dns_mode,
            dns_servers=self._dns_servers,
        )
        self._sleep = sleep
        if (
            isinstance(max_response_bytes, bool)
            or not isinstance(max_response_bytes, int)
            or max_response_bytes < _RESPONSE_CHUNK_BYTES
        ):
            raise AIProviderError(
                "CONFIGURATION", "AI 响应长度上限无效"
            )
        self._max_response_bytes = max_response_bytes

    def __repr__(self) -> str:
        return f"{type(self).__name__}(base_url={self.base_url!r}, api_key='[hidden]')"

    @staticmethod
    def _cancelled(cancellation: CancellationToken | None) -> bool:
        return bool(cancellation and cancellation.is_cancelled())

    def list_models(self, *, timeout: float = 30) -> list[dict]:
        request = urllib.request.Request(
            self._models_url,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
            method="GET",
        )
        response = None
        try:
            try:
                response = self._urlopen(request, timeout=float(timeout))
            except urllib.error.HTTPError as exc:
                response = exc
                raise
            raw = _read_response_body(
                response, maximum_bytes=min(self._max_response_bytes, 4 * 1024 * 1024)
            )
            try:
                body = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise AIProviderError(
                    "INVALID_RESPONSE",
                    "AI 服务返回的模型列表格式异常",
                    error_class="invalid_model_list",
                ) from exc
            return _validated_model_items(body)
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403}:
                raise AIProviderError(
                    "AUTHENTICATION",
                    "AI 服务认证失败，请检查已保存的 API 密钥",
                    error_class="authentication",
                ) from None
            retryable = exc.code == 429 or 500 <= exc.code < 600
            raise AIProviderError(
                "RATE_LIMITED" if exc.code == 429 else "PROVIDER_ERROR",
                f"AI 模型列表暂时不可用（HTTP {exc.code}）",
                retryable=retryable,
                error_class="rate_limited" if exc.code == 429 else "http_error",
            ) from None
        except (
            urllib.error.URLError,
            TimeoutError,
            socket.timeout,
            ssl.SSLError,
            ConnectionError,
            http.client.HTTPException,
        ) as exc:
            raise AIProviderError(
                "NETWORK",
                "AI 模型列表连接失败，请检查网络设置",
                retryable=True,
                error_class=_network_error_class(exc),
            ) from None
        finally:
            _close_response_safely(response)

    def count_response_input_tokens(
        self, body: dict, *, timeout: float = 30
    ) -> dict:
        """Count one frozen Responses input without creating a response."""

        if self.wire_api != "responses":
            raise AIProviderError(
                "UNSUPPORTED_CAPABILITY",
                "当前 AI 通信协议不支持 Responses 输入计数",
                error_class="input_token_count_unsupported",
            )
        allowed = {"model", "input", "reasoning", "text"}
        if (
            not isinstance(body, dict)
            or set(body) != allowed
            or not str(body.get("model") or "").strip()
            or not isinstance(body.get("input"), list)
            or not body["input"]
            or not isinstance(body.get("reasoning"), dict)
            or not isinstance(body.get("text"), dict)
        ):
            raise AIProviderError(
                "CONFIGURATION",
                "Responses 输入计数请求无效",
                error_class="input_token_count_request_invalid",
            )
        try:
            resolved_timeout = float(timeout)
        except (TypeError, ValueError) as exc:
            raise AIProviderError(
                "CONFIGURATION",
                "Responses 输入计数超时无效",
                error_class="input_token_count_timeout_invalid",
            ) from exc
        if resolved_timeout <= 0:
            raise AIProviderError(
                "CONFIGURATION",
                "Responses 输入计数超时必须大于 0",
                error_class="input_token_count_timeout_invalid",
            )
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            openai_responses_input_tokens_url(self.base_url),
            data=payload,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
            method="POST",
        )
        response = None
        started = time.monotonic()
        try:
            try:
                response = self._urlopen(request, timeout=resolved_timeout)
            except urllib.error.HTTPError as exc:
                response = exc
                raise
            raw = _read_response_body(
                response,
                maximum_bytes=min(self._max_response_bytes, 1024 * 1024),
            )
            try:
                value = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise AIProviderError(
                    "INVALID_RESPONSE",
                    "AI 服务返回的输入计数格式异常",
                    error_class="input_token_count_invalid_json",
                ) from exc
            count = value.get("input_tokens") if isinstance(value, dict) else None
            object_type = value.get("object") if isinstance(value, dict) else None
            if (
                isinstance(count, bool)
                or not isinstance(count, int)
                or count < 0
                or object_type not in (None, "response.input_tokens")
                or set(value).difference({"input_tokens", "object"})
            ):
                raise AIProviderError(
                    "INVALID_RESPONSE",
                    "AI 服务返回的输入计数结构不受支持",
                    error_class="input_token_count_invalid_shape",
                )
            return {
                "input_tokens": count,
                "object": object_type,
                "endpoint_kind": "responses_input_tokens",
                "duration_ms": max(
                    0, round((time.monotonic() - started) * 1000)
                ),
            }
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403}:
                raise AIProviderError(
                    "AUTHENTICATION",
                    "AI 服务输入计数认证失败",
                    error_class="input_token_count_authentication",
                ) from None
            if exc.code in {404, 405, 501}:
                raise AIProviderError(
                    "UNSUPPORTED_CAPABILITY",
                    "当前 AI 服务未提供 Responses 输入计数接口",
                    error_class="input_token_count_unsupported",
                ) from None
            retryable = exc.code == 429 or 500 <= exc.code < 600
            raise AIProviderError(
                "RATE_LIMITED" if exc.code == 429 else "PROVIDER_ERROR",
                f"AI 服务输入计数失败（HTTP {exc.code}）",
                retryable=retryable,
                error_class=(
                    "input_token_count_rate_limited"
                    if exc.code == 429
                    else "input_token_count_incompatible"
                    if exc.code in {400, 415, 422}
                    else "input_token_count_http_error"
                ),
            ) from None
        except AIProviderError:
            raise
        except (
            urllib.error.URLError,
            TimeoutError,
            socket.timeout,
            ssl.SSLError,
            ConnectionError,
            http.client.HTTPException,
        ) as exc:
            raise AIProviderError(
                "NETWORK",
                "AI 服务输入计数连接失败",
                retryable=True,
                error_class=f"input_token_count_{_network_error_class(exc)}",
            ) from None
        finally:
            _close_response_safely(response)

    def stream_complete(
        self,
        messages: Sequence[dict],
        *,
        model: str,
        temperature: float = 0.7,
        timeout: float = 180,
        max_attempts: int = 3,
        max_tokens: int | None = None,
        thinking: dict | None = None,
        reasoning_effort: str | None = None,
        response_format: dict | None = None,
        task_policy: str | None = None,
        cancellation: CancellationToken | None = None,
    ) -> Iterator[ChatStreamEvent]:
        del task_policy
        resolved_model = str(model or "").strip()
        if not resolved_model:
            yield ChatStreamEvent(
                "error", error=AIProviderError("CONFIGURATION", "未配置 AI 模型")
            )
            return
        try:
            timeout = float(timeout)
            attempts = max(1, int(max_attempts))
        except (TypeError, ValueError):
            yield ChatStreamEvent(
                "error", error=AIProviderError("CONFIGURATION", "AI 请求参数无效")
            )
            return
        if timeout <= 0:
            yield ChatStreamEvent(
                "error",
                error=AIProviderError("CONFIGURATION", "AI 请求超时必须大于 0"),
            )
            return

        if max_tokens is not None:
            try:
                resolved_max_tokens = max(1, int(max_tokens))
            except (TypeError, ValueError):
                yield ChatStreamEvent(
                    "error", error=AIProviderError("CONFIGURATION", "AI 输出上限无效")
                )
                return
        else:
            resolved_max_tokens = None
        if self.wire_api == "responses":
            request_body = _responses_request_body(
                messages,
                model=resolved_model,
                temperature=temperature,
                stream=True,
                max_tokens=resolved_max_tokens,
                reasoning_effort=reasoning_effort,
                response_format=response_format,
            )
        else:
            request_body = {
                "model": resolved_model,
                "messages": list(messages),
                "temperature": temperature,
                "stream": True,
            }
            if resolved_max_tokens is not None:
                request_body["max_tokens"] = resolved_max_tokens
            if thinking is not None:
                request_body["thinking"] = thinking
            if reasoning_effort is not None:
                request_body["reasoning_effort"] = str(reasoning_effort)
            if response_format is not None:
                request_body["response_format"] = response_format
        payload = json.dumps(request_body, ensure_ascii=False).encode("utf-8")
        headers = {
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }
        started = time.monotonic()

        def elapsed_ms() -> int:
            return max(0, round((time.monotonic() - started) * 1000))

        for attempt in range(attempts):
            response = None
            text_emitted = False
            try:
                if self._cancelled(cancellation):
                    raise AIProviderError(
                        "CANCELLED", "AI 请求已取消", error_class="cancelled"
                    )
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    raise AIProviderError(
                        "NETWORK",
                        "AI 服务连接超时，请稍后重试",
                        retryable=True,
                        error_class="timeout",
                    )
                request = urllib.request.Request(
                    self._request_url, data=payload, headers=headers
                )
                try:
                    response = self._urlopen(request, timeout=remaining)
                except urllib.error.HTTPError as exc:
                    response = exc
                    raise

                parts: list[str] = []
                usage = None
                finish_reason = None
                response_model = resolved_model
                response_request_id = None
                saw_terminal = False
                for _event_name, data in _iter_sse_records(
                    response,
                    cancellation=cancellation,
                    maximum_bytes=self._max_response_bytes,
                ):
                    if data.strip() == "[DONE]":
                        if self.wire_api == "chat_completions":
                            saw_terminal = True
                        break
                    try:
                        body = json.loads(data)
                    except json.JSONDecodeError as exc:
                        raise AIProviderError(
                            "INVALID_RESPONSE",
                            "AI 流式响应格式异常，请稍后重试",
                            retryable=True,
                            error_class="invalid_stream_json",
                        ) from exc
                    if self.wire_api == "responses":
                        if not isinstance(body, dict):
                            raise AIProviderError(
                                "INVALID_RESPONSE",
                                "AI 流式响应缺少事件类型",
                                retryable=True,
                                error_class="invalid_stream_chunk",
                            )
                        event_type = str(
                            body.get("type")
                            or (_event_name if _event_name != "message" else "")
                        ).strip()
                        if event_type == "error":
                            raise AIProviderError(
                                "PROVIDER_ERROR",
                                "AI 服务在流式响应中返回错误",
                                retryable=True,
                                error_class="stream_error",
                            )
                        if event_type in {"response.failed", "response.incomplete"}:
                            error_class = (
                                "response_failed"
                                if event_type == "response.failed"
                                else "response_incomplete"
                            )
                            raise AIProviderError(
                                "PROVIDER_ERROR",
                                "AI 服务未能完成响应",
                                retryable=True,
                                error_class=error_class,
                            )
                        if event_type in {
                            "codex.rate_limits",
                            "codex.response.metadata",
                            "responsesapi.websocket_timing",
                        }:
                            # Some Responses-compatible gateways emit this
                            # metadata-only control data before the standard
                            # response stream.  It carries no model text and
                            # must not invalidate the following response.*
                            # events.  Unknown non-response events remain
                            # fail-closed below.
                            yield ChatStreamEvent("activity")
                            continue
                        if event_type == "response.output_text.delta":
                            content = body.get("delta")
                            if not isinstance(content, str):
                                raise AIProviderError(
                                    "INVALID_RESPONSE",
                                    "AI 流式文本分片格式异常",
                                    retryable=True,
                                    error_class="invalid_stream_chunk",
                                )
                            if content:
                                parts.append(content)
                                text_emitted = True
                                yield ChatStreamEvent("text_delta", text=content)
                            else:
                                yield ChatStreamEvent("activity")
                            continue
                        if event_type == "response.completed":
                            completed_body = body.get("response")
                            if not isinstance(completed_body, dict):
                                raise AIProviderError(
                                    "INVALID_RESPONSE",
                                    "AI 完成事件缺少响应对象",
                                    retryable=True,
                                    error_class="invalid_stream_chunk",
                                )
                            status = str(completed_body.get("status") or "completed")
                            if status != "completed":
                                error_class = (
                                    "response_incomplete"
                                    if status == "incomplete"
                                    else "response_failed"
                                )
                                raise AIProviderError(
                                    "PROVIDER_ERROR",
                                    "AI 服务未能完成响应",
                                    retryable=True,
                                    error_class=error_class,
                                )
                            response_model = str(
                                completed_body.get("model") or resolved_model
                            ).strip()
                            usage = _responses_usage(completed_body.get("usage"))
                            response_request_id = str(
                                completed_body.get("id") or ""
                            ).strip() or None
                            if not parts:
                                completed_text = _responses_output_text(completed_body)
                                if completed_text:
                                    parts.append(completed_text)
                                    text_emitted = True
                            finish_reason = "stop"
                            saw_terminal = True
                            yield ChatStreamEvent("activity")
                            continue
                        if event_type.startswith("response."):
                            yield ChatStreamEvent("activity")
                            continue
                        raise AIProviderError(
                            "INVALID_RESPONSE",
                            "AI 流式响应缺少事件类型",
                            retryable=True,
                            error_class="invalid_stream_chunk",
                        )
                    if isinstance(body, dict) and isinstance(body.get("error"), dict):
                        raise AIProviderError(
                            "PROVIDER_ERROR",
                            "AI 服务在流式响应中返回错误",
                            retryable=True,
                            error_class="stream_error",
                        )
                    if isinstance(body, dict):
                        if isinstance(body.get("model"), str) and body["model"].strip():
                            response_model = body["model"].strip()
                        if isinstance(body.get("usage"), dict):
                            usage = body["usage"]
                        choices = body.get("choices")
                        if choices is None or choices == []:
                            yield ChatStreamEvent("activity")
                            continue
                    try:
                        choice = body["choices"][0]
                    except (KeyError, IndexError, TypeError) as exc:
                        raise AIProviderError(
                            "INVALID_RESPONSE",
                            "AI 流式响应缺少结果分片",
                            retryable=True,
                            error_class="invalid_stream_chunk",
                        ) from exc
                    delta = choice.get("delta") if isinstance(choice, dict) else None
                    content = delta.get("content") if isinstance(delta, dict) else None
                    if isinstance(content, str) and content:
                        parts.append(content)
                        text_emitted = True
                        yield ChatStreamEvent("text_delta", text=content)
                    else:
                        yield ChatStreamEvent("activity")
                    raw_finish = choice.get("finish_reason") if isinstance(choice, dict) else None
                    if raw_finish is not None:
                        finish_reason = str(raw_finish)
                        saw_terminal = True
                content = "".join(parts).strip()
                if not saw_terminal:
                    raise AIProviderError(
                        "NETWORK",
                        "AI 流式响应意外中断",
                        retryable=True,
                        error_class="incomplete_read",
                    )
                if not content:
                    raise AIProviderError(
                        "INVALID_RESPONSE",
                        "AI 服务返回内容为空，请稍后重试",
                        retryable=True,
                        error_class="invalid_response",
                    )
                response_headers = getattr(response, "headers", None)
                request_id = (
                    response_headers.get("x-request-id") if response_headers else None
                ) or response_request_id
                completed = ChatResponse(
                    content=content,
                    model=response_model,
                    request_id=request_id,
                    usage=usage,
                    finish_reason=finish_reason,
                    duration_ms=elapsed_ms(),
                    attempts=attempt + 1,
                )
                yield ChatStreamEvent("completed", response=completed)
                return
            except urllib.error.HTTPError as exc:
                if exc.code in {400, 404, 405, 415, 422}:
                    error = AIProviderError(
                        "STREAM_UNSUPPORTED",
                        "当前 AI 服务不支持流式响应",
                        attempts=attempt + 1,
                        duration_ms=elapsed_ms(),
                        error_class="stream_unsupported",
                    )
                elif exc.code in {401, 403}:
                    error = AIProviderError(
                        "AUTHENTICATION",
                        "AI 服务认证失败，请检查 API 密钥",
                        attempts=attempt + 1,
                        duration_ms=elapsed_ms(),
                        error_class="authentication",
                    )
                else:
                    retryable = exc.code == 429 or 500 <= exc.code < 600
                    error = AIProviderError(
                        "RATE_LIMITED" if exc.code == 429 else "PROVIDER_ERROR",
                        f"AI 服务暂时不可用（HTTP {exc.code}）",
                        retryable=retryable,
                        attempts=attempt + 1,
                        duration_ms=elapsed_ms(),
                        error_class="rate_limited" if exc.code == 429 else "http_error",
                    )
            except AIProviderError as exc:
                error = AIProviderError(
                    exc.code,
                    str(exc),
                    retryable=exc.retryable,
                    attempts=attempt + 1,
                    duration_ms=elapsed_ms(),
                    error_class=exc.error_class,
                )
            except (
                urllib.error.URLError,
                TimeoutError,
                socket.timeout,
                ssl.SSLError,
                ConnectionError,
                http.client.HTTPException,
            ) as exc:
                error = AIProviderError(
                    "NETWORK",
                    f"AI 服务连接失败（已重试 {attempts} 次）",
                    retryable=True,
                    attempts=attempt + 1,
                    duration_ms=elapsed_ms(),
                    error_class=_network_error_class(exc),
                )
            finally:
                _close_response_safely(response)

            if (
                not error.retryable
                or text_emitted
                or attempt == attempts - 1
                or error.code in {"CANCELLED", "STREAM_UNSUPPORTED"}
            ):
                yield ChatStreamEvent("error", error=error)
                return
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                yield ChatStreamEvent(
                    "error",
                    error=AIProviderError(
                        "NETWORK",
                        "AI 服务连接超时，请稍后重试",
                        retryable=True,
                        attempts=attempt + 1,
                        duration_ms=elapsed_ms(),
                        error_class="timeout",
                    ),
                )
                return
            self._sleep(min(2 ** attempt, 8, remaining))

    def complete(
        self,
        messages: Sequence[dict],
        *,
        model: str,
        temperature: float = 0.7,
        timeout: float = 180,
        max_attempts: int = 3,
        max_tokens: int | None = None,
        thinking: dict | None = None,
        reasoning_effort: str | None = None,
        response_format: dict | None = None,
        task_policy: str | None = None,
        cancellation: CancellationToken | None = None,
    ) -> ChatResponse:
        resolved_model = str(model or "").strip()
        if not resolved_model:
            raise AIProviderError("CONFIGURATION", "未配置 AI 模型")
        try:
            timeout = float(timeout)
            attempts = max(1, int(max_attempts))
        except (TypeError, ValueError) as exc:
            raise AIProviderError("CONFIGURATION", "AI 请求参数无效") from exc
        if timeout <= 0:
            raise AIProviderError("CONFIGURATION", "AI 请求超时必须大于 0")

        if max_tokens is not None:
            try:
                resolved_max_tokens = max(1, int(max_tokens))
            except (TypeError, ValueError) as exc:
                raise AIProviderError("CONFIGURATION", "AI 输出上限无效") from exc
        else:
            resolved_max_tokens = None
        if self.wire_api == "responses":
            request_body = _responses_request_body(
                messages,
                model=resolved_model,
                temperature=temperature,
                stream=False,
                max_tokens=resolved_max_tokens,
                reasoning_effort=reasoning_effort,
                response_format=response_format,
            )
        else:
            request_body = {
                "model": resolved_model,
                "messages": list(messages),
                "temperature": temperature,
            }
            if resolved_max_tokens is not None:
                request_body["max_tokens"] = resolved_max_tokens
            if thinking is not None:
                request_body["thinking"] = thinking
            if reasoning_effort is not None:
                request_body["reasoning_effort"] = str(reasoning_effort)
            if response_format is not None:
                request_body["response_format"] = response_format

        payload = json.dumps(
            request_body,
            ensure_ascii=False,
        ).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }

        started = time.monotonic()

        def elapsed_ms() -> int:
            return max(0, round((time.monotonic() - started) * 1000))

        def request_timeout(attempt: int) -> float:
            if attempt == 0:
                return timeout
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise AIProviderError(
                    "NETWORK",
                    "AI 服务连接超时，请稍后重试",
                    retryable=True,
                    attempts=attempt,
                    duration_ms=elapsed_ms(),
                    error_class="timeout",
                )
            return remaining

        for attempt in range(attempts):
            if self._cancelled(cancellation):
                raise AIProviderError(
                    "CANCELLED",
                    "AI 请求已取消",
                    attempts=attempt,
                    duration_ms=elapsed_ms(),
                    error_class="cancelled",
                )
            request = urllib.request.Request(
                self._request_url, data=payload, headers=headers
            )
            response = None
            try:
                try:
                    response = self._urlopen(
                        request, timeout=request_timeout(attempt)
                    )
                except urllib.error.HTTPError as exc:
                    # HTTPError is also a response object and owns a socket/file
                    # handle, so route it through the same finally cleanup.
                    response = exc
                    raise
                raw = _read_response_body(
                    response, maximum_bytes=self._max_response_bytes
                )
                invalid_response_message = None
                invalid_response_cause = None
                try:
                    body = json.loads(raw)
                    if self.wire_api == "responses":
                        if not isinstance(body, dict):
                            raise TypeError("response body must be an object")
                        if isinstance(body.get("error"), dict):
                            raise AIProviderError(
                                "PROVIDER_ERROR",
                                "AI 服务返回错误响应",
                                retryable=True,
                                attempts=attempt + 1,
                                duration_ms=elapsed_ms(),
                                error_class="response_error",
                            )
                        status = str(body.get("status") or "completed").casefold()
                        if status in {"failed", "incomplete", "cancelled"}:
                            raise AIProviderError(
                                "PROVIDER_ERROR",
                                "AI 服务未能完成响应",
                                retryable=status != "cancelled",
                                attempts=attempt + 1,
                                duration_ms=elapsed_ms(),
                                error_class=f"response_{status}",
                            )
                        content = _responses_output_text(body)
                        finish_reason = "stop"
                    else:
                        content = body["choices"][0]["message"]["content"]
                        finish_reason = body["choices"][0].get("finish_reason")
                except AIProviderError:
                    raise
                except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
                    invalid_response_message = "AI 服务返回格式异常，请稍后重试"
                    invalid_response_cause = exc
                else:
                    if not isinstance(content, str) or not content.strip():
                        invalid_response_message = "AI 服务返回内容为空，请稍后重试"
                if invalid_response_message is not None:
                    if attempt == attempts - 1:
                        raise AIProviderError(
                            "INVALID_RESPONSE",
                            invalid_response_message,
                            retryable=True,
                            attempts=attempt + 1,
                            duration_ms=elapsed_ms(),
                            error_class="invalid_response",
                        ) from invalid_response_cause
                else:
                    response_headers = getattr(response, "headers", None)
                    request_id = (
                        response_headers.get("x-request-id") if response_headers else None
                    )
                    if self.wire_api == "responses" and not request_id:
                        request_id = str(body.get("id") or "").strip() or None
                    return ChatResponse(
                        content=content.strip(),
                        model=str(body.get("model") or resolved_model),
                        request_id=request_id,
                        usage=(
                            _responses_usage(body.get("usage"))
                            if self.wire_api == "responses"
                            else body.get("usage")
                            if isinstance(body.get("usage"), dict)
                            else None
                        ),
                        finish_reason=str(finish_reason) if finish_reason is not None else None,
                        duration_ms=elapsed_ms(),
                        attempts=attempt + 1,
                    )
            except urllib.error.HTTPError as exc:
                if exc.code in {401, 403}:
                    raise AIProviderError(
                        "AUTHENTICATION",
                        "AI 服务认证失败，请检查 API 密钥",
                        attempts=attempt + 1,
                        duration_ms=elapsed_ms(),
                        error_class="authentication",
                    ) from None
                retryable = exc.code == 429 or 500 <= exc.code < 600
                if not retryable or attempt == attempts - 1:
                    code = "RATE_LIMITED" if exc.code == 429 else "PROVIDER_ERROR"
                    raise AIProviderError(
                        code,
                        f"AI 服务暂时不可用（HTTP {exc.code}）",
                        retryable=retryable,
                        attempts=attempt + 1,
                        duration_ms=elapsed_ms(),
                        error_class="rate_limited" if exc.code == 429 else "http_error",
                    ) from None
            except (
                urllib.error.URLError,
                TimeoutError,
                socket.timeout,
                ssl.SSLError,
                ConnectionError,
                http.client.HTTPException,
            ) as exc:
                if attempt == attempts - 1:
                    raise AIProviderError(
                        "NETWORK",
                        f"AI 服务连接失败（已重试 {attempts} 次）",
                        retryable=True,
                        attempts=attempt + 1,
                        duration_ms=elapsed_ms(),
                        error_class=_network_error_class(exc),
                    ) from None
            finally:
                _close_response_safely(response)

            if self._cancelled(cancellation):
                raise AIProviderError(
                    "CANCELLED",
                    "AI 请求已取消",
                    attempts=attempt + 1,
                    duration_ms=elapsed_ms(),
                    error_class="cancelled",
                )
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise AIProviderError(
                    "NETWORK",
                    "AI 服务连接超时，请稍后重试",
                    retryable=True,
                    attempts=attempt + 1,
                    duration_ms=elapsed_ms(),
                    error_class="timeout",
                )
            self._sleep(min(2 ** attempt, 8, remaining))


class OpenAIResponsesProvider(OpenAICompatibleProvider):
    """OpenAI-compatible provider using the Responses wire protocol."""

    wire_api = "responses"


class AnthropicProvider:
    """Native Anthropic Messages API provider."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        urlopen: Callable | None = None,
        sleep: Callable[[float], None] = time.sleep,
        dns_mode: str = "proxy",
        dns_servers: object = None,
        max_response_bytes: int = 8 * 1024 * 1024,
    ):
        try:
            self.base_url = validate_provider_url(base_url)
            self._request_url = anthropic_messages_url(self.base_url)
            self._models_url = provider_models_url(self.base_url)
            self._dns_mode = validate_dns_mode(dns_mode)
            self._dns_servers = validate_dns_servers(dns_servers)
        except ValueError as exc:
            raise AIProviderError("CONFIGURATION", str(exc)) from exc
        self._api_key = str(api_key or "").strip()
        if not self._api_key:
            raise AIProviderError("CONFIGURATION", "未配置 AI API 密钥")
        self._urlopen = urlopen or functools.partial(
            direct_urlopen,
            dns_mode=self._dns_mode,
            dns_servers=self._dns_servers,
        )
        self._sleep = sleep
        if (
            isinstance(max_response_bytes, bool)
            or not isinstance(max_response_bytes, int)
            or max_response_bytes < _RESPONSE_CHUNK_BYTES
        ):
            raise AIProviderError("CONFIGURATION", "AI 响应长度上限无效")
        self._max_response_bytes = max_response_bytes

    def __repr__(self) -> str:
        return f"{type(self).__name__}(base_url={self.base_url!r}, api_key='[hidden]')"

    @staticmethod
    def _cancelled(cancellation: CancellationToken | None) -> bool:
        return bool(cancellation and cancellation.is_cancelled())

    @staticmethod
    def _messages(messages: Sequence[dict]) -> tuple[str, list[dict]]:
        systems: list[str] = []
        conversation: list[dict] = []
        for item in messages:
            if not isinstance(item, dict):
                raise AIProviderError("CONFIGURATION", "AI 消息格式无效")
            role = str(item.get("role") or "").strip().casefold()
            content = item.get("content")
            if isinstance(content, str):
                normalized_content = content
            elif isinstance(content, list):
                text_parts = [
                    str(block.get("text") or "")
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                ]
                normalized_content = "\n".join(part for part in text_parts if part)
            else:
                normalized_content = ""
            if role == "system":
                if normalized_content:
                    systems.append(normalized_content)
                continue
            if role not in {"user", "assistant"} or not normalized_content:
                raise AIProviderError("CONFIGURATION", "AI 消息格式无效")
            conversation.append({"role": role, "content": normalized_content})
        if not conversation:
            raise AIProviderError("CONFIGURATION", "AI 消息不能为空")
        return "\n\n".join(systems), conversation

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Api-Key": self._api_key,
            "anthropic-version": "2023-06-01",
        }

    def list_models(self, *, timeout: float = 30) -> list[dict]:
        request = urllib.request.Request(
            self._models_url,
            headers=self._headers(),
            method="GET",
        )
        response = None
        try:
            try:
                response = self._urlopen(request, timeout=float(timeout))
            except urllib.error.HTTPError as exc:
                response = exc
                raise
            raw = _read_response_body(
                response, maximum_bytes=min(self._max_response_bytes, 4 * 1024 * 1024)
            )
            try:
                body = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise AIProviderError(
                    "INVALID_RESPONSE",
                    "Claude 服务返回的模型列表格式异常",
                    error_class="invalid_model_list",
                ) from exc
            return _validated_model_items(body)
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403}:
                raise AIProviderError(
                    "AUTHENTICATION",
                    "Claude 服务认证失败，请检查已保存的 API 密钥",
                    error_class="authentication",
                ) from None
            retryable = exc.code == 429 or 500 <= exc.code < 600
            raise AIProviderError(
                "RATE_LIMITED" if exc.code == 429 else "PROVIDER_ERROR",
                f"Claude 模型列表暂时不可用（HTTP {exc.code}）",
                retryable=retryable,
                error_class="rate_limited" if exc.code == 429 else "http_error",
            ) from None
        except (
            urllib.error.URLError,
            TimeoutError,
            socket.timeout,
            ssl.SSLError,
            ConnectionError,
            http.client.HTTPException,
        ) as exc:
            raise AIProviderError(
                "NETWORK",
                "Claude 模型列表连接失败，请检查网络设置",
                retryable=True,
                error_class=_network_error_class(exc),
            ) from None
        finally:
            _close_response_safely(response)

    def stream_complete(
        self,
        messages: Sequence[dict],
        *,
        model: str,
        temperature: float = 0.7,
        timeout: float = 180,
        max_attempts: int = 3,
        max_tokens: int | None = None,
        thinking: dict | None = None,
        reasoning_effort: str | None = None,
        response_format: dict | None = None,
        task_policy: str | None = None,
        cancellation: CancellationToken | None = None,
    ) -> Iterator[ChatStreamEvent]:
        del task_policy
        resolved_model = str(model or "").strip()
        if not resolved_model:
            yield ChatStreamEvent(
                "error", error=AIProviderError("CONFIGURATION", "未配置 AI 模型")
            )
            return
        try:
            timeout = float(timeout)
            attempts = max(1, int(max_attempts))
            output_limit = max(1, int(max_tokens or 4096))
            numeric_temperature = float(temperature)
        except (TypeError, ValueError):
            yield ChatStreamEvent(
                "error", error=AIProviderError("CONFIGURATION", "AI 请求参数无效")
            )
            return
        if timeout <= 0:
            yield ChatStreamEvent(
                "error",
                error=AIProviderError("CONFIGURATION", "AI 请求超时必须大于 0"),
            )
            return

        try:
            system, conversation = self._messages(messages)
        except AIProviderError as exc:
            yield ChatStreamEvent("error", error=exc)
            return
        request_body: dict[str, object] = {
            "model": resolved_model,
            "messages": conversation,
            "max_tokens": output_limit,
            "stream": True,
        }
        if system:
            request_body["system"] = system
        thinking_type = (
            str(thinking.get("type") or "").strip().casefold()
            if isinstance(thinking, dict)
            else ""
        )
        if thinking_type in {"enabled", "adaptive"}:
            request_body["thinking"] = {"type": "adaptive"}
        elif 0 <= numeric_temperature <= 1:
            request_body["temperature"] = numeric_temperature
        output_config: dict[str, object] = {}
        effort = str(reasoning_effort or "").strip().casefold()
        if effort in {"low", "medium", "high", "max"} and "haiku" not in resolved_model.casefold():
            output_config["effort"] = effort
        if (
            isinstance(response_format, dict)
            and response_format.get("type") == "json_schema"
            and isinstance(response_format.get("schema"), dict)
        ):
            output_config["format"] = response_format
        if output_config:
            request_body["output_config"] = output_config
        payload = json.dumps(request_body, ensure_ascii=False).encode("utf-8")
        started = time.monotonic()

        def elapsed_ms() -> int:
            return max(0, round((time.monotonic() - started) * 1000))

        for attempt in range(attempts):
            response = None
            text_emitted = False
            try:
                if self._cancelled(cancellation):
                    raise AIProviderError(
                        "CANCELLED", "AI 请求已取消", error_class="cancelled"
                    )
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    raise AIProviderError(
                        "NETWORK",
                        "Claude 服务连接超时，请稍后重试",
                        retryable=True,
                        error_class="timeout",
                    )
                headers = {**self._headers(), "Accept": "text/event-stream"}
                request = urllib.request.Request(
                    self._request_url, data=payload, headers=headers
                )
                try:
                    response = self._urlopen(request, timeout=remaining)
                except urllib.error.HTTPError as exc:
                    response = exc
                    raise

                parts: list[str] = []
                raw_usage: dict[str, object] = {}
                finish_reason = None
                response_model = resolved_model
                request_id = None
                saw_terminal = False
                for event_name, data in _iter_sse_records(
                    response,
                    cancellation=cancellation,
                    maximum_bytes=self._max_response_bytes,
                ):
                    try:
                        body = json.loads(data)
                    except json.JSONDecodeError as exc:
                        raise AIProviderError(
                            "INVALID_RESPONSE",
                            "Claude 流式响应格式异常，请稍后重试",
                            retryable=True,
                            error_class="invalid_stream_json",
                        ) from exc
                    event_type = str(body.get("type") or event_name)
                    if event_type == "error":
                        raise AIProviderError(
                            "PROVIDER_ERROR",
                            "Claude 服务在流式响应中返回错误",
                            retryable=True,
                            error_class="stream_error",
                        )
                    if event_type == "message_start":
                        message = body.get("message")
                        if isinstance(message, dict):
                            if isinstance(message.get("model"), str):
                                response_model = message["model"] or response_model
                            if isinstance(message.get("id"), str):
                                request_id = message["id"] or request_id
                            if isinstance(message.get("usage"), dict):
                                raw_usage.update(message["usage"])
                        yield ChatStreamEvent("activity")
                        continue
                    if event_type == "content_block_delta":
                        delta = body.get("delta")
                        delta_type = str(delta.get("type") or "") if isinstance(delta, dict) else ""
                        text = delta.get("text") if isinstance(delta, dict) else None
                        if delta_type == "text_delta" and isinstance(text, str) and text:
                            parts.append(text)
                            text_emitted = True
                            yield ChatStreamEvent("text_delta", text=text)
                        else:
                            yield ChatStreamEvent("activity")
                        continue
                    if event_type == "message_delta":
                        delta = body.get("delta")
                        if isinstance(delta, dict) and delta.get("stop_reason") is not None:
                            finish_reason = str(delta["stop_reason"])
                        if isinstance(body.get("usage"), dict):
                            raw_usage.update(body["usage"])
                        yield ChatStreamEvent("activity")
                        continue
                    if event_type == "message_stop":
                        saw_terminal = True
                        break
                    yield ChatStreamEvent("activity")

                content = "".join(parts).strip()
                if not saw_terminal:
                    raise AIProviderError(
                        "NETWORK",
                        "Claude 流式响应意外中断",
                        retryable=True,
                        error_class="incomplete_read",
                    )
                if not content:
                    raise AIProviderError(
                        "INVALID_RESPONSE",
                        "Claude 服务返回内容为空，请稍后重试",
                        retryable=True,
                        error_class="invalid_response",
                    )
                input_tokens = raw_usage.get("input_tokens")
                output_tokens = raw_usage.get("output_tokens")
                usage = dict(raw_usage)
                usage.update({
                    "prompt_tokens": input_tokens,
                    "completion_tokens": output_tokens,
                    "total_tokens": int(input_tokens or 0) + int(output_tokens or 0),
                    "cached_tokens": raw_usage.get("cache_read_input_tokens"),
                })
                response_headers = getattr(response, "headers", None)
                if response_headers:
                    request_id = (
                        response_headers.get("request-id")
                        or response_headers.get("x-request-id")
                        or request_id
                    )
                completed = ChatResponse(
                    content=content,
                    model=response_model,
                    request_id=request_id,
                    usage=usage,
                    finish_reason=finish_reason,
                    duration_ms=elapsed_ms(),
                    attempts=attempt + 1,
                )
                yield ChatStreamEvent("completed", response=completed)
                return
            except urllib.error.HTTPError as exc:
                if exc.code in {400, 404, 405, 415, 422}:
                    error = AIProviderError(
                        "STREAM_UNSUPPORTED",
                        "当前 Claude 服务不支持流式响应",
                        attempts=attempt + 1,
                        duration_ms=elapsed_ms(),
                        error_class="stream_unsupported",
                    )
                elif exc.code in {401, 403}:
                    error = AIProviderError(
                        "AUTHENTICATION",
                        "Claude 服务认证失败，请检查 API 密钥",
                        attempts=attempt + 1,
                        duration_ms=elapsed_ms(),
                        error_class="authentication",
                    )
                else:
                    retryable = exc.code == 429 or 500 <= exc.code < 600
                    error = AIProviderError(
                        "RATE_LIMITED" if exc.code == 429 else "PROVIDER_ERROR",
                        f"Claude 服务暂时不可用（HTTP {exc.code}）",
                        retryable=retryable,
                        attempts=attempt + 1,
                        duration_ms=elapsed_ms(),
                        error_class="rate_limited" if exc.code == 429 else "http_error",
                    )
            except AIProviderError as exc:
                error = AIProviderError(
                    exc.code,
                    str(exc),
                    retryable=exc.retryable,
                    attempts=attempt + 1,
                    duration_ms=elapsed_ms(),
                    error_class=exc.error_class,
                )
            except (
                urllib.error.URLError,
                TimeoutError,
                socket.timeout,
                ssl.SSLError,
                ConnectionError,
                http.client.HTTPException,
            ) as exc:
                error = AIProviderError(
                    "NETWORK",
                    f"Claude 服务连接失败（已重试 {attempts} 次）",
                    retryable=True,
                    attempts=attempt + 1,
                    duration_ms=elapsed_ms(),
                    error_class=_network_error_class(exc),
                )
            finally:
                _close_response_safely(response)

            if (
                not error.retryable
                or text_emitted
                or attempt == attempts - 1
                or error.code in {"CANCELLED", "STREAM_UNSUPPORTED"}
            ):
                yield ChatStreamEvent("error", error=error)
                return
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                yield ChatStreamEvent(
                    "error",
                    error=AIProviderError(
                        "NETWORK",
                        "Claude 服务连接超时，请稍后重试",
                        retryable=True,
                        attempts=attempt + 1,
                        duration_ms=elapsed_ms(),
                        error_class="timeout",
                    ),
                )
                return
            self._sleep(min(2 ** attempt, 8, remaining))

    def complete(
        self,
        messages: Sequence[dict],
        *,
        model: str,
        temperature: float = 0.7,
        timeout: float = 180,
        max_attempts: int = 3,
        max_tokens: int | None = None,
        thinking: dict | None = None,
        reasoning_effort: str | None = None,
        response_format: dict | None = None,
        task_policy: str | None = None,
        cancellation: CancellationToken | None = None,
    ) -> ChatResponse:
        del task_policy
        resolved_model = str(model or "").strip()
        if not resolved_model:
            raise AIProviderError("CONFIGURATION", "未配置 AI 模型")
        try:
            timeout = float(timeout)
            attempts = max(1, int(max_attempts))
            output_limit = max(1, int(max_tokens or 4096))
        except (TypeError, ValueError) as exc:
            raise AIProviderError("CONFIGURATION", "AI 请求参数无效") from exc
        if timeout <= 0:
            raise AIProviderError("CONFIGURATION", "AI 请求超时必须大于 0")

        system, conversation = self._messages(messages)
        request_body: dict[str, object] = {
            "model": resolved_model,
            "messages": conversation,
            "max_tokens": output_limit,
        }
        if system:
            request_body["system"] = system
        thinking_type = (
            str(thinking.get("type") or "").strip().casefold()
            if isinstance(thinking, dict)
            else ""
        )
        if thinking_type in {"enabled", "adaptive"}:
            request_body["thinking"] = {"type": "adaptive"}
        else:
            try:
                numeric_temperature = float(temperature)
            except (TypeError, ValueError) as exc:
                raise AIProviderError("CONFIGURATION", "AI 温度参数无效") from exc
            if 0 <= numeric_temperature <= 1:
                request_body["temperature"] = numeric_temperature
        output_config: dict[str, object] = {}
        effort = str(reasoning_effort or "").strip().casefold()
        if effort in {"low", "medium", "high", "max"} and "haiku" not in resolved_model.casefold():
            output_config["effort"] = effort
        if (
            isinstance(response_format, dict)
            and response_format.get("type") == "json_schema"
            and isinstance(response_format.get("schema"), dict)
        ):
            output_config["format"] = response_format
        if output_config:
            request_body["output_config"] = output_config

        payload = json.dumps(request_body, ensure_ascii=False).encode("utf-8")
        started = time.monotonic()

        def elapsed_ms() -> int:
            return max(0, round((time.monotonic() - started) * 1000))

        def request_timeout(attempt: int) -> float:
            if attempt == 0:
                return timeout
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise AIProviderError(
                    "NETWORK",
                    "Claude 服务连接超时，请稍后重试",
                    retryable=True,
                    attempts=attempt,
                    duration_ms=elapsed_ms(),
                    error_class="timeout",
                )
            return remaining

        for attempt in range(attempts):
            if self._cancelled(cancellation):
                raise AIProviderError(
                    "CANCELLED",
                    "AI 请求已取消",
                    attempts=attempt,
                    duration_ms=elapsed_ms(),
                    error_class="cancelled",
                )
            request = urllib.request.Request(
                self._request_url,
                data=payload,
                headers=self._headers(),
            )
            response = None
            try:
                try:
                    response = self._urlopen(
                        request, timeout=request_timeout(attempt)
                    )
                except urllib.error.HTTPError as exc:
                    response = exc
                    raise
                raw = _read_response_body(
                    response, maximum_bytes=self._max_response_bytes
                )
                try:
                    body = json.loads(raw)
                    blocks = body["content"]
                    if not isinstance(blocks, list):
                        raise TypeError("content must be a list")
                    content = "\n".join(
                        str(block.get("text") or "")
                        for block in blocks
                        if isinstance(block, dict) and block.get("type") == "text"
                    ).strip()
                    if not content:
                        raise ValueError("empty text response")
                except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                    if attempt == attempts - 1:
                        raise AIProviderError(
                            "INVALID_RESPONSE",
                            "Claude 服务返回格式异常，请稍后重试",
                            retryable=True,
                            attempts=attempt + 1,
                            duration_ms=elapsed_ms(),
                            error_class="invalid_response",
                        ) from exc
                else:
                    raw_usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
                    input_tokens = raw_usage.get("input_tokens")
                    output_tokens = raw_usage.get("output_tokens")
                    usage = dict(raw_usage)
                    usage.update(
                        {
                            "prompt_tokens": input_tokens,
                            "completion_tokens": output_tokens,
                            "total_tokens": (
                                int(input_tokens or 0) + int(output_tokens or 0)
                            ),
                            "cached_tokens": raw_usage.get("cache_read_input_tokens"),
                        }
                    )
                    response_headers = getattr(response, "headers", None)
                    request_id = None
                    if response_headers:
                        request_id = response_headers.get("request-id") or response_headers.get("x-request-id")
                    return ChatResponse(
                        content=content,
                        model=str(body.get("model") or resolved_model),
                        request_id=request_id,
                        usage=usage,
                        finish_reason=str(body.get("stop_reason") or "") or None,
                        duration_ms=elapsed_ms(),
                        attempts=attempt + 1,
                    )
            except urllib.error.HTTPError as exc:
                if exc.code in {401, 403}:
                    raise AIProviderError(
                        "AUTHENTICATION",
                        "Claude 服务认证失败，请检查 API 密钥",
                        attempts=attempt + 1,
                        duration_ms=elapsed_ms(),
                        error_class="authentication",
                    ) from None
                retryable = exc.code == 429 or 500 <= exc.code < 600
                if not retryable or attempt == attempts - 1:
                    raise AIProviderError(
                        "RATE_LIMITED" if exc.code == 429 else "PROVIDER_ERROR",
                        f"Claude 服务暂时不可用（HTTP {exc.code}）",
                        retryable=retryable,
                        attempts=attempt + 1,
                        duration_ms=elapsed_ms(),
                        error_class="rate_limited" if exc.code == 429 else "http_error",
                    ) from None
            except (
                urllib.error.URLError,
                TimeoutError,
                socket.timeout,
                ssl.SSLError,
                ConnectionError,
                http.client.HTTPException,
            ) as exc:
                if attempt == attempts - 1:
                    raise AIProviderError(
                        "NETWORK",
                        f"Claude 服务连接失败（已重试 {attempts} 次）",
                        retryable=True,
                        attempts=attempt + 1,
                        duration_ms=elapsed_ms(),
                        error_class=_network_error_class(exc),
                    ) from None
            finally:
                _close_response_safely(response)

            if self._cancelled(cancellation):
                raise AIProviderError(
                    "CANCELLED",
                    "AI 请求已取消",
                    attempts=attempt + 1,
                    duration_ms=elapsed_ms(),
                    error_class="cancelled",
                )
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise AIProviderError(
                    "NETWORK",
                    "Claude 服务连接超时，请稍后重试",
                    retryable=True,
                    attempts=attempt + 1,
                    duration_ms=elapsed_ms(),
                    error_class="timeout",
                )
            self._sleep(min(2 ** attempt, 8, remaining))


def create_provider(
    provider_id: str,
    profile: dict,
    api_key: str,
    **kwargs,
) -> AIProvider:
    if provider_id == "openai_compatible":
        try:
            wire_api = validate_wire_api(profile.get("wire_api"))
        except ValueError as exc:
            raise AIProviderError("CONFIGURATION", str(exc)) from exc
        if wire_api == "messages":
            raise AIProviderError(
                "CONFIGURATION", "OpenAI 兼容 Provider 不支持 Messages 协议"
            )
        provider_type = (
            OpenAIResponsesProvider
            if wire_api == "responses"
            else OpenAICompatibleProvider
        )
        return provider_type(
            profile.get("base_url", ""),
            api_key,
            dns_mode=profile.get("dns_mode", "domestic"),
            dns_servers=profile.get("dns_servers"),
            **kwargs,
        )
    if provider_id == "anthropic":
        return AnthropicProvider(
            profile.get("base_url", ""),
            api_key,
            dns_mode=profile.get("dns_mode", "proxy"),
            dns_servers=profile.get("dns_servers"),
            **kwargs,
        )
    raise AIProviderError("CONFIGURATION", f"不支持的 AI Provider：{provider_id}")
