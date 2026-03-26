# -*- coding: utf-8 -*-
"""
Leasing asset market analyzer (CLI).

Key features:
- Dedicated Avito list-page parser (HTML only, no LLM).
- Reusable Selenium driver with configurable scroll depth.
- Safe JSON parsing of LLM output.
- JSON-LD extraction as a structured data source.
- Market price analysis with outlier filtering (IQR).
"""

import io
import json
import logging
import os
import re
import statistics
import sys
import time
import uuid
from abc import ABC, abstractmethod
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from typing import Optional, TypedDict
from urllib.parse import urljoin, urlparse

import requests
import urllib3
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from tqdm import tqdm

from dotenv import load_dotenv

load_dotenv()

# =============================
# Logging configuration
# =============================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# Ensure stdout handles UTF-8 on Windows to avoid mojibake in console.
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Configure requests connection pool
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Create session with larger connection pool
_requests_session = requests.Session()
retry_strategy = Retry(
    total=3,
    backoff_factor=1,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["POST", "GET"]
)
adapter = HTTPAdapter(
    pool_connections=10,
    pool_maxsize=20,
    max_retries=retry_strategy
)
_requests_session.mount("http://", adapter)
_requests_session.mount("https://", adapter)

# Отдельная сессия для Sonar БЕЗ автоматических retry (чтобы наша логика retry работала)
_sonar_requests_session = requests.Session()
# Создаем адаптер БЕЗ retry для Sonar
sonar_adapter = HTTPAdapter(
    pool_connections=5,
    pool_maxsize=10,
    max_retries=0  # Отключаем автоматические retry - используем нашу логику
)
_sonar_requests_session.mount("http://", sonar_adapter)
_sonar_requests_session.mount("https://", sonar_adapter)


# =============================
# Configuration
# =============================
@dataclass(frozen=True)
class Config:
    """Application configuration with sensible defaults."""
    
    # API Keys (loaded from environment)
    serper_api_key: Optional[str] = field(default_factory=lambda: os.getenv("SERPER_API_KEY"))
    perplexity_api_key: Optional[str] = field(default_factory=lambda: os.getenv("PERPLEXITY_API_KEY"))

    # Sonar (Perplexity) API settings
    sonar_base_url: Optional[str] = field(default_factory=lambda: os.getenv("PERPLEXITY_BASE_URL"))
    sonar_api_url: str = "https://api.perplexity.ai/chat/completions"
    sonar_model: str = field(default_factory=lambda: os.getenv("PERPLEXITY_MODEL", "sonar-reasoning-pro"))

    gigachat_auth_data: Optional[str] = field(default_factory=lambda: os.getenv("GIGACHAT_AUTH_DATA"))
    
    
    # HTTP settings
    http_timeout: int = 25
    http_long_timeout: int = 60  # Увеличено для стабильности Sonar
    
    # Selenium settings
    scroll_wait: float = 1.5
    default_scroll_times: int = 2
    avito_scroll_times: int = 2
    other_scroll_times: int = 3
    
    # Content processing
    max_content_length: int = 10000
    
    # Market analysis
    price_deviation_tolerance: float = 0.20
    min_valid_price: int = 100
    min_large_price: int = 10000
    outlier_min_samples: int = 5
    iqr_multiplier: float = 1.5
    
    # GigaChat settings
    gigachat_model: str = "GigaChat-2"
    gigachat_oauth_url: str = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
    gigachat_api_url: str = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"
    
    # Perplexity Sonar settings
    # Поддержка как прямого API, так и прокси через artemox.com
    sonar_base_url: Optional[str] = field(default_factory=lambda: os.getenv("PERPLEXITY_BASE_URL"))
    sonar_api_url: str = "https://api.perplexity.ai/chat/completions"  # Будет переопределено если указан base_url
    sonar_model: str = "sonar-reasoning-pro"  # Минимальные токены (sonar-reasoning-pro для прокси)
    sonar_max_analogs: int = 3  # Всегда 3 аналога
    
    # Search settings
    default_num_results: int = 5
    max_analogs: int = 5
    min_analogs_before_ai: int = 3
    
    # Domain settings
    avito_domain: str = "avito.ru"
    default_search_suffix: str = "лизинг"
    fallback_search_suffix: str = "купить"
    
    # Selenium timeout settings
    page_load_timeout: int = 45  # Increased for slow pages
    implicit_wait: int = 10
    script_timeout: int = 30  # Timeout for JavaScript execution
    
    # Parallel processing
    max_workers: int = 3
    
    # Rate limiting (more conservative to avoid 429 errors)
    google_rate_limit_calls: int = 10
    google_rate_limit_period: float = 60.0
    gigachat_rate_limit_calls: int = 15  # Reduced from 20
    gigachat_rate_limit_period: float = 60.0
    gigachat_min_delay: float = 0.5  # Minimum delay between requests (seconds)
    sonar_rate_limit_calls: int = 10
    sonar_rate_limit_period: float = 60.0
    sonar_min_delay: float = 0.3
    
    # Currency exchange rates (to RUB)
    exchange_rates: dict[str, float] = field(default_factory=lambda: {
        "USD": 100.0,
        "EUR": 110.0,
        "RUB": 1.0,
    })


# Global config instance
CONFIG = Config()


# =============================
# Rate Limiting
# =============================
class RateLimiter:
    """Thread-safe rate limiter to prevent API throttling."""
    
    def __init__(self, max_calls: int, period: float, min_delay: float = 0.0):
        self.calls = deque()
        self.max_calls = max_calls
        self.period = period
        self.min_delay = min_delay
        self.last_call_time = 0.0
        self._lock = Lock()  # Thread safety
    
    def wait_if_needed(self):
        """Wait if rate limit would be exceeded (thread-safe)."""
        with self._lock:
            now = time.time()
            
            # Enforce minimum delay between requests
            if self.min_delay > 0 and self.last_call_time > 0:
                time_since_last = now - self.last_call_time
                if time_since_last < self.min_delay:
                    sleep_time = self.min_delay - time_since_last
                    logger.debug(f"Min delay: waiting {sleep_time:.2f}s")
                    time.sleep(sleep_time)
                    now = time.time()
            
            # Remove old calls outside the period
            while self.calls and self.calls[0] < now - self.period:
                self.calls.popleft()
            
            # If at limit, wait until oldest call expires
            if len(self.calls) >= self.max_calls:
                sleep_time = self.period - (now - self.calls[0])
                if sleep_time > 0:
                    logger.debug(f"Rate limit: waiting {sleep_time:.1f}s")
                    time.sleep(sleep_time)
                    now = time.time()
                    # Re-check after sleep
                    while self.calls and self.calls[0] < now - self.period:
                        self.calls.popleft()
            
            self.calls.append(now)
            self.last_call_time = now


# Global rate limiters
google_rate_limiter = RateLimiter(CONFIG.google_rate_limit_calls, CONFIG.google_rate_limit_period)
gigachat_rate_limiter = RateLimiter(
    CONFIG.gigachat_rate_limit_calls, 
    CONFIG.gigachat_rate_limit_period,
    min_delay=CONFIG.gigachat_min_delay
)
sonar_rate_limiter = RateLimiter(
    CONFIG.sonar_rate_limit_calls,
    CONFIG.sonar_rate_limit_period,
    min_delay=CONFIG.sonar_min_delay
)


# =============================
# URL Validation
# =============================
def is_valid_url(url: str) -> bool:
    """Validate URL format."""
    if not url or not isinstance(url, str):
        return False
    try:
        result = urlparse(url)
        return all([result.scheme, result.netloc])
    except Exception:
        return False


# =============================
# Currency Normalization
# =============================
def normalize_price(price: Optional[int], currency: Optional[str]) -> Optional[int]:
    """Convert price to RUB using exchange rates."""
    if price is None:
        return None
    
    if not currency or currency.upper() == "RUB":
        return price
    
    rate = CONFIG.exchange_rates.get(currency.upper())
    if rate is None:
        logger.warning(f"Unknown currency: {currency}, assuming RUB")
        return price
    
    return int(price * rate)


# =============================
# Parser Abstraction (Strategy Pattern)
# =============================
class ParserStrategy(ABC):
    """Abstract base class for parsing strategies."""
    
    @abstractmethod
    def parse(self, html: str, url: str, model_name: str, title: str = "") -> list["LeasingOffer"]:
        """Parse HTML and return list of offers."""
        pass


class AvitoParserStrategy(ParserStrategy):
    """Parser for Avito listing pages."""
    
    def parse(self, html: str, url: str, model_name: str, title: str = "") -> list["LeasingOffer"]:
        """Parse Avito list page."""
        return parse_avito_list_page(html, model_name)


# =============================
# Offer Creation Helper
# =============================
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


# =============================
# Type definitions
# =============================
class BasicParseResult(TypedDict, total=False):
    """Result from basic HTML parsing."""
    price_on_request: bool
    price: int
    year: int
    power: str
    mileage: str
    vendor: str


class AIAnalysisResult(TypedDict, total=False):
    """Result from AI analysis."""
    category: str
    vendor: str
    model: str
    price: int
    currency: str
    monthly_payment: int
    year: int
    condition: str
    location: str
    specs: dict
    pros: list[str]
    cons: list[str]
    analogs_mentioned: list[str]


class AnalogReview(TypedDict, total=False):
    """AI review of an analog model."""
    pros: list[str]
    cons: list[str]
    price_hint: Optional[int]
    note: str
    best_link: Optional[str]


class ValidationResult(TypedDict, total=False):
    """Result of AI report validation."""
    is_valid: bool
    comment: str


class SearchResult(TypedDict):
    """Search result from Serper API."""
    title: str
    link: str
    snippet: str


class ListingSummary(TypedDict):
    """Summary of a listing for analog comparison."""
    title: str
    link: str
    snippet: str
    price_guess: Optional[int]


class SonarAnalogResult(TypedDict, total=False):
    """Result from Sonar analog search."""
    name: str
    description: str
    price_range: str
    key_difference: str


class SonarComparisonResult(TypedDict, total=False):
    """Result from Sonar offer comparison."""
    winner: str
    original_advantages: list[str]
    original_disadvantages: list[str]
    analog_advantages: list[str]
    analog_disadvantages: list[str]
    recommendation: str
    price_diff: str
    price_verdict: str
    original_url: str
    original_title: str
    original_price: Optional[int]
    analog_url: str
    analog_title: str
    analog_price: Optional[int]
    sonar_comparison: bool


class UserInput(TypedDict):
    """User input parameters."""
    item: str
    client_price: Optional[int]
    use_ai: bool
    num_results: int


# =============================
# Data structures
# =============================
@dataclass
class LeasingOffer:
    """Represents a single leasing offer."""
    title: str
    url: str
    source: str
    model: str = ""
    price: Optional[int] = None
    price_str: Optional[str] = None
    monthly_payment: Optional[int] = None
    monthly_payment_str: Optional[str] = None
    price_on_request: bool = False
    year: Optional[int] = None
    power: Optional[str] = None
    mileage: Optional[str] = None
    vendor: Optional[str] = None
    condition: Optional[str] = None
    location: Optional[str] = None
    specs: dict = field(default_factory=dict)
    category: Optional[str] = None
    currency: Optional[str] = None
    pros: list[str] = field(default_factory=list)
    cons: list[str] = field(default_factory=list)
    analogs: list[str] = field(default_factory=list)
    analogs_suggested: list[str] = field(default_factory=list)

    def has_data(self) -> bool:
        """Check if offer contains meaningful pricing data."""
        return any([
                self.price is not None,
                self.monthly_payment is not None,
                self.price_on_request,
        ])


# =============================
# Utility helpers
# =============================
def normalize_whitespace(text: str) -> str:
    """Collapse multiple whitespace characters into single space."""
    return re.sub(r"\s+", " ", text).strip()


def digits_to_int(text: str) -> Optional[int]:
    """Extract integer from text, removing non-digit characters."""
    digits = re.sub(r"[^\d]", "", text or "")
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        logger.debug(f"Failed to convert '{digits}' to int")
        return None


def format_price(value: Optional[int]) -> Optional[str]:
    """Format integer price as human-readable string with currency."""
    if value is None:
        return None
    return f"{value:,}".replace(",", " ") + " ₽"


def ensure_list_str(value) -> list[str]:
    """Ensure value is a list of non-empty strings."""
    if not value:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value).strip()]


def describe_price_difference(price1: Optional[int], price2: Optional[int]) -> str:
    """Build a short human-readable price difference summary."""
    if not price1 or not price2:
        return "Недостаточно данных по цене"
    if price1 == price2:
        return "Цена объявлений примерно одинаковая"

    diff_pct = abs(price1 - price2) / max(min(price1, price2), 1) * 100
    if price1 < price2:
        return f"Оригинал дешевле примерно на {diff_pct:.1f}%"
    return f"Аналог дешевле примерно на {diff_pct:.1f}%"


def extract_price_candidate(text: str) -> Optional[int]:
    """
    Smart extraction of price from text.
    
    Looks for numbers followed by currency markers (₽, руб, rub, $, €).
    Falls back to large numbers that aren't years (19xx, 20xx).
    """
    if not text:
        return None
    
    # Try currency patterns first
    currency_pattern = r"(\d[\d\s]*)\s*(₽|руб|rub|\$|€)"
    matches = re.findall(currency_pattern, text, flags=re.IGNORECASE)
    for match in matches:
        val = digits_to_int(match[0])
        if val and val > CONFIG.min_valid_price:
            return val
            
    # Fallback: look for generic big numbers, avoiding years
    nums = re.findall(r"\b\d[\d\s]*\b", text)
    for n in nums:
        val = digits_to_int(n)
        if not val: 
            continue
        # Avoid likely years (1900-2030)
        if 1900 <= val <= 2030:
            continue
        # Assume valid price is likely > 10000 for machinery/cars
        if val > CONFIG.min_large_price:
            return val
    return None


def normalize_url(url: str, base: str = "https://www.avito.ru") -> str:
    """Normalize relative URL to absolute."""
    if not url:
        return url
    if url.startswith("http"):
        return url
    return urljoin(base, url)


def is_relevant_avito_title(title: str, model_name: str) -> bool:
    """Check if all model keywords are present in title."""
    if not model_name:
        return True
    title_lower = title.lower()
    keywords = [w for w in re.split(r"\s+", model_name.lower()) if w]
    return all(k in title_lower for k in keywords)


def safe_json_loads(content: str) -> Optional[dict]:
    """
    Robust JSON loader for LLM output.
    
    - Strips code fences
    - Finds first {...} block
    - Handles nested JSON
    - Returns None on failure
    """
    if not content:
        return None
    
    # Удаляем markdown code fences
    cleaned = content.replace("```json", "").replace("```", "").strip()
    
    # Пробуем найти JSON объект - ищем первую { и последнюю }
    start = cleaned.find("{")
    if start == -1:
        return None
    
    # Находим соответствующую закрывающую скобку, учитывая вложенность
    bracket_count = 0
    end = -1
    for i in range(start, len(cleaned)):
        if cleaned[i] == '{':
            bracket_count += 1
        elif cleaned[i] == '}':
            bracket_count -= 1
            if bracket_count == 0:
                end = i
                break
    
    if end == -1 or end <= start:
        return None
    
    candidate = cleaned[start:end + 1]
    
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as e:
        logger.debug(f"JSON parse error: {e}, candidate: {candidate[:200]}")
        # Пробуем найти JSON в других местах (может быть несколько блоков)
        # Ищем все возможные JSON блоки с правильной вложенностью
        json_pattern = r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}'
        matches = re.finditer(json_pattern, cleaned)
        for match in matches:
            try:
                candidate = match.group(0)
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue
        return None


# =============================
# Text extraction helpers
# =============================
def _extract_year_from_text(text: str) -> Optional[int]:
    """Extract year (1900-2049) from text."""
    match = re.search(r"(20[0-4]\d|19\d{2})", text)
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            return None
    return None


def _extract_power(text: str) -> Optional[str]:
    """Extract engine power (hp/л.с.) from text."""
    match = re.search(r"(\d{2,4})\s*(л\.?с\.?|hp)", text, flags=re.IGNORECASE)
    return match.group(1) if match else None


def _extract_mileage(text: str) -> Optional[str]:
    """Extract mileage (km/км) from text."""
    match = re.search(r"(\d[\d\s]{2,6})\s*(км|km)", text, flags=re.IGNORECASE)
    return normalize_whitespace(match.group(0)) if match else None


