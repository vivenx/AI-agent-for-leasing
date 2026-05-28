from __future__ import annotations

import logging
from typing import Optional
from dataclasses import asdict

# Ядро
from leasing_analyzer.core.config import CONFIG
from leasing_analyzer.core.models import LeasingOffer, UserInput
from leasing_analyzer.core.utils import (
    digits_to_int,
    format_price,
    ensure_list_str,
)

# Клиенты (AI / Sonar)
from leasing_analyzer.clients.gigachat import GigaChatClient
from leasing_analyzer.clients.ai_analyzer import AIAnalyzer
from leasing_analyzer.clients.sonar import get_sonar_finder

# Сервисы
from leasing_analyzer.services.fetcher import SeleniumFetcher
from leasing_analyzer.services.search import search_and_analyze
from leasing_analyzer.services.market import (
    analyze_market,
    collect_analogs,
    compare_best_offers_original_vs_analogs,
    fetch_listing_summaries,
    find_best_offer_from_list,
)
from leasing_analyzer.services.specs import extract_specs_from_multiple_sites

# Парсинг
from leasing_analyzer.parsing.content_cleaner import ContentCleaner

logger = logging.getLogger(__name__)


def extract_model_from_query(query: str) -> str:
    """Извлекает название модели из запроса по первым двум словам."""
    parts = query.split()
    return " ".join(parts[:2]) if parts else ""

def get_user_input() -> UserInput:
    """Запрашивает и валидирует пользовательский ввод."""
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

