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
    """Загрузчик страниц на базе Selenium с ленивой инициализацией и автовосстановлением."""
    
    def __init__(self):
        self.driver: Optional[webdriver.Chrome] = None
        self._options: Optional[Options] = None
        self._max_restart_attempts = 3
    
    def _get_options(self) -> Options:
        """Возвращает настройки Chrome с ленивой инициализацией."""
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
        """Проверяет, отвечает ли драйвер."""
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
        """Перезапускает драйвер после ошибки соединения."""
        logger.warning("Restarting Chrome driver due to connection issues...")
        self.close()
        time.sleep(2)  # Даем ChromeDriver полностью завершиться
        self.driver = None  # Принудительно создадим новый экземпляр
    
    def _get_driver(self) -> webdriver.Chrome:
        """Возвращает существующий ChromeDriver или создает новый после проверки состояния."""
        if self.driver and self._is_driver_alive():
            return self.driver
        
        # Драйвер умер или отсутствует, создаем новый
        if self.driver:
            logger.warning("Driver is not responsive, recreating...")
            self.close()
        
        try:
            self.driver = webdriver.Chrome(options=self._get_options())
            # Настраиваем таймауты
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
        """Закрывает драйвер и освобождает ресурсы."""
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
        """Загружает страницу со скроллом для динамического контента и автовосстановлением."""
        if not is_valid_url(url):
            logger.warning(f"Invalid URL: {url}")
            return None
        
        for attempt in range(self._max_restart_attempts):
            try:
                driver = self._get_driver()
                
                # Пытаемся загрузить страницу с таймаутом
                try:
                    driver.set_page_load_timeout(CONFIG.page_load_timeout)
                    driver.get(url)
                except TimeoutException:
                    # Если загрузка превысила таймаут, пытаемся взять частичный контент
                    logger.warning(f"Page load timeout for {url}, trying to get partial content...")
                    try:
                        # Пытаемся все равно получить HTML страницы
                        return driver.page_source
                    except Exception as e:
                        logger.debug(f"Could not get partial content: {e}")
                        # Проверяем, жив ли драйвер
                        if not self._is_driver_alive():
                            logger.warning("Driver died after timeout, restarting...")
                            self._restart_driver()
                            if attempt < self._max_restart_attempts - 1:
                                continue
                        return None
                
                # Скроллим страницу с защитой от зависаний
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
                            # Проверяем, жив ли драйвер
                            if not self._is_driver_alive():
                                logger.warning("Driver died during scroll, restarting...")
                                self._restart_driver()
                                if attempt < self._max_restart_attempts - 1:
                                    break  # Прерываем цикл скролла и повторяем загрузку
                            else:
                                break  # Просто выходим из скролла и продолжаем с текущим HTML
                except Exception as scroll_err:
                    logger.debug(f"Scroll failed for {url}: {scroll_err}")
                    # Проверяем, жив ли драйвер
                    if not self._is_driver_alive():
                        logger.warning("Driver died during scroll, restarting...")
                        self._restart_driver()
                        if attempt < self._max_restart_attempts - 1:
                            continue
                    # Все равно продолжаем: часть контента уже могла загрузиться
                
                # HTML успешно получен
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
                # Проверяем ошибки соединения
                if any(keyword in error_str for keyword in [
                    "connection", "winerror 10061", "refused", 
                    "newconnectionerror", "max retries exceeded"
                ]):
                    logger.warning(f"Connection error loading {url}: {e}")
                    if attempt < self._max_restart_attempts - 1:
                        self._restart_driver()
                        time.sleep(2)  # Для проблем соединения ждем чуть дольше
                        continue
                    return None
                else:
                    logger.error(f"Failed to load {url}: {e}")
                    if attempt < self._max_restart_attempts - 1:
                        # Для прочих ошибок тоже пробуем один перезапуск
                        self._restart_driver()
                        time.sleep(1)
                        continue
                    return None
        
        # Все попытки исчерпаны
        logger.error(f"Failed to load {url} after {self._max_restart_attempts} attempts")
        return None