# =============================
# Content cleaner
# =============================
class ContentCleaner:
    """Cleans HTML content for AI processing."""
    
    TAGS_TO_REMOVE = ["script", "style", "nav", "footer", "header", "iframe", "noscript", "aside"]
    
    def clean(self, html_content: str, max_length: int = CONFIG.max_content_length) -> str:
        """Remove non-content tags and extract text."""
        if not html_content:
            return ""
        soup = BeautifulSoup(html_content, "html.parser")
        for tag in soup(self.TAGS_TO_REMOVE):
            tag.decompose()
        return soup.get_text(separator=" ", strip=True)[:max_length]


# =============================
# GigaChat client
# =============================
class GigaChatClient:
    """Client for GigaChat API with token management."""
    
    def __init__(self, auth_data: str):
        self.auth_data = auth_data
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0
    
    def _get_token(self) -> Optional[str]:
        """Get or refresh access token."""
        now = time.time()
        if self._access_token and now < self._token_expires_at:
            return self._access_token

        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "RqUID": str(uuid.uuid4()),
            "Authorization": f"Basic {self.auth_data}",
        }
        payload = {"scope": "GIGACHAT_API_PERS"}
        
        try:
            resp = _requests_session.post(
                CONFIG.gigachat_oauth_url,
                headers=headers,
                data=payload,
                verify=False,
                timeout=CONFIG.http_timeout
            )
            resp.raise_for_status()
            data = resp.json()
            self._access_token = data["access_token"]
            self._token_expires_at = data.get("expires_at", 0) / 1000 or now + 1700
            return self._access_token
        except requests.RequestException as exc:
            logger.error(f"GigaChat auth error: {exc}")
            return None
    
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type(requests.RequestException)
    )
    def chat(
        self,
        system_prompt: str,
        user_content: str,
        temperature: float = 0.1,
        max_tokens: int = 500
    ) -> Optional[dict]:
        """
        Send chat completion request to GigaChat.
        
        Returns parsed JSON from response or None on failure.
        """
        token = self._get_token()
        if not token:
            return None

        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        }
        
        payload = {
            "model": CONFIG.gigachat_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        
        max_retries = 3
        base_retry_delay = 5  # Increased base delay
        
        for attempt in range(max_retries):
            try:
                gigachat_rate_limiter.wait_if_needed()
                resp = _requests_session.post(
                    CONFIG.gigachat_api_url,
                    headers=headers,
                    json=payload,
                    verify=False,
                    timeout=CONFIG.http_long_timeout
                )
                
                # Handle 429 Too Many Requests
                if resp.status_code == 429:
                    # Try to get Retry-After header, otherwise use exponential backoff
                    retry_after_header = resp.headers.get("Retry-After")
                    if retry_after_header:
                        try:
                            retry_after = int(retry_after_header)
                        except ValueError:
                            retry_after = base_retry_delay * (2 ** attempt)
                    else:
                        retry_after = base_retry_delay * (2 ** attempt)  # Exponential backoff: 5, 10, 20
                    
                    logger.warning(f"Rate limited (429), waiting {retry_after}s before retry {attempt + 1}/{max_retries}")
                    if attempt < max_retries - 1:
                        time.sleep(retry_after)
                        continue
                    else:
                        resp.raise_for_status()
                
                resp.raise_for_status()
                result = resp.json()
                content = result["choices"][0]["message"]["content"]
                return safe_json_loads(content)
            except requests.HTTPError as exc:
                if exc.response and exc.response.status_code == 429 and attempt < max_retries - 1:
                    retry_after_header = exc.response.headers.get("Retry-After")
                    if retry_after_header:
                        try:
                            retry_after = int(retry_after_header)
                        except ValueError:
                            retry_after = base_retry_delay * (2 ** attempt)
                    else:
                        retry_after = base_retry_delay * (2 ** attempt)
                    
                    logger.warning(f"Rate limited (429), waiting {retry_after}s before retry {attempt + 1}/{max_retries}")
                    time.sleep(retry_after)
                    continue
                logger.error(f"GigaChat API error: {exc}")
                if attempt == max_retries - 1:
                    raise
            except requests.RequestException as exc:
                logger.error(f"GigaChat API error: {exc}")
                if attempt == max_retries - 1:
                    raise
                # Exponential backoff for network errors
                time.sleep(base_retry_delay * (2 ** attempt))
            except (KeyError, IndexError) as exc:
                logger.error(f"GigaChat response parse error: {exc}")
                return None
        
        # If all retries failed
        return None


# =============================
# AI Analyzer
# =============================
class AIAnalyzer:
    """AI-powered analysis using GigaChat."""
    
    ANALYSIS_PROMPT = """Ты аналитик рынка лизинга авто. По тексту объявления заполни поля и верни только JSON.
Требуется:
1) Категория (Category).
2) Бренд и модель.
3) 3–5 ключевых характеристик (Specs) — тип двигателя/привода, пробег, мощность/л.с., состояние и пр.
4) Плюсы (Pros) и минусы/оговорки (Cons).
5) Если в тексте упомянуты аналоги или конкуренты (например, "как Volvo ..."), добавь их в analogs_mentioned.

Структура ответа:
{
  "category": "string (например: 'легковые автомобили', 'коммерческий транспорт')",
  "vendor": "string (производитель, например: 'Volvo', 'BMW')",
  "model": "string (модель, например: 'XC60', 'X5 M')",
  "price": int (цена в валюте, null если нет или "по запросу"),
  "currency": "string (RUB, USD, EUR)",
  "monthly_payment": int (платёж в месяц, null если нет),
  "year": int (год выпуска),
  "condition": "string (новый / б/у / не указан)",
  "location": "string (город/регион)",
  "specs": {
    "характеристика_1": "значение",
    "характеристика_2": "значение",
    "характеристика_3": "значение"
  },
  "pros": ["плюс 1", "плюс 2"],
  "cons": ["минус 1", "минус 2"],
  "analogs_mentioned": ["аналог 1", "аналог 2"]
}

ПРИМЕРЫ specs для разных типов:
- Автомобиль: {"двигатель": "2.0 л 150 л.с.", "пробег": "50000 км", "привод": "полный"}
- Экскаватор: {"ковш": "1.2 м³", "глубина_копания": "6.5 м", "мощность": "120 кВт"}
- Станок ЧПУ: {"точность": "0.01 мм", "рабочая_зона": "800x600x500", "шпиндель": "24000 об/мин"}
- Сервер: {"процессор": "2x Xeon Gold 6248R", "RAM": "256 GB", "диски": "8x 1.92TB SSD"}
- Трактор: {"мощность": "240 л.с.", "тип": "колесный", "количество_передач": "16"}

Отвечай ТОЛЬКО валидным JSON без markdown и комментариев."""

    ANALOGS_PROMPT = """Ты эксперт по промышленному оборудованию, технике и лизинговому рынку. Подбери РЕАЛЬНЫЕ конкурентные аналоги для указанного актива.

УНИВЕРСАЛЬНЫЕ КРИТЕРИИ ПОДБОРА:
1. Тот же ТИП и НАЗНАЧЕНИЕ (не предлагай трактор вместо экскаватора)
2. Тот же КЛАСС и СЕГМЕНТ (премиум к премиуму, промышленный к промышленному)
3. Схожий ЦЕНОВОЙ ДИАПАЗОН (+-30% от оригинала)
4. Сравнимые ХАРАКТЕРИСТИКИ (производительность, мощность, размер)
5. Доступность на российском рынке
6. Актуальные модели (предпочтительно текущего поколения)

ПРИМЕРЫ ПО КАТЕГОРИЯМ:

АВТОМОБИЛИ:
- BMW X5 → Audi Q7, Mercedes GLE, Volvo XC90, Porsche Cayenne
- Toyota Camry → Kia K5, Hyundai Sonata, Skoda Superb

КОММЕРЧЕСКИЙ ТРАНСПОРТ:
- Mercedes Sprinter → Ford Transit, Fiat Ducato, Iveco Daily, ГАЗель NEXT
- Volvo FH → Scania R-series, MAN TGX, DAF XF

СПЕЦТЕХНИКА:
- Caterpillar 320 → Komatsu PC210, Hitachi ZX200, Volvo EC210
- JCB 3CX → Caterpillar 428, Case 580, Terex 860

СЕЛЬХОЗТЕХНИКА:
- John Deere 6M → Case IH Puma, New Holland T6, Fendt 700 Vario
- Claas Lexion → John Deere S-series, Case IH Axial-Flow

СТАНКИ И ОБОРУДОВАНИЕ:
- DMG MORI NLX 2500 → Mazak Quick Turn, Okuma LB3000, Haas ST-30
- Trumpf TruLaser → Bystronic ByStar, Prima Power, Amada

IT-ОБОРУДОВАНИЕ:
- Dell PowerEdge R750 → HPE ProLiant DL380, Lenovo ThinkSystem SR650, Cisco UCS
- NetApp FAS → Dell EMC PowerStore, HPE Nimble, Pure Storage

МЕДИЦИНСКОЕ ОБОРУДОВАНИЕ:
- Siemens Healthineers → GE Healthcare, Philips Healthcare, Canon Medical

НЕ ВКЛЮЧАЙ:
- Оборудование другого класса или назначения
- Устаревшие снятые с производства модели
- Несопоставимые по масштабу (промышленный vs бытовой)
- Неизвестные сомнительные бренды

Верни JSON: {"analogs": ["Производитель Модель", "Производитель Модель", ...]}
Максимум 5 наиболее релевантных конкурентов."""
    
    REVIEW_PROMPT = """Ты аналитик лизингового рынка с экспертизой в ЛЮБЫХ активах. Проанализируй объявления по аналогу и составь экспертный обзор.

ЗАДАЧА:
1. Оцени рыночную цену аналога по найденным объявлениям
2. Выдели РЕАЛЬНЫЕ преимущества (не маркетинг, только факты и цифры)
3. Укажи РЕАЛЬНЫЕ недостатки и подводные камни
4. Выбери лучшее объявление по соотношению цена/качество

КРИТЕРИИ ОЦЕНКИ:
- Полнота информации: фото, характеристики, история обслуживания, документы
- Адекватность цены: не завышена, не подозрительно низкая
- Надежность продавца: официальный дилер > проверенная компания > частник
- Прозрачность: открытые данные о состоянии, наработке, ремонтах
- Условия: гарантия, возможность лизинга, доставка

ПРИМЕРЫ ХОРОШИХ ПЛЮСОВ/МИНУСОВ:

ХОРОШО (с конкретикой):
- "На 15% дешевле рыночной цены при аналогичном состоянии"
- "Низкий расход топлива 8 л/100км vs 12 л у конкурентов"
- "Широкая сеть сервисных центров в РФ, запчасти доступны"
- "Наработка 2000 моточасов при норме 5000 до капремонта"

ПЛОХО (без конкретики):
- "Хорошая цена" (насколько? по сравнению с чем?)
- "Надежный" (на основе чего? статистика?)
- "Качественный" (какие показатели качества?)

ВЫБОР ЛУЧШЕГО ОБЪЯВЛЕНИЯ:
Приоритет: полнота данных > адекватность цены > надежность продавца

Верни JSON:
{
  "pros": [
    "Конкретное преимущество с цифрами и фактами",
    "Еще преимущество с обоснованием",
    "Третье преимущество"
  ],
  "cons": [
    "Конкретный недостаток с цифрами",
    "Еще недостаток с последствиями",
    "Третий недостаток"
  ],
  "price_hint": 4500000,
  "note": "Краткий вывод на 2-3 предложения: стоит ли рассматривать как альтернативу и ПОЧЕМУ с финансовым обоснованием",
  "best_link": "URL лучшего объявления или null"
}"""
    
    VALIDATION_PROMPT = """Ты финансовый аналитик с экспертизой в оценке ЛЮБЫХ активов для лизинга. Проверь адекватность рыночной оценки.

ТИПИЧНЫЕ ДИАПАЗОНЫ ЦЕН ПО КАТЕГОРИЯМ:

ТРАНСПОРТ:
- Легковые авто эконом: 1-4 млн руб
- Легковые авто премиум: 4-15 млн руб
- Люкс/спорткары: 10-50+ млн руб
- Легкий коммерческий (до 3.5т): 2-6 млн руб
- Грузовики средние: 5-15 млн руб
- Грузовики тяжелые: 8-30+ млн руб

СПЕЦТЕХНИКА:
- Мини-экскаваторы: 2-5 млн руб
- Экскаваторы средние: 5-20 млн руб
- Бульдозеры: 10-40 млн руб
- Автокраны: 15-100+ млн руб
- Погрузчики: 3-15 млн руб

СЕЛЬХОЗТЕХНИКА:
- Тракторы малые: 1-3 млн руб
- Тракторы средние: 3-10 млн руб
- Тракторы мощные: 10-30 млн руб
- Комбайны: 15-60+ млн руб

ПРОИЗВОДСТВЕННОЕ ОБОРУДОВАНИЕ:
- Станки с ЧПУ малые: 2-10 млн руб
- Станки с ЧПУ средние: 10-50 млн руб
- Обрабатывающие центры: 20-200+ млн руб
- Прессы, гибочное оборудование: 5-100 млн руб
- Производственные линии: 50-500+ млн руб

IT-ОБОРУДОВАНИЕ:
- Серверы начальные: 200 тыс - 1 млн руб
- Серверы средние: 1-5 млн руб
- Серверы enterprise: 5-30+ млн руб
- СХД: 2-50+ млн руб
- Сетевое оборудование: 100 тыс - 10 млн руб

МЕДИЦИНСКОЕ ОБОРУДОВАНИЕ:
- УЗИ аппараты: 1-10 млн руб
- Рентген: 5-20 млн руб
- КТ/МРТ: 30-150+ млн руб
- Лабораторное: 500 тыс - 50 млн руб

КРИТЕРИИ ВАЛИДАЦИИ:
1. СООТВЕТСТВИЕ КАТЕГОРИИ: Цена в разумных пределах для типа оборудования
2. РАЗБРОС: Разница между min и max не должна быть более 5x (иначе подозрительно)
3. КОЛИЧЕСТВО ДАННЫХ: Минимум 3 предложения для достоверности
4. АНОМАЛИИ:
   - Цена < 50 000 руб для промышленного оборудования = ПОДОЗРИТЕЛЬНО
   - Цена > 1 млрд руб для стандартной техники = ОШИБКА
   - Все цены идентичны = возможны дубликаты
   - Слишком узкий диапазон (<10% разброс) = мало данных или однотипные источники

ВЕРНИ JSON:
{
  "is_valid": true/false,
  "comment": "Подробное объяснение: почему оценка валидна или что вызывает сомнения",
  "confidence": "high | medium | low",
  "suggestions": "Рекомендации по улучшению оценки (если есть)"
}"""
    
    SPECS_EXTRACTION_PROMPT = """Ты технический эксперт по ЛЮБЫМ типам оборудования и техники. Извлеки ВСЕ технические характеристики из текста.

ПРАВИЛА ИЗВЛЕЧЕНИЯ:
1. Сохраняй ТОЧНЫЕ значения из текста (не округляй, не преобразуй)
2. Используй СТАНДАРТНЫЕ единицы измерения
3. Адаптируй характеристики под ТИП оборудования
4. Если значение диапазон — сохраняй как диапазон ("190-250 л.с.")

===========================================================
ХАРАКТЕРИСТИКИ ПО ТИПАМ ОБОРУДОВАНИЯ:
===========================================================

АВТОМОБИЛИ:
- двигатель: тип, объем, мощность (л.с./кВт)
- привод: передний/задний/полный
- КПП: механика/автомат/робот + передачи
- пробег: км
- расход: л/100км
- габариты: длина/ширина/высота (мм)
- масса: кг

СПЕЦТЕХНИКА (экскаваторы, погрузчики):
- мощность_двигателя: кВт или л.с.
- вместимость_ковша: м³
- глубина_копания: м
- высота_выгрузки: м
- грузоподъемность: кг или тонн
- рабочая_масса: тонн
- тип_ходовой: гусеничная/колесная

СЕЛЬХОЗТЕХНИКА:
- мощность: л.с.
- тип: колесный/гусеничный
- рабочая_ширина: м
- производительность: га/час
- бункер: литры или м³
- количество_цилиндров: шт

СТАНКИ И ОБОРУДОВАНИЕ:
- точность_обработки: мм
- рабочая_зона: мм (X/Y/Z)
- мощность_шпинделя: кВт
- обороты_шпинделя: об/мин
- максимальная_нагрузка: кг
- класс_точности: по стандарту
- количество_осей: шт

IT-ОБОРУДОВАНИЕ:
- процессор: модель, количество ядер, частота
- оперативная_память: GB
- накопители: тип, объем
- сеть: скорость портов (1G/10G/40G)
- энергопотребление: Вт
- форм_фактор: размер (1U, 2U, tower)
- поддержка_виртуализации: да/нет

МЕДИЦИНСКОЕ ОБОРУДОВАНИЕ:
- тип_исследования: что диагностирует
- разрешение: пиксели или линии
- точность: процент или класс
- производительность: пациентов/час
- мощность_излучения: если применимо
- класс_безопасности: медицинский класс"""

    COMPARE_OFFERS_PROMPT = """Ты эксперт по оценке объявлений для лизинга. Сравни два объявления и определи, какое лучше.

Объявление 1:
{offer1}

Объявление 2:
{offer2}

Критерии сравнения:
1. Адекватность цены (соответствие рыночной стоимости)
2. Состояние и характеристики
3. Наличие важных параметров
4. Надежность источника
5. Общее качество предложения

Верни JSON:
{{
  "winner": 1 или 2 (какое объявление лучше),
  "score_1": float от 0 до 10 (оценка первого объявления),
  "score_2": float от 0 до 10 (оценка второго объявления),
  "reason": "краткое объяснение почему выбран победитель",
  "pros_winner": ["плюс 1", "плюс 2"],
  "cons_winner": ["минус 1", "минус 2"],
  "pros_loser": ["плюс 1", "плюс 2"],
  "cons_loser": ["минус 1", "минус 2"]
}}"""

    FIND_BEST_OFFER_PROMPT = """Ты эксперт по оценке объявлений для лизинга. Из списка объявлений найди ЛУЧШЕЕ.

Объявления:
{offers_list}

Критерии выбора лучшего:
1. Адекватность цены (соответствие рыночной стоимости)
2. Состояние и характеристики
3. Полнота информации
4. Надежность источника
5. Общее качество предложения

Верни JSON:
{{
  "best_index": int (индекс лучшего объявления, начиная с 0),
  "best_score": float от 0 до 10,
  "reason": "почему это объявление лучшее",
  "ranking": [
    {{"index": 0, "score": 8.5, "brief_reason": "..."}},
    {{"index": 1, "score": 7.2, "brief_reason": "..."}}
  ]
}}"""

    COMPARE_BEST_OFFERS_PROMPT = """Ты эксперт по лизингу. Проведи ДЕТАЛЬНОЕ СРАВНЕНИЕ лучшего объявления оригинала с лучшим объявлением аналога.

Твоя задача - не просто описать плюсы и минусы, а ПРЯМО СРАВНИТЬ эти два предложения по ключевым критериям:
1. Цена и стоимость владения
2. Технические характеристики и качество
3. Условия лизинга и финансирования
4. Надежность и репутация
5. Соответствие потребностям клиента

Лучшее объявление ОРИГИНАЛА ({original_name}):
{best_original}

Лучшее объявление АНАЛОГА ({analog_name}):
{best_analog}

Проведи ПОСЛЕДОВАТЕЛЬНОЕ сравнение по каждому критерию и вынеси обоснованное решение.

Верни JSON:
{{
  "winner": "original" или "analog",
  "original_score": float от 0 до 10,
  "analog_score": float от 0 до 10,
  "comparison_details": {{
    "price": "детальное сравнение цен и стоимости",
    "quality": "сравнение качества и характеристик",
    "financing": "сравнение условий лизинга",
    "reliability": "сравнение надежности",
    "value": "сравнение соотношения цена/качество"
  }},
  "price_comparison": {{
    "original_price": int,
    "analog_price": int,
    "difference_percent": float,
    "price_verdict": "original_cheaper" | "analog_cheaper" | "similar",
    "monthly_payment_original": int или null,
    "monthly_payment_analog": int или null
  }},
  "pros_original": ["конкретное преимущество оригинала", "еще преимущество"],
  "cons_original": ["конкретный недостаток оригинала", "еще недостаток"],
  "pros_analog": ["конкретное преимущество аналога", "еще преимущество"],
  "cons_analog": ["конкретный недостаток аналога", "еще недостаток"],
  "recommendation": "детальная рекомендация с обоснованием выбора",
  "use_cases_original": ["конкретная ситуация когда лучше выбрать оригинал"],
  "use_cases_analog": ["конкретная ситуация когда лучше выбрать аналог"],
  "key_differences": ["главное отличие 1", "главное отличие 2", "главное отличие 3"]
}}"""
    
    def __init__(self, client: GigaChatClient, cleaner: ContentCleaner):
        self.client = client
        self.cleaner = cleaner
    
    def analyze_content(self, html_content: str) -> Optional[AIAnalysisResult]:
        """Analyze HTML content and extract structured data."""
        text = self.cleaner.clean(html_content)
        if not text:
            return None
        
        try:
            result = self.client.chat(
                self.ANALYSIS_PROMPT,
                text,
                temperature=0.1,
                max_tokens=1500
            )
            return result
        except requests.RequestException:
            logger.warning("Failed to analyze content with AI")
            return None
    
    def suggest_analogs(self, item_name: str) -> list[str]:
        """Get analog suggestions from AI."""
        try:
            result = self.client.chat(
                self.ANALOGS_PROMPT,
                item_name,
                temperature=0.2,
                max_tokens=500
            )
            if result:
                return ensure_list_str(result.get("analogs"))
        except requests.RequestException:
            logger.warning(f"Failed to get analog suggestions for {item_name}")
        return []
    
    def extract_specs_from_text(self, text: str) -> dict:
        """Extract technical specifications from text using AI."""
        if not text or len(text.strip()) < 50:
            return {}
        
        try:
            result = self.client.chat(
                self.SPECS_EXTRACTION_PROMPT,
                text[:8000],  # Limit text length
                temperature=0.1,
                max_tokens=2000
            )
            if result and "specs" in result:
                return result["specs"]
        except requests.RequestException as e:
            logger.warning(f"Failed to extract specs with AI: {e}")
        return {}
    
    def review_analog(self, analog_name: str, listings: list[dict]) -> AnalogReview:
        """Get AI review of an analog model."""
        listings_text = "\n".join(
            f"- {l.get('title', '')} ({l.get('link', '')}) {l.get('snippet', '')}"
            for l in listings
        )
        user_content = f"Модель: {analog_name}\nОбъявления:\n{listings_text}"
        
        try:
            result = self.client.chat(
                self.REVIEW_PROMPT,
                user_content,
                temperature=0.2,
                max_tokens=600
            )
            return result or {}
        except requests.RequestException:
            logger.warning(f"Failed to review analog {analog_name}")
            return {}
    
    def validate_report(self, report: dict) -> ValidationResult:
        """Validate market report with AI sanity check."""
        summary = {
            "item": report.get("item"),
            "median_price": report.get("median_price"),
            "mean_price": report.get("mean_price"),
            "market_range": report.get("market_range"),
            "offers_count": len(report.get("offers_used", [])),
        }
        details = json.dumps(summary, ensure_ascii=False, default=str)
        
        try:
            result = self.client.chat(
                self.VALIDATION_PROMPT,
                f"Отчет:\n{details}",
                temperature=0.1,
                max_tokens=500
            )
            return result or {"is_valid": True, "comment": "Parse error"}
        except requests.RequestException:
            logger.warning("Failed to validate report with AI")
            return {"is_valid": True, "comment": "AI not available"}
    
    def compare_two_offers(self, offer1: dict, offer2: dict) -> dict:
        """Compare two offers and determine which is better."""
        offer1_str = json.dumps(offer1, ensure_ascii=False, default=str, indent=2)
        offer2_str = json.dumps(offer2, ensure_ascii=False, default=str, indent=2)
        
        prompt = self.COMPARE_OFFERS_PROMPT.format(
            offer1=offer1_str,
            offer2=offer2_str
        )
        
        try:
            result = self.client.chat(
                prompt,
                "Сравни объявления",
                temperature=0.2,
                max_tokens=800
            )
            return result or {"winner": 1, "score_1": 5.0, "score_2": 5.0, "reason": "Comparison failed"}
        except requests.RequestException:
            logger.warning("Failed to compare offers")
            return {"winner": 1, "score_1": 5.0, "score_2": 5.0, "reason": "AI unavailable"}
    
    def find_best_offer(self, offers: list[dict]) -> dict:
        """Find the best offer from a list of offers."""
        if not offers:
            return {"best_index": -1, "best_score": 0.0, "reason": "No offers"}
        
        if len(offers) == 1:
            return {"best_index": 0, "best_score": 8.0, "reason": "Only one offer", "ranking": [{"index": 0, "score": 8.0, "brief_reason": "Single offer"}]}
        
        # Format offers for AI
        offers_list = "\n\n".join([
            f"Объявление {i}:\n{json.dumps(offer, ensure_ascii=False, default=str, indent=2)}"
            for i, offer in enumerate(offers, 1)
        ])
        
        prompt = self.FIND_BEST_OFFER_PROMPT.format(offers_list=offers_list)
        
        try:
            result = self.client.chat(
                prompt,
                "Найди лучшее объявление",
                temperature=0.2,
                max_tokens=1000
            )
            if result and "best_index" in result:
                return result
            else:
                # Fallback: return first offer
                return {"best_index": 0, "best_score": 7.0, "reason": "AI parsing failed", "ranking": []}
        except requests.RequestException:
            logger.warning("Failed to find best offer")
            # Fallback: return first offer
            return {"best_index": 0, "best_score": 7.0, "reason": "AI unavailable", "ranking": []}
    
    def compare_best_offers(self, best_original: dict, best_analog: dict, original_name: str, analog_name: str) -> dict:
        """Compare best original offer with best analog offer."""
        original_str = json.dumps(best_original, ensure_ascii=False, default=str, indent=2)
        analog_str = json.dumps(best_analog, ensure_ascii=False, default=str, indent=2)
        
        prompt = self.COMPARE_BEST_OFFERS_PROMPT.format(
            original_name=original_name,
            best_original=original_str,
            analog_name=analog_name,
            best_analog=analog_str
        )
        
        try:
            result = self.client.chat(
                prompt,
                "Сравни лучшие объявления",
                temperature=0.2,
                max_tokens=1200
            )
            return result or {
                "winner": "original",
                "original_score": 5.0,
                "analog_score": 5.0,
                "recommendation": "Comparison failed"
            }
        except requests.RequestException:
            logger.warning("Failed to compare best offers")
            return {
                "winner": "original",
                "original_score": 5.0,
                "analog_score": 5.0,
                "recommendation": "AI unavailable"
            }


# =============================
# Sonar Analog Finder (Perplexity API)
# =============================
class SonarAnalogFinder:
    """
    Находит аналоги через Perplexity Sonar API.
    Оптимизирован для минимального расхода токенов.
    """
    
    # Улучшенный промпт для поиска аналогов
    JSON_ONLY_SYSTEM_PROMPT = (
        "You are a JSON API. Return exactly one valid JSON object and nothing else. "
        "Do not use markdown. Do not explain anything outside JSON. "
        "If information is insufficient, still return the requested JSON shape with conservative values."
    )

    ANALOG_PROMPT = """Используй данные из поисковых результатов для поиска РОВНО 3 лучших конкурентных аналога для: {item}

Требования к аналогам:
1. Тот же тип продукта (если авто - то авто того же класса, если техника - то аналогичная техника)
2. Сопоставимый ценовой сегмент (+-30%)
3. Доступны для покупки в России в 2024-2025 году
4. Популярные и проверенные модели с хорошими отзывами

Для каждого аналога укажи:
- Точное название (производитель + модель)
- Реальный диапазон цен в рублях (на основе данных из поисковых результатов)
- Главное отличие от оригинала

ВАЖНО: Используй информацию из предоставленных поисковых результатов. Если информации недостаточно, используй свои знания о рынке.

КРИТИЧЕСКИ ВАЖНО: 
- Верни ТОЛЬКО валидный JSON, без дополнительного текста до или после
- Не добавляй объяснений, комментариев или markdown разметки (включая ```json)
- Формат ответа должен быть строго JSON объект, начинающийся с {{ и заканчивающийся }}

{{"analogs": [
  {{"name": "Производитель Модель", "price_range": "X-Y млн руб", "key_diff": "главное отличие"}},
  {{"name": "Производитель Модель", "price_range": "X-Y млн руб", "key_diff": "главное отличие"}},
  {{"name": "Производитель Модель", "price_range": "X-Y млн руб", "key_diff": "главное отличие"}}
]}}"""

    # Улучшенный промпт для сравнения объявлений
    COMPARE_PROMPT = """Используй данные из поисковых результатов для сравнения двух объявлений:

ОРИГИНАЛ - {original_name}:
- Объявление: {original_title}
- Цена: {original_price}
- Ссылка: {original_url}

АНАЛОГ - {analog_name}:
- Объявление: {analog_title}
- Цена: {analog_price}
- Ссылка: {analog_url}

Проанализируй оба варианта по следующим критериям:
- Цена и соотношение цена/качество
- Технические характеристики и комплектация
- Надежность и репутация бренда
- Доступность запчастей и сервиса в России
- Условия покупки и лизинга

ВАЖНО: Используй информацию из предоставленных поисковых результатов для обоих автомобилей. Если информации о конкретном объявлении недостаточно, используй общие данные о модели из поисковых результатов.

КРИТИЧЕСКИ ВАЖНО:
- Верни ТОЛЬКО валидный JSON, без дополнительного текста до или после
- Не добавляй объяснений, комментариев или markdown разметки (включая ```json)
- Формат ответа должен быть строго JSON объект, начинающийся с {{ и заканчивающийся }}
- Если информации недостаточно, все равно верни JSON с доступными данными

{{"winner": "original" или "analog" или "tie",
"orig_pros": ["конкретный плюс 1", "конкретный плюс 2"],
"orig_cons": ["конкретный минус 1"],
"analog_pros": ["конкретный плюс 1", "конкретный плюс 2"],
"analog_cons": ["конкретный минус 1"],
"price_diff": "Аналог дешевле/дороже на X% или примерно равно",
"verdict": "Четкая рекомендация что выбрать и почему (2-3 предложения)"}}"""

    FIND_BEST_OFFER_PROMPT = """Проанализируй список объявлений и выбери лучшее по соотношению цена/качество для лизинга.

Объявления:
{offers_list}

Критерии выбора:
1. Цена и соответствие рынку
2. Состояние и технические характеристики
3. Полнота и достоверность информации
4. Надежность источника объявления
5. Ликвидность и практичность для рынка РФ

Верни только JSON:
{{"best_index": 0, "best_score": 8.5, "reason": "почему это объявление лучше", "ranking": [
  {{"index": 0, "score": 8.5, "brief_reason": "краткая причина"}},
  {{"index": 1, "score": 7.3, "brief_reason": "краткая причина"}}
]}}"""

    VALIDATE_MARKET_PRICES_PROMPT = """Оцени рыночные цены для {item_name}.

Данные:
- Минимальная цена: {min_price}
- Максимальная цена: {max_price}
- Медианная цена: {median_price}
- Средняя цена: {mean_price}
- Цена клиента: {client_price}
- Количество объявлений: {offers_count}

Проверь:
1. Адекватность диапазона цен
2. Наличие аномалий
3. Насколько цена клиента соответствует рынку

Верни только JSON:
{{"is_valid": true, "explanation": "объяснение", "anomalies": ["аномалия 1"], "client_price_verdict": "fair"}}"""

    ENRICH_OFFER_PROMPT = """Извлеки и структурируй данные объявления.

Название: {title}
Цена: {price}
Описание: {description}

Верни только JSON:
{{"vendor": "бренд", "model": "модель", "year": 2024, "condition": "новый", "specs": {{"key": "value"}}, "pros": ["плюс"], "cons": ["минус"]}}"""

    def __init__(self):
        self.api_key = CONFIG.perplexity_api_key
        
        # Определяем URL API: если указан base_url (прокси), используем его, иначе прямой API
        # Также проверяем ключ: если начинается с sk-, вероятно это прокси
        base_url = CONFIG.sonar_base_url
        
        # Если base_url не указан, но ключ начинается с sk-, предполагаем прокси artemox
        if not base_url and self.api_key and self.api_key.startswith("sk-"):
            base_url = "https://api.artemox.com/v1"
            logger.info("[SONAR] Detected proxy API (artemox.com) based on key format")
        
        if base_url:
            # Прокси через artemox.com или другой сервис
            self.api_url = f"{base_url.rstrip('/')}/chat/completions"
            # Для прокси пробуем разные модели (некоторые прокси не поддерживают sonar-reasoning)
            # Список моделей для попытки в порядке приоритета
            user_model = os.getenv("PERPLEXITY_MODEL")
            # Если пользователь не указал модель, используем sonar-reasoning-pro по умолчанию (как в примере)
            if not user_model:
                user_model = "sonar-reasoning-pro"
            
            self.model_candidates = [
                user_model,  # Пользовательская модель из .env или "sonar-reasoning-pro" по умолчанию
                "sonar",  # Fallback на sonar (более легкая модель)
                "gpt-4",  # Fallback на GPT-4
                "gpt-3.5-turbo"  # Последний fallback
            ]
            # Убираем дубликаты и None из списка
            seen = set()
            self.model_candidates = [m for m in self.model_candidates if m and m not in seen and not seen.add(m)]
            # Начинаем с первой доступной модели
            self.model = self.model_candidates[0] if self.model_candidates else "sonar-reasoning-pro"
            self.current_model_index = 0
            logger.info(f"[SONAR] Using proxy API: {self.api_url} with model: {self.model}")
            logger.info(f"[SONAR] Available model fallbacks: {', '.join(self.model_candidates)}")
        else:
            # Прямой Perplexity API
            self.api_url = CONFIG.sonar_api_url
            self.model = CONFIG.sonar_model
            logger.info(f"[SONAR] Using direct Perplexity API: {self.api_url} with model: {self.model}")
        
    def is_available(self) -> bool:
        """Check if Sonar API is available."""
        # Accept keys starting with 'pplx-' (Perplexity) or 'sk-' (proxy services like artemox)
        return bool(self.api_key and (self.api_key.startswith("pplx-") or self.api_key.startswith("sk-")))
    
    def _call_sonar(
        self,
        prompt: str,
        max_tokens: int = 400,
        retries: int = 2,
        return_raw_on_parse_failure: bool = False
    ) -> Optional[dict]:
        """
        Make a call to Sonar API with minimal tokens.
        Includes retry logic for 500 errors and timeouts.
        """
        if not self.api_key:
            return None
        
        # Увеличиваем таймаут для прокси (они могут быть медленнее)
        # В примере используется timeout=600 (10 минут), но для нашего случая 300 секунд (5 минут) должно быть достаточно
        timeout = 300 if "artemox.com" in self.api_url else CONFIG.http_long_timeout
        
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        
        # Формируем payload в зависимости от типа API
        # Для прокси (artemox.com) используем МИНИМАЛЬНЫЙ набор параметров (как в примере)
        if "artemox.com" in self.api_url or hasattr(self, 'model_candidates'):
            # Минимальный payload для прокси - ТОЛЬКО model и messages (как в примере)
            # НЕ добавляем max_tokens и temperature, т.к. прокси может их не поддерживать
            payload = {
                "model": self.model,  # Используем текущую модель (может быть изменена при ошибке)
                "messages": [
                    {"role": "system", "content": self.JSON_ONLY_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt}
                ]
            }
            # НЕ добавляем дополнительные параметры для прокси - используем минимальный формат
        else:
            # Для прямого Perplexity API полный набор параметров
            payload = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": self.JSON_ONLY_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt}
                ],
                "max_tokens": max_tokens,
                "temperature": 0.1,
                "return_citations": False,
                "return_images": False
            }
        
        # Retry логика для 500 ошибок и таймаутов
        for attempt in range(retries + 1):
            try:
                sonar_rate_limiter.wait_if_needed()
                
                # Логируем детали запроса для отладки (только для прокси)
                if "artemox.com" in self.api_url and attempt == 0:
                    logger.info(f"[SONAR] Sending request to {self.api_url}")
                    logger.info(f"[SONAR] Model: {self.model}")
                    logger.debug(f"[SONAR] Payload: {json.dumps(payload, ensure_ascii=False)[:300]}...")
                
                # Используем отдельную сессию для Sonar без автоматических retry
                response = _sonar_requests_session.post(
                    self.api_url,
                    headers=headers,
                    json=payload,
                    timeout=timeout
                )
                
                # Логируем ответ для отладки
                if response.status_code != 200:
                    try:
                        error_body = response.text[:200]  # Первые 200 символов ошибки
                        logger.warning(f"[SONAR] Response status {response.status_code}: {error_body}")
                    except:
                        pass
                
                # Handle 401 Unauthorized - invalid or missing API key
                if response.status_code == 401:
                    logger.error("[SONAR] 401 Unauthorized - Invalid or missing PERPLEXITY_API_KEY")
                    logger.error("[SONAR] Please check your .env file and ensure PERPLEXITY_API_KEY is set correctly")
                    logger.error("[SONAR] Key should start with 'pplx-' or 'sk-' (without quotes)")
                    return None
                
                # Handle 500 errors - может быть из-за неподдерживаемой модели
                if response.status_code == 500:
                    # Проверяем, не связана ли ошибка с моделью
                    try:
                        error_data = response.json()
                        error_msg = str(error_data.get("error", {}).get("message", "")).lower()
                        # Если ошибка связана с моделью, пробуем другую
                        if "model" in error_msg or "guardrail" in error_msg or "dissalowed" in error_msg:
                            if hasattr(self, 'model_candidates') and self.current_model_index < len(self.model_candidates) - 1:
                                self.current_model_index += 1
                                self.model = self.model_candidates[self.current_model_index]
                                payload["model"] = self.model
                                logger.warning(f"[SONAR] Model not allowed, switching to: {self.model}")
                                continue
                    except:
                        pass
                    
                    if attempt < retries:
                        wait_time = (attempt + 1) * 3  # 3, 6, 9 секунд
                        logger.warning(f"[SONAR] 500 Internal Server Error from {self.api_url}")
                        logger.warning(f"[SONAR] This usually means the proxy server is overloaded or having issues")
                        logger.warning(f"[SONAR] Retrying in {wait_time}s (attempt {attempt + 1}/{retries + 1})...")
                        time.sleep(wait_time)
                        continue
                    else:
                        logger.error(f"[SONAR] 500 error persisted after {retries + 1} attempts")
                        logger.error(f"[SONAR] Proxy server {self.api_url} may be temporarily unavailable")
                        logger.error("[SONAR] System will use fallback methods (GigaChat/Google)")
                        return None
                
                response.raise_for_status()
                
                data = response.json()
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                
                # Логируем сырой ответ для отладки (первые 500 символов)
                if not content:
                    logger.warning(f"[SONAR] Empty content in API response. Full response keys: {list(data.keys())}")
                    logger.debug(f"[SONAR] Full response structure: {str(data)[:500]}")
                    return None
                
                logger.debug(f"[SONAR] Received content length: {len(content)} chars")
                logger.debug(f"[SONAR] Content preview: {content[:200]}...")
                
                # Parse JSON from response
                parsed = safe_json_loads(content)
                if not parsed:
                    logger.warning(f"[SONAR] Failed to parse JSON from content. Content preview: {content[:500]}")
                    if return_raw_on_parse_failure:
                        return {"_raw_content": content}
                else:
                    logger.debug(f"[SONAR] Successfully parsed JSON. Keys: {list(parsed.keys()) if isinstance(parsed, dict) else 'not a dict'}")
                
                return parsed
                
            except requests.Timeout as e:
                if attempt < retries:
                    wait_time = (attempt + 1) * 2
                    logger.warning(f"[SONAR] Timeout, retrying in {wait_time}s (attempt {attempt + 1}/{retries + 1})...")
                    time.sleep(wait_time)
                    continue
                else:
                    logger.error(f"[SONAR] Timeout after {retries + 1} attempts: {e}")
                    return None
            except requests.HTTPError as e:
                if e.response and e.response.status_code == 401:
                    logger.error("[SONAR] Authentication failed - check PERPLEXITY_API_KEY in .env file")
                    return None
                if e.response and e.response.status_code == 500:
                    # Проверяем, не связана ли ошибка с моделью
                    try:
                        error_data = e.response.json()
                        error_msg = str(error_data.get("error", {}).get("message", "")).lower()
                        if ("model" in error_msg or "guardrail" in error_msg or "dissalowed" in error_msg) and \
                           hasattr(self, 'model_candidates') and self.current_model_index < len(self.model_candidates) - 1:
                            self.current_model_index += 1
                            self.model = self.model_candidates[self.current_model_index]
                            payload["model"] = self.model
                            logger.warning(f"[SONAR] Model error detected, switching to: {self.model}")
                            continue
                    except:
                        pass
                    
                    if attempt < retries:
                        wait_time = (attempt + 1) * 3
                        logger.warning(f"[SONAR] HTTP 500 error, retrying in {wait_time}s...")
                        time.sleep(wait_time)
                        continue
                logger.warning(f"[SONAR] HTTP error: {e}")
                return None
            except requests.RequestException as e:
                # Обрабатываем ResponseError от HTTPAdapter (too many 500 errors)
                error_str = str(e).lower()
                is_retryable = (
                    "500" in error_str or 
                    "timeout" in error_str or 
                    "too many" in error_str or
                    "connection" in error_str
                )
                
                if attempt < retries and is_retryable:
                    wait_time = (attempt + 1) * 3  # Увеличиваем задержку для 500 ошибок
                    logger.warning(f"[SONAR] Request error (retryable), retrying in {wait_time}s: {e}")
                    time.sleep(wait_time)
                    continue
                else:
                    logger.warning(f"[SONAR] Request error (non-retryable or max retries): {e}")
                    if "too many 500" in error_str:
                        logger.error("[SONAR] Proxy server is returning too many 500 errors")
                        logger.error("[SONAR] This may indicate server overload or temporary unavailability")
                        logger.error("[SONAR] Will use fallback methods (GigaChat/Google)")
                    return None
            except Exception as e:
                logger.warning(f"[SONAR] Parse error: {e}")
                return None
        
        return None

    def _build_fallback_comparison(
        self,
        original_name: str,
        original_offer: dict,
        analog_name: str,
        analog_offer: dict,
        raw_content: str = ""
    ) -> dict:
        """Convert a non-JSON Sonar answer into a usable structured comparison."""
        orig_price = original_offer.get("price")
        analog_price = analog_offer.get("price")
        price_diff = describe_price_difference(orig_price, analog_price)

        orig_pros: list[str] = []
        orig_cons: list[str] = []
        analog_pros: list[str] = []
        analog_cons: list[str] = []
        winner = "tie"

        if orig_price and analog_price:
            cheaper_is_original = orig_price < analog_price
            diff_ratio = abs(orig_price - analog_price) / max(min(orig_price, analog_price), 1)
            if diff_ratio >= 0.05:
                winner = "original" if cheaper_is_original else "analog"
                if cheaper_is_original:
                    orig_pros.append("Ниже цена объявления")
                    analog_cons.append("Выше цена объявления")
                else:
                    analog_pros.append("Ниже цена объявления")
                    orig_cons.append("Выше цена объявления")

        insufficiency_markers = (
            "cannot complete",
            "do not contain any information",
            "would need search results",
            "insufficient",
            "not contain information",
        )
        if raw_content and any(marker in raw_content.lower() for marker in insufficiency_markers):
            note = "Недостаточно подтвержденных данных в поисковой выдаче Sonar для полного сравнения."
        else:
            note = "Sonar вернул ответ вне JSON, поэтому использована безопасная локальная деградация."

        return {
            "winner": winner,
            "orig_pros": orig_pros,
            "orig_cons": orig_cons,
            "analog_pros": analog_pros,
            "analog_cons": analog_cons,
            "price_diff": price_diff,
            "verdict": (
                f"{note} Сравнение построено по доступным полям объявлений "
                f"{original_name} и {analog_name} без неподтвержденных допущений."
            ),
        }
    
    def find_analogs(self, item_name: str) -> list[SonarAnalogResult]:
        """
        Найти РОВНО 3 аналога через Sonar.
        Возвращает список из 3 аналогов с описанием.
        """
        if not self.is_available():
            logger.warning("[SONAR] API not available - PERPLEXITY_API_KEY not set or invalid format")
            logger.warning("[SONAR] Key should start with 'pplx-' or 'sk-'")
            return []
        
        logger.info(f"[SONAR] Searching for 3 analogs for: {item_name}")
        
        try:
            prompt = self.ANALOG_PROMPT.format(item=item_name)
            # Увеличиваем max_tokens для поиска аналогов (нужно больше контекста)
            result = self._call_sonar(prompt, max_tokens=800, retries=3)
            
            if not result:
                logger.warning("[SONAR] Failed to get analogs from API response - _call_sonar returned None")
                logger.warning("[SONAR] Possible reasons: timeout, 500 error, invalid API key, or JSON parse error")
                logger.warning("[SONAR] Will use fallback methods (GigaChat/Google)")
                return []
            
            if "analogs" not in result:
                logger.warning(f"[SONAR] API response doesn't contain 'analogs' key")
                logger.warning(f"[SONAR] Response keys: {list(result.keys()) if isinstance(result, dict) else 'not a dict'}")
                logger.warning(f"[SONAR] Response preview: {str(result)[:500]}")
                logger.warning("[SONAR] Will use fallback methods (GigaChat/Google)")
                return []
        except Exception as e:
            logger.error(f"[SONAR] Exception during analog search: {e}")
            logger.info("[SONAR] Falling back to GigaChat/Google methods")
            return []
        
        raw_analogs = result.get("analogs", [])
        if not raw_analogs:
            logger.warning("[SONAR] No analogs in API response")
            return []
        
        # Обрабатываем результаты
        analogs = []
        for a in raw_analogs[:3]:  # Берем максимум 3
            name = a.get("name", "").strip()
            if name:  # Только если есть название
                analogs.append({
                    "name": name,
                    "description": a.get("key_diff", "") or a.get("description", ""),
                    "price_range": a.get("price_range", ""),
                    "key_difference": a.get("key_diff", "") or a.get("key_difference", "")
                })
        
        # Если получили меньше 3, логируем предупреждение
        if len(analogs) < 3:
            logger.warning(f"[SONAR] Got only {len(analogs)} analogs instead of 3")
        
        if analogs:
            logger.info(f"[SONAR] Found {len(analogs)} analogs: {', '.join([a['name'] for a in analogs])}")
        else:
            logger.warning("[SONAR] No valid analogs found")
        
        return analogs
    
    def compare_offers(
        self, 
        original_name: str,
        original_offer: dict,
        analog_name: str, 
        analog_offer: dict
    ) -> SonarComparisonResult:
        """
        Сравнить конкретные объявления оригинала и аналога через Sonar.
        Включает ссылки на объявления.
        
        Args:
            original_name: Название оригинальной модели
            original_offer: Dict с данными объявления (title, price, url)
            analog_name: Название аналога
            analog_offer: Dict с данными объявления аналога
        """
        if not self.is_available():
            return {"winner": "unknown", "recommendation": "Sonar unavailable"}
        
        # Извлекаем данные объявлений
        orig_title = original_offer.get("title", original_name)
        orig_price = original_offer.get("price")
        orig_url = original_offer.get("url", "")
        
        analog_title = analog_offer.get("title", analog_name)
        analog_price = analog_offer.get("price")
        analog_url = analog_offer.get("url", "")
        
        logger.info(f"[SONAR] Comparing offers: {orig_title[:50]}... vs {analog_title[:50]}...")
        
        orig_price_str = format_price(orig_price) if orig_price else "цена не указана"
        analog_price_str = format_price(analog_price) if analog_price else "цена не указана"
        
        prompt = self.COMPARE_PROMPT.format(
            original_name=original_name,
            original_title=orig_title[:100],
            original_price=orig_price_str,
            original_url=orig_url,
            analog_name=analog_name,
            analog_title=analog_title[:100],
            analog_price=analog_price_str,
            analog_url=analog_url
        )
        
        # Увеличиваем retries для сравнений (важная операция)
        result = self._call_sonar(
            prompt,
            max_tokens=600,
            retries=3,
            return_raw_on_parse_failure=True
        )

        if result and "_raw_content" in result:
            logger.warning(
                f"[SONAR] Comparison response for {original_name} vs {analog_name} was not JSON; "
                "using structured fallback instead of failing"
            )
            result = self._build_fallback_comparison(
                original_name=original_name,
                original_offer=original_offer,
                analog_name=analog_name,
                analog_offer=analog_offer,
                raw_content=str(result.get("_raw_content", "")),
            )
        
        if not result:
            logger.warning(f"[SONAR] Comparison failed for {original_name} vs {analog_name}")
            return {
                "winner": "unknown", 
                "recommendation": "Сравнение через Sonar не удалось (ошибка API или таймаут)",
                "original_url": orig_url,
                "analog_url": analog_url,
                "original_title": orig_title,
                "analog_title": analog_title
            }
        
        return {
            "winner": result.get("winner", "tie"),
            "original_advantages": ensure_list_str(result.get("orig_pros", [])),
            "original_disadvantages": ensure_list_str(result.get("orig_cons", [])),
            "analog_advantages": ensure_list_str(result.get("analog_pros", [])),
            "analog_disadvantages": ensure_list_str(result.get("analog_cons", [])),
            "recommendation": result.get("verdict", ""),
            "price_diff": result.get("price_diff", ""),
            "price_verdict": result.get("price_diff", "similar"),
            # Включаем ссылки на объявления
            "original_url": orig_url,
            "original_title": orig_title,
            "original_price": orig_price,
            "analog_url": analog_url,
            "analog_title": analog_title,
            "analog_price": analog_price,
            "sonar_comparison": True
        }
    
    def find_best_offer(self, offers: list[dict]) -> Optional[dict]:
        """
        Найти лучшее объявление из списка через Sonar.
        
        Args:
            offers: Список объявлений в формате dict
            
        Returns:
            Dict с best_index, best_score, reason, ranking или None при ошибке
        """
        if not self.is_available():
            return None
        
        if not offers:
            return {"best_index": -1, "best_score": 0.0, "reason": "No offers", "ranking": []}
        
        if len(offers) == 1:
            return {"best_index": 0, "best_score": 8.0, "reason": "Only one offer", "ranking": [{"index": 0, "score": 8.0, "brief_reason": "Single offer"}]}
        
        # Форматируем объявления для промпта
        offers_list = "\n\n".join([
            f"Объявление {i}:\n{json.dumps(offer, ensure_ascii=False, default=str, indent=2)}"
            for i, offer in enumerate(offers, 1)
        ])
        
        prompt = self.FIND_BEST_OFFER_PROMPT.format(offers_list=offers_list)
        
        logger.info(f"[SONAR] Finding best offer from {len(offers)} offers...")
        result = self._call_sonar(prompt, max_tokens=800, retries=2)
        
        if not result:
            logger.warning("[SONAR] Failed to find best offer via Sonar")
            return None
        
        # Проверяем наличие обязательных полей
        if "best_index" not in result:
            logger.warning(f"[SONAR] Invalid response format: missing best_index. Keys: {list(result.keys())}")
            return None
        
        best_index = result.get("best_index", 0)
        if not (0 <= best_index < len(offers)):
            logger.warning(f"[SONAR] Invalid best_index {best_index}, using 0")
            best_index = 0
        
        logger.info(f"[SONAR] Best offer selected: index {best_index}, score {result.get('best_score', 0):.1f}/10")
        return result
    
    def validate_market_prices(
        self,
        item_name: str,
        min_price: Optional[int],
        max_price: Optional[int],
        median_price: Optional[float],
        mean_price: Optional[int],
        client_price: Optional[int],
        offers_count: int
    ) -> Optional[dict]:
        """
        Валидировать и объяснить рыночные цены через Sonar.
        
        Returns:
            Dict с is_valid, explanation, anomalies, client_price_verdict или None
        """
        if not self.is_available():
            return None
        
        min_price_str = format_price(min_price) if min_price else "не указано"
        max_price_str = format_price(max_price) if max_price else "не указано"
        median_price_str = format_price(int(median_price)) if median_price else "не указано"
        mean_price_str = format_price(mean_price) if mean_price else "не указано"
        client_price_str = format_price(client_price) if client_price else "не указано"
        
        prompt = self.VALIDATE_MARKET_PRICES_PROMPT.format(
            item_name=item_name,
            min_price=min_price_str,
            max_price=max_price_str,
            median_price=median_price_str,
            mean_price=mean_price_str,
            client_price=client_price_str,
            offers_count=offers_count
        )
        
        logger.info(f"[SONAR] Validating market prices for {item_name}...")
        result = self._call_sonar(prompt, max_tokens=500, retries=2)
        
        if not result:
            logger.warning("[SONAR] Failed to validate market prices via Sonar")
            return None
        
        return result
    
    def enrich_offer_data(self, title: str, price: Optional[int], description: str = "") -> Optional[dict]:
        """
        Обогатить данные объявления через Sonar (извлечь vendor, model, year, specs и т.д.).
        
        Returns:
            Dict с vendor, model, year, condition, specs, pros, cons или None
        """
        if not self.is_available():
            return None
        
        price_str = format_price(price) if price else "не указана"
        desc = description[:500] if description else ""  # Ограничиваем длину
        
        prompt = self.ENRICH_OFFER_PROMPT.format(
            title=title[:200],
            price=price_str,
            description=desc
        )
        
        logger.debug(f"[SONAR] Enriching offer data for: {title[:50]}...")
        result = self._call_sonar(prompt, max_tokens=400, retries=1)
        
        if not result:
            logger.debug("[SONAR] Failed to enrich offer data via Sonar")
            return None
        
        return result


