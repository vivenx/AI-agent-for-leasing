from __future__ import annotations

import os
import shutil
import time
import threading
import queue
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
    """Загрузчик страниц на базе Selenium с пулом потокобезопасных драйверов и автовосстановлением."""
    
    def __init__(self):
        self._drivers_pool = queue.Queue()
        self._all_drivers = []
        self._drivers_lock = threading.Lock()
        self._options: Optional[Options] = None
        self._max_restart_attempts = 3

    @staticmethod
    def _resolve_binary(env_name: str, candidates: list[str]) -> Optional[str]:
        configured_path = os.getenv(env_name)
        if configured_path:
            if os.path.exists(configured_path):
                return configured_path
            logger.warning("%s задан, но не существует: %s", env_name, configured_path)

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
            self._options.add_argument("--disable-software-rasterizer")
            self._options.add_argument("--disable-extensions")
            self._options.add_argument("--disable-blink-features=AutomationControlled")
            self._options.add_experimental_option("excludeSwitches", ["enable-logging", "enable-automation"])
            self._options.add_experimental_option("useAutomationExtension", False)
            self._options.page_load_strategy = "eager"
            
            chrome_bin = self._resolve_binary(
                "CHROME_BIN",
                ["chromium", "chromium-browser", "google-chrome", "chrome"],
            )
            if chrome_bin:
                self._options.binary_location = chrome_bin
                logger.info("Путь к chromium определен: %s", chrome_bin)
            self._options.add_argument(
                "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
            )
        return self._options

    def _is_driver_alive(self, driver: webdriver.Chrome) -> bool:
        """Проверяет, отвечает ли драйвер."""
        if not driver:
            return False
        try:
            service = getattr(driver, "service", None)
            process = getattr(service, "process", None)
            if process is not None and process.poll() is not None:
                return False
            return bool(getattr(driver, "session_id", None))
        except Exception:
            return False
            
    def _create_driver(self) -> webdriver.Chrome:
        """Создает новый ChromeDriver с авто-загрузкой."""
        try:
            driver_bin = self._resolve_binary("CHROMEDRIVER_PATH", ["chromedriver"])
            
            if driver_bin:
                logger.info("Используется chromedriver по ручному пути: %s", driver_bin)
                service_obj = Service(driver_bin)
            else:
                logger.info("Chromedriver не найден локально. Загружаем через webdriver-manager...")
                service_obj = ChromeService(ChromeDriverManager().install())
            
            driver = webdriver.Chrome(service=service_obj, options=self._get_options())
            
            driver.set_page_load_timeout(CONFIG.page_load_timeout)
            driver.implicitly_wait(CONFIG.implicit_wait)
            driver.set_script_timeout(CONFIG.script_timeout)
            logger.info("Chromium загружен")
            logger.info("Chromedriver готов к работе")
            
            with self._drivers_lock:
                self._all_drivers.append(driver)
            return driver
        except Exception as e:
            logger.error(f"Не удалось создать Chrome драйвер: {e}")
            raise

    def _acquire_driver(self) -> webdriver.Chrome:
        """Берет драйвер из пула или создает новый."""
        try:
            while True:
                driver = self._drivers_pool.get_nowait()
                if self._is_driver_alive(driver):
                    return driver
                else:
                    self._discard_driver(driver)
        except queue.Empty:
            return self._create_driver()

    def _release_driver(self, driver: webdriver.Chrome):
        """Возвращает драйвер в пул."""
        if driver and self._is_driver_alive(driver):
            self._drivers_pool.put(driver)
        else:
            self._discard_driver(driver)

    def _discard_driver(self, driver: webdriver.Chrome):
        """Уничтожает сломанный драйвер."""
        if driver:
            self._close_single_driver(driver)
            with self._drivers_lock:
                if driver in self._all_drivers:
                    self._all_drivers.remove(driver)

    def close(self):
        """Закрывает все драйверы и освобождает ресурсы."""
        with self._drivers_lock:
            for d in self._all_drivers:
                self._close_single_driver(d)
            self._all_drivers.clear()
        while not self._drivers_pool.empty():
            try:
                self._drivers_pool.get_nowait()
            except queue.Empty:
                break

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
            logger.debug(f"Ошибка при закрытии драйвера: {e}")

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
            logger.warning(f"Некорректный URL для скриншота: {url}")
            return None

        for attempt in range(self._max_restart_attempts):
            driver = None
            try:
                driver = self._acquire_driver()
                driver.set_page_load_timeout(CONFIG.page_load_timeout)
                driver.get(url)
                time.sleep(wait)

                for _ in range(max(0, scroll_times)):
                    try:
                        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                        time.sleep(wait)
                    except Exception as scroll_err:
                        logger.debug(f"Ошибка скроллинга при скриншоте {url}: {scroll_err}")
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
                
                png = driver.get_screenshot_as_png()
                self._release_driver(driver)
                return png

            except TimeoutException as e:
                logger.warning(f"Таймаут при создании скриншота для {url} (Попытка {attempt + 1}), пробуем сделать резервный снимок...")
                try:
                    if driver:
                        fallback_png = driver.get_screenshot_as_png()
                        logger.info(f"Успешно получен частичный скриншот для {url} несмотря на таймаут.")
                        self._release_driver(driver)
                        return fallback_png
                except Exception as fallback_err:
                    logger.error(f"Не удалось получить частичный скриншот при таймауте: {fallback_err}")

                self._discard_driver(driver)
                if attempt < self._max_restart_attempts - 1:
                    time.sleep(1)
                    continue
                return None
                
            except Exception as e:
                logger.error(f"Не удалось сделать скриншот для {url}: {e}")
                self._discard_driver(driver)
                if attempt < self._max_restart_attempts - 1:
                    time.sleep(1)
                    continue
                return None

        logger.error(f"Не удалось сделать скриншот для {url} после {self._max_restart_attempts} попыток")
        return None

    def fetch_page(
        self,
        url: str,
        scroll_times: int = CONFIG.default_scroll_times,
        wait: float = CONFIG.scroll_wait
    ) -> Optional[str]:
        """Загружает страницу со скроллом для динамического контента и автовосстановлением."""
        if not is_valid_url(url):
            logger.warning(f"Некорректный URL: {url}")
            return None
        
        for attempt in range(self._max_restart_attempts):
            driver = None
            try:
                driver = self._acquire_driver()
                
                # Пытаемся загрузить страницу с таймаутом
                try:
                    driver.set_page_load_timeout(CONFIG.page_load_timeout)
                    driver.get(url)
                except TimeoutException:
                    logger.warning(f"Таймаут загрузки страницы {url}, пробуем получить частичный контент...")
                    try:
                        html = driver.page_source
                        self._release_driver(driver)
                        return html
                    except Exception as e:
                        logger.debug(f"Не удалось получить частичный контент: {e}")
                        self._discard_driver(driver)
                        if attempt < self._max_restart_attempts - 1:
                            time.sleep(1)
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
                            logger.debug(f"Ошибка скроллинга для {url}: {scroll_err}")
                            break
                except Exception as scroll_err:
                    logger.debug(f"Скроллинг завершился неудачей для {url}: {scroll_err}")
                
                # html успешно загружен
                try:
                    html = driver.page_source
                    self._release_driver(driver)
                    return html
                except Exception as e:
                    logger.debug(f"Не удалось получить исходный код страницы: {e}")
                    self._discard_driver(driver)
                    if attempt < self._max_restart_attempts - 1:
                        time.sleep(1)
                        continue
                    return None
                    
            except TimeoutException as e:
                logger.warning(f"Таймаут при загрузке {url}: {e}")
                self._discard_driver(driver)
                if attempt < self._max_restart_attempts - 1:
                    time.sleep(1)
                    continue
                return None
            except Exception as e:
                error_str = str(e).lower()
                self._discard_driver(driver)
                if any(keyword in error_str for keyword in [
                    "connection", "winerror 10061", "refused", 
                    "newconnectionerror", "max retries exceeded",
                    "tab crashed", "session deleted"
                ]):
                    logger.warning(f"Ошибка соединения при загрузке {url}: {e}")
                    if attempt < self._max_restart_attempts - 1:
                        time.sleep(2)
                        continue
                else:
                    logger.error(f"Не удалось загрузить {url}: {e}")
                    if attempt < self._max_restart_attempts - 1:
                        time.sleep(1)
                        continue
                return None
        
        # Попытки исчерпаны
        logger.error(f"Не удалось загрузить {url} после {self._max_restart_attempts} попыток")
        return None