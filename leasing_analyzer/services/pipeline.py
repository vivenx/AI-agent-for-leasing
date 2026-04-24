from __future__ import annotations

from dataclasses import asdict
from typing import Optional

from leasing_analyzer.clients.ai_analyzer import AIAnalyzer
from leasing_analyzer.clients.gigachat import GigaChatClient
from leasing_analyzer.clients.sonar import get_sonar_finder
from leasing_analyzer.core.audit import AgentAuditTrail
from leasing_analyzer.core.config import CONFIG
from leasing_analyzer.core.logging import get_logger
from leasing_analyzer.core.models import LeasingOffer, UserInput
from leasing_analyzer.core.utils import digits_to_int
from leasing_analyzer.parsing.content_cleaner import ContentCleaner
from leasing_analyzer.services.fetcher import SeleniumFetcher
from leasing_analyzer.services.market import analyze_market, collect_analogs
from leasing_analyzer.services.search import search_and_analyze

logger = get_logger(__name__)


def extract_model_from_query(query: str) -> str:
    parts = query.split()
    return " ".join(parts[:2]) if parts else ""


def get_user_input() -> UserInput:
    print("=" * 70)
    print("Leasing Asset Market Analyzer")
    print("=" * 70)

    item = input("\nEnter leasing item (e.g., BMW M5 2024): ").strip()
    if not item:
        raise ValueError("Empty query")

    client_price_input = input("Client price (digits only, optional): ").strip()
    client_price = digits_to_int(client_price_input) if client_price_input else None

    use_ai_str = input("Use AI for parsing (y/n, default y): ").strip().lower()
    use_ai = use_ai_str != "n"

    num_input = input("Number of results to search (default 5): ").strip()
    num_results = int(num_input) if num_input.isdigit() else CONFIG.default_num_results

    return {
        "item": item,
        "client_price": client_price,
        "use_ai": use_ai,
        "num_results": num_results,
    }


def summarize_sources(offers: list[LeasingOffer]) -> list[dict]:
    summary: dict[str, dict] = {}

    for offer in offers:
        source_name = offer.source or "unknown"
        bucket = summary.setdefault(
            source_name,
            {
                "source": source_name,
                "offers": 0,
                "priced_offers": 0,
                "offers_with_specs": 0,
            },
        )
        bucket["offers"] += 1
        if offer.price is not None:
            bucket["priced_offers"] += 1
        if offer.specs:
            bucket["offers_with_specs"] += 1

    return sorted(
        summary.values(),
        key=lambda value: (-value["offers"], -value["priced_offers"], value["source"]),
    )


def attach_audit(report: dict, audit_trail: AgentAuditTrail) -> dict:
    report["agent_audit"] = audit_trail.export()
    report["agent_audit_summary"] = audit_trail.summary()
    report.setdefault("analogs_details", [])
    report.setdefault("best_original_offer", None)
    report.setdefault("best_original_analysis", {})
    report.setdefault("best_offers_comparison", {})
    return report


