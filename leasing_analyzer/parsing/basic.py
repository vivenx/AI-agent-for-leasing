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


def parse_page_basic(html_content: str, model_name: str) -> BasicParseResult:
    """Extract basic structured data from HTML using regex."""
    result: BasicParseResult = {}
    if not html_content:
        return result
    
    soup = BeautifulSoup(html_content, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "noscript", "iframe"]):
        tag.decompose()
    text = soup.get_text(separator=" ", strip=True)

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

    # Vendor heuristic from model name
    if model_name:
        result.setdefault("vendor", model_name.split()[0])

    return result