def run_pipeline(params: UserInput) -> tuple[list[LeasingOffer], dict]:
    """Выполняет основной пайплайн анализа."""
    item = params["item"]
    client_price = params["client_price"]
    use_ai = params["use_ai"]
    num_results = params["num_results"]
    memory_context = params.get("memory_context")


    fetcher = SeleniumFetcher()
    cleaner = ContentCleaner()

    sonar_finder = get_sonar_finder()
    if sonar_finder:
        logger.info("=" * 70)
        logger.info("[SONAR] Perplexity Sonar API initialized - will be used for deep offer analysis")
        logger.info("=" * 70)
    else:
        logger.warning("=" * 70)
        logger.warning(
            "[SONAR] Perplexity API not available - PERPLEXITY_API_KEY not set "
            "or invalid format (should start with 'pplx-' or 'sk-')"
        )
        logger.warning("[SONAR] Best-offer analysis will use fallback logic")
        logger.warning("=" * 70)

    analyzer = None
    if use_ai and CONFIG.gigachat_auth_data:
        client = GigaChatClient(CONFIG.gigachat_auth_data)
        analyzer = AIAnalyzer(client, cleaner, memory_context=memory_context)

    try:
        query = f"{item} {CONFIG.default_search_suffix}"
        offers = search_and_analyze(
            query,
            fetcher,
            analyzer,
            num_results=num_results,
            use_ai=use_ai,
            item_name=item,
        )

        if not offers:
            logger.warning("Direct search returned no results, trying simpler query...")
            query_simple = f"{item} {CONFIG.fallback_search_suffix}"
            offers = search_and_analyze(
                query_simple,
                fetcher,
                analyzer,
                num_results=num_results,
                use_ai=use_ai,
                item_name=item,
            )

        if not offers:
            logger.error("Could not extract offers even after retry")
            return [], {}

        # =============================
        # ОБОГАЩЕНИЕ ХАРАКТЕРИСТИКАМИ ИЗ СПЕЦИАЛИЗИРОВАННЫХ САЙТОВ
        # =============================
        if use_ai and analyzer:
            logger.info("=" * 70)
            logger.info("Enriching offers with technical specifications...")
            logger.info("=" * 70)

            item_specs = extract_specs_from_multiple_sites(
                item_name=item,
                fetcher=fetcher,
                analyzer=analyzer,
                num_sites=5,
            )

            if item_specs:
                logger.info(f"Extracted {len(item_specs)} specifications, enriching offers...")
                enriched_count = 0

                for offer in offers:
                    if len(offer.specs) < 3:
                        added_count = 0
                        for key, value in item_specs.items():
                            if key not in offer.specs and value:
                                offer.specs[key] = value
                                added_count += 1

                        if added_count > 0:
                            enriched_count += 1
                            logger.debug(
                                f"Enriched offer '{offer.title[:50]}...' "
                                f"with {added_count} new specs (total: {len(offer.specs)})"
                            )

                if enriched_count > 0:
                    logger.info(f"Enriched {enriched_count} offers with technical specifications")

        # =============================
        # ПЕРВИЧНЫЙ РЫНОЧНЫЙ ОТЧЕТ
        # =============================
        report = analyze_market(item, offers, client_price, sonar_finder=sonar_finder)

        # Проверяем отчет через AI
        if use_ai and analyzer and report.get("median_price"):
            logger.info("Validating report with AI...")
            validation = analyzer.validate_report(report)
            if not validation.get("is_valid"):
                logger.warning(f"AI flagged report as suspicious: {validation.get('comment')}")
                report["ai_flag"] = "SUSPICIOUS"
                report["ai_comment"] = validation.get("comment")
            else:
                logger.info("AI confirmed report validity")

        # =============================
        # ГЛУБОКИЙ АНАЛИЗ: поиск лучшего исходного предложения
        # =============================
        best_original_offer: Optional[LeasingOffer] = None
        best_original_analysis: dict = {}

        if use_ai and analyzer and offers:
            logger.info("=" * 70)
            logger.info("DEEP ANALYSIS: Comparing original offers to find the best one...")
            logger.info("=" * 70)

            best_original_offer, best_original_analysis = find_best_offer_from_list(
                offers=offers,
                analyzer=analyzer,
                use_ai=use_ai,
                item_name=item,
                sonar_finder=sonar_finder,
            )

            report["best_original_offer"] = asdict(best_original_offer) if best_original_offer else None
            report["best_original_analysis"] = best_original_analysis
        else:
            report["best_original_offer"] = None
            report["best_original_analysis"] = {}

        # =============================
        # СБОР АНАЛОГОВ
        # =============================
        analogs, sonar_analog_details = collect_analogs(
            item_name=item,
            offers=offers,
            use_ai=use_ai,
            analyzer=analyzer,
            sonar_finder=sonar_finder,
        )

        report["analogs_suggested"] = analogs

        analog_details: list[dict] = []
        best_analog_offers: list[tuple[str, Optional[LeasingOffer]]] = []

        sonar_lookup = {d["name"]: d for d in sonar_analog_details} if sonar_analog_details else {}

        if analogs:
            logger.info("Collecting analog listings...")
            for analog in analogs[:3]:
                sonar_info = sonar_lookup.get(analog, {})

                query_analog = f"{analog} {CONFIG.fallback_search_suffix}"
                analog_offers = search_and_analyze(
                    query_analog,
                    fetcher,
                    analyzer,
                    num_results=3,
                    use_ai=use_ai,
                    item_name=analog,
                )

                best_analog_offer: Optional[LeasingOffer] = None
                best_analog_analysis: dict = {}

                if analog_offers and use_ai and analyzer:
                    logger.info(f"Finding best offer for analog '{analog}'...")
                    best_analog_offer, best_analog_analysis = find_best_offer_from_list(
                        offers=analog_offers,
                        analyzer=analyzer,
                        use_ai=use_ai,
                        item_name=analog,
                        sonar_finder=sonar_finder,
                    )

                best_analog_offers.append((analog, best_analog_offer))

                listings = fetch_listing_summaries(f"{analog} купить", top_n=3)
                price_list = [l["price_guess"] for l in listings if l.get("price_guess")]
                avg_price_math = int(sum(price_list) / len(price_list)) if price_list else None

                pros: list[str] = []
                cons: list[str] = []
                note = ""
                price_hint = None
                best_link = None

                if sonar_info:
                    note = sonar_info.get("description", "") or sonar_info.get("key_difference", "")
                    price_range_str = sonar_info.get("price_range", "")
                    if price_range_str:
                        note = (
                            f"{note} | Ценовой диапазон: {price_range_str}"
                            if note
                            else f"Ценовой диапазон: {price_range_str}"
                        )

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
                    logger.warning(f"[SONAR] No Sonar info for {analog} - pros/cons will be empty")

                final_price = price_hint if price_hint else avg_price_math
                if best_analog_offer and best_analog_offer.price:
                    final_price = best_analog_offer.price

                analog_details.append(
                    {
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
                        "sonar_info": sonar_info if sonar_info else None,
                    }
                )

        report["analogs_details"] = analog_details

        # =============================
        # ГЛУБОКИЙ АНАЛИЗ: сравнение лучшего исходного предложения с КАЖДЫМ аналогом
        # =============================
        comparisons: dict = {}

        if best_original_offer and best_analog_offers:
            logger.info("=" * 70)
            logger.info("DEEP ANALYSIS: Comparing best original with each analog...")
            logger.info("=" * 70)

            original_offer_data = {
                "title": best_original_offer.title,
                "price": best_original_offer.price,
                "url": best_original_offer.url,
            }

            sonar_comparison_failed = False

            if sonar_finder and sonar_finder.is_available():
                logger.info("[SONAR] Using Sonar to compare offers (with links)...")
                for analog_name, best_analog in best_analog_offers:
                    if not best_analog:
                        continue

                    analog_offer_data = {
                        "title": best_analog.title,
                        "price": best_analog.price,
                        "url": best_analog.url,
                    }

                    logger.info(f"[SONAR] Comparing with {analog_name}:")
                    logger.info(f"  Original: {best_original_offer.title[:60]}... -> {best_original_offer.url}")
                    logger.info(f"  Analog:   {best_analog.title[:60]}... -> {best_analog.url}")

                    try:
                        sonar_comparison = sonar_finder.compare_offers(
                            original_name=item,
                            original_offer=original_offer_data,
                            analog_name=analog_name,
                            analog_offer=analog_offer_data,
                        )

                        if sonar_comparison and sonar_comparison.get("winner") != "unknown":
                            comparisons[analog_name] = {
                                "winner": sonar_comparison.get("winner", "tie"),
                                "original_score": 7.0,
                                "analog_score": 7.0,
                                "price_comparison": {
                                    "original_price": best_original_offer.price,
                                    "analog_price": best_analog.price,
                                    "price_diff": sonar_comparison.get("price_diff", ""),
                                },
                                "pros_original": sonar_comparison.get("original_advantages", []),
                                "cons_original": sonar_comparison.get("original_disadvantages", []),
                                "pros_analog": sonar_comparison.get("analog_advantages", []),
                                "cons_analog": sonar_comparison.get("analog_disadvantages", []),
                                "recommendation": sonar_comparison.get("recommendation", ""),
                                "original_url": best_original_offer.url,
                                "analog_url": best_analog.url,
                                "original_title": best_original_offer.title,
                                "analog_title": best_analog.title,
                                "original_price_formatted": format_price(best_original_offer.price),
                                "analog_price_formatted": format_price(best_analog.price),
                                "sonar_comparison": True,
                            }

                            logger.info(f"[SONAR] Winner: {sonar_comparison.get('winner', 'tie')}")
                        else:
                            logger.warning(f"[SONAR] Comparison failed for {analog_name}, will use fallback")
                            sonar_comparison_failed = True

                    except Exception as e:
                        logger.error(f"[SONAR] Exception during comparison with {analog_name}: {e}")
                        sonar_comparison_failed = True

            if (
                (not sonar_finder or not sonar_finder.is_available() or sonar_comparison_failed or len(comparisons) == 0)
                and use_ai
                and analyzer
            ):
                if sonar_comparison_failed:
                    logger.warning("[FALLBACK] Sonar comparison failed, falling back to GigaChat...")
                else:
                    logger.info("[FALLBACK] Using GigaChat for comparison...")

                gigachat_comparisons = compare_best_offers_original_vs_analogs(
                    best_original=best_original_offer,
                    best_analogs=best_analog_offers,
                    original_name=item,
                    analyzer=analyzer,
                    use_ai=use_ai,
                )

                for analog_name, _best_analog in best_analog_offers:
                    if analog_name not in comparisons and analog_name in gigachat_comparisons:
                        comparisons[analog_name] = gigachat_comparisons[analog_name]
                        comparisons[analog_name]["sonar_comparison"] = False

        report["best_offers_comparison"] = comparisons
        report["sonar_used"] = bool(best_original_analysis.get("sonar_used"))

        return offers, report

    finally:
        fetcher.close()

def run_analysis(
    item: str,
    client_price: int | None = None,
    use_ai: bool = True,
    num_results: int = 5,
    memory_context: str | None = None,
) -> dict:
    """
    Точка входа API для программного запуска анализа.

    Аргументы:
        item: Объект для анализа, например "BMW M5 2024"
        client_price: Ожидаемая цена клиента, если она известна
        use_ai: Использовать ли AI-анализ
        num_results: Количество поисковых результатов для обработки

    Возвращает:
        Словарь с результатами анализа только для запрошенной модели
    """
    params: UserInput = {
        "item": item,
        "client_price": client_price,
        "use_ai": use_ai,
        "num_results": num_results,
        "memory_context": memory_context,

    }
    
    offers, report = run_pipeline(params)
    
    return {
        "item": item,
        "offers_used": [asdict(o) for o in offers],
        "market_report": report,
    }



