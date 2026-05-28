from __future__ import annotations

from functools import lru_cache

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def build_retry_session(*, total_retries: int, pool_connections: int, pool_maxsize: int, allowed_methods: tuple[str, ...] = ("GET", "POST"), status_forcelist: tuple[int, ...] = (429, 500, 502, 503, 504)) -> requests.Session:
    session = requests.Session()
    retry_strategy = Retry(total=total_retries, backoff_factor=1, status_forcelist=list(status_forcelist), allowed_methods=list(allowed_methods))
    adapter = HTTPAdapter(pool_connections=pool_connections, pool_maxsize=pool_maxsize, max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def build_no_retry_session(*, pool_connections: int, pool_maxsize: int) -> requests.Session:
    session = requests.Session()
    adapter = HTTPAdapter(pool_connections=pool_connections, pool_maxsize=pool_maxsize, max_retries=0)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


@lru_cache(maxsize=1)
def get_http_session() -> requests.Session:
    """Общая сессия для HTTP-вызовов с пулом соединений и повторами."""
    return build_retry_session(
        total_retries=3,
        pool_connections=20,
        pool_maxsize=20,
    )


@lru_cache(maxsize=1)
def get_sonar_session() -> requests.Session:
    """Отдельная сессия для Sonar без повторов на уровне адаптера.

    Клиент Sonar уже реализует собственную стратегию повторов и логику
    запасных сценариев, поэтому транспортные повторы только засоряют диагностику.
    """
    return build_no_retry_session(
        pool_connections=10,
        pool_maxsize=10,
    )


@lru_cache(maxsize=1)
def get_gigachat_session() -> requests.Session:
    """Отдельная сессия для GigaChat без транспортных автоповторов.

    Клиент GigaChat сам управляет backoff и повторными попытками. Повторы на уровне
    urllib3 скрывают реальное время ожидания и многократно раздувают один timeout.
    """
    return build_no_retry_session(
        pool_connections=10,
        pool_maxsize=10,
    )
