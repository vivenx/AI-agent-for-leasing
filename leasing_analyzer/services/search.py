from __future__ import annotations

import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from typing import Optional
from urllib.parse import urlparse

from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential
from tqdm import tqdm

from leasing_analyzer.clients.ai_analyzer import AIAnalyzer
from leasing_analyzer.core.config import CONFIG
from leasing_analyzer.core.logging import get_logger
from leasing_analyzer.core.models import LeasingOffer, SearchResult
from leasing_analyzer.core.rate_limit import google_rate_limiter
from leasing_analyzer.core.sessions import get_http_session
from leasing_analyzer.core.utils import (
    extract_query_constraints,
    extract_year_from_text,
    is_valid_url,
)
from leasing_analyzer.parsing.base import (
    AvitoParserStrategy,
    GenericParserStrategy,
    ParserStrategy,
)
from leasing_analyzer.parsing.helpers import deduplicate_offers
from leasing_analyzer.services.fetcher import SeleniumFetcher
from leasing_analyzer.services.market import (
    filter_low_quality_offers,
    filter_price_outliers,
)


logger = get_logger(__name__)
_requests_session = get_http_session()
_STATUS_CHECK_TIMEOUT = 8
_BROWSER_REACHABLE_STATUSES = {401, 403, 405, 406, 407, 408, 409, 425, 429}
_DEAD_STATUSES = {404, 410, 451, 500, 501, 502, 503, 504, 521, 522, 523, 524}


def _get_url_status(url: str) -> Optional[int]:
    """Возвращает итоговый HTTP-статус URL или None, если URL недоступен."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        )
    }

    try:
        response = _requests_session.head(
            url,
            allow_redirects=True,
            timeout=_STATUS_CHECK_TIMEOUT,
            headers=headers,
        )
        if response.status_code in {403, 405, 406, 429}:
            logger.debug(f"[URL_CHECK] HEAD returned {response.status_code}, retrying with GET: {url}")
            response = _requests_session.get(
                url,
                allow_redirects=True,
                timeout=_STATUS_CHECK_TIMEOUT,
                headers=headers,
                stream=True,
            )
        logger.info(f"[URL_CHECK] status={response.status_code} url={url}")
        return response.status_code
    except requests.RequestException as exc:
        logger.warning(f"[URL_CHECK] failed url={url} error={exc}")
        return None


def _is_status_browser_reachable(status_code: Optional[int]) -> bool:
    """Оценивает, есть ли смысл пробовать открыть URL в Selenium."""
    if status_code is None:
        return False
    if 200 <= status_code < 400:
        return True
    if status_code in _BROWSER_REACHABLE_STATUSES:
        return True
    if status_code in _DEAD_STATUSES:
        return False
    return False


def is_url_available(url: str) -> bool:
    """Проверяет, что URL не выглядит явно мертвым до открытия в браузере."""
    if not is_valid_url(url):
        return False

    status_code = _get_url_status(url)
    if not _is_status_browser_reachable(status_code):
        logger.debug(f"[URL_CHECK] rejected status={status_code} url={url}")
        return False
    return True


def filter_available_results(results: list[dict]) -> list[dict]:
    """Оставляет только результаты, которые не выглядят заведомо недоступными."""
    if not results:
        return []

    available_with_index: list[tuple[int, dict]] = []
    max_workers = min(max(1, len(results)), CONFIG.max_workers)
    logger.info(f"[URL_CHECK] batch_start total={len(results)}")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_get_url_status, result.get("link", "")): (idx, result)
            for idx, result in enumerate(results)
        }
        for future in as_completed(futures):
            idx, result = futures[future]
            url = result.get("link", "")
            try:
                status_code = future.result()
                if _is_status_browser_reachable(status_code):
                    enriched_result = dict(result)
                    enriched_result["http_status"] = status_code
                    available_with_index.append((idx, enriched_result))
                    logger.info(f"[URL_CHECK] accepted status={status_code} url={url}")
                else:
                    logger.info(
                        f"[URL_CHECK] filtered status="
                        f"{status_code if status_code is not None else 'unreachable'} url={url}"
                    )
            except Exception as exc:
                logger.warning(f"[URL_CHECK] crashed url={url} error={exc}")

    available_with_index.sort(key=lambda item: item[0])
    available_results = [result for _, result in available_with_index]
    logger.info(
        f"[URL_CHECK] batch_done reachable={len(available_results)} total={len(results)}"
    )
    return available_results


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
    retry=retry_if_exception_type(requests.RequestException),
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
        mandatory.append(
            {
                "link": url,
                "title": f"{model_name} - {source['name']}",
                "is_mandatory": True,
                "source_name": source["name"],
            }
        )
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
    total: int,
) -> list[LeasingOffer]:
    """Обрабатывает один URL и возвращает найденные предложения."""
    url = result.get("link", "")
    title = result.get("title", "")

    if not is_valid_url(url):
        logger.debug(f"[{idx}/{total}] Invalid URL: {url}")
        return []

    if result.get("http_status") is None and not is_url_available(url):
        logger.debug(f"[{idx}/{total}] [URL_CHECK] unavailable_before_fetch url={url}")
        return []

    domain = urlparse(url).netloc.replace("www.", "")
    logger.debug(f"[{idx}/{total}] Processing {domain} | {url}")

    is_avito = CONFIG.avito_domain in domain
    scroll_times = CONFIG.avito_scroll_times if is_avito else CONFIG.other_scroll_times
    html = fetcher.fetch_page(url, scroll_times=scroll_times, wait=CONFIG.scroll_wait)

    if not html:
        logger.warning(f"[{idx}/{total}] [URL_CHECK] selenium_load_failed url={url}")
        return []

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
    logger.info(f"Total URLs before availability check: {len(all_results)}")

    if not all_results:
        logger.warning("No URLs to process")
        return []

    all_results = filter_available_results(all_results)
    logger.info(f"Total URLs after availability check: {len(all_results)}")

    if not all_results:
        logger.warning("No reachable URLs to process after availability filtering")
        return []

    avito_parser = AvitoParserStrategy()
    generic_parser = GenericParserStrategy(analyzer, use_ai)

    offers: list[LeasingOffer] = []

    with ThreadPoolExecutor(max_workers=CONFIG.max_workers) as executor:
        futures = {}

        for idx, result in enumerate(all_results, 1):
            url = result.get("link", "")
            domain = urlparse(url).netloc.replace("www.", "")
            is_avito = CONFIG.avito_domain in domain

            parser = avito_parser if is_avito else generic_parser

            future = executor.submit(
                _process_single_url,
                result,
                model_name,
                fetcher,
                parser,
                idx,
                len(all_results),
            )
            futures[future] = (idx, url)

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

    offers = filter_low_quality_offers(offers)
    offers = deduplicate_offers(offers)
    offers = filter_offers_by_requested_year(offers, requested_year)
    offers = filter_price_outliers(offers)

    logger.info(f"Total offers after processing: {len(offers)}")
    return offers
