from __future__ import annotations

import requests
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from typing import Optional
from urllib.parse import quote_plus, unquote, urlparse

from bs4 import BeautifulSoup
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential
from tqdm import tqdm

from leasing_analyzer.clients.ai_analyzer import AIAnalyzer
from leasing_analyzer.core.audit import AgentAuditTrail
from leasing_analyzer.core.config import CONFIG
from leasing_analyzer.core.logging import get_logger
from leasing_analyzer.core.models import LeasingOffer, SearchResult
from leasing_analyzer.core.rate_limit import google_rate_limiter
from leasing_analyzer.core.sessions import get_http_session
from leasing_analyzer.core.utils import (
    clean_search_query,
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
from leasing_analyzer.services.user_sources import (
    STATUS_ERROR,
    STATUS_INSUFFICIENT_DATA,
    STATUS_SUCCESS,
    UserSource,
    validate_user_source_url,
)


logger = get_logger(__name__)
_requests_session = get_http_session()
_STATUS_CHECK_TIMEOUT = 8
_BROWSER_REACHABLE_STATUSES = {401, 403, 405, 406, 407, 408, 409, 425, 429, 503}
_DEAD_STATUSES = {404, 410, 451, 500, 501, 502, 504, 521, 522, 523, 524}
_WIKIPEDIA_DOMAINS = {"wikipedia.org", "wikimedia.org", "wikiwand.com"}
_IRRELEVANT_ROUTE_MARKERS = {
    "article": (
        "/article",
        "/articles",
        "/blog",
        "/blogs",
        "/journal",
        "/publication",
        "/publications",
        "/stati",
        "/statya",
        "/статьи",
        "/статья",
        "/блог",
    ),
    "news": (
        "/news",
        "/novosti",
        "/novost",
        "/press",
        "/press-release",
        "/новости",
        "/новость",
        "/пресс",
    ),
    "forum": (
        "/forum",
        "/forums",
        "/thread",
        "/threads",
        "/topic",
        "/topics",
        "/community",
        "/discussion",
        "/discussions",
        "/форум",
        "/тема",
        "/обсуждение",
    ),
    "spare_parts": (
        "/parts",
        "/spare",
        "/spares",
        "/spare-parts",
        "/zapchasti",
        "/zapchast",
        "/autoparts",
        "/auto-parts",
        "/partscatalog",
        "/parts-catalog",
        "/запчаст",
        "/детал",
    ),
}
_IRRELEVANT_DOMAIN_MARKERS = {
    "article": ("blog.", "journal."),
    "news": ("news.", "novosti."),
    "forum": ("forum.", "forums.", "community."),
    "spare_parts": ("zapchasti.", "parts.", "autoparts."),
}
_IRRELEVANT_TEXT_PATTERNS = {
    "article": (
        r"(?<!\w)стать(?:я|и|ю|е|ей)(?!\w)",
        r"(?<!\w)articles?(?!\w)",
        r"(?<!\w)blogs?(?!\w)",
    ),
    "news": (
        r"(?<!\w)новост(?:ь|и|ей|ям|ями|ях)(?!\w)",
        r"(?<!\w)news(?!\w)",
        r"(?<!\w)press release(?!\w)",
    ),
    "forum": (
        r"(?<!\w)форум(?:ы|е|ов|ам|ами|ах)?(?!\w)",
        r"(?<!\w)обсуждени(?:е|я|й|ю|ем|ях)(?!\w)",
        r"(?<!\w)forums?(?!\w)",
        r"(?<!\w)threads?(?!\w)",
    ),
    "spare_parts": (
        r"(?<!\w)(?:авто)?запчаст\w*(?!\w)",
        r"(?<!\w)spare parts?(?!\w)",
        r"(?<!\w)parts catalog(?!\w)",
        r"(?<!\w)parts for(?!\w)",
        r"(?<!\w)детали для(?!\w)",
    ),
}


def _normalize_search_result_text(result: dict) -> str:
    parts = [
        result.get("title", ""),
        result.get("snippet", ""),
        result.get("displayedLink", ""),
        result.get("source_name", ""),
    ]
    return f" {' '.join(str(part) for part in parts if part).lower()} "


def _domain_matches(domain: str, blocked_domains: set[str]) -> bool:
    return any(domain == blocked or domain.endswith(f".{blocked}") for blocked in blocked_domains)


def get_irrelevant_page_reason(result: dict) -> Optional[str]:
    """Returns why a search result should not be fetched or parsed."""
    url = result.get("link", "")
    if not url:
        return "empty_url"

    parsed = urlparse(url)
    domain = parsed.netloc.lower().removeprefix("www.")
    path = unquote(parsed.path or "").lower()
    query = unquote(parsed.query or "").lower()
    route = f"{path}?{query}" if query else path
    text = _normalize_search_result_text(result)
    file_format = str(result.get("fileFormat", "")).lower()

    if _domain_matches(domain, _WIKIPEDIA_DOMAINS):
        return "wikipedia"

    if "pdf" in file_format or path.endswith(".pdf") or ".pdf" in route or "[pdf]" in text:
        return "pdf"

    for reason, markers in _IRRELEVANT_DOMAIN_MARKERS.items():
        if any(marker in domain for marker in markers):
            return reason

    for reason, markers in _IRRELEVANT_ROUTE_MARKERS.items():
        if any(marker in route for marker in markers):
            return reason

    for reason, patterns in _IRRELEVANT_TEXT_PATTERNS.items():
        if any(re.search(pattern, text) for pattern in patterns):
            return reason

    return None


def filter_irrelevant_results(results: list[dict]) -> list[dict]:
    """Drops pages that are not useful for extracting market offers."""
    filtered = []
    for result in results:
        url = result.get("link", "")
        reason = get_irrelevant_page_reason(result)
        if reason:
            logger.info(f"[PAGE_FILTER] отфильтровано reason={reason} url={url}")
            continue
        filtered.append(result)
    return filtered


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
            logger.debug(f"[URL_CHECK] HEAD вернул {response.status_code}, повторяем с GET: {url}")
            response = _requests_session.get(
                url,
                allow_redirects=True,
                timeout=_STATUS_CHECK_TIMEOUT,
                headers=headers,
                stream=True,
            )
        logger.info(f"[URL_CHECK] статус={response.status_code} url={url}")
        return response.status_code
    except requests.RequestException as exc:
        logger.warning(f"[URL_CHECK] ошибка url={url} error={exc}")
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
        logger.debug(f"[URL_CHECK] отклонено статус={status_code} url={url}")
        return False
    return True


def filter_available_results(results: list[dict]) -> list[dict]:
    """Оставляет только результаты, которые не выглядят заведомо недоступными."""
    if not results:
        return []

    available_with_index: list[tuple[int, dict]] = []
    max_workers = min(max(1, len(results)), CONFIG.max_workers)
    logger.info(f"[URL_CHECK] начало обработки пакета, всего={len(results)}")

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
                    logger.info(f"[URL_CHECK] принято статус={status_code} url={url}")
                else:
                    logger.info(
                        f"[URL_CHECK] отфильтровано статус="
                        f"{status_code if status_code is not None else 'недоступен'} url={url}"
                    )
            except Exception as exc:
                logger.warning(f"[URL_CHECK] сбой url={url} error={exc}")

    available_with_index.sort(key=lambda item: item[0])
    available_results = [result for _, result in available_with_index]
    logger.info(
        f"[URL_CHECK] пакет обработан, доступно={len(available_results)} всего={len(results)}"
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
        logger.warning("SERPER_API_KEY не задан")
        return tuple()
    try:
        results = _search_google_request(query, num_results)
        return tuple(results)
    except requests.RequestException as exc:
        logger.error(f"Ошибка поиска: {exc}")
        return tuple()


def search_google(
    query: str,
    num_results: int = 10,
    reject_noisy_markers: bool = True,
    clean: bool = True,
) -> list[dict]:
    """Поиск Google через Serper API с возвратом списка для совместимости."""
    if clean:
        cleaned_query = clean_search_query(query, reject_noisy_markers=reject_noisy_markers)
        if not cleaned_query:
            logger.warning(f"Пропуск некорректного поискового запроса: {query!r}")
            return []
        if cleaned_query != query:
            logger.info(f"Очищенный поисковый запрос: {query!r} -> {cleaned_query!r}")
    else:
        cleaned_query = query

    if not CONFIG.serper_api_key:
        logger.warning("SERPER_API_KEY не задан, пропускаем поиск в Google")
        return []

    results = search_google_cached(cleaned_query, num_results)
    if not results:
        logger.debug(f"Нет результатов Google для запроса: {cleaned_query}")
    return list(results)


MANDATORY_DOMAINS = [
    "alfaleasing.ru",
    "sberleasing.ru",
    "avito.ru",
]


def generate_mandatory_urls(model_name: str) -> list[dict]:
    """Ищет конкретные объявления на обязательных площадках через Google."""
    model_name = clean_search_query(model_name, max_words=8, max_length=80)
    if not model_name:
        return []

    mandatory = []
    for domain in MANDATORY_DOMAINS:
        query = f"site:{domain} {model_name} лизинг"
        results = search_google(query, num_results=3, reject_noisy_markers=False, clean=False)
        for r in results:
            r["is_mandatory"] = True
            r["source_name"] = domain
        mandatory.extend(results)
    return mandatory


def _is_noisy_search_result(result: dict, model_name: str = "") -> bool:
    url = result.get("link", "")
    parsed = urlparse(url)
    path = parsed.path.lower()
    title = (result.get("title") or "").lower()
    domain = parsed.netloc.lower().replace("www.", "")

    noisy_path_parts = (
        "/spec/",
        "/specs/",
        "/characteristics/",
        "/manual",
        "/zapchasti",
        "/parts",
    )
    if any(part in path for part in noisy_path_parts):
        return True

    if domain == "avito.ru":
        if not re.search(r"_\d+/?$", path):
            return True

    if "q=" in parsed.query.lower():
        return True

    path_parts = [p for p in path.strip('/').split('/') if p]
    if path_parts:
        last_part = path_parts[-1]
        model_words = [w.lower() for w in model_name.split() if w]
        
        if last_part in ("katalog", "catalog", "products", "freight-transport", "gruzovye", "kommercheskiy-transport"):
            return True
            
        if model_words and last_part in model_words:
            return True
            
        if not re.search(r"\d", path):
            return True

    noisy_title_markers = (
        "характеристик",
        "технические данные",
        "спецификац",
        "каталог",
        "обзор",
        "руководство",
        "запчаст",
        "manual",
        "specification",
        "technical details",
    )
    return any(marker in title for marker in noisy_title_markers)


def filter_search_results(results: list[dict], model_name: str, max_results: int = 10) -> list[dict]:
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
        reason = get_irrelevant_page_reason(result)
        if reason:
            logger.info(f"[PAGE_FILTER] отфильтровано reason={reason} url={url}")
            continue
        if _is_noisy_search_result(result, model_name):
            logger.debug(f"Пропуск зашумленного результата поиска: {url}")
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
    fallback_parser: Optional[ParserStrategy] = None,
) -> list[LeasingOffer]:
    """Обрабатывает один URL и возвращает найденные предложения."""
    url = result.get("link", "")
    title = result.get("title", "")

    if not is_valid_url(url):
        logger.debug(f"[{idx}/{total}] Некорректный URL: {url}")
        return []

    irrelevant_reason = get_irrelevant_page_reason(result)
    if irrelevant_reason:
        logger.debug(f"[{idx}/{total}] [PAGE_FILTER] пропущено reason={irrelevant_reason} url={url}")
        return []

    if result.get("http_status") is None and not is_url_available(url):
        logger.debug(f"[{idx}/{total}] [URL_CHECK] недоступно до загрузки url={url}")
        return []

    domain = urlparse(url).netloc.replace("www.", "")
    logger.debug(f"[{idx}/{total}] Обработка {domain} | {url}")

    is_avito = CONFIG.avito_domain in domain
    scroll_times = CONFIG.avito_scroll_times if is_avito else CONFIG.other_scroll_times
    html = fetcher.fetch_page(url, scroll_times=scroll_times, wait=CONFIG.scroll_wait)

    if not html:
        logger.warning(f"[{idx}/{total}] [URL_CHECK] ошибка загрузки selenium url={url}")
        return []

    try:
        offers = parser.parse(html, url, model_name, title)
        
        if not offers and fallback_parser:
            logger.debug(f"[{idx}/{total}] Основной парсер не нашел предложений, пробуем fallback_parser")
            offers = fallback_parser.parse(html, url, model_name, title)

        if offers:
            logger.debug(f"[{idx}/{total}] Найдено {len(offers)} предложений на {domain}")
        return offers
    except Exception as e:
        logger.warning(f"[{idx}/{total}] Ошибка при парсинге {url}: {e}")
        return []


def process_user_source_urls(
    user_sources: list[UserSource],
    item_name: str,
    fetcher: SeleniumFetcher,
    analyzer: Optional[AIAnalyzer],
    use_ai: bool = True,
) -> tuple[list[LeasingOffer], list[UserSource]]:
    """Processes user-provided concrete pages through the existing Selenium parser."""
    if not user_sources:
        return [], []

    model_name, requested_year = extract_query_constraints(item_name)
    avito_parser = AvitoParserStrategy()
    generic_parser = GenericParserStrategy(analyzer, use_ai)

    all_offers: list[LeasingOffer] = []
    reports: list[UserSource] = []

    for idx, source in enumerate(user_sources, 1):
        url = source.get("url", "")
        report: UserSource = {
            "id": source.get("id", ""),
            "url": url,
            "status": STATUS_ERROR,
            "reason": "",
            "error_message": None,
            "found_data": {},
            "participated_in_calculation": False,
        }

        is_valid, validation_reason = validate_user_source_url(url, resolve_host=False)
        if not is_valid:
            report["reason"] = validation_reason
            report["error_message"] = validation_reason
            reports.append(report)
            continue

        domain = urlparse(url).netloc.replace("www.", "")
        result = {
            "link": url,
            "title": f"Пользовательский источник: {domain}",
            "snippet": "",
            "source_name": domain,
            "is_user_source": True,
        }
        parser = avito_parser if CONFIG.avito_domain in domain else generic_parser

        try:
            scroll_times = CONFIG.avito_scroll_times if CONFIG.avito_domain in domain else CONFIG.other_scroll_times
            html = fetcher.fetch_page(url, scroll_times=scroll_times, wait=CONFIG.scroll_wait)
            if not html:
                report["reason"] = "Selenium не смог загрузить страницу."
                report["error_message"] = "Пустой HTML или ошибка загрузки страницы."
                reports.append(report)
                continue
            page_title = BeautifulSoup(html, "html.parser").title
            title = page_title.get_text(" ", strip=True) if page_title else result["title"]
            raw_offers = parser.parse(html, url, model_name or item_name, title)
            if not raw_offers and parser is not generic_parser:
                raw_offers = generic_parser.parse(html, url, model_name or item_name, title)
        except Exception as exc:
            report["reason"] = "Источник не удалось обработать."
            report["error_message"] = str(exc)[:300]
            reports.append(report)
            logger.warning("[USER_SOURCE] processing failed url=%s error=%s", url, exc)
            continue

        offers = filter_low_quality_offers(raw_offers)
        offers = filter_offers_by_requested_year(offers, requested_year)
        offers = deduplicate_offers(offers)

        found_data = {
            "offers_count": len(offers),
            "prices_count": sum(1 for offer in offers if offer.price is not None),
            "monthly_payments_count": sum(1 for offer in offers if offer.monthly_payment is not None),
            "offers": [
                {
                    "title": offer.title,
                    "url": offer.url,
                    "price": offer.price,
                    "price_str": offer.price_str,
                    "monthly_payment": offer.monthly_payment,
                    "monthly_payment_str": offer.monthly_payment_str,
                    "year": offer.year,
                    "model": offer.model,
                    "source": offer.source,
                }
                for offer in offers[:5]
            ],
        }

        report["found_data"] = found_data
        if offers:
            all_offers.extend(offers)
            report["status"] = STATUS_SUCCESS
            report["participated_in_calculation"] = True
            report["reason"] = "Источник дал данные для расчёта."
        else:
            report["status"] = STATUS_INSUFFICIENT_DATA
            report["participated_in_calculation"] = False
            report["reason"] = "На странице не удалось найти достаточно данных для расчёта."

        reports.append(report)

    return all_offers, reports


def search_and_analyze(
    query: str,
    fetcher: SeleniumFetcher,
    analyzer: Optional[AIAnalyzer],
    num_results: int = 15,
    use_ai: bool = True,
    item_name: Optional[str] = None,
    audit_trail: AgentAuditTrail | None = None,
    search_label: str = "primary",
) -> list[LeasingOffer]:
    """Основной поисковый пайплайн с параллельной обработкой."""
    cleaned_query = clean_search_query(query)
    if not cleaned_query:
        logger.warning(f"Пропуск некорректного запроса пайплайна: {query!r}")
        return []
    query = cleaned_query

    logger.info("=" * 70)
    logger.info(f"Поисковый запрос: {query}")
    logger.info("=" * 70)

    if item_name:
        item_name = clean_search_query(item_name, max_words=8, max_length=80) or item_name
        model_name, requested_year = extract_query_constraints(item_name)
    else:
        model_name = extract_model_from_query(query)
        requested_year = extract_year_from_text(query)

    mandatory_query_name = model_name
    if model_name and requested_year is not None:
        mandatory_query_name = f"{model_name} {requested_year}"

    mandatory_urls = generate_mandatory_urls(mandatory_query_name or model_name)
    logger.info(f"Обязательные источники: {len(mandatory_urls)}")

    search_results = search_google(query, num_results * 2)
    if not search_results:
        logger.warning(f"Нет результатов Google для запроса: {query}")
        filtered_google = []
    else:
        filtered_google = filter_search_results(search_results, mandatory_query_name or model_name, num_results)

    all_results = merge_with_mandatory(filtered_google, mandatory_urls)
    logger.info(f"Всего URL до фильтра релевантности: {len(all_results)}")
    all_results = filter_irrelevant_results(all_results)
    logger.info(f"Всего URL после фильтра релевантности: {len(all_results)}")

    if audit_trail is not None:
        audit_trail.record(
            action="search.collect_urls",
            status="ok" if all_results else "warning",
            risk="low" if len(all_results) >= num_results else ("medium" if all_results else "high"),
            confidence=0.8 if len(all_results) >= num_results else (0.45 if all_results else 0.15),
            message="Candidate URLs collected for market search" if all_results else "No candidate URLs collected for market search",
            search=search_label,
            mandatory=len(mandatory_urls),
            google=len(filtered_google),
            total=len(all_results),
        )

    if not all_results:
        logger.warning("Нет URL для обработки")
        return []

    all_results = filter_available_results(all_results)
    logger.info(f"Всего URL после проверки доступности: {len(all_results)}")

    if audit_trail is not None:
        audit_trail.record(
            action="search.url_availability",
            status="ok" if all_results else "warning",
            risk="low" if len(all_results) >= max(1, num_results // 2) else ("medium" if all_results else "high"),
            confidence=0.75 if len(all_results) >= max(1, num_results // 2) else (0.4 if all_results else 0.1),
            message="Reachable URLs passed availability check" if all_results else "No reachable URLs after availability check",
            search=search_label,
            reachable=len(all_results),
        )

    if not all_results:
        logger.warning("Нет доступных URL для обработки после фильтрации")
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
            fallback_parser = generic_parser if is_avito else None

            future = executor.submit(
                _process_single_url,
                result,
                model_name,
                fetcher,
                parser,
                idx,
                len(all_results),
                fallback_parser,
            )
            futures[future] = (idx, url)

        with tqdm(total=len(futures), desc="Обработка URL", unit="url") as pbar:
            for future in as_completed(futures):
                idx, url = futures[future]
                try:
                    url_offers = future.result()
                    offers.extend(url_offers)
                except Exception as e:
                    logger.error(f"Ошибка при обработке {url}: {e}")
                finally:
                    pbar.update(1)

    offers = filter_low_quality_offers(offers)
    offers = deduplicate_offers(offers)
    offers = filter_offers_by_requested_year(offers, requested_year)
    offers = filter_price_outliers(offers)

    logger.info(f"Всего предложений после обработки: {len(offers)}")

    if audit_trail is not None:
        audit_trail.record(
            action="search.extract_offers",
            status="ok" if offers else "warning",
            risk="low" if len(offers) >= 3 else ("medium" if offers else "high"),
            confidence=0.85 if len(offers) >= 3 else (0.5 if offers else 0.1),
            message="Market offers extracted from reachable sources" if offers else "No offers extracted from reachable sources",
            search=search_label,
            offers=len(offers),
            requested_year=requested_year,
            urls=len(all_results),
        )
    return offers
