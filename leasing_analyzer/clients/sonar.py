from __future__ import annotations

import json
import os
import time
from typing import Optional

import requests

from leasing_analyzer.core.config import CONFIG
from leasing_analyzer.core.logging import get_logger
from leasing_analyzer.core.models import SonarAnalogResult, SonarComparisonResult
from leasing_analyzer.core.rate_limit import sonar_rate_limiter
from leasing_analyzer.core.sessions import get_sonar_session
from leasing_analyzer.core.utils import (
    describe_price_difference,
    ensure_list_str,
    format_price,
    safe_json_loads,
)

logger = get_logger(__name__)

_sonar_finder: Optional["SonarAnalogFinder"] = None
_sonar_cache: dict[str, list] = {}


class SonarAnalogFinder:
    """
    Находит аналоги через Perplexity Sonar API.
    Оптимизирован для минимального расхода токенов.
    """
    
    # Улучшенный промпт для поиска аналогов
    JSON_ONLY_SYSTEM_PROMPT = (
        "You are a JSON API. Return exactly one valid JSON object and nothing else. "
        "Do not use markdown. Do not explain anything outside JSON. "
        "If information is insufficient, still return the requested JSON shape with conservative values."
    )

    ANALOG_PROMPT = """Используй данные из поисковых результатов для поиска РОВНО 3 лучших конкурентных аналога для: {item}

Требования к аналогам:
1. Тот же тип продукта (если авто - то авто того же класса, если техника - то аналогичная техника)
2. Сопоставимый ценовой сегмент (+-30%)
3. Доступны для покупки в России в 2024-2025 году
4. Популярные и проверенные модели с хорошими отзывами

Для каждого аналога укажи:
- Точное название (производитель + модель)
- Реальный диапазон цен в рублях (на основе данных из поисковых результатов)
- Главное отличие от оригинала

ВАЖНО: Используй информацию из предоставленных поисковых результатов. Если информации недостаточно, используй свои знания о рынке.

КРИТИЧЕСКИ ВАЖНО: 
- Верни ТОЛЬКО валидный JSON, без дополнительного текста до или после
- Не добавляй объяснений, комментариев или markdown разметки (включая ```json)
- Формат ответа должен быть строго JSON объект, начинающийся с {{ и заканчивающийся }}

{{"analogs": [
  {{"name": "Производитель Модель", "price_range": "X-Y млн руб", "key_diff": "главное отличие"}},
  {{"name": "Производитель Модель", "price_range": "X-Y млн руб", "key_diff": "главное отличие"}},
  {{"name": "Производитель Модель", "price_range": "X-Y млн руб", "key_diff": "главное отличие"}}
]}}"""

    # Улучшенный промпт для сравнения объявлений
    COMPARE_PROMPT = """Используй данные из поисковых результатов для сравнения двух объявлений:

ОРИГИНАЛ - {original_name}:
- Объявление: {original_title}
- Цена: {original_price}
- Ссылка: {original_url}

АНАЛОГ - {analog_name}:
- Объявление: {analog_title}
- Цена: {analog_price}
- Ссылка: {analog_url}

Проанализируй оба варианта по следующим критериям:
- Цена и соотношение цена/качество
- Технические характеристики и комплектация
- Надежность и репутация бренда
- Доступность запчастей и сервиса в России
- Условия покупки и лизинга

ВАЖНО: Используй информацию из предоставленных поисковых результатов для обоих автомобилей. Если информации о конкретном объявлении недостаточно, используй общие данные о модели из поисковых результатов.

КРИТИЧЕСКИ ВАЖНО:
- Верни ТОЛЬКО валидный JSON, без дополнительного текста до или после
- Не добавляй объяснений, комментариев или markdown разметки (включая ```json)
- Формат ответа должен быть строго JSON объект, начинающийся с {{ и заканчивающийся }}
- Если информации недостаточно, все равно верни JSON с доступными данными

{{"winner": "original" или "analog" или "tie",
"orig_pros": ["конкретный плюс 1", "конкретный плюс 2"],
"orig_cons": ["конкретный минус 1"],
"analog_pros": ["конкретный плюс 1", "конкретный плюс 2"],
"analog_cons": ["конкретный минус 1"],
"price_diff": "Аналог дешевле/дороже на X% или примерно равно",
"verdict": "Четкая рекомендация что выбрать и почему (2-3 предложения)"}}"""

    FIND_BEST_OFFER_PROMPT = """Проанализируй список объявлений и выбери лучшее по соотношению цена/качество для лизинга.

Объявления:
{offers_list}

Критерии выбора:
1. Цена и соответствие рынку
2. Состояние и технические характеристики
3. Полнота и достоверность информации
4. Надежность источника объявления
5. Ликвидность и практичность для рынка РФ

Верни только JSON:
{{"best_index": 0, "best_score": 8.5, "reason": "почему это объявление лучше", "ranking": [
  {{"index": 0, "score": 8.5, "brief_reason": "краткая причина"}},
  {{"index": 1, "score": 7.3, "brief_reason": "краткая причина"}}
]}}"""

    VALIDATE_MARKET_PRICES_PROMPT = """Оцени рыночные цены для {item_name}.

Данные:
- Минимальная цена: {min_price}
- Максимальная цена: {max_price}
- Медианная цена: {median_price}
- Средняя цена: {mean_price}
- Цена клиента: {client_price}
- Количество объявлений: {offers_count}

Проверь:
1. Адекватность диапазона цен
2. Наличие аномалий
3. Насколько цена клиента соответствует рынку

Верни только JSON:
{{"is_valid": true, "explanation": "объяснение", "anomalies": ["аномалия 1"], "client_price_verdict": "fair"}}"""

    ENRICH_OFFER_PROMPT = """Извлеки и структурируй данные объявления.

Название: {title}
Цена: {price}
Описание: {description}

Верни только JSON:
{{"vendor": "бренд", "model": "модель", "year": 2024, "condition": "новый", "specs": {{"key": "value"}}, "pros": ["плюс"], "cons": ["минус"]}}"""

    def __init__(self):
        self.api_key = CONFIG.perplexity_api_key
        
        # Определяем URL API: если указан base_url (прокси), используем его, иначе прямой API
        # Также проверяем ключ: если начинается с sk-, вероятно это прокси
        base_url = CONFIG.sonar_base_url
        
        # Если base_url не указан, но ключ начинается с sk-, предполагаем прокси artemox
        if not base_url and self.api_key and self.api_key.startswith("sk-"):
            base_url = "https://api.artemox.com/v1"
            logger.info("[SONAR] Detected proxy API (artemox.com) based on key format")
        
        if base_url:
            # Прокси через artemox.com или другой сервис
            self.api_url = f"{base_url.rstrip('/')}/chat/completions"
            # Для прокси пробуем разные модели (некоторые прокси не поддерживают sonar-reasoning)
            # Список моделей для попытки в порядке приоритета
            user_model = os.getenv("PERPLEXITY_MODEL")
            # Если пользователь не указал модель, используем sonar-reasoning-pro по умолчанию (как в примере)
            if not user_model:
                user_model = "sonar-reasoning-pro"
            
            self.model_candidates = [
                user_model,  # Пользовательская модель из .env или "sonar-reasoning-pro" по умолчанию
                "sonar",  # Fallback на sonar (более легкая модель)
                "gpt-4",  # Fallback на GPT-4
                "gpt-3.5-turbo"  # Последний fallback
            ]
            # Убираем дубликаты и None из списка
            seen = set()
            self.model_candidates = [m for m in self.model_candidates if m and m not in seen and not seen.add(m)]
            # Начинаем с первой доступной модели
            self.model = self.model_candidates[0] if self.model_candidates else "sonar-reasoning-pro"
            self.current_model_index = 0
            logger.info(f"[SONAR] Using proxy API: {self.api_url} with model: {self.model}")
            logger.info(f"[SONAR] Available model fallbacks: {', '.join(self.model_candidates)}")
        else:
            # Прямой Perplexity API
            self.api_url = CONFIG.sonar_api_url
            self.model = CONFIG.sonar_model
            logger.info(f"[SONAR] Using direct Perplexity API: {self.api_url} with model: {self.model}")
        
    def is_available(self) -> bool:
        """Check if Sonar API is available."""
        # Accept keys starting with 'pplx-' (Perplexity) or 'sk-' (proxy services like artemox)
        return bool(self.api_key and (self.api_key.startswith("pplx-") or self.api_key.startswith("sk-")))
    
    def _call_sonar(
        self,
        prompt: str,
        max_tokens: int = 400,
        retries: int = 2,
        return_raw_on_parse_failure: bool = False
    ) -> Optional[dict]:
        """
        Make a call to Sonar API with minimal tokens.
        Includes retry logic for 500 errors and timeouts.
        """
        if not self.api_key:
            return None
        
        # Увеличиваем таймаут для прокси (они могут быть медленнее)
        # В примере используется timeout=600 (10 минут), но для нашего случая 300 секунд (5 минут) должно быть достаточно
        timeout = 300 if "artemox.com" in self.api_url else CONFIG.http_long_timeout
        
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        
        # Формируем payload в зависимости от типа API
        # Для прокси (artemox.com) используем МИНИМАЛЬНЫЙ набор параметров (как в примере)
        if "artemox.com" in self.api_url or hasattr(self, 'model_candidates'):
            # Минимальный payload для прокси - ТОЛЬКО model и messages (как в примере)
            # НЕ добавляем max_tokens и temperature, т.к. прокси может их не поддерживать
            payload = {
                "model": self.model,  # Используем текущую модель (может быть изменена при ошибке)
                "messages": [
                    {"role": "system", "content": self.JSON_ONLY_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt}
                ]
            }
            # НЕ добавляем дополнительные параметры для прокси - используем минимальный формат
        else:
            # Для прямого Perplexity API полный набор параметров
            payload = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": self.JSON_ONLY_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt}
                ],
                "max_tokens": max_tokens,
                "temperature": 0.1,
                "return_citations": False,
                "return_images": False
            }
        
        # Retry логика для 500 ошибок и таймаутов
        for attempt in range(retries + 1):
            try:
                sonar_rate_limiter.wait_if_needed()
                
                # Логируем детали запроса для отладки (только для прокси)
                if "artemox.com" in self.api_url and attempt == 0:
                    logger.info(f"[SONAR] Sending request to {self.api_url}")
                    logger.info(f"[SONAR] Model: {self.model}")
                    logger.debug(f"[SONAR] Payload: {json.dumps(payload, ensure_ascii=False)[:300]}...")
                
                # Используем отдельную сессию для Sonar без автоматических retry
                response = get_sonar_session().post(
                    self.api_url,
                    headers=headers,
                    json=payload,
                    timeout=timeout
                )
                
                # Логируем ответ для отладки
                if response.status_code != 200:
                    try:
                        error_body = response.text[:200]  # Первые 200 символов ошибки
                        logger.warning(f"[SONAR] Response status {response.status_code}: {error_body}")
                    except:
                        pass
                
                # Handle 401 Unauthorized - invalid or missing API key
                if response.status_code == 401:
                    logger.error("[SONAR] 401 Unauthorized - Invalid or missing PERPLEXITY_API_KEY")
                    logger.error("[SONAR] Please check your .env file and ensure PERPLEXITY_API_KEY is set correctly")
                    logger.error("[SONAR] Key should start with 'pplx-' or 'sk-' (without quotes)")
                    return None
                
                # Handle 500 errors - может быть из-за неподдерживаемой модели
                if response.status_code == 500:
                    # Проверяем, не связана ли ошибка с моделью
                    try:
                        error_data = response.json()
                        error_msg = str(error_data.get("error", {}).get("message", "")).lower()
                        # Если ошибка связана с моделью, пробуем другую
                        if "model" in error_msg or "guardrail" in error_msg or "dissalowed" in error_msg:
                            if hasattr(self, 'model_candidates') and self.current_model_index < len(self.model_candidates) - 1:
                                self.current_model_index += 1
                                self.model = self.model_candidates[self.current_model_index]
                                payload["model"] = self.model
                                logger.warning(f"[SONAR] Model not allowed, switching to: {self.model}")
                                continue
                    except:
                        pass
                    
                    if attempt < retries:
                        wait_time = (attempt + 1) * 3  # 3, 6, 9 секунд
                        logger.warning(f"[SONAR] 500 Internal Server Error from {self.api_url}")
                        logger.warning(f"[SONAR] This usually means the proxy server is overloaded or having issues")
                        logger.warning(f"[SONAR] Retrying in {wait_time}s (attempt {attempt + 1}/{retries + 1})...")
                        time.sleep(wait_time)
                        continue
                    else:
                        logger.error(f"[SONAR] 500 error persisted after {retries + 1} attempts")
                        logger.error(f"[SONAR] Proxy server {self.api_url} may be temporarily unavailable")
                        logger.error("[SONAR] System will use fallback methods (GigaChat/Google)")
                        return None
                
                response.raise_for_status()
                
                data = response.json()
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                
                # Логируем сырой ответ для отладки (первые 500 символов)
                if not content:
                    logger.warning(f"[SONAR] Empty content in API response. Full response keys: {list(data.keys())}")
                    logger.debug(f"[SONAR] Full response structure: {str(data)[:500]}")
                    return None
                
                logger.debug(f"[SONAR] Received content length: {len(content)} chars")
                logger.debug(f"[SONAR] Content preview: {content[:200]}...")
                
                # Parse JSON from response
                parsed = safe_json_loads(content)
                if not parsed:
                    logger.warning(f"[SONAR] Failed to parse JSON from content. Content preview: {content[:500]}")
                    if return_raw_on_parse_failure:
                        return {"_raw_content": content}
                else:
                    logger.debug(f"[SONAR] Successfully parsed JSON. Keys: {list(parsed.keys()) if isinstance(parsed, dict) else 'not a dict'}")
                
                return parsed
                
            except requests.Timeout as e:
                if attempt < retries:
                    wait_time = (attempt + 1) * 2
                    logger.warning(f"[SONAR] Timeout, retrying in {wait_time}s (attempt {attempt + 1}/{retries + 1})...")
                    time.sleep(wait_time)
                    continue
                else:
                    logger.error(f"[SONAR] Timeout after {retries + 1} attempts: {e}")
                    return None
            except requests.HTTPError as e:
                if e.response and e.response.status_code == 401:
                    logger.error("[SONAR] Authentication failed - check PERPLEXITY_API_KEY in .env file")
                    return None
                if e.response and e.response.status_code == 500:
                    # Проверяем, не связана ли ошибка с моделью
                    try:
                        error_data = e.response.json()
                        error_msg = str(error_data.get("error", {}).get("message", "")).lower()
                        if ("model" in error_msg or "guardrail" in error_msg or "dissalowed" in error_msg) and \
                           hasattr(self, 'model_candidates') and self.current_model_index < len(self.model_candidates) - 1:
                            self.current_model_index += 1
                            self.model = self.model_candidates[self.current_model_index]
                            payload["model"] = self.model
                            logger.warning(f"[SONAR] Model error detected, switching to: {self.model}")
                            continue
                    except:
                        pass
                    
                    if attempt < retries:
                        wait_time = (attempt + 1) * 3
                        logger.warning(f"[SONAR] HTTP 500 error, retrying in {wait_time}s...")
                        time.sleep(wait_time)
                        continue
                logger.warning(f"[SONAR] HTTP error: {e}")
                return None
            except requests.RequestException as e:
                # Обрабатываем ResponseError от HTTPAdapter (too many 500 errors)
                error_str = str(e).lower()
                is_retryable = (
                    "500" in error_str or 
                    "timeout" in error_str or 
                    "too many" in error_str or
                    "connection" in error_str
                )
                
                if attempt < retries and is_retryable:
                    wait_time = (attempt + 1) * 3  # Увеличиваем задержку для 500 ошибок
                    logger.warning(f"[SONAR] Request error (retryable), retrying in {wait_time}s: {e}")
                    time.sleep(wait_time)
                    continue
                else:
                    logger.warning(f"[SONAR] Request error (non-retryable or max retries): {e}")
                    if "too many 500" in error_str:
                        logger.error("[SONAR] Proxy server is returning too many 500 errors")
                        logger.error("[SONAR] This may indicate server overload or temporary unavailability")
                        logger.error("[SONAR] Will use fallback methods (GigaChat/Google)")
                    return None
            except Exception as e:
                logger.warning(f"[SONAR] Parse error: {e}")
                return None
        
        return None

    def _build_fallback_comparison(
        self,
        original_name: str,
        original_offer: dict,
        analog_name: str,
        analog_offer: dict,
        raw_content: str = ""
    ) -> dict:
        """Convert a non-JSON Sonar answer into a usable structured comparison."""
        orig_price = original_offer.get("price")
        analog_price = analog_offer.get("price")
        price_diff = describe_price_difference(orig_price, analog_price)

        orig_pros: list[str] = []
        orig_cons: list[str] = []
        analog_pros: list[str] = []
        analog_cons: list[str] = []
        winner = "tie"

        if orig_price and analog_price:
            cheaper_is_original = orig_price < analog_price
            diff_ratio = abs(orig_price - analog_price) / max(min(orig_price, analog_price), 1)
            if diff_ratio >= 0.05:
                winner = "original" if cheaper_is_original else "analog"
                if cheaper_is_original:
                    orig_pros.append("Ниже цена объявления")
                    analog_cons.append("Выше цена объявления")
                else:
                    analog_pros.append("Ниже цена объявления")
                    orig_cons.append("Выше цена объявления")

        insufficiency_markers = (
            "cannot complete",
            "do not contain any information",
            "would need search results",
            "insufficient",
            "not contain information",
        )
        if raw_content and any(marker in raw_content.lower() for marker in insufficiency_markers):
            note = "Недостаточно подтвержденных данных в поисковой выдаче Sonar для полного сравнения."
        else:
            note = "Sonar вернул ответ вне JSON, поэтому использована безопасная локальная деградация."

        return {
            "winner": winner,
            "orig_pros": orig_pros,
            "orig_cons": orig_cons,
            "analog_pros": analog_pros,
            "analog_cons": analog_cons,
            "price_diff": price_diff,
            "verdict": (
                f"{note} Сравнение построено по доступным полям объявлений "
                f"{original_name} и {analog_name} без неподтвержденных допущений."
            ),
        }
    
    def find_analogs(self, item_name: str) -> list[SonarAnalogResult]:
        """
        Найти РОВНО 3 аналога через Sonar.
        Возвращает список из 3 аналогов с описанием.
        """
        if not self.is_available():
            logger.warning("[SONAR] API not available - PERPLEXITY_API_KEY not set or invalid format")
            logger.warning("[SONAR] Key should start with 'pplx-' or 'sk-'")
            return []
        
        logger.info(f"[SONAR] Searching for 3 analogs for: {item_name}")
        
        try:
            prompt = self.ANALOG_PROMPT.format(item=item_name)
            # Увеличиваем max_tokens для поиска аналогов (нужно больше контекста)
            result = self._call_sonar(prompt, max_tokens=800, retries=3)
            
            if not result:
                logger.warning("[SONAR] Failed to get analogs from API response - _call_sonar returned None")
                logger.warning("[SONAR] Possible reasons: timeout, 500 error, invalid API key, or JSON parse error")
                logger.warning("[SONAR] Will use fallback methods (GigaChat/Google)")
                return []
            
            if "analogs" not in result:
                logger.warning(f"[SONAR] API response doesn't contain 'analogs' key")
                logger.warning(f"[SONAR] Response keys: {list(result.keys()) if isinstance(result, dict) else 'not a dict'}")
                logger.warning(f"[SONAR] Response preview: {str(result)[:500]}")
                logger.warning("[SONAR] Will use fallback methods (GigaChat/Google)")
                return []
        except Exception as e:
            logger.error(f"[SONAR] Exception during analog search: {e}")
            logger.info("[SONAR] Falling back to GigaChat/Google methods")
            return []
        
        raw_analogs = result.get("analogs", [])
        if not raw_analogs:
            logger.warning("[SONAR] No analogs in API response")
            return []
        
        # Обрабатываем результаты
        analogs = []
        for a in raw_analogs[:3]:  # Берем максимум 3
            name = a.get("name", "").strip()
            if name:  # Только если есть название
                analogs.append({
                    "name": name,
                    "description": a.get("key_diff", "") or a.get("description", ""),
                    "price_range": a.get("price_range", ""),
                    "key_difference": a.get("key_diff", "") or a.get("key_difference", "")
                })
        
        # Если получили меньше 3, логируем предупреждение
        if len(analogs) < 3:
            logger.warning(f"[SONAR] Got only {len(analogs)} analogs instead of 3")
        
        if analogs:
            logger.info(f"[SONAR] Found {len(analogs)} analogs: {', '.join([a['name'] for a in analogs])}")
        else:
            logger.warning("[SONAR] No valid analogs found")
        
        return analogs
    
    def compare_offers(
        self, 
        original_name: str,
        original_offer: dict,
        analog_name: str, 
        analog_offer: dict
    ) -> SonarComparisonResult:
        """
        Сравнить конкретные объявления оригинала и аналога через Sonar.
        Включает ссылки на объявления.
        
        Args:
            original_name: Название оригинальной модели
            original_offer: Dict с данными объявления (title, price, url)
            analog_name: Название аналога
            analog_offer: Dict с данными объявления аналога
        """
        if not self.is_available():
            return {"winner": "unknown", "recommendation": "Sonar unavailable"}
        
        # Извлекаем данные объявлений
        orig_title = original_offer.get("title", original_name)
        orig_price = original_offer.get("price")
        orig_url = original_offer.get("url", "")
        
        analog_title = analog_offer.get("title", analog_name)
        analog_price = analog_offer.get("price")
        analog_url = analog_offer.get("url", "")
        
        logger.info(f"[SONAR] Comparing offers: {orig_title[:50]}... vs {analog_title[:50]}...")
        
        orig_price_str = format_price(orig_price) if orig_price else "цена не указана"
        analog_price_str = format_price(analog_price) if analog_price else "цена не указана"
        
        prompt = self.COMPARE_PROMPT.format(
            original_name=original_name,
            original_title=orig_title[:100],
            original_price=orig_price_str,
            original_url=orig_url,
            analog_name=analog_name,
            analog_title=analog_title[:100],
            analog_price=analog_price_str,
            analog_url=analog_url
        )
        
        # Увеличиваем retries для сравнений (важная операция)
        result = self._call_sonar(
            prompt,
            max_tokens=600,
            retries=3,
            return_raw_on_parse_failure=True
        )

        if result and "_raw_content" in result:
            logger.warning(
                f"[SONAR] Comparison response for {original_name} vs {analog_name} was not JSON; "
                "using structured fallback instead of failing"
            )
            result = self._build_fallback_comparison(
                original_name=original_name,
                original_offer=original_offer,
                analog_name=analog_name,
                analog_offer=analog_offer,
                raw_content=str(result.get("_raw_content", "")),
            )
        
        if not result:
            logger.warning(f"[SONAR] Comparison failed for {original_name} vs {analog_name}")
            return {
                "winner": "unknown", 
                "recommendation": "Сравнение через Sonar не удалось (ошибка API или таймаут)",
                "original_url": orig_url,
                "analog_url": analog_url,
                "original_title": orig_title,
                "analog_title": analog_title
            }
        
        return {
            "winner": result.get("winner", "tie"),
            "original_advantages": ensure_list_str(result.get("orig_pros", [])),
            "original_disadvantages": ensure_list_str(result.get("orig_cons", [])),
            "analog_advantages": ensure_list_str(result.get("analog_pros", [])),
            "analog_disadvantages": ensure_list_str(result.get("analog_cons", [])),
            "recommendation": result.get("verdict", ""),
            "price_diff": result.get("price_diff", ""),
            "price_verdict": result.get("price_diff", "similar"),
            # Включаем ссылки на объявления
            "original_url": orig_url,
            "original_title": orig_title,
            "original_price": orig_price,
            "analog_url": analog_url,
            "analog_title": analog_title,
            "analog_price": analog_price,
            "sonar_comparison": True
        }
    
    def find_best_offer(self, offers: list[dict]) -> Optional[dict]:
        """
        Найти лучшее объявление из списка через Sonar.
        
        Args:
            offers: Список объявлений в формате dict
            
        Returns:
            Dict с best_index, best_score, reason, ranking или None при ошибке
        """
        if not self.is_available():
            return None
        
        if not offers:
            return {"best_index": -1, "best_score": 0.0, "reason": "No offers", "ranking": []}
        
        if len(offers) == 1:
            return {"best_index": 0, "best_score": 8.0, "reason": "Only one offer", "ranking": [{"index": 0, "score": 8.0, "brief_reason": "Single offer"}]}
        
        # Форматируем объявления для промпта
        offers_list = "\n\n".join([
            f"Объявление {i}:\n{json.dumps(offer, ensure_ascii=False, default=str, indent=2)}"
            for i, offer in enumerate(offers, 1)
        ])
        
        prompt = self.FIND_BEST_OFFER_PROMPT.format(offers_list=offers_list)
        
        logger.info(f"[SONAR] Finding best offer from {len(offers)} offers...")
        result = self._call_sonar(prompt, max_tokens=800, retries=2)
        
        if not result:
            logger.warning("[SONAR] Failed to find best offer via Sonar")
            return None
        
        # Проверяем наличие обязательных полей
        if "best_index" not in result:
            logger.warning(f"[SONAR] Invalid response format: missing best_index. Keys: {list(result.keys())}")
            return None
        
        best_index = result.get("best_index", 0)
        if not (0 <= best_index < len(offers)):
            logger.warning(f"[SONAR] Invalid best_index {best_index}, using 0")
            best_index = 0
        
        logger.info(f"[SONAR] Best offer selected: index {best_index}, score {result.get('best_score', 0):.1f}/10")
        return result
    
    def validate_market_prices(
        self,
        item_name: str,
        min_price: Optional[int],
        max_price: Optional[int],
        median_price: Optional[float],
        mean_price: Optional[int],
        client_price: Optional[int],
        offers_count: int
    ) -> Optional[dict]:
        """
        Валидировать и объяснить рыночные цены через Sonar.
        
        Returns:
            Dict с is_valid, explanation, anomalies, client_price_verdict или None
        """
        if not self.is_available():
            return None
        
        min_price_str = format_price(min_price) if min_price else "не указано"
        max_price_str = format_price(max_price) if max_price else "не указано"
        median_price_str = format_price(int(median_price)) if median_price else "не указано"
        mean_price_str = format_price(mean_price) if mean_price else "не указано"
        client_price_str = format_price(client_price) if client_price else "не указано"
        
        prompt = self.VALIDATE_MARKET_PRICES_PROMPT.format(
            item_name=item_name,
            min_price=min_price_str,
            max_price=max_price_str,
            median_price=median_price_str,
            mean_price=mean_price_str,
            client_price=client_price_str,
            offers_count=offers_count
        )
        
        logger.info(f"[SONAR] Validating market prices for {item_name}...")
        result = self._call_sonar(prompt, max_tokens=500, retries=2)
        
        if not result:
            logger.warning("[SONAR] Failed to validate market prices via Sonar")
            return None
        
        return result
    
    def enrich_offer_data(self, title: str, price: Optional[int], description: str = "") -> Optional[dict]:
        """
        Обогатить данные объявления через Sonar (извлечь vendor, model, year, specs и т.д.).
        
        Returns:
            Dict с vendor, model, year, condition, specs, pros, cons или None
        """
        if not self.is_available():
            return None
        
        price_str = format_price(price) if price else "не указана"
        desc = description[:500] if description else ""  # Ограничиваем длину
        
        prompt = self.ENRICH_OFFER_PROMPT.format(
            title=title[:200],
            price=price_str,
            description=desc
        )
        
        logger.debug(f"[SONAR] Enriching offer data for: {title[:50]}...")
        result = self._call_sonar(prompt, max_tokens=400, retries=1)
        
        if not result:
            logger.debug("[SONAR] Failed to enrich offer data via Sonar")
            return None
        
        return result
    
def get_sonar_finder() -> Optional[SonarAnalogFinder]:
    """Get or create Sonar finder instance."""
    global _sonar_finder
    if _sonar_finder is None:
        _sonar_finder = SonarAnalogFinder()
    return _sonar_finder if _sonar_finder.is_available() else None


def get_cached_sonar_analogs(item_name: str) -> Optional[list]:
    """Get cached Sonar analogs if available."""
    cache_key = item_name.lower().strip()
    return _sonar_cache.get(f"analogs_{cache_key}")


def cache_sonar_analogs(item_name: str, analogs: list) -> None:
    """Cache Sonar analogs for the item."""
    cache_key = item_name.lower().strip()
    _sonar_cache[f"analogs_{cache_key}"] = analogs


def clear_sonar_cache() -> None:
    """Clear Sonar cache."""
    global _sonar_cache
    _sonar_cache = {}