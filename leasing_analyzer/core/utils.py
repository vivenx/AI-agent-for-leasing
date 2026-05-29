from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

from .config import CONFIG

logger = logging.getLogger(__name__)


def normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


_URL_RE = re.compile(r"(?:https?://|www\.)\S+", flags=re.IGNORECASE)
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\((?:https?://|www\.)[^)]+\)", flags=re.IGNORECASE)
_DOMAIN_RE = re.compile(
    r"\b[\w.-]+\.(?:ru|com|net|org|рф|su|kz|by|info|biz|io)(?:/\S*)?\b",
    flags=re.IGNORECASE,
)
_NOISY_QUERY_MARKERS = (
    "\u0441\u0441\u044b\u043b\u043a",  # ssylk
    "\u043f\u043e\u0434\u0440\u043e\u0431",  # podrob
    "\u0445\u0430\u0440\u0430\u043a\u0442\u0435\u0440\u0438\u0441\u0442\u0438\u043a",  # harakteristik
    "\u0434\u0435\u0442\u0430\u043b",  # detal
    "\u0437\u0430\u043f\u0447\u0430\u0441\u0442",  # zapchast
    "\u043a\u0430\u0442\u0430\u043b\u043e\u0433",  # katalog
    "\u043e\u0431\u0437\u043e\u0440",  # obzor
    "pdf",
    "manual",
    "specification",
    "technical detail",
    "details",
    "read more",
)
_QUERY_LABEL_RE = re.compile(
    r"^\s*(?:[-*•]\s*)?(?:\d+[.)]\s*)?"
    r"(?:(?:name|model|analog|query|url|link|title|название|модель|аналог|запрос|ссылка)\s*[:：-]\s*)+",
    flags=re.IGNORECASE,
)


def _remove_noisy_parentheses(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        inner = match.group(1).lower()
        if _URL_RE.search(inner) or _DOMAIN_RE.search(inner):
            return " "
        if any(marker in inner for marker in _NOISY_QUERY_MARKERS):
            return " "
        return match.group(0)

    return re.sub(r"\(([^)]*)\)", replace, text)


def _remove_unclosed_bracket_tail(text: str) -> str:
    for opening, closing in (("(", ")"), ("[", "]")):
        open_pos = text.rfind(opening)
        close_pos = text.rfind(closing)
        if open_pos > close_pos:
            text = text[:open_pos]
    return text


def clean_search_query(
    query: str,
    *,
    max_words: int = 12,
    max_length: int = 120,
    reject_noisy_markers: bool = True,
) -> str:
    if not isinstance(query, str):
        return ""

    original = query
    query = _MARKDOWN_LINK_RE.sub(r"\1", query)
    query = _URL_RE.sub(" ", query)
    query = _DOMAIN_RE.sub(" ", query)
    query = _remove_noisy_parentheses(query)
    query = _remove_unclosed_bracket_tail(query)
    query = query.replace("`", " ").replace('"', " ").replace("'", " ")

    candidates = re.split(r"[\r\n;]+", query)
    query = next((part for part in candidates if part.strip()), "")
    query = _QUERY_LABEL_RE.sub("", query)
    query = normalize_whitespace(query)
    query = query.strip(" \t\r\n:：,.;|/\\()[]{}<>-")
    query = normalize_whitespace(query)

    lower_query = query.lower()
    if not query or not re.search(r"[A-Za-zА-Яа-яЁё0-9]", query):
        return ""
    if _URL_RE.search(original) and not query:
        return ""
    if reject_noisy_markers and any(marker in lower_query for marker in _NOISY_QUERY_MARKERS):
        return ""
    if len(query) > max_length or len(query.split()) > max_words:
        return ""

    return query


def clean_analog_name(name: str) -> str:
    if not isinstance(name, str):
        return ""

    cleaned = name
    cleaned = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", " ", cleaned)
    cleaned = _remove_unclosed_bracket_tail(cleaned)

    for separator in (" — ", " – ", " - ", " | "):
        if separator in cleaned:
            head, tail = cleaned.split(separator, 1)
            tail_lower = tail.lower()
            if any(marker in tail_lower for marker in _NOISY_QUERY_MARKERS) or len(tail.split()) > 3:
                cleaned = head
                break

    cleaned = clean_search_query(cleaned, max_words=8, max_length=80)
    if not cleaned:
        return ""

    cleaned = re.sub(
        r"\b(?:купить|лизинг|цена|стоимость|аналог|buy|lease|leasing|price)\b",
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )
    return clean_search_query(cleaned, max_words=8, max_length=80)


def digits_to_int(text: str) -> Optional[int]:
    digits = re.sub(r"[^\d]", "", text or "")
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        logger.debug("Failed to convert %r to int", digits)
        return None


def format_price(value: Optional[int | float]) -> Optional[str]:
    if value is None:
        return None
    return f"{int(round(value)):,}".replace(",", " ") + " ₽"


def ensure_list_str(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value).strip()]


def is_valid_url(url: str) -> bool:
    if not url or not isinstance(url, str):
        return False
    try:
        parsed = urlparse(url)
        return all([parsed.scheme, parsed.netloc])
    except Exception:
        return False


def normalize_url(url: str, base: str = "https://www.avito.ru") -> str:
    if not url:
        return url
    if url.startswith("http"):
        return url
    return urljoin(base, url)


def normalize_price(price: Optional[int], currency: Optional[str]) -> Optional[int]:
    if price is None:
        return None
    if not currency or currency.upper() == "RUB":
        return price
    rate = CONFIG.exchange_rates.get(currency.upper())
    if rate is None:
        logger.warning("Unknown currency %s, assuming RUB", currency)
        return price
    return int(price * rate)


def normalize_model_name(model_name: str) -> str:
    if not model_name:
        return ""
    return " ".join(word.capitalize() if word else "" for word in model_name.split()).strip()


def normalize_vendor_name(vendor: Optional[str]) -> Optional[str]:
    if not vendor:
        return None
    vendor_map = {
        "bmw": "BMW",
        "mercedes": "Mercedes-Benz",
        "mercedes-benz": "Mercedes-Benz",
        "audi": "Audi",
        "volvo": "Volvo",
        "toyota": "Toyota",
        "lexus": "Lexus",
        "porsche": "Porsche",
        "bentley": "Bentley",
        "ferrari": "Ferrari",
        "apple": "Apple",
        "iphone": "Apple",
        "samsung": "Samsung",
        "huawei": "Huawei",
        "google": "Google",
    }
    normalized = vendor.lower().strip()
    return vendor_map.get(normalized, vendor.capitalize())


def normalize_offer_title(title: str) -> str:
    if not title:
        return ""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", title.lower())).strip()


def normalize_url_for_comparison(url: str) -> str:
    if not url:
        return ""
    try:
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc.replace('www.', '').lower()}{parsed.path.rstrip('/').lower()}"
    except Exception:
        return url.lower()


def extract_price_candidate(text: str) -> Optional[int]:
    if not text:
        return None
    currency_pattern = r"(\d[\d\s]*)\s*(₽|руб|rub|\$|€)"
    for raw_value, _ in re.findall(currency_pattern, text, flags=re.IGNORECASE):
        val = digits_to_int(raw_value)
        if val and val > CONFIG.min_valid_price:
            return val
    for token in re.findall(r"\b\d[\d\s]*\b", text):
        val = digits_to_int(token)
        if not val:
            continue
        if 1900 <= val <= 2030:
            continue
        if val > CONFIG.min_large_price:
            return val
    return None


def describe_price_difference(price1: Optional[int], price2: Optional[int]) -> str:
    if not price1 or not price2:
        return "Недостаточно данных по цене"
    if price1 == price2:
        return "Цена объявлений примерно одинаковая"
    diff_pct = abs(price1 - price2) / max(min(price1, price2), 1) * 100
    return f"Оригинал дешевле примерно на {diff_pct:.1f}%" if price1 < price2 else f"Аналог дешевле примерно на {diff_pct:.1f}%"


def safe_json_loads(content: str) -> Optional[dict]:
    if not content:
        return None
    cleaned = content.replace("```json", "").replace("```", "").strip()
    start = cleaned.find("{")
    if start == -1:
        return None
    bracket_count = 0
    end = -1
    for i in range(start, len(cleaned)):
        if cleaned[i] == "{":
            bracket_count += 1
        elif cleaned[i] == "}":
            bracket_count -= 1
            if bracket_count == 0:
                end = i
                break
    if end == -1 or end <= start:
        return None
    candidate = cleaned[start:end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        for match in re.finditer(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', cleaned):
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                continue
        return None


def extract_year_from_text(text: str) -> Optional[int]:
    match = re.search(r"(20[0-4]\d|19\d{2})", text or "")
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def extract_power(text: str) -> Optional[str]:
    match = re.search(r"(\d{2,4})\s*(л\.?с\.?|hp)", text or "", flags=re.IGNORECASE)
    return match.group(1) if match else None


def extract_mileage(text: str) -> Optional[str]:
    match = re.search(r"(\d[\d\s]{2,6})\s*(км|km)", text or "", flags=re.IGNORECASE)
    return normalize_whitespace(match.group(0)) if match else None


def extract_query_constraints(query: str) -> tuple[str, Optional[int]]:
    normalized = normalize_whitespace(query)
    if not normalized:
        return "", None
    requested_year = extract_year_from_text(normalized)
    if requested_year is None:
        return normalized, None
    parts: list[str] = []
    year_removed = False
    for part in normalized.split():
        digits = re.sub(r"[^\d]", "", part)
        if not year_removed and digits == str(requested_year):
            year_removed = True
            continue
        parts.append(part)
    model_name = normalize_whitespace(" ".join(parts))
    return model_name or normalized, requested_year


def is_relevant_avito_title(title: str, model_name: str) -> bool:
    if not model_name:
        return True
    title_lower = title.lower()
    keywords = [w for w in re.split(r"\s+", model_name.lower()) if w]
    return all(keyword in title_lower for keyword in keywords)


def _extract_year_from_text(text: str) -> Optional[int]:
    """Алиас для обратной совместимости со старыми модулями парсинга."""
    return extract_year_from_text(text)


def _extract_power(text: str) -> Optional[str]:
    """Алиас для обратной совместимости со старыми модулями парсинга."""
    return extract_power(text)


def _extract_mileage(text: str) -> Optional[str]:
    """Алиас для обратной совместимости со старыми модулями парсинга."""
    return extract_mileage(text)
