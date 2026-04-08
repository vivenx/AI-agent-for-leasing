from __future__ import annotations

import time
from typing import Optional

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import TimeoutException

from leasing_analyzer.core.config import CONFIG
from leasing_analyzer.core.logging import get_logger
from leasing_analyzer.core.utils import is_valid_url

logger = get_logger(__name__)

class SeleniumFetcher:
    """Selenium-based page fetcher with lazy initialization and auto-recovery."""
    
    def __init__(self):
        self.driver: Optional[webdriver.Chrome] = None
        self._options: Optional[Options] = None
        self._max_restart_attempts = 3
    
    def _get_options(self) -> Options:
        """Get Chrome options (lazy initialization)."""
        if self._options is None:
            self._options = Options()
            self._options.add_argument("--headless=new")
            self._options.add_argument("--disable-gpu")
            self._options.add_argument("--no-sandbox")
            self._options.add_argument("--window-size=1920,1080")
            self._options.add_argument("--log-level=3")
            self._options.add_argument("--disable-logging")
            self._options.add_argument("--disable-dev-shm-usage")
            self._options.add_experimental_option("excludeSwitches", ["enable-logging"])
            self._options.add_argument(
            "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
        )
        return self._options

    def _is_driver_alive(self) -> bool:
        """Check if driver is still responsive."""
        if not self.driver:
            return False
        try:
            service = getattr(self.driver, "service", None)
            process = getattr(service, "process", None)
            if process is not None and process.poll() is not None:
                return False
            return bool(getattr(self.driver, "session_id", None))
        except Exception:
            return False
    
    def _restart_driver(self):
        """Restart driver after connection error."""
        logger.warning("Restarting Chrome driver due to connection issues...")
        self.close()
        time.sleep(2)  # Give ChromeDriver time to fully close
        self.driver = None  # Force recreation
    
    def _get_driver(self) -> webdriver.Chrome:
        """Get or create Chrome driver with health check."""
        if self.driver and self._is_driver_alive():
            return self.driver
        
        # Driver is dead or doesn't exist, create new one
        if self.driver:
            logger.warning("Driver is not responsive, recreating...")
            self.close()
        
        try:
            self.driver = webdriver.Chrome(options=self._get_options())
            # Set timeouts
            self.driver.set_page_load_timeout(CONFIG.page_load_timeout)
            self.driver.implicitly_wait(CONFIG.implicit_wait)
            self.driver.set_script_timeout(CONFIG.script_timeout)
            logger.debug("Chrome driver created successfully")
            return self.driver
        except Exception as e:
            logger.error(f"Failed to create Chrome driver: {e}")
            self.driver = None
            raise

    def close(self):
        """Close driver and release resources."""
        if self.driver:
            service = getattr(self.driver, "service", None)
            process = getattr(service, "process", None)
            service_alive = bool(process is not None and process.poll() is None)
            try:
                if service_alive and getattr(self.driver, "session_id", None):
                    self.driver.quit()
                elif service and hasattr(service, "stop"):
                    service.stop()
            except Exception as e:
                logger.debug(f"Error closing driver: {e}")
            finally:
                self.driver = None

    def fetch_page(
        self,
        url: str,
        scroll_times: int = CONFIG.default_scroll_times,
        wait: float = CONFIG.scroll_wait
    ) -> Optional[str]:
        """Fetch page with scrolling to load dynamic content, with auto-recovery."""
        if not is_valid_url(url):
            logger.warning(f"Invalid URL: {url}")
            return None
        
        for attempt in range(self._max_restart_attempts):
            try:
                driver = self._get_driver()
                
                # Try to load page with timeout
                try:
                    driver.set_page_load_timeout(CONFIG.page_load_timeout)
                    driver.get(url)
                except TimeoutException:
                    # If page load times out, try to get what we have
                    logger.warning(f"Page load timeout for {url}, trying to get partial content...")
                    try:
                        # Try to get page source anyway
                        return driver.page_source
                    except Exception as e:
                        logger.debug(f"Could not get partial content: {e}")
                        # Check if driver is still alive
                        if not self._is_driver_alive():
                            logger.warning("Driver died after timeout, restarting...")
                            self._restart_driver()
                            if attempt < self._max_restart_attempts - 1:
                                continue
                        return None
                
                # Scroll with timeout protection
                try:
                    last_height = driver.execute_script("return document.body.scrollHeight")
                    
                    for _ in range(max(0, scroll_times)):
                        try:
                            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                            time.sleep(wait)
                            new_height = driver.execute_script("return document.body.scrollHeight")
                            if new_height == last_height:
                                break
                            last_height = new_height
                        except Exception as scroll_err:
                            logger.debug(f"Scroll error for {url}: {scroll_err}")
                            # Check if driver is still alive
                            if not self._is_driver_alive():
                                logger.warning("Driver died during scroll, restarting...")
                                self._restart_driver()
                                if attempt < self._max_restart_attempts - 1:
                                    break  # Break scroll loop, will retry fetch
                            else:
                                break  # Just break scroll loop, continue with page source
                except Exception as scroll_err:
                    logger.debug(f"Scroll failed for {url}: {scroll_err}")
                    # Check if driver is still alive
                    if not self._is_driver_alive():
                        logger.warning("Driver died during scroll, restarting...")
                        self._restart_driver()
                        if attempt < self._max_restart_attempts - 1:
                            continue
                    # Continue anyway, we might have some content
                
                # Successfully got page source
                try:
                    return driver.page_source
                except Exception as e:
                    logger.debug(f"Could not get page source: {e}")
                    if not self._is_driver_alive():
                        logger.warning("Driver died when getting page source, restarting...")
                        self._restart_driver()
                        if attempt < self._max_restart_attempts - 1:
                            continue
                    return None
                    
            except TimeoutException as e:
                logger.warning(f"Timeout loading {url}: {e}")
                if attempt < self._max_restart_attempts - 1:
                    self._restart_driver()
                    time.sleep(1)
                    continue
                return None
            except Exception as e:
                error_str = str(e).lower()
                # Check for connection errors
                if any(keyword in error_str for keyword in [
                    "connection", "winerror 10061", "refused", 
                    "newconnectionerror", "max retries exceeded"
                ]):
                    logger.warning(f"Connection error loading {url}: {e}")
                    if attempt < self._max_restart_attempts - 1:
                        self._restart_driver()
                        time.sleep(2)  # Wait longer for connection issues
                        continue
                    return None
                else:
                    logger.error(f"Failed to load {url}: {e}")
                    if attempt < self._max_restart_attempts - 1:
                        # For other errors, still try restart once
                        self._restart_driver()
                        time.sleep(1)
                        continue
                    return None
        
        # All attempts failed
        logger.error(f"Failed to load {url} after {self._max_restart_attempts} attempts")
        return None