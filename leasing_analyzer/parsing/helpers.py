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
    """Проверяет качество данных предложения и возвращает `(is_valid, reason)`."""
    if not title or len(title.strip()) < 3:
        return False, "Title too short"
    
    if not url or not is_valid_url(url):
        return False, "Invalid URL"
    
    # Проверяем, есть ли хотя бы какие-то полезные данные
    has_price = merged.get("price") is not None
    has_monthly = merged.get("monthly_payment") is not None
    has_price_request = merged.get("price_on_request", False)
    
    if not (has_price or has_monthly or has_price_request):
        # Разрешаем предложения без цены, если в них есть другие полезные данные
        has_other_data = any([
            merged.get("year"),
            merged.get("vendor"),
            merged.get("model"),
            merged.get("specs"),
        ])
        if not has_other_data:
            return False, "No meaningful data"
    
    # Валидируем цену, если она присутствует
    price = merged.get("price")
    if price is not None:
        if price < 0:
            return False, "Negative price"
        if price > 10**12:  # Нереалистично большая цена
            return False, "Price too large"
    
    return True, "OK"


def normalize_model_name(model_name: str) -> str:
    """Нормализует название модели: капитализация и удаление лишних пробелов."""
    if not model_name:
        return ""
    # Делаем заглавной первую букву каждого слова
    words = model_name.split()
    normalized = " ".join(word.capitalize() if word else "" for word in words)
    return normalized.strip()


def normalize_vendor_name(vendor: Optional[str]) -> Optional[str]:
    """Нормализует название производителя с учетом типовых сокращений."""
    if not vendor:
        return None
    
    # Типовые нормализации названий производителей
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
    
    # Делаем первую букву заглавной
    return vendor.capitalize()


def enrich_offer_data(merged: dict, title: str, text: str = "") -> dict:
    """Обогащает данные предложения дополнительной извлеченной информацией."""
    enriched = dict(merged)
    
    # Извлекаем телефон, если его еще нет
    if "phone" not in enriched and text:
        phone_match = re.search(r'[\+]?[7-8]?[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}', text)
        if phone_match:
            enriched["phone"] = phone_match.group(0)
    
    # Извлекаем email, если его еще нет
    if "email" not in enriched and text:
        email_match = re.search(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b', text)
        if email_match:
            enriched["email"] = email_match.group(0)
    
    # Нормализуем производителя
    if enriched.get("vendor"):
        enriched["vendor"] = normalize_vendor_name(enriched["vendor"])
    
    # Нормализуем модель, если она есть
    if enriched.get("model"):
        enriched["model"] = normalize_model_name(enriched["model"])
    
    # Извлекаем состояние из текста, если оно еще не задано
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
    """Создает `LeasingOffer` из объединенных результатов парсинга с валидацией и обогащением."""
    # Обогащаем данные
    enriched = enrich_offer_data(merged, title, text)
    
    # Валидируем данные
    is_valid, reason = validate_offer_data(title, url, enriched)
    if not is_valid:
        logger.debug(f"Skipping invalid offer: {reason} - {title[:50]}")
        return None
    
    price = enriched.get("price")
    currency = enriched.get("currency", "RUB")
    
    # Нормализуем цену в RUB
    normalized_price = normalize_price(price, currency)
    
    # Нормализуем название модели
    normalized_model = normalize_model_name(model_name) if model_name else ""
    
    # Нормализуем заголовок, убирая лишние пробелы
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
        currency="RUB",  # После нормализации всегда RUB
        pros=ensure_list_str(enriched.get("pros")),
        cons=ensure_list_str(enriched.get("cons")),
        analogs=ensure_list_str(enriched.get("analogs_mentioned")),
    )


# =============================
# Дедупликация предложений
# =============================
def normalize_offer_title(title: str) -> str:
    """Нормализует заголовок предложения для сравнения."""
    if not title:
        return ""
    # Переводим в нижний регистр, убираем лишние пробелы и спецсимволы
    normalized = re.sub(r'[^\w\s]', '', title.lower())
    normalized = re.sub(r'\s+', ' ', normalized).strip()
    return normalized


def normalize_url_for_comparison(url: str) -> str:
    """Нормализует URL для поиска дубликатов."""
    if not url:
        return ""
    try:
        parsed = urlparse(url)
        # Убираем www, завершающие слеши, фрагменты и query-параметры для сравнения
        netloc = parsed.netloc.replace("www.", "").lower()
        path = parsed.path.rstrip("/").lower()
        return f"{parsed.scheme}://{netloc}{path}"
    except Exception:
        return url.lower()


def are_offers_similar(offer1: "LeasingOffer", offer2: "LeasingOffer", similarity_threshold: float = 0.85) -> bool:
    """Проверяет, похожи ли два предложения по нескольким критериям."""
    # Сходство URL: точное совпадение после нормализации
    url1 = normalize_url_for_comparison(offer1.url)
    url2 = normalize_url_for_comparison(offer2.url)
    if url1 == url2 and url1:
        return True
    
    # Сходство заголовков: нечеткое сравнение
    title1 = normalize_offer_title(offer1.title)
    title2 = normalize_offer_title(offer2.title)
    if title1 and title2:
        # Простая метрика: проверяем, насколько слова одного заголовка входят в другой
        words1 = set(title1.split())
        words2 = set(title2.split())
        if len(words1) > 0 and len(words2) > 0:
            intersection = words1 & words2
            union = words1 | words2
            similarity = len(intersection) / len(union) if union else 0
            if similarity >= similarity_threshold:
                # Дополнительно проверяем близость цены
                if offer1.price and offer2.price:
                    price_diff = abs(offer1.price - offer2.price) / max(offer1.price, offer2.price)
                    if price_diff < 0.05:  # Разница в цене менее 5%
                        return True
    
    return False


def deduplicate_offers(offers: list["LeasingOffer"]) -> list["LeasingOffer"]:
    """Удаляет дубликаты предложений по URL и сходству содержимого."""
    if not offers:
        return []
    
    seen_urls = set()
    unique_offers = []
    duplicates_removed = 0
    
    for offer in offers:
        # Пропускаем невалидные предложения
        if not offer.url or not offer.title:
            logger.debug(f"Skipping offer with missing URL or title")
            duplicates_removed += 1
            continue
        
        # Нормализуем URL для сравнения
        normalized_url = normalize_url_for_comparison(offer.url)
        
        # Проверяем точное совпадение URL
        if normalized_url in seen_urls:
            logger.debug(f"Skipping duplicate URL: {normalized_url}")
            duplicates_removed += 1
            continue
        
        # Проверяем похожие предложения по содержимому
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
