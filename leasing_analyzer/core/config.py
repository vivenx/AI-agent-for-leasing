from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv


_REPO_ROOT = Path(__file__).resolve().parents[2]
_ENV_PATH = _REPO_ROOT / ".env"

# Р—Р°РіСЂСѓР¶Р°РµРј .env РёР· РєРѕСЂРЅСЏ СЂРµРїРѕР·РёС‚РѕСЂРёСЏ РґРѕ С‡С‚РµРЅРёСЏ Р»СЋР±С‹С… РїРѕР»РµР№ РєРѕРЅС„РёРіР° РёР· РѕРєСЂСѓР¶РµРЅРёСЏ.
load_dotenv(_ENV_PATH if _ENV_PATH.exists() else None)


@dataclass(frozen=True)
class Config:
    """РљРѕРЅС„РёРіСѓСЂР°С†РёСЏ РїСЂРёР»РѕР¶РµРЅРёСЏ СЃ СЂР°Р·СѓРјРЅС‹РјРё Р·РЅР°С‡РµРЅРёСЏРјРё РїРѕ СѓРјРѕР»С‡Р°РЅРёСЋ."""
    
    # API-РєР»СЋС‡Рё (Р·Р°РіСЂСѓР¶Р°СЋС‚СЃСЏ РёР· РѕРєСЂСѓР¶РµРЅРёСЏ)
    serper_api_key: Optional[str] = field(default_factory=lambda: os.getenv("SERPER_API_KEY"))
    perplexity_api_key: Optional[str] = field(default_factory=lambda: os.getenv("PERPLEXITY_API_KEY"))

    # РќР°СЃС‚СЂРѕР№РєРё API Sonar (Perplexity)
    sonar_base_url: Optional[str] = field(default_factory=lambda: os.getenv("PERPLEXITY_BASE_URL"))
    sonar_api_url: str = "https://api.perplexity.ai/chat/completions"
    sonar_model: str = field(default_factory=lambda: os.getenv("PERPLEXITY_MODEL", "sonar-reasoning-pro"))

    gigachat_auth_data: Optional[str] = field(default_factory=lambda: os.getenv("GIGACHAT_AUTH_DATA"))
    
    
    # РќР°СЃС‚СЂРѕР№РєРё HTTP
    http_timeout: int = 25
    http_long_timeout: int = 60  # РЈРІРµР»РёС‡РµРЅРѕ РґР»СЏ СЃС‚Р°Р±РёР»СЊРЅРѕСЃС‚Рё Sonar
    
    # РќР°СЃС‚СЂРѕР№РєРё Selenium
    scroll_wait: float = 1.5
    default_scroll_times: int = 2
    avito_scroll_times: int = 2
    other_scroll_times: int = 3
    
    # РћР±СЂР°Р±РѕС‚РєР° РєРѕРЅС‚РµРЅС‚Р°
    max_content_length: int = 10000
    
    # Р С‹РЅРѕС‡РЅС‹Р№ Р°РЅР°Р»РёР·
    price_deviation_tolerance: float = 0.20
    min_valid_price: int = 100
    min_large_price: int = 10000
    outlier_min_samples: int = 5
    iqr_multiplier: float = 1.5
    
    # РќР°СЃС‚СЂРѕР№РєРё GigaChat
    gigachat_model: str = "GigaChat-2"
    gigachat_oauth_url: str = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
    gigachat_api_url: str = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"
    
    # РќР°СЃС‚СЂРѕР№РєРё Perplexity Sonar
    # РџРѕРґРґРµСЂР¶РєР° РєР°Рє РїСЂСЏРјРѕРіРѕ API, С‚Р°Рє Рё РїСЂРѕРєСЃРё С‡РµСЂРµР· artemox.com
    sonar_base_url: Optional[str] = field(default_factory=lambda: os.getenv("PERPLEXITY_BASE_URL"))
    sonar_api_url: str = "https://api.perplexity.ai/chat/completions"  # Р‘СѓРґРµС‚ РїРµСЂРµРѕРїСЂРµРґРµР»РµРЅРѕ РµСЃР»Рё СѓРєР°Р·Р°РЅ base_url
    sonar_model: str = "sonar-reasoning-pro"  # РњРёРЅРёРјР°Р»СЊРЅС‹Рµ С‚РѕРєРµРЅС‹ (sonar-reasoning-pro РґР»СЏ РїСЂРѕРєСЃРё)
    sonar_max_analogs: int = 3  # Р’СЃРµРіРґР° 3 Р°РЅР°Р»РѕРіР°
    
    # РќР°СЃС‚СЂРѕР№РєРё РїРѕРёСЃРєР°
    default_num_results: int = 5
    max_analogs: int = 5
    min_analogs_before_ai: int = 3
    
    # РќР°СЃС‚СЂРѕР№РєРё РґРѕРјРµРЅРѕРІ
    avito_domain: str = "avito.ru"
    default_search_suffix: str = "Р»РёР·РёРЅРі"
    fallback_search_suffix: str = "РєСѓРїРёС‚СЊ"
    
    # РўР°Р№РјР°СѓС‚С‹ Selenium
    page_load_timeout: int = 45  # Increased for slow pages
    implicit_wait: int = 10
    script_timeout: int = 30  # Timeout for JavaScript execution
    
    # РџР°СЂР°Р»Р»РµР»СЊРЅР°СЏ РѕР±СЂР°Р±РѕС‚РєР°
    max_workers: int = 3
    
    # РћРіСЂР°РЅРёС‡РµРЅРёРµ С‡Р°СЃС‚РѕС‚С‹ Р·Р°РїСЂРѕСЃРѕРІ (Р±РѕР»РµРµ РєРѕРЅСЃРµСЂРІР°С‚РёРІРЅРѕ, С‡С‚РѕР±С‹ РёР·Р±РµРіР°С‚СЊ 429)
    google_rate_limit_calls: int = 10
    google_rate_limit_period: float = 60.0
    gigachat_rate_limit_calls: int = 15  # Reduced from 20
    gigachat_rate_limit_period: float = 60.0
    gigachat_min_delay: float = 0.5  # Minimum delay between requests (seconds)
    sonar_rate_limit_calls: int = 10
    sonar_rate_limit_period: float = 60.0
    sonar_min_delay: float = 0.3

    # РќР°СЃС‚СЂРѕР№РєРё РїР°РјСЏС‚Рё
    memory_enabled: bool = field(default_factory=lambda: os.getenv("MEMORY_ENABLED", "true").lower() == "true")
    memory_db_path: str = field(default_factory=lambda: os.getenv("MEMORY_DB_PATH", str(_REPO_ROOT / "data" / "agent_memory.sqlite")))
    memory_recent_limit: int = 5
    memory_related_limit: int = 5
    memory_dataset_limit: int = 10
    memory_summary_history_limit: int = 10
    memory_summary_max_chars: int = 4000
    
    # РљСѓСЂСЃС‹ РІР°Р»СЋС‚ (Рє RUB)
    exchange_rates: dict[str, float] = field(default_factory=lambda: {
        "USD": 100.0,
        "EUR": 110.0,
        "RUB": 1.0,
    })


# Р“Р»РѕР±Р°Р»СЊРЅС‹Р№ СЌРєР·РµРјРїР»СЏСЂ РєРѕРЅС„РёРіСѓСЂР°С†РёРё
CONFIG = Config()
