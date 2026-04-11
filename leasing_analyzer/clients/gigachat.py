from __future__ import annotations

import json
import time
import uuid
from typing import Optional

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from leasing_analyzer.core.config import CONFIG
from leasing_analyzer.core.logging import get_logger
from leasing_analyzer.core.rate_limit import RateLimiter
from leasing_analyzer.core.sessions import get_http_session
from leasing_analyzer.core.utils import safe_json_loads

logger = get_logger(__name__)



class GigaChatClient:
    """Клиент для GigaChat API с управлением токеном и логикой повторов."""

    def __init__(self, auth_data: str):
        self.auth_data = auth_data
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0

    def _get_token(self) -> Optional[str]:
        """Получает или обновляет токен доступа."""
        now = time.time()

        if self._access_token and now < self._token_expires_at:
            return self._access_token

        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "RqUID": str(uuid.uuid4()),
            "Authorization": f"Basic {self.auth_data}",
        }

        payload = {"scope": "GIGACHAT_API_PERS"}

        try:
            resp = get_http_session().post(
                CONFIG.gigachat_oauth_url,
                headers=headers,
                data=payload,
                verify=False,
                timeout=CONFIG.http_timeout,
            )
            resp.raise_for_status()

            data = resp.json()

            self._access_token = data["access_token"]
            self._token_expires_at = data.get("expires_at", 0) / 1000 or (now + 1700)

            return self._access_token

        except requests.RequestException as exc:
            logger.error(f"GigaChat auth error: {exc}")
            return None

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type(requests.RequestException),
    )
    def chat(
        self,
        system_prompt: str,
        user_content: str,
        temperature: float = 0.1,
        max_tokens: int = 500,
    ) -> Optional[dict]:
        """Отправляет chat-запрос в GigaChat и возвращает разобранный JSON."""

        token = self._get_token()
        if not token:
            return None

        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        }

        payload = {
            "model": CONFIG.gigachat_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        max_retries = 3
        base_delay = 5

        for attempt in range(max_retries):
            try:
                gigachat_rate_limiter.wait_if_needed()

                resp = get_http_session().post(
                    CONFIG.gigachat_api_url,
                    headers=headers,
                    json=payload,
                    verify=False,
                    timeout=CONFIG.http_long_timeout,
                )

                # 🔴 Rate limit handling
                if resp.status_code == 429:
                    retry_after = resp.headers.get("Retry-After")
                    delay = int(retry_after) if retry_after and retry_after.isdigit() else base_delay * (2 ** attempt)

                    logger.warning(f"GigaChat 429 → sleep {delay}s")
                    time.sleep(delay)
                    continue

                resp.raise_for_status()

                data = resp.json()
                content = data["choices"][0]["message"]["content"]

                return safe_json_loads(content)

            except requests.HTTPError as exc:
                logger.error(f"GigaChat HTTP error: {exc}")

                if attempt == max_retries - 1:
                    raise

            except requests.RequestException as exc:
                logger.error(f"GigaChat request error: {exc}")

                if attempt == max_retries - 1:
                    raise

                time.sleep(base_delay * (2 ** attempt))

            except (KeyError, IndexError) as exc:
                logger.error(f"GigaChat response parse error: {exc}")
                return None

        return None
    
google_rate_limiter = RateLimiter(CONFIG.google_rate_limit_calls, CONFIG.google_rate_limit_period)
gigachat_rate_limiter = RateLimiter(
    CONFIG.gigachat_rate_limit_calls, 
    CONFIG.gigachat_rate_limit_period,
    min_delay=CONFIG.gigachat_min_delay
)
sonar_rate_limiter = RateLimiter(
    CONFIG.sonar_rate_limit_calls,
    CONFIG.sonar_rate_limit_period,
    min_delay=CONFIG.sonar_min_delay
)
