from __future__ import annotations

import re

from bs4 import BeautifulSoup

from leasing_analyzer.core.models import LeasingOffer
from leasing_analyzer.core.utils import (
    digits_to_int,
    format_price,
    normalize_url,
    normalize_whitespace,
    safe_json_loads,
    _extract_year_from_text,
    _extract_power,
    _extract_mileage,
)
from leasing_analyzer.parsing.helpers import create_offer_from_merged


def is_relevant_avito_title(title: str, model_name: str) -> bool:
    """Check if all model keywords are present in title."""
    if not model_name:
        return True
    title_lower = title.lower()
    keywords = [w for w in re.split(r"\s+", model_name.lower()) if w]
    return all(k in title_lower for k in keywords)

def extract_offers_from_ld_json(html: str) -> list[dict]:
    """Extract structured data from JSON-LD scripts."""
    offers = []
    soup = BeautifulSoup(html or "", "html.parser")
    scripts = soup.find_all("script", {"type": "application/ld+json"})
    
    for script in scripts:
        data = safe_json_loads(script.get_text(strip=True))
        if not data:
            continue
        items = data if isinstance(data, list) else [data]
        
        for item in items:
            if not isinstance(item, dict):
                continue
            item_type = item.get("@type", "")
            if isinstance(item_type, list):
                item_type = ",".join(item_type)
            if "Offer" in item_type or "Product" in item_type:
                offers.append(item)
            # Some Avito pages embed LD inside "itemListElement"
            if "itemListElement" in item and isinstance(item["itemListElement"], list):
                for sub in item["itemListElement"]:
                    if isinstance(sub, dict) and "item" in sub and isinstance(sub["item"], dict):
                        offers.append(sub["item"])
    return offers


def parse_avito_list_page(html: str, model_name: str) -> list[LeasingOffer]:
    """Parse Avito listing page and extract offers."""
    soup = BeautifulSoup(html or "", "html.parser")
    cards = soup.select('[data-marker="item"]')
    if not cards:
        cards = soup.select("div.iva-item-root")

    offers: list[LeasingOffer] = []
    
    for card in cards:
        title_tag = card.select_one('[data-marker="item-title"]') or card.select_one("a")
        if not title_tag:
            continue
        title = normalize_whitespace(title_tag.get_text(" ", strip=True))
        if not is_relevant_avito_title(title, model_name):
            continue

        href = title_tag.get("href") or ""
        url = normalize_url(href)
        
        price_tag = card.select_one('[data-marker="item-price"]') or card.select_one("meta[itemprop='price']")
        price_val = None
        price_display = None
        if price_tag:
            price_text = price_tag.get("content") or price_tag.get_text(" ", strip=True)
            price_val = digits_to_int(price_text)
            price_display = format_price(price_val) if price_val else None

        location_tag = card.select_one('[data-marker="item-location"]')
        location = normalize_whitespace(location_tag.get_text(" ", strip=True)) if location_tag else None

        subtitle = card.get_text(" ", strip=True)
        year = _extract_year_from_text(subtitle)
        power = _extract_power(subtitle)
        mileage = _extract_mileage(subtitle)
        
        # Create merged dict for validation
        merged_data = {
            "price": price_val,
            "year": year,
            "power": power,
            "mileage": mileage,
            "location": location,
        }
        
        # Validate and create offer using improved function
        offer = create_offer_from_merged(
            title=title,
            url=url,
            domain="avito.ru",
            model_name=model_name,
            merged=merged_data,
            text=subtitle
        )
        
        if offer:
            offers.append(offer)

    # JSON-LD as supplemental source
    for item in extract_offers_from_ld_json(html):
        name = normalize_whitespace(item.get("name", "") or item.get("title", ""))
        href = item.get("url") or item.get("mainEntityOfPage") or ""
        price_val = digits_to_int(str(item.get("price", "")))
        if not name and not href:
            continue
        if not is_relevant_avito_title(name, model_name):
            continue
        
        merged_data = {
            "price": price_val,
            "location": normalize_whitespace(item.get("address", "")) or None,
            "year": _extract_year_from_text(name),
        }
        
        offer = create_offer_from_merged(
            title=name or "Offer",
            url=normalize_url(href),
            domain="avito.ru",
            model_name=model_name,
            merged=merged_data,
            text=name
        )
        
        if offer:
            offers.append(offer)

    return offers
