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
    """Shared session for HTTP calls with connection pooling and retries."""
    return build_retry_session(
        total_retries=3,
        pool_connections=20,
        pool_maxsize=20,
    )


@lru_cache(maxsize=1)
def get_sonar_session() -> requests.Session:
    """Dedicated Sonar session without adapter-level retries.

    Sonar client already implements its own retry strategy and fallback logic,
    so transport-level retries only make diagnostics noisier.
    """
    return build_no_retry_session(
        pool_connections=10,
        pool_maxsize=10,
    )