def run_pipeline(params: UserInput) -> tuple[list[LeasingOffer], dict]:
    item = params["item"]
    client_price = params["client_price"]
    use_ai = params["use_ai"]
    num_results = params["num_results"]
    memory_context = params.get("memory_context")

    audit_trail = AgentAuditTrail()
    audit_trail.record(
        action="pipeline.start",
        status="ok",
        risk="low",
        confidence=1.0,
        message="Market analysis pipeline started",
        item=item,
        use_ai=use_ai,
        num_results=num_results,
    )

    fetcher = SeleniumFetcher()
    cleaner = ContentCleaner()

    sonar_finder = get_sonar_finder()
    audit_trail.record(
        action="integration.sonar",
        status="ok" if sonar_finder else "warning",
        risk="low" if sonar_finder else "medium",
        confidence=0.9 if sonar_finder else 0.35,
        message="Sonar fallback is available" if sonar_finder else "Sonar fallback is unavailable",
    )

    analyzer = None
    if use_ai and CONFIG.gigachat_auth_data:
        client = GigaChatClient(CONFIG.gigachat_auth_data)
        analyzer = AIAnalyzer(
            client,
            cleaner,
            memory_context=memory_context,
            audit_trail=audit_trail,
        )
        audit_trail.record(
            action="integration.gigachat",
            status="ok",
            risk="low",
            confidence=0.9,
            message="GigaChat parsing is enabled",
        )
    elif use_ai:
        audit_trail.record(
            action="integration.gigachat",
            status="warning",
            risk="medium",
            confidence=0.25,
            message="AI parsing requested, but GigaChat credentials are missing",
        )
    else:
        audit_trail.record(
            action="integration.gigachat",
            status="ok",
            risk="low",
            confidence=1.0,
            message="AI parsing is disabled by request",
        )

    try:
        offers: list[LeasingOffer] = []
        analysis_basis = "original_model"
        fallback_analogs: list[str] = []
        fallback_analog: Optional[str] = None

        primary_query = f"{item} {CONFIG.default_search_suffix}"
        offers = search_and_analyze(
            primary_query,
            fetcher,
            analyzer,
            num_results=num_results,
            use_ai=use_ai,
            item_name=item,
            audit_trail=audit_trail,
            search_label="original_exact",
        )

        if not offers:
            logger.warning("Exact model search returned no results, trying relaxed query...")
            relaxed_query = f"{item} {CONFIG.fallback_search_suffix}"
            offers = search_and_analyze(
                relaxed_query,
                fetcher,
                analyzer,
                num_results=num_results,
                use_ai=use_ai,
                item_name=item,
                audit_trail=audit_trail,
                search_label="original_relaxed",
            )

        if not offers:
            fallback_analogs, _ = collect_analogs(
                item_name=item,
                offers=[],
                use_ai=use_ai,
                analyzer=analyzer,
                sonar_finder=sonar_finder,
            )
            audit_trail.record(
                action="pipeline.analog_fallback",
                status="warning",
                risk="medium",
                confidence=0.35,
                message="Exact model listings were not found, switching to analog fallback",
                analogs=len(fallback_analogs),
            )

            best_fallback_offers: list[LeasingOffer] = []
            best_fallback_name: Optional[str] = None

            for analog in fallback_analogs[:3]:
                analog_query = f"{analog} {CONFIG.fallback_search_suffix}"
                analog_offers = search_and_analyze(
                    analog_query,
                    fetcher,
                    analyzer,
                    num_results=num_results,
                    use_ai=use_ai,
                    item_name=analog,
                    audit_trail=audit_trail,
                    search_label=f"fallback_analog:{analog}",
                )
                if len(analog_offers) > len(best_fallback_offers):
                    best_fallback_offers = analog_offers
                    best_fallback_name = analog

            if best_fallback_offers:
                offers = best_fallback_offers
                analysis_basis = "analog_fallback"
                fallback_analog = best_fallback_name
                audit_trail.record(
                    action="pipeline.analog_fallback",
                    status="warning",
                    risk="medium",
                    confidence=0.45,
                    message="Market fallback switched to the best available analog",
                    fallback_analog=fallback_analog,
                    offers=len(offers),
                )

        if not offers:
            report = analyze_market(item, [], client_price, sonar_finder=sonar_finder)
            report["source_summary"] = []
            report["analysis_basis"] = analysis_basis
            report["fallback_used"] = analysis_basis == "analog_fallback"
            report["fallback_analog"] = fallback_analog
            report["fallback_analogs"] = fallback_analogs
            report["analogs_suggested"] = fallback_analogs if fallback_analogs else []
            audit_trail.record(
                action="pipeline.market_report",
                status="warning",
                risk="high",
                confidence=0.1,
                message="No offers were collected for the requested model or fallback analogs",
            )
            return [], attach_audit(report, audit_trail)

        source_summary = summarize_sources(offers)
        audit_trail.record(
            action="market.sources",
            status="ok",
            risk="low" if len(source_summary) >= 2 else "medium",
            confidence=0.9 if len(source_summary) >= 2 else 0.6,
            message="Source summary prepared for the market report",
            sources=len(source_summary),
            offers=len(offers),
        )

        report = analyze_market(item, offers, client_price, sonar_finder=sonar_finder)
        report["source_summary"] = source_summary
        report["analysis_basis"] = analysis_basis
        report["fallback_used"] = analysis_basis == "analog_fallback"
        report["fallback_analog"] = fallback_analog
        report["fallback_analogs"] = fallback_analogs
        report["analogs_suggested"] = fallback_analogs if analysis_basis == "analog_fallback" else []

        if analysis_basis == "analog_fallback" and fallback_analog:
            report["explanation"] = (
                f"{report.get('explanation', '').strip()} "
                f"Fallback used analog market: {fallback_analog}."
            ).strip()

        audit_trail.record(
            action="pipeline.market_report",
            status="ok" if report.get("median_price") else "warning",
            risk="low" if report.get("median_price") and len(offers) >= 3 else "medium",
            confidence=0.85 if report.get("median_price") and len(offers) >= 3 else 0.5,
            message="Market report calculated from collected sources",
            offers=len(offers),
            sources=len(source_summary),
            basis=analysis_basis,
        )

        return offers, attach_audit(report, audit_trail)

    finally:
        fetcher.close()


def run_analysis(
    item: str,
    client_price: int | None = None,
    use_ai: bool = True,
    num_results: int = 5,
    memory_context: str | None = None,
) -> dict:
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
        "offers_used": [asdict(offer) for offer in offers],
        "market_report": report,
    }
