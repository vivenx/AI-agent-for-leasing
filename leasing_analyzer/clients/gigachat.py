from __future__ import annotations

import time
import uuid
from typing import Optional

import requests

from leasing_analyzer.core.config import CONFIG
from leasing_analyzer.core.logging import get_logger
from leasing_analyzer.core.rate_limit import gigachat_rate_limiter
from leasing_analyzer.core.sessions import get_gigachat_session
from leasing_analyzer.core.utils import safe_json_loads

logger = get_logger(__name__)


class GigaChatClient:
    """Client for GigaChat API with explicit rate limiting and retries."""

    def __init__(self, auth_data: str):
        self.auth_data = auth_data
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0

    @staticmethod
    def _parse_retry_after(response: requests.Response) -> Optional[float]:
        retry_after = response.headers.get("Retry-After")
        if not retry_after:
            return None
        try:
            value = float(retry_after)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    def _get_token(self) -> Optional[str]:
        """Fetches or reuses the current access token."""
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
            response = get_gigachat_session().post(
                CONFIG.gigachat_oauth_url,
                headers=headers,
                data=payload,
                verify=False,
                timeout=CONFIG.http_timeout,
            )
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as exc:
            logger.error("GigaChat auth error: %s", exc)
            return None

        self._access_token = data["access_token"]
        self._token_expires_at = data.get("expires_at", 0) / 1000 or (now + 1700)
        return self._access_token

    def chat(
        self,
        system_prompt: str,
        user_content: str,
        temperature: float = 0.1,
        max_tokens: int = 500,
        action_name: str = "gigachat.chat",
    ) -> Optional[dict]:
        """Sends a chat request and returns parsed JSON if possible."""

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

        max_attempts = max(1, CONFIG.gigachat_request_attempts)
        base_delay = max(0.0, CONFIG.gigachat_retry_base_delay)

        logger.info(
            "[LLM] action=%s request model=%s system_chars=%s user_chars=%s temperature=%.2f max_tokens=%s",
            action_name,
            CONFIG.gigachat_model,
            len(system_prompt or ""),
            len(user_content or ""),
            temperature,
            max_tokens,
        )
        logger.info(
            "[GIGACHAT] request_config action=%s attempts=%s timeout=%ss base_delay=%.1fs",
            action_name,
            max_attempts,
            CONFIG.gigachat_request_timeout,
            base_delay,
        )

        for attempt in range(max_attempts):
            try:
                waited = gigachat_rate_limiter.wait_if_needed()
                if waited >= 1.0:
                    logger.info("[GIGACHAT] cooldown before request: %.1fs", waited)

                response = get_gigachat_session().post(
                    CONFIG.gigachat_api_url,
                    headers=headers,
                    json=payload,
                    verify=False,
                    timeout=CONFIG.gigachat_request_timeout,
                )

                if response.status_code == 429:
                    delay = self._parse_retry_after(response) or (base_delay * (2**attempt))
                    if attempt == max_attempts - 1:
                        logger.error(
                            "GigaChat 429 persisted for %s after %s attempts",
                            action_name,
                            max_attempts,
                        )
                        return None
                    logger.warning("GigaChat 429 for %s, retrying in %.1fs", action_name, delay)
                    time.sleep(delay)
                    continue

                response.raise_for_status()

                data = response.json()
                content = data["choices"][0]["message"]["content"]
                parsed = safe_json_loads(content)

                if parsed is None:
                    logger.warning(
                        "[LLM] action=%s parse_failed attempt=%s response_chars=%s",
                        action_name,
                        attempt + 1,
                        len(content or ""),
                    )
                else:
                    logger.info(
                        "[LLM] action=%s parsed_json attempt=%s keys=%s",
                        action_name,
                        attempt + 1,
                        ",".join(sorted(parsed.keys())) or "-",
                    )

                return parsed

            except requests.HTTPError as exc:
                logger.error("GigaChat HTTP error for %s: %s", action_name, exc)
                if attempt == max_attempts - 1:
                    raise

            except requests.RequestException as exc:
                logger.error("GigaChat request error for %s: %s", action_name, exc)
                if attempt == max_attempts - 1:
                    raise

            except (KeyError, IndexError) as exc:
                logger.error("GigaChat response parse error for %s: %s", action_name, exc)
                return None

            delay = base_delay * (2**attempt)
            if delay > 0:
                time.sleep(delay)

        return None
