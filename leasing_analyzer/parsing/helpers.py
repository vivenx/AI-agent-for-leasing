from __future__ import annotations

import re
import statistics
from typing import Optional
from urllib.parse import urlparse

from leasing_analyzer.core.logging import get_logger
from leasing_analyzer.core.models import LeasingOffer
from leasing_analyzer.core.utils import (
    ensure_list_str,
    format_price,
    is_valid_url,
    normalize_price,
    normalize_url,
    normalize_whitespace,
)

logger = get_logger(__name__)

GENERIC_SELLER_NAMES = {
    "company",
    "contact",
    "seller",
    "shop",
    "компания",
    "контакт",
    "контактное лицо",
    "магазин",
    "организация",
    "продавец",
    "частное лицо",
    "частник",
}


def normalize_phone(phone: Optional[str]) -> Optional[str]:
    """Normalizes phone to a comparable 11-digit form."""
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 10:
        digits = "7" + digits
    elif len(digits) == 11 and digits[0] in {"7", "8"}:
        digits = "7" + digits[1:]

    if len(digits) != 11:
        return None
    return digits


def normalize_email(email: Optional[str]) -> Optional[str]:
    """Normalizes email for seller comparison."""
    value = normalize_whitespace(email or "").lower()
    return value or None


def normalize_model_name(model_name: str) -> str:
    """Normalizes model name casing and whitespace."""
    if not model_name:
        return ""
    words = model_name.split()
    normalized = " ".join(word.capitalize() if word else "" for word in words)
    return normalized.strip()


def normalize_vendor_name(vendor: Optional[str]) -> Optional[str]:
    """Normalizes vendor name using a small dictionary of common aliases."""
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

    vendor_lower = vendor.lower().strip()
    if vendor_lower in vendor_map:
        return vendor_map[vendor_lower]

    return vendor.capitalize()


def normalize_seller_name(name: Optional[str]) -> Optional[str]:
    """Cleans seller name and removes generic placeholders."""
    value = normalize_whitespace(name or "")
    if not value:
        return None

    value = re.sub(
        r"^(продавец|контактное лицо|компания|организация|дилер|seller|contact|company)\s*[:\-]\s*",
        "",
        value,
        flags=re.IGNORECASE,
    ).strip(" -,:")

    if len(value) < 2:
        return None

    if value.casefold() in GENERIC_SELLER_NAMES:
        return None

    return value


def extract_seller_name(text: str) -> Optional[str]:
    """Extracts seller name from visible page text when possible."""
    normalized_text = normalize_whitespace(text)
    if not normalized_text:
        return None

    patterns = [
        r"(?:продавец|контактное лицо|компания|организация|дилер)\s*[:\-]\s*([A-Za-zА-Яа-я0-9\"'()«»._ -]{2,80})",
        r"(?:seller|company|contact)\s*[:\-]\s*([A-Za-zА-Яа-я0-9\"'()«»._ -]{2,80})",
    ]

    for pattern in patterns:
        match = re.search(pattern, normalized_text, flags=re.IGNORECASE)
        if not match:
            continue

        candidate = re.split(
            r"(?:телефон|phone|email|e-mail|почта)\s*[:\-]",
            match.group(1),
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0]
        seller_name = normalize_seller_name(candidate)
        if seller_name:
            return seller_name

    return None


def validate_offer_data(title: str, url: str, merged: dict) -> tuple[bool, str]:
    """Validates parsed offer data and returns `(is_valid, reason)`."""
    if not title or len(title.strip()) < 3:
        return False, "Title too short"

    if not url or not is_valid_url(url):
        return False, "Invalid URL"

    has_price = merged.get("price") is not None
    has_monthly = merged.get("monthly_payment") is not None
    has_price_request = merged.get("price_on_request", False)

    if not (has_price or has_monthly or has_price_request):
        has_other_data = any(
            [
                merged.get("year"),
                merged.get("vendor"),
                merged.get("model"),
                merged.get("specs"),
            ]
        )
        if not has_other_data:
            return False, "No meaningful data"

    price = merged.get("price")
    if price is not None:
        if price < 0:
            return False, "Negative price"
        if price > 10**12:
            return False, "Price too large"

    return True, "OK"


