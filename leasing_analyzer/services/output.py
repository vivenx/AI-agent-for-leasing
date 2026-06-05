from __future__ import annotations

import json
from dataclasses import asdict
from typing import Optional

from leasing_analyzer.core.logging import get_logger
from leasing_analyzer.core.models import LeasingOffer
from leasing_analyzer.core.utils import format_price


logger = get_logger(__name__)

def print_offer(idx: int, o: LeasingOffer):
    """Печатает детали одного предложения."""
    print(f"\n[{idx}] {o.title}")
    print(f"    Источник: {o.source}")
    print(f"    Модель: {o.model}")
    if o.category:
        print(f"    Категория: {o.category}")
    print(f"    Ссылка: {o.url}")
    
    if o.price_str or o.monthly_payment_str or o.price_on_request:
        print("    --- Цены ---")
        if o.price_on_request and not o.price:
            print("    Цена по запросу")
        if o.price_str:
            if o.currency and o.currency.upper() != "RUB":
                print(f"    Цена: {o.price_str} ({o.currency})")
            else:
                print(f"    Цена: {o.price_str}")
        if o.monthly_payment_str:
            print(f"    Ежемесячный платеж: {o.monthly_payment_str}")
    
    if any([o.year, o.power, o.mileage, o.vendor, o.condition, o.location]):
        print("    --- Характеристики ---")
        if o.vendor:
            print(f"    Производитель: {o.vendor}")
        if o.year:
            print(f"    Год: {o.year}")
        if o.condition:
            print(f"    Состояние: {o.condition}")
        if o.power:
            print(f"    Мощность: {o.power}")
        if o.mileage:
            print(f"    Пробег: {o.mileage}")
        if o.location:
            print(f"    Расположение: {o.location}")
    
    if o.specs:
        print("    --- Дополнительные характеристики ---")
        for k, v in o.specs.items():
            print(f"    {k}: {v}")
    
    if o.pros:
        print("    --- Плюсы ---")
        for p in o.pros:
            print(f"    + {p}")
    
    if o.cons:
        print("    --- Минусы ---")
        for c in o.cons:
            print(f"    - {c}")
    
    if o.analogs:
        print("    --- Упомянутые аналоги ---")
        for a in o.analogs:
            print(f"    • {a}")


def print_results(offers: list[LeasingOffer]):
    """Печатает все результаты."""
    print("\n" + "=" * 70)
    print(f"Найдено предложений: {len(offers)}")
    print("=" * 70)
    for i, o in enumerate(offers, 1):
        print_offer(i, o)


def print_analog_details(analog_details: list[dict]):
    """Печатает детали сравнения аналогов."""
    print("\nСравнение аналогов:")
    for a in analog_details:
        p_est = a.get("avg_price_guess")
        print(f"--- {a['name']} ---")
        print(f"  Цена ~ {format_price(p_est) if p_est else 'Нет данных'}")
        if a.get('note'):
            print(f"  Примечание: {a['note']}")
        if a['pros']:
            print(f"  [+] {', '.join(a['pros'])}")
        if a['cons']:
            print(f"  [-] {', '.join(a['cons'])}")
        
        # Печатаем источники
        print("  Источники:")
        printed_links = set()
        if a.get("best_link"):
            print(f"    [Рекомендуется] {a['best_link']}")
            printed_links.add(a['best_link'])
        
        if a.get("listings"):
            for l in a["listings"]:
                lnk = l.get('link', '')
                if lnk and lnk not in printed_links:
                    print(f"    {l.get('title', 'Ссылка')}: {lnk}")

def print_best_offer_analysis(best_offer: Optional[LeasingOffer], analysis: dict, item_name: str):
    """Печатает анализ лучшего предложения."""
    if not best_offer:
        return
    
    print("\n" + "=" * 70)
    print(f"🏆 ЛУЧШЕЕ ПРЕДЛОЖЕНИЕ: {item_name}")
    print("=" * 70)
    
    print(f"\n📋 {best_offer.title}")
    print(f"   Ссылка: {best_offer.url}")
    print(f"   Источник: {best_offer.source}")
    
    if best_offer.price_str:
        print(f"   💰 Цена: {best_offer.price_str}")
    if best_offer.year:
        print(f"   📅 Год: {best_offer.year}")
    if best_offer.condition:
        print(f"   ⚙️  Состояние: {best_offer.condition}")
    if best_offer.location:
        print(f"   📍 Расположение: {best_offer.location}")
    
    score = analysis.get("best_score", 0)
    reason = analysis.get("reason", "")
    print(f"\n   ⭐ Оценка: {score:.1f}/10")
    if reason:
        print(f"   💡 Причина: {reason}")
    
    ranking = analysis.get("ranking", [])
    if ranking and len(ranking) > 1:
        print(f"\n   📊 Рейтинг всех предложений:")
        for rank in ranking[:5]:  # Топ-5
            idx = rank.get("index", 0)
            score_r = rank.get("score", 0)
            brief = rank.get("brief_reason", "")
            print(f"      {idx+1}. Оценка {score_r:.1f}/10 - {brief}")


