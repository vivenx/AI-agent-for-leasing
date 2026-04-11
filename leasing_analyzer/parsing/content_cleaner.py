from __future__ import annotations

from bs4 import BeautifulSoup

from leasing_analyzer.core.config import CONFIG


class ContentCleaner:
    """Очищает HTML-контент для последующей обработки AI."""
    
    TAGS_TO_REMOVE = ["script", "style", "nav", "footer", "header", "iframe", "noscript", "aside"]
    
    def clean(self, html_content: str, max_length: int = CONFIG.max_content_length) -> str:
        """Удаляет служебные теги и извлекает текст."""
        if not html_content:
            return ""
        soup = BeautifulSoup(html_content, "html.parser")
        for tag in soup(self.TAGS_TO_REMOVE):
            tag.decompose()
        return soup.get_text(separator=" ", strip=True)[:max_length]
