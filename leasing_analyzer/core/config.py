from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv


_REPO_ROOT = Path(__file__).resolve().parents[2]
_ENV_PATH = _REPO_ROOT / ".env"

# Загружаем .env из корня репозитория до чтения любых полей конфига из окружения.
load_dotenv(_ENV_PATH if _ENV_PATH.exists() else None)


@dataclass(frozen=True)
class Config:
    """Конфигурация приложения с разумными значениями по умолчанию."""

    # API-ключи
    serper_api_key: Optional[str] = field(default_factory=lambda: os.getenv("SERPER_API_KEY"))
    perplexity_api_key: Optional[str] = field(default_factory=lambda: os.getenv("PERPLEXITY_API_KEY"))
    gigachat_auth_data: Optional[str] = field(default_factory=lambda: os.getenv("GIGACHAT_AUTH_DATA"))

    # Настройки Sonar / Perplexity
    sonar_base_url: Optional[str] = field(default_factory=lambda: os.getenv("PERPLEXITY_BASE_URL"))
    sonar_api_url: str = "https://api.perplexity.ai/chat/completions"
    sonar_model: str = field(default_factory=lambda: os.getenv("PERPLEXITY_MODEL", "sonar-reasoning-pro"))
    sonar_max_analogs: int = 3

    # Настройки HTTP
    http_timeout: int = 25
    http_long_timeout: int = 60

    # Настройки Selenium
    scroll_wait: float = 1.5
    default_scroll_times: int = 2
    avito_scroll_times: int = 2
    other_scroll_times: int = 3
    page_load_timeout: int = 45
    implicit_wait: int = 10
    script_timeout: int = 30

    # Обработка контента
    max_content_length: int = 10000

    # Рыночный анализ
    price_deviation_tolerance: float = 0.20
    min_valid_price: int = 100
    min_large_price: int = 10000
    outlier_min_samples: int = 5
    iqr_multiplier: float = 1.5

    # Настройки GigaChat
    gigachat_model: str = "GigaChat-2"
    gigachat_oauth_url: str = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
    gigachat_api_url: str = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"
    gigachat_request_timeout: int = field(default_factory=lambda: int(os.getenv("GIGACHAT_REQUEST_TIMEOUT", "45")))
    gigachat_request_attempts: int = field(default_factory=lambda: int(os.getenv("GIGACHAT_REQUEST_ATTEMPTS", "2")))
    gigachat_retry_base_delay: float = field(default_factory=lambda: float(os.getenv("GIGACHAT_RETRY_BASE_DELAY", "3.0")))

    # Настройки поиска
    default_num_results: int = 5
    max_analogs: int = 5
    min_analogs_before_ai: int = 3

    # Настройки доменов
    avito_domain: str = "avito.ru"
    default_search_suffix: str = "лизинг"
    fallback_search_suffix: str = "купить"

    # Параллельная обработка
    max_workers: int = 3

    # Лимиты запросов
    google_rate_limit_calls: int = 10
    google_rate_limit_period: float = 60.0
    gigachat_rate_limit_calls: int = 15
    gigachat_rate_limit_period: float = 60.0
    gigachat_min_delay: float = 0.5
    sonar_rate_limit_calls: int = field(default_factory=lambda: int(os.getenv("SONAR_RATE_LIMIT_CALLS", "10")))
    sonar_rate_limit_period: float = field(default_factory=lambda: float(os.getenv("SONAR_RATE_LIMIT_PERIOD", "60.0")))
    sonar_min_delay: float = field(
        default_factory=lambda: float(os.getenv("SONAR_MIN_DELAY", os.getenv("PERPLEXITY_MIN_DELAY", "3.0")))
    )
    sonar_retry_after_default: float = field(
        default_factory=lambda: float(os.getenv("SONAR_RETRY_AFTER_DEFAULT", "15.0"))
    )

    # Настройки памяти
    memory_enabled: bool = field(default_factory=lambda: os.getenv("MEMORY_ENABLED", "true").lower() == "true")
    memory_db_path: str = field(
        default_factory=lambda: os.getenv("MEMORY_DB_PATH", str(_REPO_ROOT / "data" / "agent_memory.sqlite"))
    )
    memory_recent_limit: int = 5
    memory_related_limit: int = 5
    memory_dataset_limit: int = 10
    memory_summary_history_limit: int = 10
    memory_summary_max_chars: int = 4000

    # Курсы валют к RUB
    exchange_rates: dict[str, float] = field(
        default_factory=lambda: {
            "USD": 100.0,
            "EUR": 110.0,
            "RUB": 1.0,
        }
    )


CONFIG = Config()
