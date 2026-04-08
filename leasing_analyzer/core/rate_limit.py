from __future__ import annotations

import time
from collections import deque
from threading import Lock

from leasing_analyzer.core.config import CONFIG
from leasing_analyzer.core.logging import get_logger


logger = get_logger(__name__)


class RateLimiter:
    """Thread-safe rate limiter to prevent API throttling."""
    
    def __init__(self, max_calls: int, period: float, min_delay: float = 0.0):
        self.calls = deque()
        self.max_calls = max_calls
        self.period = period
        self.min_delay = min_delay
        self.last_call_time = 0.0
        self._lock = Lock()  # Thread safety
    
    def wait_if_needed(self):
        """Wait if rate limit would be exceeded (thread-safe)."""
        with self._lock:
            now = time.time()
            
            # Enforce minimum delay between requests
            if self.min_delay > 0 and self.last_call_time > 0:
                time_since_last = now - self.last_call_time
                if time_since_last < self.min_delay:
                    sleep_time = self.min_delay - time_since_last
                    logger.debug(f"Min delay: waiting {sleep_time:.2f}s")
                    time.sleep(sleep_time)
                    now = time.time()
            
            # Remove old calls outside the period
            while self.calls and self.calls[0] < now - self.period:
                self.calls.popleft()
            
            # If at limit, wait until oldest call expires
            if len(self.calls) >= self.max_calls:
                sleep_time = self.period - (now - self.calls[0])
                if sleep_time > 0:
                    logger.debug(f"Rate limit: waiting {sleep_time:.1f}s")
                    time.sleep(sleep_time)
                    now = time.time()
                    # Re-check after sleep
                    while self.calls and self.calls[0] < now - self.period:
                        self.calls.popleft()
            
            self.calls.append(now)
            self.last_call_time = now


google_rate_limiter = RateLimiter(
    CONFIG.google_rate_limit_calls,
    CONFIG.google_rate_limit_period,
)
gigachat_rate_limiter = RateLimiter(
    CONFIG.gigachat_rate_limit_calls,
    CONFIG.gigachat_rate_limit_period,
    min_delay=CONFIG.gigachat_min_delay,
)
sonar_rate_limiter = RateLimiter(
    CONFIG.sonar_rate_limit_calls,
    CONFIG.sonar_rate_limit_period,
    min_delay=CONFIG.sonar_min_delay,
)
