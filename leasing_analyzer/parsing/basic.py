from __future__ import annotations

import re

from bs4 import BeautifulSoup

from leasing_analyzer.core.models import BasicParseResult
from leasing_analyzer.core.utils import (
    digits_to_int,
    _extract_year_from_text,
    _extract_power,
    _extract_mileage,
)


def extract_visible_text(html_content: str) -> str:
    """Извлекает видимый текст страницы без служебных блоков."""
    if not html_content:
        return ""

    soup = BeautifulSoup(html_content, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "noscript", "iframe"]):
        tag.decompose()
    return soup.get_text(separator=" ", strip=True)


def parse_page_basic(html_content: str, model_name: str) -> BasicParseResult:
    """Извлекает базовые структурированные данные из HTML с помощью regex."""
    result: BasicParseResult = {}
    if not html_content:
        return result
    
    text = extract_visible_text(html_content)

    if re.search(r"цена\s+по\s+запросу|по\s+договоренности", text, flags=re.IGNORECASE):
        result["price_on_request"] = True

    price = digits_to_int(text)
    if price:
        result["price"] = price

    year = _extract_year_from_text(text)
    if year:
        result["year"] = year

    power = _extract_power(text)
    if power:
        result["power"] = power

    mileage = _extract_mileage(text)
    if mileage:
        result["mileage"] = mileage

    # Эвристически определяем производителя из названия модели
    if model_name:
        result.setdefault("vendor", model_name.split()[0])

    return result
