from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlparse

from leasing_analyzer.core.logging import get_logger
from leasing_analyzer.core.models import LeasingOffer
from leasing_analyzer.core.utils import (
    ensure_list_str,
    format_price,
    is_valid_url,
    normalize_price,
    normalize_whitespace,
)

logger = get_logger(__name__)


def validate_offer_data(title: str, url: str, merged: dict) -> tuple[bool, str]:
    """Validate offer data quality. Returns (is_valid, reason)."""
    if not title or len(title.strip()) < 3:
        return False, "Title too short"
    
    if not url or not is_valid_url(url):
        return False, "Invalid URL"
    
    # Check if we have at least some meaningful data
    has_price = merged.get("price") is not None
    has_monthly = merged.get("monthly_payment") is not None
    has_price_request = merged.get("price_on_request", False)
    
    if not (has_price or has_monthly or has_price_request):
        # Allow offers without price if they have other useful data
        has_other_data = any([
            merged.get("year"),
            merged.get("vendor"),
            merged.get("model"),
            merged.get("specs"),
        ])
        if not has_other_data:
            return False, "No meaningful data"
    
    # Validate price if present
    price = merged.get("price")
    if price is not None:
        if price < 0:
            return False, "Negative price"
        if price > 10**12:  # Unrealistically large price
            return False, "Price too large"
    
    return True, "OK"


def normalize_model_name(model_name: str) -> str:
    """Normalize model name (capitalize, remove extra spaces)."""
    if not model_name:
        return ""
    # Capitalize first letter of each word
    words = model_name.split()
    normalized = " ".join(word.capitalize() if word else "" for word in words)
    return normalized.strip()


def normalize_vendor_name(vendor: Optional[str]) -> Optional[str]:
    """Normalize vendor name (capitalize, common abbreviations)."""
    if not vendor:
        return None
    
    # Common vendor normalizations
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
    
    # Capitalize first letter
    return vendor.capitalize()


def enrich_offer_data(merged: dict, title: str, text: str = "") -> dict:
    """Enrich offer data with additional extracted information."""
    enriched = dict(merged)
    
    # Extract phone number if not present
    if "phone" not in enriched and text:
        phone_match = re.search(r'[\+]?[7-8]?[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}', text)
        if phone_match:
            enriched["phone"] = phone_match.group(0)
    
    # Extract email if not present
    if "email" not in enriched and text:
        email_match = re.search(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b', text)
        if email_match:
            enriched["email"] = email_match.group(0)
    
    # Normalize vendor
    if enriched.get("vendor"):
        enriched["vendor"] = normalize_vendor_name(enriched["vendor"])
    
    # Normalize model if present
    if enriched.get("model"):
        enriched["model"] = normalize_model_name(enriched["model"])
    
    # Extract condition from text if not present
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
    text: str = ""
) -> Optional["LeasingOffer"]:
    """Create LeasingOffer from merged parsing results with validation and enrichment."""
    # Enrich data
    enriched = enrich_offer_data(merged, title, text)
    
    # Validate data
    is_valid, reason = validate_offer_data(title, url, enriched)
    if not is_valid:
        logger.debug(f"Skipping invalid offer: {reason} - {title[:50]}")
        return None
    
    price = enriched.get("price")
    currency = enriched.get("currency", "RUB")
    
    # Normalize price to RUB
    normalized_price = normalize_price(price, currency)
    
    # Normalize model name
    normalized_model = normalize_model_name(model_name) if model_name else ""
    
    # Normalize title (remove extra spaces)
    normalized_title = normalize_whitespace(title)
    
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
        specs=enriched.get("specs", {}),
        category=enriched.get("category"),
        currency="RUB",  # Always RUB after normalization
        pros=ensure_list_str(enriched.get("pros")),
        cons=ensure_list_str(enriched.get("cons")),
        analogs=ensure_list_str(enriched.get("analogs_mentioned")),
    )


# =============================
# Offer Deduplication
# =============================
def normalize_offer_title(title: str) -> str:
    """Normalize offer title for comparison (lowercase, remove extra spaces, special chars)."""
    if not title:
        return ""
    # Convert to lowercase, remove extra spaces, remove special punctuation
    normalized = re.sub(r'[^\w\s]', '', title.lower())
    normalized = re.sub(r'\s+', ' ', normalized).strip()
    return normalized


def normalize_url_for_comparison(url: str) -> str:
    """Normalize URL for duplicate detection."""
    if not url:
        return ""
    try:
        parsed = urlparse(url)
        # Remove www, trailing slashes, fragments, query params for comparison
        netloc = parsed.netloc.replace("www.", "").lower()
        path = parsed.path.rstrip("/").lower()
        return f"{parsed.scheme}://{netloc}{path}"
    except Exception:
        return url.lower()


def are_offers_similar(offer1: "LeasingOffer", offer2: "LeasingOffer", similarity_threshold: float = 0.85) -> bool:
    """Check if two offers are similar based on multiple criteria."""
    # URL similarity (exact match after normalization)
    url1 = normalize_url_for_comparison(offer1.url)
    url2 = normalize_url_for_comparison(offer2.url)
    if url1 == url2 and url1:
        return True
    
    # Title similarity (fuzzy matching)
    title1 = normalize_offer_title(offer1.title)
    title2 = normalize_offer_title(offer2.title)
    if title1 and title2:
        # Simple similarity: check if one title contains most words of another
        words1 = set(title1.split())
        words2 = set(title2.split())
        if len(words1) > 0 and len(words2) > 0:
            intersection = words1 & words2
            union = words1 | words2
            similarity = len(intersection) / len(union) if union else 0
            if similarity >= similarity_threshold:
                # Also check price similarity
                if offer1.price and offer2.price:
                    price_diff = abs(offer1.price - offer2.price) / max(offer1.price, offer2.price)
                    if price_diff < 0.05:  # Less than 5% price difference
                        return True
    
    return False


def deduplicate_offers(offers: list["LeasingOffer"]) -> list["LeasingOffer"]:
    """Remove duplicate offers based on URL and content similarity."""
    if not offers:
        return []
    
    seen_urls = set()
    unique_offers = []
    duplicates_removed = 0
    
    for offer in offers:
        # Skip invalid offers
        if not offer.url or not offer.title:
            logger.debug(f"Skipping offer with missing URL or title")
            duplicates_removed += 1
            continue
        
        # Normalize URL for comparison
        normalized_url = normalize_url_for_comparison(offer.url)
        
        # Check for exact URL match
        if normalized_url in seen_urls:
            logger.debug(f"Skipping duplicate URL: {normalized_url}")
            duplicates_removed += 1
            continue
        
        # Check for similar offers (content-based deduplication)
        is_duplicate = False
        for existing_offer in unique_offers:
            if are_offers_similar(offer, existing_offer):
                logger.debug(f"Skipping similar offer: {offer.title[:50]}... (similar to {existing_offer.title[:50]}...)")
                duplicates_removed += 1
                is_duplicate = True
                break
        
        if not is_duplicate:
            seen_urls.add(normalized_url)
            unique_offers.append(offer)
    
    if duplicates_removed > 0:
        logger.info(f"Removed {duplicates_removed} duplicate/similar offers (kept {len(unique_offers)} unique)")
    
    return unique_offers