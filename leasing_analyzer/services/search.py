from __future__ import annotations

import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from typing import Optional
from urllib.parse import urlparse

from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from tqdm import tqdm

from leasing_analyzer.clients.ai_analyzer import AIAnalyzer
from leasing_analyzer.core.config import CONFIG
from leasing_analyzer.core.logging import get_logger
from leasing_analyzer.core.models import LeasingOffer, SearchResult
from leasing_analyzer.core.rate_limit import google_rate_limiter
from leasing_analyzer.core.sessions import get_http_session
from leasing_analyzer.core.utils import (
    is_valid_url,
    extract_query_constraints,
    extract_year_from_text,
)

from leasing_analyzer.parsing.base import (
    ParserStrategy,
    AvitoParserStrategy,
    GenericParserStrategy,
)
from leasing_analyzer.parsing.helpers import deduplicate_offers

from leasing_analyzer.services.fetcher import SeleniumFetcher
from leasing_analyzer.services.market import (
    filter_low_quality_offers,
    filter_price_outliers,
)


logger = get_logger(__name__)
_requests_session = get_http_session()


def extract_model_from_query(query: str) -> str:
    """Извлекает название модели из произвольного поискового запроса."""
    parts = (query or "").split()
    return " ".join(parts[:2]) if parts else ""


def filter_offers_by_requested_year(
    offers: list[LeasingOffer],
    requested_year: Optional[int],
) -> list[LeasingOffer]:
    """Отдает приоритет предложениям нужного года, не отбрасывая все результаты."""
    if requested_year is None:
        return offers

    filtered = [offer for offer in offers if offer.year == requested_year]
    return filtered if filtered else offers





@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type(requests.RequestException)
)
def _search_google_request(query: str, num_results: int) -> list[SearchResult]:
    """Выполняет поисковый запрос к Serper API с повторами и лимитированием."""
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
    """Кешированный поиск Google через Serper API."""
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
    """Поиск Google через Serper API с возвратом списка для совместимости."""
    if not CONFIG.serper_api_key:
        logger.warning("SERPER_API_KEY not set, skipping Google search")
        return []
    
    results = search_google_cached(query, num_results)
    if not results:
        logger.debug(f"No Google results for query: {query}")
    return list(results)

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
    """Генерирует URL для обязательных лизинговых источников."""
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
    """Фильтрует поисковые результаты, удаляя заблокированные домены."""
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
    """Объединяет результаты поиска с обязательными источниками."""
    existing_domains = {urlparse(r.get("link", "")).netloc.replace("www.", "") for r in search_results}
    merged = []
    
    for m in mandatory:
        domain = m.get("source_name", "")
        if domain not in existing_domains:
            merged.append(m)
            existing_domains.add(domain)
    merged.extend(search_results)
    return merged

def _process_single_url(
    result: dict,
    model_name: str,
    fetcher: SeleniumFetcher,
    parser: ParserStrategy,
    idx: int,
    total: int
) -> list[LeasingOffer]:
    """Обрабатывает один URL и возвращает найденные предложения."""
    url = result.get("link", "")
    title = result.get("title", "")
    
    if not is_valid_url(url):
        logger.debug(f"[{idx}/{total}] Invalid URL: {url}")
        return []
    
    domain = urlparse(url).netloc.replace("www.", "")
    logger.debug(f"[{idx}/{total}] Processing {domain} | {url}")
    
    # Загружаем страницу
    is_avito = CONFIG.avito_domain in domain
    scroll_times = CONFIG.avito_scroll_times if is_avito else CONFIG.other_scroll_times
    html = fetcher.fetch_page(url, scroll_times=scroll_times, wait=CONFIG.scroll_wait)
    
    if not html:
        logger.debug(f"[{idx}/{total}] Failed to load {url}")
        return []
    
    # Разбираем страницу выбранной стратегией
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
    item_name: Optional[str] = None,
) -> list[LeasingOffer]:
    """Основной поисковый пайплайн с параллельной обработкой."""
    logger.info("=" * 70)
    logger.info(f"Search query: {query}")
    logger.info("=" * 70)

    if item_name:
        model_name, requested_year = extract_query_constraints(item_name)
    else:
        model_name = extract_model_from_query(query)
        requested_year = extract_year_from_text(query)

    mandatory_query_name = model_name
    if model_name and requested_year is not None:
        mandatory_query_name = f"{model_name} {requested_year}"

    mandatory_urls = generate_mandatory_urls(mandatory_query_name or model_name)
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

    # Создаем стратегии парсинга
    avito_parser = AvitoParserStrategy()
    generic_parser = GenericParserStrategy(analyzer, use_ai)

    offers: list[LeasingOffer] = []
    
    # Обрабатываем URL параллельно
    with ThreadPoolExecutor(max_workers=CONFIG.max_workers) as executor:
        futures = {}
        
        for idx, result in enumerate(all_results, 1):
            url = result.get("link", "")
            domain = urlparse(url).netloc.replace("www.", "")
            is_avito = CONFIG.avito_domain in domain
            
            # Выбираем стратегию парсинга
            parser = avito_parser if is_avito else generic_parser
            
            # Отправляем задачу в пул
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
        
        # Собираем результаты с прогресс-баром
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

    # Удаляем дубликаты и отфильтровываем выбросы
    # Порядок фильтров: качество -> дедупликация -> год -> выбросы
    offers = filter_low_quality_offers(offers)
    offers = deduplicate_offers(offers)
    offers = filter_offers_by_requested_year(offers, requested_year)
    offers = filter_price_outliers(offers)
    
    logger.info(f"Total offers after processing: {len(offers)}")
    return offers
