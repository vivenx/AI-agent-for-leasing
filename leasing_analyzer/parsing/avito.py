from __future__ import annotations

import re

from bs4 import BeautifulSoup

from leasing_analyzer.core.models import LeasingOffer
from leasing_analyzer.core.utils import (
    _extract_mileage,
    _extract_power,
    _extract_year_from_text,
    digits_to_int,
    normalize_url,
    normalize_whitespace,
    safe_json_loads,
)
from leasing_analyzer.parsing.helpers import create_offer_from_merged


def is_relevant_avito_title(title: str, model_name: str) -> bool:
    """Checks whether the title contains all target model keywords."""
    if not model_name:
        return True
    title_lower = title.lower()
    keywords = [word for word in re.split(r"\s+", model_name.lower()) if word]
    return all(keyword in title_lower for keyword in keywords)


def extract_offers_from_ld_json(html: str) -> list[dict]:
    """Extracts structured items from JSON-LD blocks."""
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

            if "itemListElement" in item and isinstance(item["itemListElement"], list):
                for sub_item in item["itemListElement"]:
                    if isinstance(sub_item, dict) and isinstance(sub_item.get("item"), dict):
                        offers.append(sub_item["item"])

    return offers


def extract_avito_seller_data(card) -> dict:
    """Extracts seller name/profile from an Avito card when available."""
    seller_name = None
    seller_profile_url = None

    seller_selectors = [
        '[data-marker="seller-info/name"]',
        '[data-marker="seller-info/link"]',
        '[data-marker="item-seller-info"]',
        '[data-marker="item-author"]',
        '[data-marker="seller-name"]',
    ]

    for selector in seller_selectors:
        seller_tag = card.select_one(selector)
        if not seller_tag:
            continue

        seller_name = normalize_whitespace(seller_tag.get_text(" ", strip=True)) or seller_name
        href = seller_tag.get("href") if getattr(seller_tag, "attrs", None) else None
        if href:
            seller_profile_url = normalize_url(href)
        if seller_name or seller_profile_url:
            break

    if not seller_profile_url:
        for link in card.select("a[href]"):
            href = (link.get("href") or "").strip()
            if not href:
                continue
            if re.search(r"/(user|profile|shop|company|seller)/", href, flags=re.IGNORECASE):
                seller_profile_url = normalize_url(href)
                if not seller_name:
                    seller_name = normalize_whitespace(link.get_text(" ", strip=True))
                break

    return {
        "seller_name": seller_name or None,
        "seller_profile_url": seller_profile_url or None,
    }


def extract_structured_seller_data(item: dict) -> dict:
    """Extracts seller data from JSON-LD blocks when present."""
    seller = item.get("seller")
    if not seller and isinstance(item.get("offers"), dict):
        seller = item["offers"].get("seller")
    if not seller:
        seller = item.get("author")

    seller_name = None
    seller_profile_url = None

    if isinstance(seller, dict):
        seller_name = seller.get("name") or seller.get("legalName")
        seller_profile_url = seller.get("url")
    elif isinstance(seller, str):
        seller_name = seller

    return {
        "seller_name": normalize_whitespace(str(seller_name)) if seller_name else None,
        "seller_profile_url": normalize_url(str(seller_profile_url)) if seller_profile_url else None,
    }


def parse_avito_list_page(html: str, model_name: str) -> list[LeasingOffer]:
    """Parses Avito search/list pages into offers."""
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
        if price_tag:
            price_text = price_tag.get("content") or price_tag.get_text(" ", strip=True)
            price_val = digits_to_int(price_text)

        location_tag = card.select_one('[data-marker="item-location"]')
        location = normalize_whitespace(location_tag.get_text(" ", strip=True)) if location_tag else None

        subtitle = card.get_text(" ", strip=True)
        year = _extract_year_from_text(subtitle)
        power = _extract_power(subtitle)
        mileage = _extract_mileage(subtitle)

        merged_data = {
            "price": price_val,
            "year": year,
            "power": power,
            "mileage": mileage,
            "location": location,
        }
        merged_data.update(extract_avito_seller_data(card))

        offer = create_offer_from_merged(
            title=title,
            url=url,
            domain="avito.ru",
            model_name=model_name,
            merged=merged_data,
            text=subtitle,
        )

        if offer:
            offers.append(offer)

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
        merged_data.update(extract_structured_seller_data(item))

        offer = create_offer_from_merged(
            title=name or "Offer",
            url=normalize_url(href),
            domain="avito.ru",
            model_name=model_name,
            merged=merged_data,
            text=name,
        )

        if offer:
            offers.append(offer)

    return offers