# Global Sonar instance
_sonar_finder: Optional[SonarAnalogFinder] = None

# Simple cache for Sonar results (session-based)
_sonar_cache: dict = {}


def get_sonar_finder() -> Optional[SonarAnalogFinder]:
    """Get or create Sonar finder instance."""
    global _sonar_finder
    if _sonar_finder is None:
        _sonar_finder = SonarAnalogFinder()
    return _sonar_finder if _sonar_finder.is_available() else None


def get_cached_sonar_analogs(item_name: str) -> Optional[list]:
    """Get cached Sonar analogs if available."""
    cache_key = item_name.lower().strip()
    return _sonar_cache.get(f"analogs_{cache_key}")


def cache_sonar_analogs(item_name: str, analogs: list) -> None:
    """Cache Sonar analogs for the item."""
    cache_key = item_name.lower().strip()
    _sonar_cache[f"analogs_{cache_key}"] = analogs


def clear_sonar_cache() -> None:
    """Clear Sonar cache."""
    global _sonar_cache
    _sonar_cache = {}


# =============================
# Generic Parser Strategy (after AIAnalyzer)
# =============================
class GenericParserStrategy(ParserStrategy):
    """Generic parser using basic regex + AI."""
    
    def __init__(self, analyzer: Optional[AIAnalyzer], use_ai: bool = True):
        self.analyzer = analyzer
        self.use_ai = use_ai
    
    def parse(self, html: str, url: str, model_name: str, title: str = "") -> list["LeasingOffer"]:
        """Parse generic page using basic + AI parsing."""
        # Basic parsing
        basic = parse_page_basic(html, model_name)
        
        # AI parsing
        ai_result = None
        if self.use_ai and self.analyzer:
            ai_result = self.analyzer.analyze_content(html)
        
        # Merge results
        merged = dict(basic)
        if ai_result:
            for k, v in ai_result.items():
                if v is not None:
                    merged[k] = v
        
        if not merged:
            return []
        
        # Create offer with enrichment
        domain = urlparse(url).netloc.replace("www.", "")
        offer = create_offer_from_merged(
            title=title or "Offer",
            url=url,
            domain=domain,
            model_name=model_name,
            merged=merged,
            text=html[:5000] if html else ""  # Pass first 5000 chars for enrichment
        )
        
        if offer and offer.has_data():
            return [offer]
        return []