def enrich_offer_data(merged: dict, title: str, text: str = "") -> dict:
    """Adds extracted contact and normalization data to raw offer fields."""
    enriched = dict(merged)

    if "phone" not in enriched and text:
        phone_match = re.search(r"[\+]?[7-8]?[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}", text)
        if phone_match:
            enriched["phone"] = phone_match.group(0)

    if "email" not in enriched and text:
        email_match = re.search(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b", text)
        if email_match:
            enriched["email"] = email_match.group(0)

    if not enriched.get("seller_name") and text:
        seller_name = extract_seller_name(text)
        if seller_name:
            enriched["seller_name"] = seller_name

    if enriched.get("vendor"):
        enriched["vendor"] = normalize_vendor_name(enriched["vendor"])

    if enriched.get("model"):
        enriched["model"] = normalize_model_name(enriched["model"])

    if enriched.get("phone"):
        enriched["phone"] = normalize_phone(enriched.get("phone"))

    if enriched.get("email"):
        enriched["email"] = normalize_email(enriched.get("email"))

    if enriched.get("seller_name"):
        enriched["seller_name"] = normalize_seller_name(enriched.get("seller_name"))

    if not enriched.get("condition") and text:
        condition_patterns = {
            "новый": "новый",
            "новое": "новый",
            "б/у": "б/у",
            "б у": "б/у",
            "бывший в употреблении": "б/у",
            "used": "б/у",
            "new": "новый",
        }
        text_lower = text.lower()
        for pattern, condition in condition_patterns.items():
            if pattern in text_lower:
                enriched["condition"] = condition
                break

    return enriched


def create_offer_from_merged(
    title: str,
    url: str,
    domain: str,
    model_name: str,
    merged: dict,
    text: str = "",
) -> Optional["LeasingOffer"]:
    """Builds `LeasingOffer` from merged parser output."""
    enriched = enrich_offer_data(merged, title, text)

    is_valid, reason = validate_offer_data(title, url, enriched)
    if not is_valid:
        logger.debug("Skipping invalid offer: %s - %s", reason, title[:50])
        return None

    price = enriched.get("price")
    currency = enriched.get("currency", "RUB")
    normalized_price = normalize_price(price, currency)
    normalized_model = normalize_model_name(model_name) if model_name else ""
    normalized_title = normalize_whitespace(title)

    seller_profile_url = enriched.get("seller_profile_url")
    if seller_profile_url:
        seller_profile_url = normalize_url(str(seller_profile_url), base=f"https://{domain}")

    return LeasingOffer(
        title=normalized_title,
        url=url,
        source=domain,
        model=normalized_model,
        price=normalized_price,
        price_str=format_price(normalized_price),
        monthly_payment=enriched.get("monthly_payment"),
        monthly_payment_str=format_price(enriched.get("monthly_payment")),
        price_on_request=enriched.get("price_on_request", False),
        year=enriched.get("year"),
        power=enriched.get("power"),
        mileage=enriched.get("mileage"),
        vendor=normalize_vendor_name(enriched.get("vendor")),
        condition=enriched.get("condition"),
        location=enriched.get("location"),
        seller_name=normalize_seller_name(enriched.get("seller_name")),
        seller_phone=normalize_phone(enriched.get("phone")),
        seller_email=normalize_email(enriched.get("email")),
        seller_profile_url=seller_profile_url,
        specs=enriched.get("specs", {}),
        category=enriched.get("category"),
        currency="RUB",
        pros=ensure_list_str(enriched.get("pros")),
        cons=ensure_list_str(enriched.get("cons")),
        analogs=ensure_list_str(enriched.get("analogs_mentioned")),
    )


def normalize_offer_title(title: str) -> str:
    """Normalizes offer title for duplicate checks."""
    if not title:
        return ""
    normalized = re.sub(r"[^\w\s]", "", title.lower())
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def normalize_url_for_comparison(url: str) -> str:
    """Normalizes URL for duplicate checks."""
    if not url:
        return ""
    try:
        parsed = urlparse(url)
        netloc = parsed.netloc.replace("www.", "").lower()
        path = parsed.path.rstrip("/").lower()
        return f"{parsed.scheme}://{netloc}{path}"
    except Exception:
        return url.lower()


def are_offers_similar(
    offer1: "LeasingOffer",
    offer2: "LeasingOffer",
    similarity_threshold: float = 0.85,
) -> bool:
    """Checks whether two offers look like duplicates by URL/title/price."""
    url1 = normalize_url_for_comparison(offer1.url)
    url2 = normalize_url_for_comparison(offer2.url)
    if url1 == url2 and url1:
        return True

    title1 = normalize_offer_title(offer1.title)
    title2 = normalize_offer_title(offer2.title)
    if title1 and title2:
        words1 = set(title1.split())
        words2 = set(title2.split())
        if words1 and words2:
            intersection = words1 & words2
            union = words1 | words2
            similarity = len(intersection) / len(union) if union else 0
            if similarity >= similarity_threshold and offer1.price and offer2.price:
                price_diff = abs(offer1.price - offer2.price) / max(offer1.price, offer2.price)
                if price_diff < 0.05:
                    return True

    return False


def deduplicate_offers(offers: list["LeasingOffer"]) -> list["LeasingOffer"]:
    """Removes duplicate offers by URL and near-identical content."""
    if not offers:
        return []

    seen_urls = set()
    unique_offers = []
    duplicates_removed = 0

    for offer in offers:
        if not offer.url or not offer.title:
            logger.debug("Skipping offer with missing URL or title")
            duplicates_removed += 1
            continue

        normalized_url = normalize_url_for_comparison(offer.url)
        if normalized_url in seen_urls:
            logger.debug("Skipping duplicate URL: %s", normalized_url)
            duplicates_removed += 1
            continue

        is_duplicate = False
        for existing_offer in unique_offers:
            if are_offers_similar(offer, existing_offer):
                logger.debug(
                    "Skipping similar offer: %s... (similar to %s...)",
                    offer.title[:50],
                    existing_offer.title[:50],
                )
                duplicates_removed += 1
                is_duplicate = True
                break

        if not is_duplicate:
            seen_urls.add(normalized_url)
            unique_offers.append(offer)

    if duplicates_removed > 0:
        logger.info(
            "Removed %s duplicate/similar offers (kept %s unique)",
            duplicates_removed,
            len(unique_offers),
        )

    return unique_offers


def get_seller_keys(offer: "LeasingOffer") -> set[str]:
    """Builds a set of seller identifiers for fuzzy seller deduplication."""
    seller_keys: set[str] = set()

    if offer.seller_profile_url:
        seller_keys.add(f"profile:{normalize_url_for_comparison(offer.seller_profile_url)}")

    if offer.seller_phone:
        phone = normalize_phone(offer.seller_phone)
        if phone:
            seller_keys.add(f"phone:{phone}")

    if offer.seller_email:
        email = normalize_email(offer.seller_email)
        if email:
            seller_keys.add(f"email:{email}")

    seller_name = normalize_seller_name(offer.seller_name)
    if seller_name:
        normalized_name = seller_name.casefold()
        normalized_source = normalize_whitespace(offer.source or "").casefold()
        normalized_location = normalize_whitespace(offer.location or "").casefold()

        if normalized_source:
            seller_keys.add(f"name_source:{normalized_name}|{normalized_source}")
        if normalized_location:
            seller_keys.add(f"name_location:{normalized_name}|{normalized_location}")

    return seller_keys


def choose_representative_offer(offers: list["LeasingOffer"]) -> "LeasingOffer":
    """Chooses one representative offer for a seller group."""
    if len(offers) == 1:
        return offers[0]

    priced_values = [offer.price for offer in offers if offer.price is not None]
    seller_price_median = statistics.median(priced_values) if priced_values else None

    def score(offer: "LeasingOffer") -> tuple[int, int, int, int, int, int, float, int]:
        specs_count = len(offer.specs) if isinstance(offer.specs, dict) else 0
        seller_info_count = sum(
            bool(value)
            for value in (
                offer.seller_name,
                offer.seller_phone,
                offer.seller_email,
                offer.seller_profile_url,
            )
        )
        price_distance = (
            abs(offer.price - seller_price_median)
            if seller_price_median is not None and offer.price is not None
            else float("inf")
        )

        return (
            int(offer.price is not None),
            seller_info_count,
            specs_count,
            int(bool(offer.year)),
            int(bool(offer.condition)),
            int(bool(offer.location)),
            -float(price_distance),
            offer.year or 0,
        )

    return max(offers, key=score)


def deduplicate_offers_by_seller(offers: list["LeasingOffer"]) -> list["LeasingOffer"]:
    """Keeps only one offer per seller when seller signals are available."""
    if not offers:
        return []

    groups: list[dict] = []
    unmatched: list[tuple[int, LeasingOffer]] = []

    for index, offer in enumerate(offers):
        seller_keys = get_seller_keys(offer)
        if not seller_keys:
            unmatched.append((index, offer))
            continue

        matched_group = None
        for group in groups:
            if group["keys"] & seller_keys:
                matched_group = group
                break

        if matched_group is None:
            groups.append(
                {
                    "keys": set(seller_keys),
                    "offers": [offer],
                    "first_index": index,
                }
            )
            continue

        matched_group["keys"].update(seller_keys)
        matched_group["offers"].append(offer)

    selected_offers: list[tuple[int, LeasingOffer]] = []
    removed = 0

    for group in groups:
        selected_offers.append((group["first_index"], choose_representative_offer(group["offers"])))
        removed += max(0, len(group["offers"]) - 1)

    selected_offers.extend(unmatched)
    selected_offers.sort(key=lambda item: item[0])

    if removed > 0:
        logger.info(
            "Removed %s offers from duplicate sellers (kept %s offers)",
            removed,
            len(selected_offers),
        )

    return [offer for _, offer in selected_offers]
