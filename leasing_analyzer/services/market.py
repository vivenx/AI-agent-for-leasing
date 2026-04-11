from __future__ import annotations

import re
import statistics
from dataclasses import asdict
from typing import Optional

from leasing_analyzer.clients.ai_analyzer import AIAnalyzer
from leasing_analyzer.clients.sonar import (
    SonarAnalogFinder,
    cache_sonar_analogs,
    get_cached_sonar_analogs,
)
from leasing_analyzer.core.config import CONFIG
from leasing_analyzer.core.logging import get_logger
from leasing_analyzer.core.models import LeasingOffer, ListingSummary, SonarAnalogResult
from leasing_analyzer.core.utils import extract_price_candidate, format_price, is_valid_url


logger = get_logger(__name__)


def percentile(sorted_values: list[int], p: float) -> float:
    """Вычисляет перцентиль по отсортированным значениям безопасно для больших чисел."""
    if not sorted_values:
        return 0.0
    try:
        k = (len(sorted_values) - 1) * p
        f = int(k)
        c = min(f + 1, len(sorted_values) - 1)
        if f == c:
            val = sorted_values[int(k)]
            # Безопасное преобразование: если число слишком большое, возвращаем как есть
            try:
                return float(val)
            except OverflowError:
                # Для очень больших чисел возвращаем строковое представление как float
                return float(str(val))
        
        # Вычисляем взвешенное среднее
        d0 = sorted_values[f] * (c - k)
        d1 = sorted_values[c] * (k - f)
        result = d0 + d1
        try:
            return float(result)
        except OverflowError:
            # Запасной вариант: используем простое среднее
            return float((sorted_values[f] + sorted_values[c]) / 2)
    except (OverflowError, ValueError) as e:
        logger.warning(f"Error calculating percentile: {e}, using middle value")
        mid_idx = len(sorted_values) // 2
        return float(sorted_values[mid_idx] if len(sorted_values) % 2 == 1 else (sorted_values[mid_idx - 1] + sorted_values[mid_idx]) / 2)