# =============================
# Avito parsing
# =============================
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


# =============================
# Selenium handling
# =============================
class SeleniumFetcher:
    """Selenium-based page fetcher with lazy initialization and auto-recovery."""
    
    def __init__(self):
        self.driver: Optional[webdriver.Chrome] = None
        self._options: Optional[Options] = None
        self._max_restart_attempts = 3
    
    def _get_options(self) -> Options:
        """Get Chrome options (lazy initialization)."""
        if self._options is None:
            self._options = Options()
            self._options.add_argument("--headless=new")
            self._options.add_argument("--disable-gpu")
            self._options.add_argument("--no-sandbox")
            self._options.add_argument("--window-size=1920,1080")
            self._options.add_argument("--log-level=3")
            self._options.add_argument("--disable-logging")
            self._options.add_argument("--disable-dev-shm-usage")
            self._options.add_experimental_option("excludeSwitches", ["enable-logging"])
            self._options.add_argument(
            "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
        )
        return self._options

    def _is_driver_alive(self) -> bool:
        """Check if driver is still responsive."""
        if not self.driver:
            return False
        try:
            service = getattr(self.driver, "service", None)
            process = getattr(service, "process", None)
            if process is not None and process.poll() is not None:
                return False
            return bool(getattr(self.driver, "session_id", None))
        except Exception:
            return False
    
    def _restart_driver(self):
        """Restart driver after connection error."""
        logger.warning("Restarting Chrome driver due to connection issues...")
        self.close()
        time.sleep(2)  # Give ChromeDriver time to fully close
        self.driver = None  # Force recreation
    
    def _get_driver(self) -> webdriver.Chrome:
        """Get or create Chrome driver with health check."""
        if self.driver and self._is_driver_alive():
            return self.driver
        
        # Driver is dead or doesn't exist, create new one
        if self.driver:
            logger.warning("Driver is not responsive, recreating...")
            self.close()
        
        try:
            self.driver = webdriver.Chrome(options=self._get_options())
            # Set timeouts
            self.driver.set_page_load_timeout(CONFIG.page_load_timeout)
            self.driver.implicitly_wait(CONFIG.implicit_wait)
            self.driver.set_script_timeout(CONFIG.script_timeout)
            logger.debug("Chrome driver created successfully")
            return self.driver
        except Exception as e:
            logger.error(f"Failed to create Chrome driver: {e}")
            self.driver = None
            raise

    def close(self):
        """Close driver and release resources."""
        if self.driver:
            service = getattr(self.driver, "service", None)
            process = getattr(service, "process", None)
            service_alive = bool(process is not None and process.poll() is None)
            try:
                if service_alive and getattr(self.driver, "session_id", None):
                    self.driver.quit()
                elif service and hasattr(service, "stop"):
                    service.stop()
            except Exception as e:
                logger.debug(f"Error closing driver: {e}")
            finally:
                self.driver = None

    def fetch_page(
        self,
        url: str,
        scroll_times: int = CONFIG.default_scroll_times,
        wait: float = CONFIG.scroll_wait
    ) -> Optional[str]:
        """Fetch page with scrolling to load dynamic content, with auto-recovery."""
        if not is_valid_url(url):
            logger.warning(f"Invalid URL: {url}")
            return None
        
        for attempt in range(self._max_restart_attempts):
            try:
                driver = self._get_driver()
                
                # Try to load page with timeout
                try:
                    driver.set_page_load_timeout(CONFIG.page_load_timeout)
                    driver.get(url)
                except TimeoutException:
                    # If page load times out, try to get what we have
                    logger.warning(f"Page load timeout for {url}, trying to get partial content...")
                    try:
                        # Try to get page source anyway
                        return driver.page_source
                    except Exception as e:
                        logger.debug(f"Could not get partial content: {e}")
                        # Check if driver is still alive
                        if not self._is_driver_alive():
                            logger.warning("Driver died after timeout, restarting...")
                            self._restart_driver()
                            if attempt < self._max_restart_attempts - 1:
                                continue
                        return None
                
                # Scroll with timeout protection
                try:
                    last_height = driver.execute_script("return document.body.scrollHeight")
                    
                    for _ in range(max(0, scroll_times)):
                        try:
                            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                            time.sleep(wait)
                            new_height = driver.execute_script("return document.body.scrollHeight")
                            if new_height == last_height:
                                break
                            last_height = new_height
                        except Exception as scroll_err:
                            logger.debug(f"Scroll error for {url}: {scroll_err}")
                            # Check if driver is still alive
                            if not self._is_driver_alive():
                                logger.warning("Driver died during scroll, restarting...")
                                self._restart_driver()
                                if attempt < self._max_restart_attempts - 1:
                                    break  # Break scroll loop, will retry fetch
                            else:
                                break  # Just break scroll loop, continue with page source
                except Exception as scroll_err:
                    logger.debug(f"Scroll failed for {url}: {scroll_err}")
                    # Check if driver is still alive
                    if not self._is_driver_alive():
                        logger.warning("Driver died during scroll, restarting...")
                        self._restart_driver()
                        if attempt < self._max_restart_attempts - 1:
                            continue
                    # Continue anyway, we might have some content
                
                # Successfully got page source
                try:
                    return driver.page_source
                except Exception as e:
                    logger.debug(f"Could not get page source: {e}")
                    if not self._is_driver_alive():
                        logger.warning("Driver died when getting page source, restarting...")
                        self._restart_driver()
                        if attempt < self._max_restart_attempts - 1:
                            continue
                    return None
                    
            except TimeoutException as e:
                logger.warning(f"Timeout loading {url}: {e}")
                if attempt < self._max_restart_attempts - 1:
                    self._restart_driver()
                    time.sleep(1)
                    continue
                return None
            except Exception as e:
                error_str = str(e).lower()
                # Check for connection errors
                if any(keyword in error_str for keyword in [
                    "connection", "winerror 10061", "refused", 
                    "newconnectionerror", "max retries exceeded"
                ]):
                    logger.warning(f"Connection error loading {url}: {e}")
                    if attempt < self._max_restart_attempts - 1:
                        self._restart_driver()
                        time.sleep(2)  # Wait longer for connection issues
                        continue
                    return None
                else:
                    logger.error(f"Failed to load {url}: {e}")
                    if attempt < self._max_restart_attempts - 1:
                        # For other errors, still try restart once
                        self._restart_driver()
                        time.sleep(1)
                        continue
                    return None
        
        # All attempts failed
        logger.error(f"Failed to load {url} after {self._max_restart_attempts} attempts")
        return None


