from __future__ import annotations

import json
from dataclasses import asdict
from typing import Optional

from leasing_analyzer.core.logging import get_logger
from leasing_analyzer.core.models import LeasingOffer
from leasing_analyzer.core.utils import format_price


logger = get_logger(__name__)

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


def print_final_report(report: dict, client_price: Optional[int]):
    """Print final market report."""
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