def print_best_offers_comparison(comparisons: dict, original_name: str):
    """Печатает сравнение лучшего исходного предложения с лучшими аналогами."""
    if not comparisons:
        return
    
    print("\n" + "=" * 70)
    print("⚖️  СРАВНЕНИЕ: Лучший оригинал против лучших аналогов")
    print("=" * 70)
    
    for analog_name, comparison in comparisons.items():
        print(f"\n{'─' * 60}")
        print(f"Оригинал: {original_name}")
        print(f"Аналог: {analog_name}")
        print(f"{'─' * 60}")
        
        winner = comparison.get("winner", "original")
        orig_score = comparison.get("original_score", 0)
        analog_score = comparison.get("analog_score", 0)
        
        if winner == "original":
            print(f"🏆 Победитель: ОРИГИНАЛ ({orig_score:.1f}/10 против {analog_score:.1f}/10)")
        else:
            print(f"🏆 Победитель: АНАЛОГ ({analog_score:.1f}/10 против {orig_score:.1f}/10)")
        
        # Сравнение цен
        price_comp = comparison.get("price_comparison", {})
        if price_comp:
            orig_price = price_comp.get("original_price")
            analog_price = price_comp.get("analog_price")
            diff = price_comp.get("difference_percent", 0)
            verdict = price_comp.get("price_verdict", "similar")
            
            print(f"\n💰 Сравнение цен:")
            print(f"   Оригинал: {format_price(orig_price)}")
            print(f"   Аналог: {format_price(analog_price)}")
            if diff != 0:
                print(f"   Разница: {diff:+.1f}% ({verdict})")
        
        # Плюсы и минусы
        pros_orig = comparison.get("pros_original", [])
        cons_orig = comparison.get("cons_original", [])
        pros_analog = comparison.get("pros_analog", [])
        cons_analog = comparison.get("cons_analog", [])
        
        if pros_orig:
            print(f"\n✅ Преимущества оригинала:")
            for p in pros_orig[:3]:
                print(f"   + {p}")
        
        if cons_orig:
            print(f"\n❌ Недостатки оригинала:")
            for c in cons_orig[:3]:
                print(f"   - {c}")
        
        if pros_analog:
            print(f"\n✅ Преимущества аналога:")
            for p in pros_analog[:3]:
                print(f"   + {p}")
        
        if cons_analog:
            print(f"\n❌ Недостатки аналога:")
            for c in cons_analog[:3]:
                print(f"   - {c}")
        
        # Рекомендация
        recommendation = comparison.get("recommendation", "")
        if recommendation:
            print(f"\n💡 Рекомендация:")
            print(f"   {recommendation}")
        
        # Сценарии использования
        use_cases_orig = comparison.get("use_cases_original", [])
        use_cases_analog = comparison.get("use_cases_analog", [])
        
        if use_cases_orig:
            print(f"\n📌 Когда выбрать оригинал:")
            for uc in use_cases_orig[:2]:
                print(f"   • {uc}")
        
        if use_cases_analog:
            print(f"\n📌 Когда выбрать аналог:")
            for uc in use_cases_analog[:2]:
                print(f"   • {uc}")


def print_final_report(report: dict, client_price: Optional[int]):
    """Печатает итоговый рыночный отчет."""
    print("\n" + "=" * 70)
    print("ИТОГОВЫЙ ОТЧЕТ")
    print("=" * 70)
    
    if report["market_range"]:
        min_p, max_p = report["market_range"]
        print(f"Рыночный диапазон: {format_price(min_p)} – {format_price(max_p)}")
        print(f"Медиана: {format_price(report['median_price'])}")
    
    if client_price:
        status = "OK" if report.get("client_price_ok") else "Deviation > 20%"
        print(f"Цена клиента: {format_price(client_price)} -> {status}")
    
    print(f"Комментарий: {report['explanation']}")
    
    if report.get("ai_flag"):
        print(f"ПРЕДУПРЕЖДЕНИЕ: {report.get('ai_comment')}")
    
    # Печатаем анализ лучшего исходного предложения
    best_original = report.get("best_original_offer")
    best_original_analysis = report.get("best_original_analysis", {})
    if best_original:
        item_name = report.get("item", "Unknown")
        # При необходимости превращаем dict в LeasingOffer
        if isinstance(best_original, dict):
            best_offer_obj = LeasingOffer(**best_original)
        else:
            best_offer_obj = best_original
        print_best_offer_analysis(best_offer_obj, best_original_analysis, item_name)
    
def save_results_json(offers: list[LeasingOffer], item_name: str = "results", market_report: Optional[dict] = None):
    """Сохраняет результаты в JSON-файл."""
    safe_name = "".join(c if c.isalnum() or c in " _-" else "_" for c in item_name)
    filename = f"{safe_name}.json"
    data = {"offers": [asdict(o) for o in offers]}
    if market_report:
        data["market_report"] = market_report
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info(f"JSON сохранен в {filename}")
