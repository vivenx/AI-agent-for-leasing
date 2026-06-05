from leasing_analyzer.services.user_sources import validate_user_source_url
from leasing_analyzer.core.utils import extract_price_candidate


def test_user_source_accepts_concrete_http_page() -> None:
    valid, reason = validate_user_source_url("https://dealer.example/models/bmw-x5-2024")

    assert valid is True
    assert reason == ""


def test_user_source_rejects_unsafe_schemes_and_local_targets() -> None:
    for url in (
        "file:///tmp/page.html",
        "javascript:alert(1)",
        "data:text/html,hello",
        "http://localhost/offers/123",
        "http://127.0.0.1/offers/123",
        "http://192.168.1.10/offers/123",
    ):
        valid, reason = validate_user_source_url(url)

        assert valid is False
        assert reason


def test_user_source_dns_resolution_is_not_required_for_public_hostname() -> None:
    valid, reason = validate_user_source_url(
        "https://auto.drom.ru/spec/tobolsk/hitachi/zx330lc-5g/excavator/mining/795368387.html",
        resolve_host=False,
    )

    assert valid is True
    assert reason == ""


def test_price_candidate_does_not_glue_year_to_price() -> None:
    assert extract_price_candidate("Hitachi ZX200LC-5A 2022 86 000 000 ₽") == 86_000_000
    assert extract_price_candidate("цена 11 000 000 руб.") == 11_000_000


def test_user_source_rejects_home_catalog_and_search_pages() -> None:
    for url in (
        "https://dealer.example",
        "https://dealer.example/",
        "https://dealer.example/catalog",
        "https://dealer.example/search?q=bmw",
        "https://dealer.example/cars",
    ):
        valid, reason = validate_user_source_url(url)

        assert valid is False
        assert reason
