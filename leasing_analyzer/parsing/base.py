from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional
from urllib.parse import urlparse

from leasing_analyzer.clients.ai_analyzer import AIAnalyzer
from leasing_analyzer.core.models import LeasingOffer
from leasing_analyzer.parsing.avito import parse_avito_list_page
from leasing_analyzer.parsing.basic import extract_visible_text, parse_page_basic
from leasing_analyzer.parsing.helpers import create_offer_from_merged


class ParserStrategy(ABC):
    """Абстрактный базовый класс для стратегий парсинга."""

    @abstractmethod
    def parse(self, html: str, url: str, model_name: str, title: str = "") -> list["LeasingOffer"]:
        """Разбирает HTML и возвращает список предложений."""
        pass


class AvitoParserStrategy(ParserStrategy):
    """Парсер страниц со списком объявлений Avito."""

    def parse(self, html: str, url: str, model_name: str, title: str = "") -> list["LeasingOffer"]:
        """Разбирает страницу списка Avito."""
        return parse_avito_list_page(html, model_name)


class GenericParserStrategy(ParserStrategy):
    """Универсальный парсер на основе базовых regex и AI."""

    def __init__(self, analyzer: Optional[AIAnalyzer], use_ai: bool = True):
        self.analyzer = analyzer
        self.use_ai = use_ai

    def parse(self, html: str, url: str, model_name: str, title: str = "") -> list["LeasingOffer"]:
        """Разбирает обычную страницу с помощью базового и AI-парсинга."""
        basic = parse_page_basic(html, model_name)

        ai_result = None
        if self.use_ai and self.analyzer:
            ai_result = self.analyzer.analyze_content(html)

        merged = dict(basic)
        if ai_result:
            for key, value in ai_result.items():
                if value is not None:
                    merged[key] = value

        if not merged:
            return []

        domain = urlparse(url).netloc.replace("www.", "")
        visible_text = extract_visible_text(html)[:5000] if html else ""
        offer = create_offer_from_merged(
            title=title or "Offer",
            url=url,
            domain=domain,
            model_name=model_name,
            merged=merged,
            text=visible_text,
        )

        if offer and offer.has_data():
            return [offer]
        return []
