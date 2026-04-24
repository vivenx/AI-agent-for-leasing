import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import requests

from leasing_analyzer.clients.gigachat import GigaChatClient
from leasing_analyzer.core.sessions import get_gigachat_session


class GigaChatClientTests(unittest.TestCase):
    def test_gigachat_session_disables_transport_retries(self) -> None:
        get_gigachat_session.cache_clear()
        session = get_gigachat_session()
        retries = session.adapters["https://"].max_retries
        self.assertEqual(retries.total, 0)

    def test_chat_retries_only_explicit_attempt_count(self) -> None:
        client = GigaChatClient("test-auth")
        fake_session = MagicMock()
        fake_session.post.side_effect = requests.ReadTimeout("slow response")
        fake_limiter = MagicMock()
        fake_limiter.wait_if_needed.return_value = 0.0
        fake_config = SimpleNamespace(
            gigachat_model="GigaChat-2",
            gigachat_api_url="https://gigachat.test/api/v1/chat/completions",
            gigachat_request_timeout=7,
            gigachat_request_attempts=2,
            gigachat_retry_base_delay=0.0,
        )

        with patch.object(client, "_get_token", return_value="token"):
            with patch("leasing_analyzer.clients.gigachat.get_gigachat_session", return_value=fake_session):
                with patch("leasing_analyzer.clients.gigachat.gigachat_rate_limiter", fake_limiter):
                    with patch("leasing_analyzer.clients.gigachat.CONFIG", fake_config):
                        with self.assertRaises(requests.RequestException):
                            client.chat("system", "user", action_name="test.timeout")

        self.assertEqual(fake_session.post.call_count, 2)


if __name__ == "__main__":
    unittest.main()
