from __future__ import annotations

import os
import shutil
import time
import threading
from typing import Optional

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.common.exceptions import TimeoutException

# Импортируем авто-менеджер для скачивания ChromeDriver
from selenium.webdriver.chrome.service import Service as ChromeService
from webdriver_manager.chrome import ChromeDriverManager

from leasing_analyzer.core.config import CONFIG
from leasing_analyzer.core.logging import get_logger
from leasing_analyzer.core.utils import is_valid_url

logger = get_logger(__name__)

class SeleniumFetcher:
    """Загрузчик страниц на базе Selenium с ленивой инициализацией и автовосстановлением."""
    
    def __init__(self):
        self._local = threading.local()
        self._drivers = []
        self._drivers_lock = threading.Lock()
        self._options: Optional[Options] = None
        self._max_restart_attempts = 3

    @property
    def driver(self) -> Optional[webdriver.Chrome]:
        return getattr(self._local, "driver", None)

    @driver.setter
    def driver(self, value: Optional[webdriver.Chrome]):
        self._local.driver = value
        if value is not None:
            with self._drivers_lock:
                if value not in self._drivers:
                    self._drivers.append(value)

    @staticmethod
    def _resolve_binary(env_name: str, candidates: list[str]) -> Optional[str]:
        configured_path = os.getenv(env_name)
        if configured_path:
            if os.path.exists(configured_path):
                return configured_path
            logger.warning("%s is set but does not exist: %s", env_name, configured_path)

        for candidate in candidates:
            resolved_path = shutil.which(candidate)
            if resolved_path:
                return resolved_path

        return None
    
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
            self._options.page_load_strategy = "eager"
            
            chrome_bin = self._resolve_binary(
                "CHROME_BIN",
                ["chromium", "chromium-browser", "google-chrome", "chrome"],
            )
            if chrome_bin:
                self._options.binary_location = chrome_bin
                logger.info("chromium binary resolved: %s", chrome_bin)
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
        if self.driver:
            self._close_single_driver(self.driver)
            with self._drivers_lock:
                if self.driver in self._drivers:
                    self._drivers.remove(self.driver)
        time.sleep(2)  # Даем ChromeDriver полностью завершиться
        self.driver = None  # Принудительно создадим новый экземпляр
    
    def _get_driver(self) -> webdriver.Chrome:
        """Возвращает существующий ChromeDriver или создает новый с авто-загрузкой."""
        if self.driver and self._is_driver_alive():
            return self.driver
        
        # Драйвер умер или отсутствует, создаем новый
        if self.driver:
            logger.warning("Driver is not responsive, recreating...")
            self._close_single_driver(self.driver)
            with self._drivers_lock:
                if self.driver in self._drivers:
                    self._drivers.remove(self.driver)
            self.driver = None
        
        try:
            # Сначала проверяем, задан ли CHROMEDRIVER_PATH вручную в окружении
            driver_bin = self._resolve_binary("CHROMEDRIVER_PATH", ["chromedriver"])
            
            if driver_bin:
                logger.info("Using manually resolved chromedriver: %s", driver_bin)
                service_obj = Service(driver_bin)
            else:
                # Если в системе пути нет, webdriver-manager скачает нужный драйвер сам в фоне
                logger.info("Chromedriver not found locally. Resolving via webdriver-manager...")
                service_obj = ChromeService(ChromeDriverManager().install())
            
            self.driver = webdriver.Chrome(service=service_obj, options=self._get_options())
            
            # Настраиваем таймауты
            self.driver.set_page_load_timeout(CONFIG.page_load_timeout)
            self.driver.implicitly_wait(CONFIG.implicit_wait)
            self.driver.set_script_timeout(CONFIG.script_timeout)
            logger.info("chromium loaded")
            logger.info("chromedriver ready")
            return self.driver
        except Exception as e:
            logger.error(f"Failed to create Chrome driver: {e}")
            self.driver = None
            raise

    def close(self):
        """Закрывает все драйверы и освобождает ресурсы."""
        with self._drivers_lock:
            for d in self._drivers:
                self._close_single_driver(d)
            self._drivers.clear()
        # Reset local driver as well to be safe
        self.driver = None

    def _close_single_driver(self, driver: webdriver.Chrome):
        if not driver:
            return
        service = getattr(driver, "service", None)
        process = getattr(service, "process", None)
        service_alive = bool(process is not None and process.poll() is None)
        try:
            if service_alive and getattr(driver, "session_id", None):
                driver.quit()
            elif service and hasattr(service, "stop"):
                service.stop()
        except Exception as e:
            logger.debug(f"Error closing driver: {e}")

    def capture_screenshot(
        self,
        url: str,
        viewport_width: int = 1280,
        viewport_height: int = 900,
        max_height: int = 2400,
        scroll_times: int = 2,
        wait: float = 1.0,
    ) -> Optional[bytes]:
        """Снимает скриншот страницы через Selenium и возвращает PNG-байты."""
        if not is_valid_url(url):
            logger.warning(f"Invalid URL for screenshot: {url}")
            return None

        for attempt in range(self._max_restart_attempts):
            try:
                driver = self._get_driver()
                driver.set_page_load_timeout(CONFIG.page_load_timeout)
                driver.get(url)
                time.sleep(wait)

                for _ in range(max(0, scroll_times)):
                    try:
                        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                        time.sleep(wait)
                    except Exception as scroll_err:
                        logger.debug(f"Screenshot scroll failed for {url}: {scroll_err}")
                        break

                full_height = driver.execute_script(
                    "return Math.max(document.body.scrollHeight, document.documentElement.scrollHeight, document.body.offsetHeight, document.documentElement.offsetHeight, document.documentElement.clientHeight);"
                )
                if not isinstance(full_height, (int, float)) or full_height <= 0:
                    full_height = viewport_height
                full_height = min(int(full_height), max_height)
                driver.set_window_size(viewport_width, max(viewport_height, full_height))
                time.sleep(0.25)
                driver.execute_script("window.scrollTo(0, 0);")
                time.sleep(0.25)
                return driver.get_screenshot_as_png()

            except TimeoutException as e:
                # СПАСАТЕЛЬНАЯ ОПЕРАЦИЯ: Сайт завис, но мы пробуем сфоткать то, что успело отрендериться
                logger.warning(f"Timeout while capturing screenshot for {url} (Attempt {attempt + 1}), trying to force capture fallback...")
                try:
                    if driver:
                        # Пытаемся забрать скриншот текущего состояния страницы прямо сейчас
                        fallback_png = driver.get_screenshot_as_png()
                        logger.info(f"Successfully recovered partial screenshot for {url} despite timeout.")
                        return fallback_png
                except Exception as fallback_err:
                    logger.error(f"Failed to grab partial screenshot on timeout: {fallback_err}")

                # Если даже частичный скриншот сделать не вышло, перезапускаем драйвер по твоей логике
                if attempt < self._max_restart_attempts - 1:
                    self._restart_driver()
                    time.sleep(1)
                    continue
                return None
                
            except Exception as e:
                logger.error(f"Failed to capture screenshot for {url}: {e}")
                if not self._is_driver_alive():
                    logger.warning("Driver died during screenshot capture, restarting...")
                    self._restart_driver()
                    if attempt < self._max_restart_attempts - 1:
                        continue
                return None

        logger.error(f"Failed to capture screenshot for {url} after {self._max_restart_attempts} attempts")
        return None

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
                
                # Пытаемся загрузить страницу с таймаутом
                try:
                    driver.set_page_load_timeout(CONFIG.page_load_timeout)
                    driver.get(url)
                except TimeoutException:
                    # Если загрузка превышает таймаут, пытаемся получить часть контента
                    logger.warning(f"Page load timeout for {url}, trying to get partial content...")
                    try:
                        # Пытаемся все равно получить контент
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
                # Скроллим страницу с защитой от зависания
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
                                    break # Прерываем цикл скролла и повторяем загрузку
                            else:
                                break # Просто выходим из скролла
                except Exception as scroll_err:
                    logger.debug(f"Scroll failed for {url}: {scroll_err}")
                    # Проверяем, жив ли драйвер
                    if not self._is_driver_alive():
                        logger.warning("Driver died during scroll, restarting...")
                        self._restart_driver()
                        if attempt < self._max_restart_attempts - 1:
                            continue
                    # Все равно продолжаем: часть контента уже могла загрузиться
                
                # html успешно загружен
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
                # Проверка ошибок соединения
                if any(keyword in error_str for keyword in [
                    "connection", "winerror 10061", "refused", 
                    "newconnectionerror", "max retries exceeded"
                ]):
                    logger.warning(f"Connection error loading {url}: {e}")
                    if attempt < self._max_restart_attempts - 1:
                        self._restart_driver()
                        time.sleep(2)
                        continue
                    return None
                else:
                    logger.error(f"Failed to load {url}: {e}")
                    if attempt < self._max_restart_attempts - 1:
                        # 1 перезапуск для прочих ошибок
                        self._restart_driver()
                        time.sleep(1)
                        continue
                    return None
        
        # Попытки исчерпаны
        logger.error(f"Failed to load {url} after {self._max_restart_attempts} attempts")
        return None