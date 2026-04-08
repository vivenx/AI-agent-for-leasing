from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv


_REPO_ROOT = Path(__file__).resolve().parents[2]
_ENV_PATH = _REPO_ROOT / ".env"

# Load repository-level .env before reading any environment-backed config fields.
load_dotenv(_ENV_PATH if _ENV_PATH.exists() else None)


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