def filter_price_outliers(offers: list[LeasingOffer]) -> list[LeasingOffer]:
    """Удаляет ценовые выбросы методом IQR."""
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
    """Фильтрует низкокачественные предложения с подозрительными или неполными данными."""
    if not offers:
        return []
    
    filtered = []
    removed = 0
    
    for offer in offers:
        # Пропускаем предложения со слишком короткими заголовками
        if len(offer.title.strip()) < 5:
            logger.debug(f"Removing offer with too short title: {offer.title[:30]}")
            removed += 1
            continue
        
        # Пропускаем предложения с невалидными URL
        if not is_valid_url(offer.url):
            logger.debug(f"Removing offer with invalid URL: {offer.url}")
            removed += 1
            continue
        
        # Пропускаем предложения с подозрительно низкой ценой
        if offer.price is not None and offer.price < CONFIG.min_valid_price:
            logger.debug(f"Removing offer with suspiciously low price: {offer.price}")
            removed += 1
            continue
        
        # Оставляем предложения, где есть хоть какие-то полезные данные
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
    Собирает модели-аналоги, используя Sonar как основной метод.
    Sonar стремится вернуть ровно 3 аналога.

    Возвращает:
        Кортеж `(analog_names, sonar_details)`,
        где `analog_names` — список названий аналогов,
        а `sonar_details` — детальная информация от Sonar.
    """
    from leasing_analyzer.services.search import search_google

    sonar_details: list[SonarAnalogResult] = []
    
    # Сначала проверяем кеш
    cached_analogs = get_cached_sonar_analogs(item_name)
    if cached_analogs:
        logger.info(f"[SONAR] Using cached analogs for '{item_name}'")
        analog_names = [a["name"] for a in cached_analogs if a.get("name")]
        return analog_names[:3], cached_analogs[:3]
    
    # ОСНОВНОЙ ПУТЬ: используем Sonar для поиска аналогов, если он доступен
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
                    # Сохраняем результаты в кеш
                    cache_sonar_analogs(item_name, sonar_details[:3])
                    # Возвращаем ровно 3 аналога, либо меньше если нашли меньше
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
    
    # РЕЗЕРВНЫЙ ПУТЬ: только если Sonar недоступен или завершился ошибкой
    logger.info("[FALLBACK] Using fallback methods for analog search...")
    analogs_set = set()
    
    # Собираем аналоги из самих предложений
    for o in offers:
        for a in o.analogs:
            analogs_set.add(a.strip())
    
    # Добавляем предложения от GigaChat
    if len(analogs_set) < 3 and use_ai and analyzer:
        logger.info("[FALLBACK] Using GigaChat for analog suggestions...")
        ai_analogs = analyzer.suggest_analogs(item_name)
        for a in ai_analogs:
            analogs_set.add(a)

    # Дособираем аналоги через Google
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

    # Возвращаем ровно 3 аналога, либо меньше если не нашли
    analog_names = [a for a in analogs_set if a][:3]
    logger.info(f"[FALLBACK] Found {len(analog_names)} analogs via fallback methods")
    return analog_names, sonar_details


def fetch_listing_summaries(query: str, top_n: int = 3) -> list[ListingSummary]:
    """Получает краткие сводки объявлений для сравнения аналогов."""
    from leasing_analyzer.services.search import search_google

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
    """Выполняет рыночный анализ по собранным предложениям."""
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
    
    # Безопасно считаем медиану и среднее, чтобы избежать OverflowError
    try:
        median_p = statistics.median(prices_sorted)
        # Преобразуем в int, если число целое, чтобы избежать проблем с точностью float
        if isinstance(median_p, float) and median_p.is_integer():
            median_p = int(median_p)
    except (OverflowError, ValueError) as e:
        logger.warning(f"Error calculating median: {e}, using middle value")
        # Запасной вариант: берем средний элемент
        mid_idx = len(prices_sorted) // 2
        median_p = prices_sorted[mid_idx] if len(prices_sorted) % 2 == 1 else (prices_sorted[mid_idx - 1] + prices_sorted[mid_idx]) // 2
    
    try:
        # Безопасно считаем среднее
        total = sum(prices_sorted)
        count = len(prices_sorted)
        if count > 0:
            mean_p = total // count  # Используем целочисленное деление, чтобы не ловить проблемы float
            # Если нужна большая точность, учитываем остаток
            remainder = total % count
            if remainder > 0:
                # Округляем до ближайшего целого
                mean_p = round(total / count) if total < 10**15 else mean_p
        else:
            mean_p = 0
    except (OverflowError, ValueError) as e:
        logger.warning(f"Error calculating mean: {e}, using median")
        mean_p = median_p if isinstance(median_p, (int, float)) else 0

    result["market_range"] = [min_p, max_p]
    # Безопасно приводим медиану к float
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

def find_best_offer_from_list(
    offers: list[LeasingOffer],
    analyzer: Optional[AIAnalyzer],
    use_ai: bool,
    item_name: str,
    sonar_finder: Optional[SonarAnalogFinder] = None
) -> tuple[Optional[LeasingOffer], dict]:
    """
    Находит лучшее предложение в списке, сравнивая предложения между собой.
    Использует только Sonar API, без запасных методов.

    Возвращает:
        Кортеж `(best_offer, comparison_result)`.
    """
    if not offers:
        return None, {}
    
    if len(offers) == 1:
        return offers[0], {"best_index": 0, "best_score": 8.0, "reason": "Only one offer", "sonar_used": False}
    
    # Переводим предложения в формат словарей
    offers_dict = [asdict(o) for o in offers]
    
    # Только Sonar, без fallback-ветки
    if not use_ai or not sonar_finder or not sonar_finder.is_available():
        logger.error("[SONAR] Sonar not available - cannot find best offer without Sonar")
        # В крайнем случае возвращаем первое предложение
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
    best_analogs: list[tuple[str, Optional[LeasingOffer]]],  # [(название_аналога, лучшее_предложение), ...]
    original_name: str,
    analyzer: Optional[AIAnalyzer],
    use_ai: bool
) -> dict:
    """
    Сравнивает лучшее исходное предложение с лучшими предложениями аналогов.

    Возвращает:
        Словарь с результатами сравнения для каждого аналога.
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
            # Простое сравнение по цене
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
        
        # Добавляем URL в результат сравнения
        if comparison:
            comparison["original_url"] = best_original.url
            comparison["analog_url"] = best_analog.url
            comparison["original_title"] = best_original.title
            comparison["analog_title"] = best_analog.title
        
        comparisons[analog_name] = comparison
    
    return comparisons
