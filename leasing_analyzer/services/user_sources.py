from __future__ import annotations

import ipaddress
import json
import socket
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import TypedDict
from urllib.parse import parse_qs, unquote, urlparse, urlunparse

from leasing_analyzer.core.config import _REPO_ROOT


STATUS_PENDING = "pending"
STATUS_SUCCESS = "success"
STATUS_ERROR = "error"
STATUS_INSUFFICIENT_DATA = "insufficient_data"

_STORAGE_PATH = _REPO_ROOT / "data" / "user_sources.json"
_BLOCKED_ROUTE_SEGMENTS = {
    "catalog",
    "catalogue",
    "katalog",
    "search",
    "find",
    "results",
    "filter",
    "filters",
    "category",
    "categories",
}
_GENERIC_SINGLE_SEGMENTS = {
    "cars",
    "auto",
    "autos",
    "transport",
    "leasing",
    "offers",
    "predlozheniya",
    "models",
    "model",
}
_SEARCH_QUERY_KEYS = {"q", "query", "search", "text", "keyword", "keywords"}


class UserSource(TypedDict, total=False):
    id: str
    url: str
    status: str
    reason: str
    error_message: str | None
    found_data: dict
    participated_in_calculation: bool
    added_at: str
    last_checked_at: str | None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_user_source_url(url: str) -> str:
    parsed = urlparse((url or "").strip())
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    path = parsed.path.rstrip("/") or "/"
    return urlunparse((scheme, netloc, path, "", parsed.query, ""))


def _source_id(url: str) -> str:
    return sha256(url.encode("utf-8")).hexdigest()[:16]


def _load_raw() -> list[UserSource]:
    if not _STORAGE_PATH.exists():
        return []
    try:
        data = json.loads(_STORAGE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return data if isinstance(data, list) else []


def _save_raw(sources: list[UserSource]) -> None:
    _STORAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _STORAGE_PATH.write_text(
        json.dumps(sources, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def list_user_sources() -> list[UserSource]:
    return _load_raw()


def add_user_source(url: str) -> UserSource:
    normalized = normalize_user_source_url(url)
    valid, reason = validate_user_source_url(normalized, resolve_host=False)
    if not valid:
        raise ValueError(reason)

    sources = _load_raw()
    source_id = _source_id(normalized)
    for source in sources:
        if source.get("id") == source_id or source.get("url") == normalized:
            source.update(
                {
                    "status": STATUS_PENDING,
                    "reason": "Источник ожидает обработки.",
                    "error_message": None,
                    "found_data": {},
                    "participated_in_calculation": False,
                }
            )
            _save_raw(sources)
            return source

    source: UserSource = {
        "id": source_id,
        "url": normalized,
        "status": STATUS_PENDING,
        "reason": "Источник ожидает обработки.",
        "error_message": None,
        "found_data": {},
        "participated_in_calculation": False,
        "added_at": _now_iso(),
        "last_checked_at": None,
    }
    sources.append(source)
    _save_raw(sources)
    return source


def delete_user_source(source_id: str) -> bool:
    sources = _load_raw()
    remaining = [source for source in sources if source.get("id") != source_id]
    if len(remaining) == len(sources):
        return False
    _save_raw(remaining)
    return True


def update_user_source_results(results: list[UserSource]) -> None:
    if not results:
        return

    by_id = {result.get("id"): result for result in results if result.get("id")}
    by_url = {result.get("url"): result for result in results if result.get("url")}
    sources = _load_raw()
    changed = False
    for source in sources:
        result = by_id.get(source.get("id")) or by_url.get(source.get("url"))
        if not result:
            continue
        source.update(
            {
                "status": result.get("status", source.get("status", STATUS_PENDING)),
                "reason": result.get("reason", ""),
                "error_message": result.get("error_message"),
                "found_data": result.get("found_data", {}),
                "participated_in_calculation": bool(result.get("participated_in_calculation")),
                "last_checked_at": _now_iso(),
            }
        )
        changed = True

    if changed:
        _save_raw(sources)


def _is_forbidden_ip(ip: ipaddress._BaseAddress) -> bool:
    return any(
        [
            ip.is_private,
            ip.is_loopback,
            ip.is_link_local,
            ip.is_multicast,
            ip.is_reserved,
            ip.is_unspecified,
        ]
    )


def _host_is_safe(hostname: str, *, resolve_host: bool) -> tuple[bool, str]:
    host = hostname.strip("[]").lower()
    if not host:
        return False, "В ссылке не найден домен."
    if host == "localhost" or host.endswith(".localhost"):
        return False, "Локальные адреса запрещены."

    try:
        ip = ipaddress.ip_address(host)
        if _is_forbidden_ip(ip):
            return False, "Приватные и локальные IP-адреса запрещены."
        return True, ""
    except ValueError:
        pass

    if not resolve_host:
        return True, ""

    try:
        resolved = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False, "Не удалось проверить домен перед обработкой."

    for item in resolved:
        address = item[4][0]
        try:
            if _is_forbidden_ip(ipaddress.ip_address(address)):
                return False, "Домен указывает на приватный или локальный IP-адрес."
        except ValueError:
            return False, "Не удалось проверить IP-адрес домена."

    return True, ""


def _looks_like_concrete_page(parsed) -> tuple[bool, str]:
    path = unquote(parsed.path or "").strip("/")
    query = parse_qs(parsed.query or "", keep_blank_values=True)
    if not path:
        return False, "Главные страницы и домены без пути запрещены."

    segments = [segment.lower() for segment in path.split("/") if segment]
    if any(segment in _BLOCKED_ROUTE_SEGMENTS for segment in segments):
        return False, "Каталоги, страницы поиска и фильтры запрещены."
    if any(key.lower() in _SEARCH_QUERY_KEYS for key in query):
        return False, "Страницы поиска запрещены."
    if len(segments) == 1 and segments[0] in _GENERIC_SINGLE_SEGMENTS:
        return False, "Нужна ссылка на конкретную модель или предложение, а не раздел сайта."

    last_segment = segments[-1] if segments else ""
    has_specific_marker = any(char.isdigit() for char in last_segment) or "-" in last_segment or len(segments) >= 2
    if not has_specific_marker:
        return False, "Ссылка не похожа на конкретную страницу модели или предложения."

    return True, ""


def validate_user_source_url(url: str, *, resolve_host: bool = False) -> tuple[bool, str]:
    parsed = urlparse((url or "").strip())
    if parsed.scheme.lower() not in {"http", "https"}:
        return False, "Разрешены только ссылки HTTP/HTTPS."
    if not parsed.netloc or not parsed.hostname:
        return False, "Некорректная ссылка."
    if parsed.username or parsed.password:
        return False, "Ссылки с логином или паролем запрещены."

    host_ok, host_reason = _host_is_safe(parsed.hostname, resolve_host=resolve_host)
    if not host_ok:
        return False, host_reason

    page_ok, page_reason = _looks_like_concrete_page(parsed)
    if not page_ok:
        return False, page_reason

    return True, ""
