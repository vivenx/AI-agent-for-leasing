from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse

from leasing_analyzer.clients.ai_analyzer import AIAnalyzer
from leasing_analyzer.core.config import CONFIG
from leasing_analyzer.core.logging import get_logger

from leasing_analyzer.parsing.content_cleaner import ContentCleaner

from leasing_analyzer.services.fetcher import SeleniumFetcher
from leasing_analyzer.services.search import filter_available_results, search_google


logger = get_logger(__name__)


def search_specs_sites(item_name: str, num_sites: int = 5) -> list[dict]:
    """Ищет сайты с техническими характеристиками по целевым запросам."""
    if not CONFIG.serper_api_key:
        return []
    
    # Формируем целевые поисковые запросы для характеристик
    queries = [
        f"{item_name} характеристики",
        f"{item_name} технические характеристики",
        f"{item_name} specs характеристики",
        f"{item_name} полные характеристики",
    ]
    
    all_results = []
    seen_urls = set()
    
    for query in queries[:2]:  # Используем только первые 2 запроса, чтобы не плодить лишние обращения
        results = search_google(query, num_results=num_sites)
        for result in results:
            url = result.get("link", "")
            if url and url not in seen_urls:
                # Фильтруем сайты, связанные с характеристиками
                domain = urlparse(url).netloc.lower()
                # Отдаем приоритет известным сайтам с характеристиками
                specs_keywords = ["характеристики", "specs", "технические", "обзор", "комплектация"]
                snippet = (result.get("snippet", "") + " " + result.get("title", "")).lower()
                
                if any(keyword in snippet for keyword in specs_keywords) or any(keyword in domain for keyword in ["auto", "car", "tech", "spec"]):
                    seen_urls.add(url)
                    all_results.append(result)
                    if len(all_results) >= num_sites:
                        break
        
        if len(all_results) >= num_sites:
            break
    
    all_results = filter_available_results(all_results[:num_sites])
    logger.info(f"Found {len(all_results)} reachable specs sites for {item_name}")
    return all_results[:num_sites]


def extract_specs_from_multiple_sites(
    item_name: str,
    fetcher: "SeleniumFetcher",
    analyzer: Optional[AIAnalyzer],
    num_sites: int = 5
) -> dict:
    """Извлекает технические характеристики, анализируя несколько специализированных сайтов."""
    if not analyzer:
        logger.warning("AI analyzer not available for specs extraction")
        return {}
    
    # Ищем сайты с характеристиками
    specs_sites = search_specs_sites(item_name, num_sites)
    if not specs_sites:
        logger.warning(f"No specs sites found for {item_name}")
        return {}
    
    all_specs = {}
    successful_extractions = 0
    
    logger.info(f"Analyzing {len(specs_sites)} sites for {item_name} specifications...")
    
    for idx, site in enumerate(specs_sites, 1):
        url = site.get("link", "")
        title = site.get("title", "")
        
        if not url:
            continue
        
        try:
            # Загружаем содержимое страницы
            html = fetcher.fetch_page(url, scroll_times=1, wait=1.0)
            if not html:
                logger.debug(f"Failed to fetch {url}")
                continue
            
            # Очищаем HTML и извлекаем текст
            cleaner = ContentCleaner()
            text = cleaner.clean(html)
            
            if len(text) < 100:
                logger.debug(f"Insufficient content from {url}")
                continue
            
            # Извлекаем характеристики через AI
            specs = analyzer.extract_specs_from_text(text)
            
            if specs:
                # Объединяем характеристики: более поздние источники могут дополнять предыдущие
                for key, value in specs.items():
                    if value and (key not in all_specs or not all_specs[key]):
                        all_specs[key] = value
                
                successful_extractions += 1
                logger.debug(f"Extracted {len(specs)} specs from {title[:50]}...")
            
        except Exception as e:
            logger.warning(f"Error extracting specs from {url}: {e}")
            continue
    
    if all_specs:
        logger.info(f"Successfully extracted {len(all_specs)} specifications from {successful_extractions} sites")
    else:
        logger.warning(f"Failed to extract specs from any site for {item_name}")
    
    return all_specs