# =============================
# Basic regex parsing for non-Avito pages
# =============================
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


# =============================
# Search and URL handling
# =============================
@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type(requests.RequestException)
)
def _search_google_request(query: str, num_results: int) -> list[SearchResult]:
    """Make search request to Serper API (with retry and rate limiting)."""
    google_rate_limiter.wait_if_needed()
    resp = _requests_session.post(
        "https://google.serper.dev/search",
        headers={"X-API-KEY": CONFIG.serper_api_key, "Content-Type": "application/json"},
        json={"q": query, "gl": "ru", "hl": "ru", "num": num_results},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("organic", [])


@lru_cache(maxsize=100)
def search_google_cached(query: str, num_results: int = 10) -> tuple:
    """Cached Google search via Serper API."""
    if not CONFIG.serper_api_key:
        logger.warning("SERPER_API_KEY not set")
        return tuple()
    try:
        results = _search_google_request(query, num_results)
        return tuple(results)
    except requests.RequestException as exc:
        logger.error(f"Search error: {exc}")
        return tuple()


def search_google(query: str, num_results: int = 10) -> list[dict]:
    """Google search via Serper API (returns list for compatibility)."""
    if not CONFIG.serper_api_key:
        logger.warning("SERPER_API_KEY not set, skipping Google search")
        return []
    
    results = search_google_cached(query, num_results)
    if not results:
        logger.debug(f"No Google results for query: {query}")
    return list(results)


def search_specs_sites(item_name: str, num_sites: int = 5) -> list[dict]:
    """Search for sites with technical specifications using targeted queries."""
    if not CONFIG.serper_api_key:
        return []
    
    # Build targeted search queries for specs
    queries = [
        f"{item_name} характеристики",
        f"{item_name} технические характеристики",
        f"{item_name} specs характеристики",
        f"{item_name} полные характеристики",
    ]
    
    all_results = []
    seen_urls = set()
    
    for query in queries[:2]:  # Use first 2 queries to avoid too many requests
        results = search_google(query, num_results=num_sites)
        for result in results:
            url = result.get("link", "")
            if url and url not in seen_urls:
                # Filter for specs-related sites
                domain = urlparse(url).netloc.lower()
                # Prefer known specs sites
                specs_keywords = ["характеристики", "specs", "технические", "обзор", "комплектация"]
                snippet = (result.get("snippet", "") + " " + result.get("title", "")).lower()
                
                if any(keyword in snippet for keyword in specs_keywords) or any(keyword in domain for keyword in ["auto", "car", "tech", "spec"]):
                    seen_urls.add(url)
                    all_results.append(result)
                    if len(all_results) >= num_sites:
                        break
        
        if len(all_results) >= num_sites:
            break
    
    logger.info(f"Found {len(all_results)} specs sites for {item_name}")
    return all_results[:num_sites]


def extract_specs_from_multiple_sites(
    item_name: str,
    fetcher: "SeleniumFetcher",
    analyzer: Optional[AIAnalyzer],
    num_sites: int = 5
) -> dict:
    """Extract technical specifications by analyzing multiple specialized sites."""
    if not analyzer:
        logger.warning("AI analyzer not available for specs extraction")
        return {}
    
    # Search for specs sites
    specs_sites = search_specs_sites(item_name, num_sites)
    if not specs_sites:
        logger.warning(f"No specs sites found for {item_name}")
        return {}
    
    all_specs = {}
    successful_extractions = 0
    
    logger.info(f"Analyzing {len(specs_sites)} sites for {item_name} specifications...")
    
    for idx, site in enumerate(specs_sites, 1):
        url = site.get("link", "")
        title = site.get("title", "")
        
        if not url:
            continue
        
        try:
            # Fetch page content
            html = fetcher.fetch_page(url, scroll_times=1, wait=1.0)
            if not html:
                logger.debug(f"Failed to fetch {url}")
                continue
            
            # Clean and extract text
            cleaner = ContentCleaner()
            text = cleaner.clean(html)
            
            if len(text) < 100:
                logger.debug(f"Insufficient content from {url}")
                continue
            
            # Extract specs using AI
            specs = analyzer.extract_specs_from_text(text)
            
            if specs:
                # Merge specs (later sites can override earlier ones)
                for key, value in specs.items():
                    if value and (key not in all_specs or not all_specs[key]):
                        all_specs[key] = value
                
                successful_extractions += 1
                logger.debug(f"Extracted {len(specs)} specs from {title[:50]}...")
            
        except Exception as e:
            logger.warning(f"Error extracting specs from {url}: {e}")
            continue
    
    if all_specs:
        logger.info(f"Successfully extracted {len(all_specs)} specifications from {successful_extractions} sites")
    else:
        logger.warning(f"Failed to extract specs from any site for {item_name}")
    
    return all_specs


MANDATORY_SOURCES = [
    {
        "name": "alfaleasing.ru",
        "search_url": "https://alfaleasing.ru/search/?q={query}",
    },
    {
        "name": "sberleasing.ru",
        "search_url": "https://www.sberleasing.ru/search/?q={query}",
    },
    {
        "name": "avito.ru",
        "search_url": "https://www.avito.ru/rossiya?q={query}+лизинг",
    },
]


def generate_mandatory_urls(model_name: str) -> list[dict]:
    """Generate URLs for mandatory leasing sources."""
    query_encoded = model_name.replace(" ", "+").lower()
    mandatory = []
    for source in MANDATORY_SOURCES:
        url = source["search_url"].format(query=query_encoded)
        mandatory.append({
                "link": url,
                "title": f"{model_name} - {source['name']}",
                "is_mandatory": True,
                "source_name": source["name"],
        })
    return mandatory


def filter_search_results(results: list[dict], max_results: int = 10) -> list[dict]:
    """Filter search results, removing blocked domains."""
    filtered = []
    blocked_domains = {"chelindleasing"}
    
    for result in results:
        if len(filtered) >= max_results:
            break
        url = result.get("link", "")
        domain = urlparse(url).netloc.replace("www.", "")
        if any(blocked in domain for blocked in blocked_domains):
            continue
        filtered.append(result)
    return filtered


def merge_with_mandatory(search_results: list[dict], mandatory: list[dict]) -> list[dict]:
    """Merge search results with mandatory sources."""
    existing_domains = {urlparse(r.get("link", "")).netloc.replace("www.", "") for r in search_results}
    merged = []
    
    for m in mandatory:
        domain = m.get("source_name", "")
        if domain not in existing_domains:
            merged.append(m)
            existing_domains.add(domain)
    merged.extend(search_results)
    return merged


# =============================
# Market analysis
# =============================
def percentile(sorted_values: list[int], p: float) -> float:
    """Calculate percentile from sorted values (safe for large integers)."""
    if not sorted_values:
        return 0.0
    try:
        k = (len(sorted_values) - 1) * p
        f = int(k)
        c = min(f + 1, len(sorted_values) - 1)
        if f == c:
            val = sorted_values[int(k)]
            # Safe conversion: if value is too large, return as is
            try:
                return float(val)
            except OverflowError:
                # For very large numbers, return the integer value as float representation
                return float(str(val))
        
        # Calculate weighted average
        d0 = sorted_values[f] * (c - k)
        d1 = sorted_values[c] * (k - f)
        result = d0 + d1
        try:
            return float(result)
        except OverflowError:
            # Fallback: use simple average
            return float((sorted_values[f] + sorted_values[c]) / 2)
    except (OverflowError, ValueError) as e:
        logger.warning(f"Error calculating percentile: {e}, using middle value")
        mid_idx = len(sorted_values) // 2
        return float(sorted_values[mid_idx] if len(sorted_values) % 2 == 1 else (sorted_values[mid_idx - 1] + sorted_values[mid_idx]) / 2)


def filter_price_outliers(offers: list[LeasingOffer]) -> list[LeasingOffer]:
    """Remove price outliers using IQR method."""
    prices = [o.price for o in offers if o.price is not None]
    if len(prices) < CONFIG.outlier_min_samples:
        return offers
    
    prices_sorted = sorted(prices)
    q1 = percentile(prices_sorted, 0.25)
    q3 = percentile(prices_sorted, 0.75)
    iqr = q3 - q1
    lower = q1 - CONFIG.iqr_multiplier * iqr
    upper = q3 + CONFIG.iqr_multiplier * iqr
    
    filtered = [o for o in offers if o.price is None or (lower <= o.price <= upper)]
    removed = len(offers) - len(filtered)
    if removed:
        logger.info(f"Removed {removed} price outliers")
    return filtered


def filter_low_quality_offers(offers: list[LeasingOffer]) -> list[LeasingOffer]:
    """Filter out low-quality offers (missing critical data, suspicious content)."""
    if not offers:
        return []
    
    filtered = []
    removed = 0
    
    for offer in offers:
        # Skip offers with suspiciously short titles
        if len(offer.title.strip()) < 5:
            logger.debug(f"Removing offer with too short title: {offer.title[:30]}")
            removed += 1
            continue
        
        # Skip offers with invalid URLs
        if not is_valid_url(offer.url):
            logger.debug(f"Removing offer with invalid URL: {offer.url}")
            removed += 1
            continue
        
        # Skip offers with suspicious prices (too small for leasing)
        if offer.price is not None and offer.price < CONFIG.min_valid_price:
            logger.debug(f"Removing offer with suspiciously low price: {offer.price}")
            removed += 1
            continue
        
        # Keep offers that have at least some meaningful data
        has_meaningful_data = any([
            offer.price is not None,
            offer.monthly_payment is not None,
            offer.price_on_request,
            offer.year is not None,
            offer.vendor,
            offer.specs,
        ])
        
        if not has_meaningful_data:
            logger.debug(f"Removing offer with no meaningful data: {offer.title[:50]}")
            removed += 1
            continue
        
        filtered.append(offer)
    
    if removed > 0:
        logger.info(f"Filtered out {removed} low-quality offers (kept {len(filtered)})")
    
    return filtered


def collect_analogs(
    item_name: str,
    offers: list[LeasingOffer],
    use_ai: bool,
    analyzer: Optional[AIAnalyzer],
    sonar_finder: Optional[SonarAnalogFinder] = None
) -> tuple[list[str], list[SonarAnalogResult]]:
    """
    Collect analog models using Sonar as PRIMARY method.
    Sonar always returns exactly 3 analogs.
    
    Returns:
        Tuple of (analog_names, sonar_details)
        - analog_names: List of 3 analog names (from Sonar)
        - sonar_details: Detailed info from Sonar
    """
    sonar_details: list[SonarAnalogResult] = []
    
    # Check cache first
    cached_analogs = get_cached_sonar_analogs(item_name)
    if cached_analogs:
        logger.info(f"[SONAR] Using cached analogs for '{item_name}'")
        analog_names = [a["name"] for a in cached_analogs if a.get("name")]
        return analog_names[:3], cached_analogs[:3]
    
    # PRIMARY: Always use Sonar for analogs (if available)
    if sonar_finder and sonar_finder.is_available():
        logger.info("=" * 70)
        logger.info("[SONAR] PRIMARY METHOD: Searching for analogs using Perplexity Sonar API")
        logger.info("=" * 70)
        try:
            sonar_details = sonar_finder.find_analogs(item_name)
            
            if sonar_details:
                analog_names = [a["name"] for a in sonar_details if a.get("name")]
                if analog_names:
                    logger.info(f"[SONAR] Successfully found {len(analog_names)} analogs via Sonar")
                    logger.info(f"[SONAR] Analogs: {', '.join(analog_names)}")
                    # Cache the results
                    cache_sonar_analogs(item_name, sonar_details[:3])
                    # Return exactly 3 (or as many as we have)
                    return analog_names[:3], sonar_details[:3]
                else:
                    logger.warning("[SONAR] Sonar returned results but no valid analog names")
            else:
                logger.warning("[SONAR] Sonar did not return any analogs - will use fallback methods")
        except Exception as e:
            logger.error(f"[SONAR] Error during Sonar search: {e}")
            logger.info("[SONAR] Falling back to GigaChat/Google methods")
            sonar_details = []
    else:
        logger.warning("=" * 70)
        logger.warning("[SONAR] Sonar not available - PERPLEXITY_API_KEY not set or invalid format (should start with 'pplx-' or 'sk-')")
        logger.warning("[SONAR] Will use fallback methods (GigaChat/Google)")
        logger.warning("=" * 70)
        sonar_details = []
    
    # FALLBACK: Only if Sonar is not available or failed
    logger.info("[FALLBACK] Using fallback methods for analog search...")
    analogs_set = set()
    
    # Collect from offers
    for o in offers:
        for a in o.analogs:
            analogs_set.add(a.strip())
    
    # GigaChat suggestion
    if len(analogs_set) < 3 and use_ai and analyzer:
        logger.info("[FALLBACK] Using GigaChat for analog suggestions...")
        ai_analogs = analyzer.suggest_analogs(item_name)
        for a in ai_analogs:
            analogs_set.add(a)

    # Google search
    if len(analogs_set) < 3:
        logger.info("[FALLBACK] Using Google search for analogs...")
        fallback_results = search_google(f"{item_name} аналог", 5)
        for r in fallback_results:
            title = r.get("title") or ""
            parts = re.split(r"[–—|-]", title)
            if parts:
                candidate = parts[0].strip()
                if candidate and len(candidate.split()) <= 6:
                    analogs_set.add(candidate)

    # Return exactly 3 analogs (or less if not found)
    analog_names = [a for a in analogs_set if a][:3]
    logger.info(f"[FALLBACK] Found {len(analog_names)} analogs via fallback methods")
    return analog_names, sonar_details


def fetch_listing_summaries(query: str, top_n: int = 3) -> list[ListingSummary]:
    """Fetch brief listing summaries for analog comparison."""
    results = search_google(query, num_results=top_n)
    summaries: list[ListingSummary] = []
    
    for r in results[:top_n]:
        title = r.get("title", "")
        snippet = r.get("snippet", "")
        price_guess = extract_price_candidate(title) or extract_price_candidate(snippet)
        summaries.append({
                "title": title,
                "link": r.get("link", ""),
                "snippet": snippet,
                "price_guess": price_guess,
        })
    return summaries


def analyze_market(
    item_name: str,
    offers: list[LeasingOffer],
    client_price: Optional[int],
    sonar_finder: Optional[SonarAnalogFinder] = None
) -> dict:
    """Perform market analysis on collected offers."""
    prices = [o.price for o in offers if o.price is not None]
    
    result = {
        "item": item_name,
        "offers_used": [asdict(o) for o in offers],
        "analogs_suggested": [],
        "market_range": None,
        "median_price": None,
        "mean_price": None,
        "client_price": client_price,
        "client_price_ok": None,
        "explanation": "",
    }
    
    if not prices:
        result["explanation"] = "No prices collected."
        return result

    prices_sorted = sorted(prices)
    min_p, max_p = prices_sorted[0], prices_sorted[-1]
    
    # Safe calculation of median and mean to avoid OverflowError
    try:
        median_p = statistics.median(prices_sorted)
        # Convert to int if it's a whole number to avoid float precision issues
        if isinstance(median_p, float) and median_p.is_integer():
            median_p = int(median_p)
    except (OverflowError, ValueError) as e:
        logger.warning(f"Error calculating median: {e}, using middle value")
        # Fallback: use middle value
        mid_idx = len(prices_sorted) // 2
        median_p = prices_sorted[mid_idx] if len(prices_sorted) % 2 == 1 else (prices_sorted[mid_idx - 1] + prices_sorted[mid_idx]) // 2
    
    try:
        # Calculate mean safely
        total = sum(prices_sorted)
        count = len(prices_sorted)
        if count > 0:
            mean_p = total // count  # Use integer division to avoid float
            # If we need more precision, calculate remainder
            remainder = total % count
            if remainder > 0:
                # Round to nearest integer
                mean_p = round(total / count) if total < 10**15 else mean_p
        else:
            mean_p = 0
    except (OverflowError, ValueError) as e:
        logger.warning(f"Error calculating mean: {e}, using median")
        mean_p = median_p if isinstance(median_p, (int, float)) else 0

    result["market_range"] = [min_p, max_p]
    # Safe conversion to float
    try:
        if isinstance(median_p, int):
            result["median_price"] = float(median_p) if median_p < 10**15 else median_p
        else:
            result["median_price"] = median_p
    except OverflowError:
        result["median_price"] = median_p  # Keep as int if too large
    
    result["mean_price"] = mean_p

    if client_price is not None:
        try:
            deviation = (client_price - median_p) / median_p * 100
            ok = abs(deviation) <= CONFIG.price_deviation_tolerance * 100
            result["client_price_ok"] = ok
            verdict = "confirmed" if ok else "not confirmed"
            result["explanation"] = (
                f"Market range {format_price(min_p)} – {format_price(max_p)}, median {format_price(median_p)}. "
                f"Client price {format_price(client_price)} ({deviation:+.1f}%), {verdict}."
            )
        except (OverflowError, ZeroDivisionError) as e:
            logger.warning(f"Error calculating deviation: {e}")
            result["client_price_ok"] = None
            result["explanation"] = (
                f"Market range {format_price(min_p)} – {format_price(max_p)}, median {format_price(median_p)}. "
                f"Client price {format_price(client_price)}."
            )
    else:
        result["explanation"] = (
            f"Market range {format_price(min_p)} – {format_price(max_p)}, median {format_price(median_p)}."
        )
    
    # Улучшаем объяснение через Sonar (если доступен)
    if sonar_finder and sonar_finder.is_available():
        try:
            sonar_validation = sonar_finder.validate_market_prices(
                item_name=item_name,
                min_price=min_p,
                max_price=max_p,
                median_price=median_p,
                mean_price=mean_p,
                client_price=client_price,
                offers_count=len(offers)
            )
            
            if sonar_validation:
                # Объединяем объяснение Sonar с базовым
                sonar_explanation = sonar_validation.get("explanation", "")
                if sonar_explanation:
                    result["explanation"] = f"{result['explanation']} {sonar_explanation}"
                
                # Добавляем информацию об аномалиях
                anomalies = sonar_validation.get("anomalies", [])
                if anomalies:
                    result["anomalies"] = anomalies
                
                # Обновляем verdict для цены клиента
                client_verdict = sonar_validation.get("client_price_verdict")
                if client_verdict and client_price:
                    result["client_price_verdict"] = client_verdict
                
                result["sonar_validation"] = True
                logger.info("[SONAR] Market prices validated via Sonar")
        except Exception as e:
            logger.warning(f"[SONAR] Error validating market prices: {e}")
            result["sonar_validation"] = False
    else:
        result["sonar_validation"] = False

    return result


# =============================
# Pipeline
# =============================
def extract_model_from_query(query: str) -> str:
    """Extract model name (first two words) from query."""
    parts = query.split()
    return " ".join(parts[:2]) if parts else ""


def _process_single_url(
    result: dict,
    model_name: str,
    fetcher: SeleniumFetcher,
    parser: ParserStrategy,
    idx: int,
    total: int
) -> list[LeasingOffer]:
    """Process a single URL and return offers."""
    url = result.get("link", "")
    title = result.get("title", "")
    
    if not is_valid_url(url):
        logger.debug(f"[{idx}/{total}] Invalid URL: {url}")
        return []
    
    domain = urlparse(url).netloc.replace("www.", "")
    logger.debug(f"[{idx}/{total}] Processing {domain} | {url}")
    
    # Fetch page
    is_avito = CONFIG.avito_domain in domain
    scroll_times = CONFIG.avito_scroll_times if is_avito else CONFIG.other_scroll_times
    html = fetcher.fetch_page(url, scroll_times=scroll_times, wait=CONFIG.scroll_wait)
    
    if not html:
        logger.debug(f"[{idx}/{total}] Failed to load {url}")
        return []
    
    # Parse using strategy
    try:
        offers = parser.parse(html, url, model_name, title)
        if offers:
            logger.debug(f"[{idx}/{total}] Found {len(offers)} offers from {domain}")
        return offers
    except Exception as e:
        logger.warning(f"[{idx}/{total}] Error parsing {url}: {e}")
        return []


def search_and_analyze(
    query: str,
    fetcher: SeleniumFetcher,
    analyzer: Optional[AIAnalyzer],
    num_results: int = 5,
    use_ai: bool = True,
) -> list[LeasingOffer]:
    """Main search and analysis pipeline with parallel processing."""
    logger.info("=" * 70)
    logger.info(f"Search query: {query}")
    logger.info("=" * 70)

    model_name = extract_model_from_query(query)
    mandatory_urls = generate_mandatory_urls(model_name)
    logger.info(f"Mandatory sources: {len(mandatory_urls)}")

    search_results = search_google(query, num_results * 2)
    if not search_results:
        logger.warning(f"No Google results for query: {query}")
        filtered_google = []
    else:
        filtered_google = filter_search_results(search_results, num_results)

    all_results = merge_with_mandatory(filtered_google, mandatory_urls)
    logger.info(f"Total URLs: {len(all_results)}")

    if not all_results:
        logger.warning("No URLs to process")
        return []

    # Create parser strategies
    avito_parser = AvitoParserStrategy()
    generic_parser = GenericParserStrategy(analyzer, use_ai)

    offers: list[LeasingOffer] = []
    
    # Process URLs in parallel
    with ThreadPoolExecutor(max_workers=CONFIG.max_workers) as executor:
        futures = {}
        
        for idx, result in enumerate(all_results, 1):
            url = result.get("link", "")
            domain = urlparse(url).netloc.replace("www.", "")
            is_avito = CONFIG.avito_domain in domain
            
            # Choose parser strategy
            parser = avito_parser if is_avito else generic_parser
            
            # Submit task
            future = executor.submit(
                _process_single_url,
                result,
                model_name,
                fetcher,
                parser,
                idx,
                len(all_results)
            )
            futures[future] = (idx, url)
        
        # Collect results with progress bar
        with tqdm(total=len(futures), desc="Processing URLs", unit="url") as pbar:
            for future in as_completed(futures):
                idx, url = futures[future]
                try:
                    url_offers = future.result()
                    offers.extend(url_offers)
                except Exception as e:
                    logger.error(f"Error processing {url}: {e}")
                finally:
                    pbar.update(1)

    # Deduplicate and filter outliers
    # Apply filters in order: quality -> deduplication -> outliers
    offers = filter_low_quality_offers(offers)
    offers = deduplicate_offers(offers)
    offers = filter_price_outliers(offers)
    
    logger.info(f"Total offers after processing: {len(offers)}")
    return offers


# =============================
# Output formatting
# =============================
def print_offer(idx: int, o: LeasingOffer):
    """Print single offer details."""
    print(f"\n[{idx}] {o.title}")
    print(f"    Source: {o.source}")
    print(f"    Model: {o.model}")
    if o.category:
        print(f"    Category: {o.category}")
    print(f"    URL: {o.url}")
    
    if o.price_str or o.monthly_payment_str or o.price_on_request:
        print("    --- Pricing ---")
        if o.price_on_request and not o.price:
            print("    Price on request")
        if o.price_str:
            if o.currency and o.currency.upper() != "RUB":
                print(f"    Price: {o.price_str} ({o.currency})")
            else:
                print(f"    Price: {o.price_str}")
        if o.monthly_payment_str:
            print(f"    Monthly payment: {o.monthly_payment_str}")
    
    if any([o.year, o.power, o.mileage, o.vendor, o.condition, o.location]):
        print("    --- Specifications ---")
        if o.vendor:
            print(f"    Vendor: {o.vendor}")
        if o.year:
            print(f"    Year: {o.year}")
        if o.condition:
            print(f"    Condition: {o.condition}")
        if o.power:
            print(f"    Power: {o.power}")
        if o.mileage:
            print(f"    Mileage: {o.mileage}")
        if o.location:
            print(f"    Location: {o.location}")
    
    if o.specs:
        print("    --- Additional specs ---")
        for k, v in o.specs.items():
            print(f"    {k}: {v}")
    
    if o.pros:
        print("    --- Pros ---")
        for p in o.pros:
            print(f"    + {p}")
    
    if o.cons:
        print("    --- Cons ---")
        for c in o.cons:
            print(f"    - {c}")
    
    if o.analogs:
        print("    --- Mentioned analogs ---")
        for a in o.analogs:
            print(f"    • {a}")


def print_results(offers: list[LeasingOffer]):
    """Print all results."""
    print("\n" + "=" * 70)
    print(f"Found offers: {len(offers)}")
    print("=" * 70)
    for i, o in enumerate(offers, 1):
        print_offer(i, o)


def print_analog_details(analog_details: list[dict]):
    """Print analog comparison details."""
    print("\nAnalog comparison:")
    for a in analog_details:
        p_est = a.get("avg_price_guess")
        print(f"--- {a['name']} ---")
        print(f"  Price ~ {format_price(p_est) if p_est else 'No data'}")
        if a.get('note'):
            print(f"  Note: {a['note']}")
        if a['pros']:
            print(f"  [+] {', '.join(a['pros'])}")
        if a['cons']:
            print(f"  [-] {', '.join(a['cons'])}")
        
        # Print sources
        print("  Sources:")
        printed_links = set()
        if a.get("best_link"):
            print(f"    [Recommended] {a['best_link']}")
            printed_links.add(a['best_link'])
        
        if a.get("listings"):
            for l in a["listings"]:
                lnk = l.get('link', '')
                if lnk and lnk not in printed_links:
                    print(f"    {l.get('title', 'Link')}: {lnk}")


def print_best_offer_analysis(best_offer: Optional[LeasingOffer], analysis: dict, item_name: str):
    """Print analysis of the best offer."""
    if not best_offer:
        return
    
    print("\n" + "=" * 70)
    print(f"🏆 BEST OFFER: {item_name}")
    print("=" * 70)
    
    print(f"\n📋 {best_offer.title}")
    print(f"   URL: {best_offer.url}")
    print(f"   Source: {best_offer.source}")
    
    if best_offer.price_str:
        print(f"   💰 Price: {best_offer.price_str}")
    if best_offer.year:
        print(f"   📅 Year: {best_offer.year}")
    if best_offer.condition:
        print(f"   ⚙️  Condition: {best_offer.condition}")
    if best_offer.location:
        print(f"   📍 Location: {best_offer.location}")
    
    score = analysis.get("best_score", 0)
    reason = analysis.get("reason", "")
    print(f"\n   ⭐ Score: {score:.1f}/10")
    if reason:
        print(f"   💡 Reason: {reason}")
    
    ranking = analysis.get("ranking", [])
    if ranking and len(ranking) > 1:
        print(f"\n   📊 Ranking of all offers:")
        for rank in ranking[:5]:  # Top 5
            idx = rank.get("index", 0)
            score_r = rank.get("score", 0)
            brief = rank.get("brief_reason", "")
            print(f"      {idx+1}. Score {score_r:.1f}/10 - {brief}")


def print_best_offers_comparison(comparisons: dict, original_name: str):
    """Print comparison between best original and best analog offers."""
    if not comparisons:
        return
    
    print("\n" + "=" * 70)
    print("⚖️  COMPARISON: Best Original vs Best Analogs")
    print("=" * 70)
    
    for analog_name, comparison in comparisons.items():
        print(f"\n{'─' * 60}")
        print(f"Original: {original_name}")
        print(f"Analog: {analog_name}")
        print(f"{'─' * 60}")
        
        winner = comparison.get("winner", "original")
        orig_score = comparison.get("original_score", 0)
        analog_score = comparison.get("analog_score", 0)
        
        if winner == "original":
            print(f"🏆 Winner: ORIGINAL ({orig_score:.1f}/10 vs {analog_score:.1f}/10)")
        else:
            print(f"🏆 Winner: ANALOG ({analog_score:.1f}/10 vs {orig_score:.1f}/10)")
        
        # Price comparison
        price_comp = comparison.get("price_comparison", {})
        if price_comp:
            orig_price = price_comp.get("original_price")
            analog_price = price_comp.get("analog_price")
            diff = price_comp.get("difference_percent", 0)
            verdict = price_comp.get("price_verdict", "similar")
            
            print(f"\n💰 Price Comparison:")
            print(f"   Original: {format_price(orig_price)}")
            print(f"   Analog: {format_price(analog_price)}")
            if diff != 0:
                print(f"   Difference: {diff:+.1f}% ({verdict})")
        
        # Pros and cons
        pros_orig = comparison.get("pros_original", [])
        cons_orig = comparison.get("cons_original", [])
        pros_analog = comparison.get("pros_analog", [])
        cons_analog = comparison.get("cons_analog", [])
        
        if pros_orig:
            print(f"\n✅ Original Advantages:")
            for p in pros_orig[:3]:
                print(f"   + {p}")
        
        if cons_orig:
            print(f"\n❌ Original Disadvantages:")
            for c in cons_orig[:3]:
                print(f"   - {c}")
        
        if pros_analog:
            print(f"\n✅ Analog Advantages:")
            for p in pros_analog[:3]:
                print(f"   + {p}")
        
        if cons_analog:
            print(f"\n❌ Analog Disadvantages:")
            for c in cons_analog[:3]:
                print(f"   - {c}")
        
        # Recommendation
        recommendation = comparison.get("recommendation", "")
        if recommendation:
            print(f"\n💡 Recommendation:")
            print(f"   {recommendation}")
        
        # Use cases
        use_cases_orig = comparison.get("use_cases_original", [])
        use_cases_analog = comparison.get("use_cases_analog", [])
        
        if use_cases_orig:
            print(f"\n📌 When to choose Original:")
            for uc in use_cases_orig[:2]:
                print(f"   • {uc}")
        
        if use_cases_analog:
            print(f"\n📌 When to choose Analog:")
            for uc in use_cases_analog[:2]:
                print(f"   • {uc}")


def print_final_report(report: dict, client_price: Optional[int], analog_details: list[dict]):
    """Print final market report with deep analysis."""
    print("\n" + "=" * 70)
    print("FINAL REPORT")
    print("=" * 70)
    
    if report["market_range"]:
        min_p, max_p = report["market_range"]
        print(f"Market range: {format_price(min_p)} – {format_price(max_p)}")
        print(f"Median: {format_price(report['median_price'])}")
    
    if client_price:
        status = "OK" if report.get("client_price_ok") else "Deviation > 20%"
        print(f"Client price: {format_price(client_price)} -> {status}")
    
    print(f"Comment: {report['explanation']}")
    
    if report.get("ai_flag"):
        print(f"WARNING: {report.get('ai_comment')}")
    
    # Print best original offer analysis
    best_original = report.get("best_original_offer")
    best_original_analysis = report.get("best_original_analysis", {})
    if best_original:
        item_name = report.get("item", "Unknown")
        # Convert dict to LeasingOffer if needed
        if isinstance(best_original, dict):
            best_offer_obj = LeasingOffer(**best_original)
        else:
            best_offer_obj = best_original
        print_best_offer_analysis(best_offer_obj, best_original_analysis, item_name)
    
    # Print comparison with analogs
    comparisons = report.get("best_offers_comparison", {})
    if comparisons:
        original_name = report.get("item", "Unknown")
        print_best_offers_comparison(comparisons, original_name)

    if analog_details:
        print_analog_details(analog_details)


def save_results_json(offers: list[LeasingOffer], item_name: str = "results", market_report: Optional[dict] = None):
    """Save results to JSON file."""
    safe_name = "".join(c if c.isalnum() or c in " _-" else "_" for c in item_name)
    filename = f"{safe_name}.json"
    data = {"offers": [asdict(o) for o in offers]}
    if market_report:
        data["market_report"] = market_report
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info(f"JSON saved to {filename}")


# =============================
# User input handling
# =============================
def get_user_input() -> UserInput:
    """Get and validate user input."""
    print("=" * 70)
    print("Leasing Asset Market Analyzer (Avito + AI)")
    print("=" * 70)

    item = input("\nEnter leasing item (e.g., BMW M5 2024): ").strip()
    if not item:
        raise ValueError("Empty query")

    client_price_input = input("Client price (digits only, optional): ").strip()
    client_price = digits_to_int(client_price_input) if client_price_input else None

    use_ai_str = input("Use AI for analysis (y/n, default y): ").strip().lower()
    use_ai = use_ai_str != "n"

    num_input = input("Number of results to search (default 5): ").strip()
    num_results = int(num_input) if num_input.isdigit() else CONFIG.default_num_results

    return {
        "item": item,
        "client_price": client_price,
        "use_ai": use_ai,
        "num_results": num_results,
    }


# =============================
# Deep offer comparison
# =============================
def find_best_offer_from_list(
    offers: list[LeasingOffer],
    analyzer: Optional[AIAnalyzer],
    use_ai: bool,
    item_name: str,
    sonar_finder: Optional[SonarAnalogFinder] = None
) -> tuple[Optional[LeasingOffer], dict]:
    """
    Find the best offer from a list by comparing all offers with each other.
    Использует ТОЛЬКО Sonar API - без fallback методов.
    
    Returns:
        Tuple of (best_offer, comparison_result)
    """
    if not offers:
        return None, {}
    
    if len(offers) == 1:
        return offers[0], {"best_index": 0, "best_score": 8.0, "reason": "Only one offer", "sonar_used": False}
    
    # Convert offers to dict format
    offers_dict = [asdict(o) for o in offers]
    
    # ONLY Sonar - no fallback
    if not use_ai or not sonar_finder or not sonar_finder.is_available():
        logger.error("[SONAR] Sonar not available - cannot find best offer without Sonar")
        # Return first offer as fallback (simple fallback, not AI-based)
        return offers[0], {"best_index": 0, "best_score": 5.0, "reason": "Sonar unavailable - using first offer", "sonar_used": False}
    
    logger.info(f"[SONAR] Finding best offer from {len(offers)} offers using Sonar (ONLY METHOD)...")
    try:
        sonar_result = sonar_finder.find_best_offer(offers_dict)
        if sonar_result and "best_index" in sonar_result:
            best_index = sonar_result.get("best_index", 0)
            if 0 <= best_index < len(offers):
                best_offer = offers[best_index]
                logger.info(f"[SONAR] Best offer selected: {best_offer.title[:50]}... (score: {sonar_result.get('best_score', 0):.1f}/10)")
                sonar_result["sonar_used"] = True
                return best_offer, sonar_result
            else:
                logger.error(f"[SONAR] Invalid best_index {best_index}, using first offer")
                return offers[0], {"best_index": 0, "best_score": 5.0, "reason": "Invalid Sonar result", "sonar_used": False}
        else:
            logger.error("[SONAR] Sonar returned invalid result, using first offer")
            return offers[0], {"best_index": 0, "best_score": 5.0, "reason": "Invalid Sonar result", "sonar_used": False}
    except Exception as e:
        logger.error(f"[SONAR] Error finding best offer via Sonar: {e}")
        logger.error("[SONAR] Cannot proceed without Sonar - using first offer as fallback")
        return offers[0], {"best_index": 0, "best_score": 5.0, "reason": f"Sonar error: {str(e)[:50]}", "sonar_used": False}


def compare_best_offers_original_vs_analogs(
    best_original: Optional[LeasingOffer],
    best_analogs: list[tuple[str, Optional[LeasingOffer]]],  # [(analog_name, best_offer), ...]
    original_name: str,
    analyzer: Optional[AIAnalyzer],
    use_ai: bool
) -> dict:
    """
    Compare best original offer with best analog offers.
    
    Returns:
        Dictionary with comparison results for each analog
    """
    if not best_original:
        return {}
    
    if not best_analogs:
        return {}
    
    comparisons = {}
    
    for analog_name, best_analog in best_analogs:
        if not best_analog:
            continue
        
        if not use_ai or not analyzer:
            # Simple price comparison
            orig_price = best_original.price or 0
            analog_price = best_analog.price or 0
            if orig_price and analog_price:
                diff = ((analog_price - orig_price) / orig_price) * 100
                comparisons[analog_name] = {
                    "winner": "original" if abs(diff) < 5 else ("analog" if diff < -5 else "original"),
                    "price_comparison": {
                        "original_price": orig_price,
                        "analog_price": analog_price,
                        "difference_percent": diff,
                        "price_verdict": "analog_cheaper" if diff < -5 else ("original_cheaper" if diff > 5 else "similar")
                    },
                    "recommendation": f"Price difference: {diff:+.1f}%"
                }
            continue
        
        logger.info(f"Comparing best original with best {analog_name}...")
        
        original_dict = asdict(best_original)
        analog_dict = asdict(best_analog)
        
        comparison = analyzer.compare_best_offers(
            best_original=original_dict,
            best_analog=analog_dict,
            original_name=original_name,
            analog_name=analog_name
        )
        
        # Add URLs to comparison result
        if comparison:
            comparison["original_url"] = best_original.url
            comparison["analog_url"] = best_analog.url
            comparison["original_title"] = best_original.title
            comparison["analog_title"] = best_analog.title
        
        comparisons[analog_name] = comparison
    
    return comparisons


# =============================
# Main pipeline execution
# =============================
def run_pipeline(params: UserInput) -> tuple[list[LeasingOffer], dict, list[dict]]:
    """Execute the main analysis pipeline."""
    item = params["item"]
    client_price = params["client_price"]
    use_ai = params["use_ai"]
    num_results = params["num_results"]
    
    # Initialize components
    fetcher = SeleniumFetcher()
    cleaner = ContentCleaner()
    
    # Initialize Sonar for analog search (PRIMARY method)
    sonar_finder = get_sonar_finder()
    if sonar_finder:
        logger.info("=" * 70)
        logger.info("[SONAR] Perplexity Sonar API initialized - will be used as PRIMARY method for analog search")
        logger.info("=" * 70)
    else:
        logger.warning("=" * 70)
        logger.warning("[SONAR] Perplexity API not available - PERPLEXITY_API_KEY not set or invalid format (should start with 'pplx-' or 'sk-')")
        logger.warning("[SONAR] Will use fallback methods (GigaChat/Google) for analog search")
        logger.warning("=" * 70)
    analyzer = None
    if use_ai and CONFIG.gigachat_auth_data:
        client = GigaChatClient(CONFIG.gigachat_auth_data)
        analyzer = AIAnalyzer(client, cleaner)

    try:
        query = f"{item} {CONFIG.default_search_suffix}"
        offers = search_and_analyze(query, fetcher, analyzer, num_results=num_results, use_ai=use_ai)
        
        # Retry logic if empty
        if not offers:
            logger.warning("Direct search returned no results, trying simpler query...")
            query_simple = f"{item} {CONFIG.fallback_search_suffix}"
            offers = search_and_analyze(query_simple, fetcher, analyzer, num_results=num_results, use_ai=use_ai)
        
        if not offers:
            logger.error("Could not extract offers even after retry")
            return [], {}, []

        # =============================
        # ENRICH WITH SPECS FROM SPECIALIZED SITES
        # =============================
        if use_ai and analyzer:
            logger.info("=" * 70)
            logger.info("Enriching offers with technical specifications...")
            logger.info("=" * 70)
            
            # Extract specs for the main item
            item_specs = extract_specs_from_multiple_sites(
                item_name=item,
                fetcher=fetcher,
                analyzer=analyzer,
                num_sites=5
            )
            
            # Enrich offers with specs if they don't have enough
            if item_specs:
                logger.info(f"Extracted {len(item_specs)} specifications, enriching offers...")
                enriched_count = 0
                for offer in offers:
                    # Merge specs: prioritize existing, add missing from item_specs
                    if len(offer.specs) < 3:  # If offer has less than 3 specs, enrich it
                        # Add missing specs
                        added_count = 0
                        for key, value in item_specs.items():
                            if key not in offer.specs and value:
                                offer.specs[key] = value
                                added_count += 1
                        
                        if added_count > 0:
                            enriched_count += 1
                            logger.debug(f"Enriched offer '{offer.title[:50]}...' with {added_count} new specs (total: {len(offer.specs)})")
                
                if enriched_count > 0:
                    logger.info(f"Enriched {enriched_count} offers with technical specifications")

        # Collect analogs (Sonar primary, fallback to GigaChat/Google)
        analogs, sonar_analog_details = collect_analogs(
            item, offers, use_ai=use_ai, analyzer=analyzer, sonar_finder=sonar_finder
        )
        
        # Generate initial report
        report = analyze_market(item, offers, client_price, sonar_finder=sonar_finder)
        report["analogs_suggested"] = analogs
        report["sonar_used"] = bool(sonar_analog_details)  # Track if Sonar was used

        # Validate with AI
        if use_ai and analyzer and report["median_price"]:
            logger.info("Validating report with AI...")
            validation = analyzer.validate_report(report)
            if not validation.get("is_valid"):
                logger.warning(f"AI flagged report as suspicious: {validation.get('comment')}")
                report["ai_flag"] = "SUSPICIOUS"
                report["ai_comment"] = validation.get("comment")
            else:
                logger.info("AI confirmed report validity")

        # =============================
        # DEEP ANALYSIS: Find best original offer
        # =============================
        best_original_offer = None
        best_original_analysis = {}
        
        if use_ai and analyzer and offers:
            logger.info("=" * 70)
            logger.info("DEEP ANALYSIS: Comparing original offers to find the best one...")
            logger.info("=" * 70)
            best_original_offer, best_original_analysis = find_best_offer_from_list(
                offers=offers,
                analyzer=analyzer,
                use_ai=use_ai,
                item_name=item,
                sonar_finder=sonar_finder
            )
            report["best_original_offer"] = asdict(best_original_offer) if best_original_offer else None
            report["best_original_analysis"] = best_original_analysis
        
        # =============================
        # Analog deep dive (search listings via old scheme)
        # =============================
        analog_details = []
        best_analog_offers: list[tuple[str, Optional[LeasingOffer]]] = []
        
        # Build Sonar details lookup
        sonar_lookup = {d["name"]: d for d in sonar_analog_details} if sonar_analog_details else {}
        
        if analogs:
            logger.info("Collecting analog listings (old scheme)...")
            for i, analog in enumerate(analogs[:3]):  # Always max 3 analogs
                # Get Sonar info if available
                sonar_info = sonar_lookup.get(analog, {})
                
                # Search for analog offers (old scheme for listings)
                query_analog = f"{analog} {CONFIG.fallback_search_suffix}"
                analog_offers = search_and_analyze(
                    query_analog,
                    fetcher,
                    analyzer,
                    num_results=3,  # Fewer for analogs
                    use_ai=use_ai
                )
                
                # Find best analog offer
                best_analog_offer = None
                best_analog_analysis = {}
                
                if analog_offers and use_ai and analyzer:
                    logger.info(f"Finding best offer for analog '{analog}'...")
                    best_analog_offer, best_analog_analysis = find_best_offer_from_list(
                        offers=analog_offers,
                        analyzer=analyzer,
                        use_ai=use_ai,
                        item_name=analog,
                        sonar_finder=sonar_finder
                    )
                
                best_analog_offers.append((analog, best_analog_offer))
                
                # Legacy analog details (for compatibility)
                listings = fetch_listing_summaries(f"{analog} ??????", top_n=3)
                price_list = [l["price_guess"] for l in listings if l.get("price_guess")]
                avg_price_math = int(sum(price_list) / len(price_list)) if price_list else None
                
                # Use Sonar info if available, else fallback to GigaChat
                pros, cons, note = [], [], ""
                price_hint = None
                best_link = None

                if sonar_info:
                    # Use Sonar description as note
                    note = sonar_info.get("description", "") or sonar_info.get("key_difference", "")
                    # Parse price range from Sonar
                    price_range_str = sonar_info.get("price_range", "")
                    if price_range_str:
                        note = f"{note} | ??????? ????????: {price_range_str}" if note else f"??????? ????????: {price_range_str}"
                    # Use Sonar pros/cons if available
                    pros = ensure_list_str(sonar_info.get("pros", []))
                    cons = ensure_list_str(sonar_info.get("cons", []))
                elif use_ai and analyzer:
                    ai_review = analyzer.review_analog(analog, listings)
                    pros = ensure_list_str(ai_review.get("pros"))
                    cons = ensure_list_str(ai_review.get("cons"))
                    price_hint = ai_review.get("price_hint")
                    note = ai_review.get("note", "")
                    best_link = ai_review.get("best_link")
                else:
                    # ???? Sonar info ??? - ????????? ?????? ???????? (??? fallback)
                    logger.warning(f"[SONAR] No Sonar info for {analog} - pros/cons will be empty")
                
                final_price = price_hint if price_hint else avg_price_math
                if best_analog_offer and best_analog_offer.price:
                    final_price = best_analog_offer.price

                analog_details.append({
                    "name": analog,
                    "listings": listings,
                    "avg_price_guess": final_price,
                    "ai_price_hint": price_hint,
                    "pros": pros,
                    "cons": cons,
                    "note": note,
                    "best_link": best_link,
                    "best_offer": asdict(best_analog_offer) if best_analog_offer else None,
                    "best_offer_analysis": best_analog_analysis,
                    "sonar_info": sonar_info if sonar_info else None
                })

        report["analogs_details"] = analog_details

        # =============================
        # DEEP ANALYSIS: Compare best original vs EACH analog (with links)
        # =============================
        if best_original_offer and best_analog_offers:
            logger.info("=" * 70)
            logger.info("DEEP ANALYSIS: Comparing best original with each analog...")
            logger.info("=" * 70)
            
            comparisons = {}
            
            # Prepare original offer data
            original_offer_data = {
                "title": best_original_offer.title,
                "price": best_original_offer.price,
                "url": best_original_offer.url
            }
            
            # Use Sonar for comparison if available (compares specific offers with links)
            sonar_comparison_failed = False
            if sonar_finder and sonar_finder.is_available():
                logger.info("[SONAR] Using Sonar to compare offers (with links)...")
                for analog_name, best_analog in best_analog_offers:
                    if best_analog:
                        # Prepare analog offer data
                        analog_offer_data = {
                            "title": best_analog.title,
                            "price": best_analog.price,
                            "url": best_analog.url
                        }
                        
                        logger.info(f"[SONAR] Comparing with {analog_name}:")
                        logger.info(f"  Original: {best_original_offer.title[:60]}... -> {best_original_offer.url}")
                        logger.info(f"  Analog:   {best_analog.title[:60]}... -> {best_analog.url}")
                        
                        try:
                            # Call Sonar to compare specific offers
                            sonar_comparison = sonar_finder.compare_offers(
                                original_name=item,
                                original_offer=original_offer_data,
                                analog_name=analog_name,
                                analog_offer=analog_offer_data
                            )
                            
                            # Проверяем, что Sonar вернул валидный результат
                            if sonar_comparison and sonar_comparison.get("winner") != "unknown":
                                comparisons[analog_name] = {
                                    "winner": sonar_comparison.get("winner", "tie"),
                                    "original_score": 7.0,
                                    "analog_score": 7.0,
                                    "price_comparison": {
                                        "original_price": best_original_offer.price,
                                        "analog_price": best_analog.price,
                                        "price_diff": sonar_comparison.get("price_diff", "")
                                    },
                                    "pros_original": sonar_comparison.get("original_advantages", []),
                                    "cons_original": sonar_comparison.get("original_disadvantages", []),
                                    "pros_analog": sonar_comparison.get("analog_advantages", []),
                                    "cons_analog": sonar_comparison.get("analog_disadvantages", []),
                                    "recommendation": sonar_comparison.get("recommendation", ""),
                                    # ВАЖНО: ссылки на конкретные объявления
                                    "original_url": best_original_offer.url,
                                    "analog_url": best_analog.url,
                                    "original_title": best_original_offer.title,
                                    "analog_title": best_analog.title,
                                    "original_price_formatted": format_price(best_original_offer.price),
                                    "analog_price_formatted": format_price(best_analog.price),
                                    "sonar_comparison": True
                                }
                                
                                logger.info(f"[SONAR] Winner: {sonar_comparison.get('winner', 'tie')}")
                            else:
                                logger.warning(f"[SONAR] Comparison failed for {analog_name}, will use fallback")
                                sonar_comparison_failed = True
                        except Exception as e:
                            logger.error(f"[SONAR] Exception during comparison with {analog_name}: {e}")
                            sonar_comparison_failed = True
            
            # Fallback to GigaChat comparison if Sonar failed or not available
            if (not sonar_finder or not sonar_finder.is_available() or sonar_comparison_failed or len(comparisons) == 0) and use_ai and analyzer:
                if sonar_comparison_failed:
                    logger.warning("[FALLBACK] Sonar comparison failed, falling back to GigaChat...")
                else:
                    logger.info("[FALLBACK] Using GigaChat for comparison...")
                
                # Получаем сравнения через GigaChat для всех аналогов
                gigachat_comparisons = compare_best_offers_original_vs_analogs(
                    best_original=best_original_offer,
                    best_analogs=best_analog_offers,
                    original_name=item,
                    analyzer=analyzer,
                    use_ai=use_ai
                )
                
                # Объединяем результаты: используем Sonar где получилось, GigaChat где нет
                for analog_name, best_analog in best_analog_offers:
                    if analog_name not in comparisons and analog_name in gigachat_comparisons:
                        comparisons[analog_name] = gigachat_comparisons[analog_name]
                        comparisons[analog_name]["sonar_comparison"] = False
            
            report["best_offers_comparison"] = comparisons
        
        return offers, report, analog_details

    finally:
        fetcher.close()


# =============================
# CLI
# =============================
def main():
    """Main CLI entry point."""
    try:
        params = get_user_input()
    except ValueError as e:
        logger.error(f"Invalid input: {e}")
        return

    offers, report, analog_details = run_pipeline(params)
    
    if not offers:
        return

    print_results(offers)
    print_final_report(report, params["client_price"], analog_details)

    save_input = input("\nSave results to JSON? (y/n): ").strip().lower()
    if save_input == "y":
        save_results_json(offers, params["item"], market_report=report)


# =============================
# Entry point for API
# =============================
def run_analysis(
    item: str,
    client_price: int | None = None,
    use_ai: bool = True,
    num_results: int = 5,
) -> dict:
    """
    API entry point for running analysis programmatically.
    
    Args:
        item: Item to analyze (e.g., "BMW M5 2024")
        client_price: Optional client's expected price
        use_ai: Whether to use AI analysis
        num_results: Number of search results to process
    
    Returns:
        Dictionary with analysis results
    """
    params: UserInput = {
        "item": item,
        "client_price": client_price,
        "use_ai": use_ai,
        "num_results": num_results,
    }
    
    offers, report, analog_details = run_pipeline(params)
    
    return {
        "item": item,
        "offers_used": [asdict(o) for o in offers],
        "analogs_suggested": report.get("analogs_suggested", []),
        "analogs_details": analog_details,
        "market_report": report,
    }


if __name__ == "__main__":
    import sys
    
    # Check if running as web server or CLI
    if len(sys.argv) > 1 and sys.argv[1] == "--web":
        # Run as web server
        try:
            import uvicorn
            import os
            import sys as sys_module
            
            # Add parent directory to path for imports
            current_dir = os.path.dirname(os.path.abspath(__file__))
            if current_dir not in sys_module.path:
                sys_module.path.insert(0, current_dir)
            
            print("=" * 70)
            print("🚀 Starting Leasing Analyzer Web Server")
            print("=" * 70)
            print("📱 Open http://localhost:8000 in your browser")
            print("=" * 70)
            print("Press Ctrl+C to stop")
            print("=" * 70)
            
            # Run from root directory, import will work
            uvicorn.run("api.main:app", host="0.0.0.0", port=8000, reload=True)
        except ImportError as e:
            print("❌ Error: uvicorn not installed. Run: pip install uvicorn")
            print(f"   Details: {e}")
            sys.exit(1)
    else:
        # Run as CLI
        main()
