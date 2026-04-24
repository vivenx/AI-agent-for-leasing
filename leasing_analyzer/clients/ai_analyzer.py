from __future__ import annotations

from datetime import datetime
import json
from typing import Optional

import requests

from leasing_analyzer.clients.gigachat import GigaChatClient
from leasing_analyzer.core.audit import AgentAuditTrail
from leasing_analyzer.core.config import CONFIG
from leasing_analyzer.core.logging import get_logger
from leasing_analyzer.core.models import AIAnalysisResult, AnalogReview, ValidationResult
from leasing_analyzer.core.utils import ensure_list_str, is_valid_url, normalize_whitespace
from leasing_analyzer.parsing.content_cleaner import ContentCleaner

logger = get_logger(__name__)


class AIAnalyzer:
    """AI-анализ с использованием GigaChat."""
    
    ANALYSIS_PROMPT = """Ты аналитик рынка лизинга авто. По тексту объявления заполни поля и верни только JSON.
Требуется:
1) Категория (Category).
2) Бренд и модель.
3) 3–5 ключевых характеристик (Specs) — тип двигателя/привода, пробег, мощность/л.с., состояние и пр.
4) Плюсы (Pros) и минусы/оговорки (Cons).
5) Если в тексте упомянуты аналоги или конкуренты (например, "как Volvo ..."), добавь их в analogs_mentioned.

Структура ответа:
{
  "category": "string (например: 'легковые автомобили', 'коммерческий транспорт')",
  "vendor": "string (производитель, например: 'Volvo', 'BMW')",
  "model": "string (модель, например: 'XC60', 'X5 M')",
  "price": int (цена в валюте, null если нет или "по запросу"),
  "currency": "string (RUB, USD, EUR)",
  "monthly_payment": int (платёж в месяц, null если нет),
  "year": int (год выпуска),
  "condition": "string (новый / б/у / не указан)",
  "location": "string (город/регион)",
  "specs": {
    "характеристика_1": "значение",
    "характеристика_2": "значение",
    "характеристика_3": "значение"
  },
  "pros": ["плюс 1", "плюс 2"],
  "cons": ["минус 1", "минус 2"],
  "analogs_mentioned": ["аналог 1", "аналог 2"]
}

ПРИМЕРЫ specs для разных типов:
- Автомобиль: {"двигатель": "2.0 л 150 л.с.", "пробег": "50000 км", "привод": "полный"}
- Экскаватор: {"ковш": "1.2 м³", "глубина_копания": "6.5 м", "мощность": "120 кВт"}
- Станок ЧПУ: {"точность": "0.01 мм", "рабочая_зона": "800x600x500", "шпиндель": "24000 об/мин"}
- Сервер: {"процессор": "2x Xeon Gold 6248R", "RAM": "256 GB", "диски": "8x 1.92TB SSD"}
- Трактор: {"мощность": "240 л.с.", "тип": "колесный", "количество_передач": "16"}

Отвечай ТОЛЬКО валидным JSON без markdown и комментариев."""

    ANALOGS_PROMPT = """Ты эксперт по промышленному оборудованию, технике и лизинговому рынку. Подбери РЕАЛЬНЫЕ конкурентные аналоги для указанного актива.

УНИВЕРСАЛЬНЫЕ КРИТЕРИИ ПОДБОРА:
1. Тот же ТИП и НАЗНАЧЕНИЕ (не предлагай трактор вместо экскаватора)
2. Тот же КЛАСС и СЕГМЕНТ (премиум к премиуму, промышленный к промышленному)
3. Схожий ЦЕНОВОЙ ДИАПАЗОН (+-30% от оригинала)
4. Сравнимые ХАРАКТЕРИСТИКИ (производительность, мощность, размер)
5. Доступность на российском рынке
6. Актуальные модели (предпочтительно текущего поколения)

ПРИМЕРЫ ПО КАТЕГОРИЯМ:

АВТОМОБИЛИ:
- BMW X5 → Audi Q7, Mercedes GLE, Volvo XC90, Porsche Cayenne
- Toyota Camry → Kia K5, Hyundai Sonata, Skoda Superb

КОММЕРЧЕСКИЙ ТРАНСПОРТ:
- Mercedes Sprinter → Ford Transit, Fiat Ducato, Iveco Daily, ГАЗель NEXT
- Volvo FH → Scania R-series, MAN TGX, DAF XF

СПЕЦТЕХНИКА:
- Caterpillar 320 → Komatsu PC210, Hitachi ZX200, Volvo EC210
- JCB 3CX → Caterpillar 428, Case 580, Terex 860

СЕЛЬХОЗТЕХНИКА:
- John Deere 6M → Case IH Puma, New Holland T6, Fendt 700 Vario
- Claas Lexion → John Deere S-series, Case IH Axial-Flow

СТАНКИ И ОБОРУДОВАНИЕ:
- DMG MORI NLX 2500 → Mazak Quick Turn, Okuma LB3000, Haas ST-30
- Trumpf TruLaser → Bystronic ByStar, Prima Power, Amada

IT-ОБОРУДОВАНИЕ:
- Dell PowerEdge R750 → HPE ProLiant DL380, Lenovo ThinkSystem SR650, Cisco UCS
- NetApp FAS → Dell EMC PowerStore, HPE Nimble, Pure Storage

МЕДИЦИНСКОЕ ОБОРУДОВАНИЕ:
- Siemens Healthineers → GE Healthcare, Philips Healthcare, Canon Medical

НЕ ВКЛЮЧАЙ:
- Оборудование другого класса или назначения
- Устаревшие снятые с производства модели
- Несопоставимые по масштабу (промышленный vs бытовой)
- Неизвестные сомнительные бренды

Верни JSON: {"analogs": ["Производитель Модель", "Производитель Модель", ...]}
Максимум 5 наиболее релевантных конкурентов."""
    
    REVIEW_PROMPT = """Ты аналитик лизингового рынка с экспертизой в ЛЮБЫХ активах. Проанализируй объявления по аналогу и составь экспертный обзор.

ЗАДАЧА:
1. Оцени рыночную цену аналога по найденным объявлениям
2. Выдели РЕАЛЬНЫЕ преимущества (не маркетинг, только факты и цифры)
3. Укажи РЕАЛЬНЫЕ недостатки и подводные камни
4. Выбери лучшее объявление по соотношению цена/качество

КРИТЕРИИ ОЦЕНКИ:
- Полнота информации: фото, характеристики, история обслуживания, документы
- Адекватность цены: не завышена, не подозрительно низкая
- Надежность продавца: официальный дилер > проверенная компания > частник
- Прозрачность: открытые данные о состоянии, наработке, ремонтах
- Условия: гарантия, возможность лизинга, доставка

ПРИМЕРЫ ХОРОШИХ ПЛЮСОВ/МИНУСОВ:

ХОРОШО (с конкретикой):
- "На 15% дешевле рыночной цены при аналогичном состоянии"
- "Низкий расход топлива 8 л/100км vs 12 л у конкурентов"
- "Широкая сеть сервисных центров в РФ, запчасти доступны"
- "Наработка 2000 моточасов при норме 5000 до капремонта"

ПЛОХО (без конкретики):
- "Хорошая цена" (насколько? по сравнению с чем?)
- "Надежный" (на основе чего? статистика?)
- "Качественный" (какие показатели качества?)

ВЫБОР ЛУЧШЕГО ОБЪЯВЛЕНИЯ:
Приоритет: полнота данных > адекватность цены > надежность продавца

Верни JSON:
{
  "pros": [
    "Конкретное преимущество с цифрами и фактами",
    "Еще преимущество с обоснованием",
    "Третье преимущество"
  ],
  "cons": [
    "Конкретный недостаток с цифрами",
    "Еще недостаток с последствиями",
    "Третий недостаток"
  ],
  "price_hint": 4500000,
  "note": "Краткий вывод на 2-3 предложения: стоит ли рассматривать как альтернативу и ПОЧЕМУ с финансовым обоснованием",
  "best_link": "URL лучшего объявления или null"
}"""
    
    VALIDATION_PROMPT = """Ты финансовый аналитик с экспертизой в оценке ЛЮБЫХ активов для лизинга. Проверь адекватность рыночной оценки.

ТИПИЧНЫЕ ДИАПАЗОНЫ ЦЕН ПО КАТЕГОРИЯМ:

ТРАНСПОРТ:
- Легковые авто эконом: 1-4 млн руб
- Легковые авто премиум: 4-15 млн руб
- Люкс/спорткары: 10-50+ млн руб
- Легкий коммерческий (до 3.5т): 2-6 млн руб
- Грузовики средние: 5-15 млн руб
- Грузовики тяжелые: 8-30+ млн руб

СПЕЦТЕХНИКА:
- Мини-экскаваторы: 2-5 млн руб
- Экскаваторы средние: 5-20 млн руб
- Бульдозеры: 10-40 млн руб
- Автокраны: 15-100+ млн руб
- Погрузчики: 3-15 млн руб

СЕЛЬХОЗТЕХНИКА:
- Тракторы малые: 1-3 млн руб
- Тракторы средние: 3-10 млн руб
- Тракторы мощные: 10-30 млн руб
- Комбайны: 15-60+ млн руб

ПРОИЗВОДСТВЕННОЕ ОБОРУДОВАНИЕ:
- Станки с ЧПУ малые: 2-10 млн руб
- Станки с ЧПУ средние: 10-50 млн руб
- Обрабатывающие центры: 20-200+ млн руб
- Прессы, гибочное оборудование: 5-100 млн руб
- Производственные линии: 50-500+ млн руб

IT-ОБОРУДОВАНИЕ:
- Серверы начальные: 200 тыс - 1 млн руб
- Серверы средние: 1-5 млн руб
- Серверы enterprise: 5-30+ млн руб
- СХД: 2-50+ млн руб
- Сетевое оборудование: 100 тыс - 10 млн руб

МЕДИЦИНСКОЕ ОБОРУДОВАНИЕ:
- УЗИ аппараты: 1-10 млн руб
- Рентген: 5-20 млн руб
- КТ/МРТ: 30-150+ млн руб
- Лабораторное: 500 тыс - 50 млн руб

КРИТЕРИИ ВАЛИДАЦИИ:
1. СООТВЕТСТВИЕ КАТЕГОРИИ: Цена в разумных пределах для типа оборудования
2. РАЗБРОС: Разница между min и max не должна быть более 5x (иначе подозрительно)
3. КОЛИЧЕСТВО ДАННЫХ: Минимум 3 предложения для достоверности
4. АНОМАЛИИ:
   - Цена < 50 000 руб для промышленного оборудования = ПОДОЗРИТЕЛЬНО
   - Цена > 1 млрд руб для стандартной техники = ОШИБКА
   - Все цены идентичны = возможны дубликаты
   - Слишком узкий диапазон (<10% разброс) = мало данных или однотипные источники

ВЕРНИ JSON:
{
  "is_valid": true/false,
  "comment": "Подробное объяснение: почему оценка валидна или что вызывает сомнения",
  "confidence": "high | medium | low",
  "suggestions": "Рекомендации по улучшению оценки (если есть)"
}"""
    
    SPECS_EXTRACTION_PROMPT = """Ты технический эксперт по ЛЮБЫМ типам оборудования и техники. Извлеки ВСЕ технические характеристики из текста.

ПРАВИЛА ИЗВЛЕЧЕНИЯ:
1. Сохраняй ТОЧНЫЕ значения из текста (не округляй, не преобразуй)
2. Используй СТАНДАРТНЫЕ единицы измерения
3. Адаптируй характеристики под ТИП оборудования
4. Если значение диапазон — сохраняй как диапазон ("190-250 л.с.")

===========================================================
ХАРАКТЕРИСТИКИ ПО ТИПАМ ОБОРУДОВАНИЯ:
===========================================================

АВТОМОБИЛИ:
- двигатель: тип, объем, мощность (л.с./кВт)
- привод: передний/задний/полный
- КПП: механика/автомат/робот + передачи
- пробег: км
- расход: л/100км
- габариты: длина/ширина/высота (мм)
- масса: кг

СПЕЦТЕХНИКА (экскаваторы, погрузчики):
- мощность_двигателя: кВт или л.с.
- вместимость_ковша: м³
- глубина_копания: м
- высота_выгрузки: м
- грузоподъемность: кг или тонн
- рабочая_масса: тонн
- тип_ходовой: гусеничная/колесная

СЕЛЬХОЗТЕХНИКА:
- мощность: л.с.
- тип: колесный/гусеничный
- рабочая_ширина: м
- производительность: га/час
- бункер: литры или м³
- количество_цилиндров: шт

СТАНКИ И ОБОРУДОВАНИЕ:
- точность_обработки: мм
- рабочая_зона: мм (X/Y/Z)
- мощность_шпинделя: кВт
- обороты_шпинделя: об/мин
- максимальная_нагрузка: кг
- класс_точности: по стандарту
- количество_осей: шт

IT-ОБОРУДОВАНИЕ:
- процессор: модель, количество ядер, частота
- оперативная_память: GB
- накопители: тип, объем
- сеть: скорость портов (1G/10G/40G)
- энергопотребление: Вт
- форм_фактор: размер (1U, 2U, tower)
- поддержка_виртуализации: да/нет

МЕДИЦИНСКОЕ ОБОРУДОВАНИЕ:
- тип_исследования: что диагностирует
- разрешение: пиксели или линии
- точность: процент или класс
- производительность: пациентов/час
- мощность_излучения: если применимо
- класс_безопасности: медицинский класс"""

    COMPARE_OFFERS_PROMPT = """Ты эксперт по оценке объявлений для лизинга. Сравни два объявления и определи, какое лучше.

Объявление 1:
{offer1}

Объявление 2:
{offer2}

Критерии сравнения:
1. Адекватность цены (соответствие рыночной стоимости)
2. Состояние и характеристики
3. Наличие важных параметров
4. Надежность источника
5. Общее качество предложения

Верни JSON:
{{
  "winner": 1 или 2 (какое объявление лучше),
  "score_1": float от 0 до 10 (оценка первого объявления),
  "score_2": float от 0 до 10 (оценка второго объявления),
  "reason": "краткое объяснение почему выбран победитель",
  "pros_winner": ["плюс 1", "плюс 2"],
  "cons_winner": ["минус 1", "минус 2"],
  "pros_loser": ["плюс 1", "плюс 2"],
  "cons_loser": ["минус 1", "минус 2"]
}}"""

    FIND_BEST_OFFER_PROMPT = """Ты эксперт по оценке объявлений для лизинга. Из списка объявлений найди ЛУЧШЕЕ.

Объявления:
{offers_list}

Критерии выбора лучшего:
1. Адекватность цены (соответствие рыночной стоимости)
2. Состояние и характеристики
3. Полнота информации
4. Надежность источника
5. Общее качество предложения

Верни JSON:
{{
  "best_index": int (индекс лучшего объявления, начиная с 0),
  "best_score": float от 0 до 10,
  "reason": "почему это объявление лучшее",
  "ranking": [
    {{"index": 0, "score": 8.5, "brief_reason": "..."}},
    {{"index": 1, "score": 7.2, "brief_reason": "..."}}
  ]
}}"""

    COMPARE_BEST_OFFERS_PROMPT = """Ты эксперт по лизингу. Проведи ДЕТАЛЬНОЕ СРАВНЕНИЕ лучшего объявления оригинала с лучшим объявлением аналога.

Твоя задача - не просто описать плюсы и минусы, а ПРЯМО СРАВНИТЬ эти два предложения по ключевым критериям:
1. Цена и стоимость владения
2. Технические характеристики и качество
3. Условия лизинга и финансирования
4. Надежность и репутация
5. Соответствие потребностям клиента

Лучшее объявление ОРИГИНАЛА ({original_name}):
{best_original}

Лучшее объявление АНАЛОГА ({analog_name}):
{best_analog}

Проведи ПОСЛЕДОВАТЕЛЬНОЕ сравнение по каждому критерию и вынеси обоснованное решение.

Верни JSON:
{{
  "winner": "original" или "analog",
  "original_score": float от 0 до 10,
  "analog_score": float от 0 до 10,
  "comparison_details": {{
    "price": "детальное сравнение цен и стоимости",
    "quality": "сравнение качества и характеристик",
    "financing": "сравнение условий лизинга",
    "reliability": "сравнение надежности",
    "value": "сравнение соотношения цена/качество"
  }},
  "price_comparison": {{
    "original_price": int,
    "analog_price": int,
    "difference_percent": float,
    "price_verdict": "original_cheaper" | "analog_cheaper" | "similar",
    "monthly_payment_original": int или null,
    "monthly_payment_analog": int или null
  }},
  "pros_original": ["конкретное преимущество оригинала", "еще преимущество"],
  "cons_original": ["конкретный недостаток оригинала", "еще недостаток"],
  "pros_analog": ["конкретное преимущество аналога", "еще преимущество"],
  "cons_analog": ["конкретный недостаток аналога", "еще недостаток"],
  "recommendation": "детальная рекомендация с обоснованием выбора",
  "use_cases_original": ["конкретная ситуация когда лучше выбрать оригинал"],
  "use_cases_analog": ["конкретная ситуация когда лучше выбрать аналог"],
  "key_differences": ["главное отличие 1", "главное отличие 2", "главное отличие 3"]
}}"""
    
    def __init__(
        self,
        client: GigaChatClient,
        cleaner: ContentCleaner,
        memory_context: str | None = None,
        audit_trail: AgentAuditTrail | None = None,
    ):
        self.client = client
        self.cleaner = cleaner
        self.memory_context = memory_context
        self.audit_trail = audit_trail
        self._max_year = datetime.now().year + 1

    def _record_audit(
        self,
        action: str,
        status: str,
        risk: str,
        confidence: float,
        message: str,
        **metrics: object,
    ) -> None:
        if self.audit_trail is None:
            return
        self.audit_trail.record(
            action=action,
            status=status,
            risk=risk,
            confidence=confidence,
            message=message,
            **metrics,
        )

    @staticmethod
    def _clean_text(value: object, max_length: int = 200) -> str | None:
        if value is None:
            return None
        text = normalize_whitespace(str(value))
        if not text:
            return None
        return text[:max_length]

    @staticmethod
    def _to_int(value: object) -> int | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _to_float(value: object) -> float | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            return float(str(value).strip())
        except (TypeError, ValueError):
            return None

    def _clean_list(
        self,
        value: object,
        *,
        max_items: int = 5,
        max_length: int = 160,
    ) -> list[str]:
        items: list[str] = []
        seen: set[str] = set()

        for raw_item in ensure_list_str(value):
            text = self._clean_text(raw_item, max_length=max_length)
            if not text or len(text) < 2:
                continue
            key = text.casefold()
            if key in seen:
                continue
            seen.add(key)
            items.append(text)
            if len(items) >= max_items:
                break

        return items

    def _clean_specs(self, value: object) -> dict[str, str]:
        if not isinstance(value, dict):
            return {}

        cleaned: dict[str, str] = {}
        for raw_key, raw_val in value.items():
            key = self._clean_text(raw_key, max_length=60)
            val = self._clean_text(raw_val, max_length=120)
            if not key or not val:
                continue
            cleaned[key] = val
            if len(cleaned) >= 8:
                break
        return cleaned

    def _normalize_analysis_result(self, result: object) -> AIAnalysisResult:
        if not isinstance(result, dict):
            return {}

        normalized: AIAnalysisResult = {}

        for key in ("category", "vendor", "model", "condition", "location", "seller_name", "seller_phone", "seller_email"):
            text = self._clean_text(result.get(key))
            if text:
                normalized[key] = text

        currency = self._clean_text(result.get("currency"), max_length=8)
        if currency:
            currency = currency.upper()
            if currency in {"RUB", "USD", "EUR"}:
                normalized["currency"] = currency

        price = self._to_int(result.get("price"))
        if price is not None and CONFIG.min_valid_price <= price <= 10**12:
            normalized["price"] = price

        monthly_payment = self._to_int(result.get("monthly_payment"))
        if monthly_payment is not None and 1 <= monthly_payment <= 10**10:
            normalized["monthly_payment"] = monthly_payment

        year = self._to_int(result.get("year"))
        if year is not None and 1900 <= year <= self._max_year:
            normalized["year"] = year

        specs = self._clean_specs(result.get("specs"))
        if specs:
            normalized["specs"] = specs

        pros = self._clean_list(result.get("pros"), max_items=5)
        if pros:
            normalized["pros"] = pros

        cons = self._clean_list(result.get("cons"), max_items=5)
        if cons:
            normalized["cons"] = cons

        analogs = self._clean_list(result.get("analogs_mentioned"), max_items=5, max_length=80)
        if analogs:
            normalized["analogs_mentioned"] = analogs

        seller_profile_url = self._clean_text(result.get("seller_profile_url"), max_length=300)
        if seller_profile_url and is_valid_url(seller_profile_url):
            normalized["seller_profile_url"] = seller_profile_url

        return normalized

    def _assess_analysis_result(
        self,
        result: AIAnalysisResult,
        *,
        text_length: int,
    ) -> tuple[bool, str, str, float, str, dict[str, object]]:
        informative_fields = sum(
            bool(result.get(key))
            for key in ("category", "vendor", "model", "price", "monthly_payment", "year", "condition", "location")
        )
        specs_count = len(result.get("specs", {}))
        pros_count = len(result.get("pros", []))
        cons_count = len(result.get("cons", []))
        identity_signals = sum(bool(result.get(key)) for key in ("vendor", "model", "category")) + int(specs_count > 0)

        metrics = {
            "text_len": text_length,
            "fields": informative_fields,
            "identity_signals": identity_signals,
            "specs": specs_count,
            "pros": pros_count,
            "cons": cons_count,
        }

        if not result:
            return False, "warning", "high", 0.15, "AI parse rejected: empty or untrusted structured payload", metrics

        if identity_signals == 0:
            return False, "warning", "high", 0.2, "AI parse rejected: no reliable identity signals for the asset", metrics

        confidence = min(0.95, 0.3 + informative_fields * 0.08 + specs_count * 0.05)
        if informative_fields >= 4 or specs_count >= 3:
            return True, "ok", "low", confidence, "AI parse accepted with sufficient evidence", metrics

        return True, "warning", "medium", confidence, "AI parse accepted, but evidence is thin", metrics

    def _normalize_analogs(self, result: object) -> list[str]:
        if not isinstance(result, dict):
            return []

        generic_names = {
            "аналог",
            "аналоги",
            "лизинг",
            "объявления",
            "объявление",
            "рынок",
        }

        cleaned: list[str] = []
        for candidate in self._clean_list(result.get("analogs"), max_items=5, max_length=80):
            if candidate.casefold() in generic_names:
                continue
            cleaned.append(candidate)
        return cleaned

    def _normalize_specs_result(self, result: object) -> dict[str, str]:
        if not isinstance(result, dict):
            return {}
        return self._clean_specs(result.get("specs"))

    def _normalize_review_result(self, result: object) -> AnalogReview:
        if not isinstance(result, dict):
            return {}

        normalized: AnalogReview = {}
        pros = self._clean_list(result.get("pros"), max_items=4)
        cons = self._clean_list(result.get("cons"), max_items=4)
        note = self._clean_text(result.get("note"), max_length=240)
        price_hint = self._to_int(result.get("price_hint"))
        best_link = self._clean_text(result.get("best_link"), max_length=300)

        if pros:
            normalized["pros"] = pros
        if cons:
            normalized["cons"] = cons
        if note:
            normalized["note"] = note
        if price_hint is not None and CONFIG.min_valid_price <= price_hint <= 10**12:
            normalized["price_hint"] = price_hint
        if best_link and is_valid_url(best_link):
            normalized["best_link"] = best_link

        return normalized

    def _normalize_validation_result(self, result: object) -> ValidationResult:
        if not isinstance(result, dict):
            return {"is_valid": True, "comment": "AI validation returned invalid payload"}

        raw_is_valid = result.get("is_valid")
        if isinstance(raw_is_valid, bool):
            is_valid = raw_is_valid
        elif isinstance(raw_is_valid, str):
            is_valid = raw_is_valid.strip().lower() not in {"false", "0", "no", "suspicious"}
        else:
            is_valid = True

        comment = self._clean_text(result.get("comment"), max_length=240) or "AI validation completed"
        return {"is_valid": is_valid, "comment": comment}

    def _compose_user_content(self, text: str) -> str:
        if self.memory_context:
            return (
                f"Use the following memory from previous interactions if relevant:\n\n"
                f"{self.memory_context}\n\n"
                f"Current input:\n{text}"
            )
        return text
    
    def analyze_content(self, html_content: str) -> Optional[AIAnalysisResult]:
        """Анализирует HTML-контент и извлекает структурированные данные."""
        text = self.cleaner.clean(html_content)
        if not text:
            self._record_audit(
                "ai.analyze_content",
                "warning",
                "high",
                0.1,
                "AI parse skipped: cleaned page content is empty",
            )
            return None
        
        try:
            result = self.client.chat(
                self.ANALYSIS_PROMPT,
                self._compose_user_content(text),
                temperature=0.1,
                max_tokens=1500,
                action_name="ai.analyze_content",
            )
            normalized = self._normalize_analysis_result(result)
            accepted, status, risk, confidence, message, metrics = self._assess_analysis_result(
                normalized,
                text_length=len(text),
            )
            self._record_audit("ai.analyze_content", status, risk, confidence, message, **metrics)
            return normalized if accepted else None
        except requests.RequestException:
            logger.warning("Failed to analyze content with AI")
            self._record_audit(
                "ai.analyze_content",
                "warning",
                "high",
                0.1,
                "AI parse failed due to request error",
            )
            return None
    
    def suggest_analogs(self, item_name: str) -> list[str]:
        """Получает от AI предложения по аналогам."""
        try:
            result = self.client.chat(
                self.ANALOGS_PROMPT,
                self._compose_user_content(item_name),
                temperature=0.2,
                max_tokens=500,
                action_name="ai.suggest_analogs",
            )
            analogs = self._normalize_analogs(result)
            if analogs:
                self._record_audit(
                    "ai.suggest_analogs",
                    "ok" if len(analogs) >= 3 else "warning",
                    "low" if len(analogs) >= 3 else "medium",
                    0.8 if len(analogs) >= 3 else 0.45,
                    "Analog suggestions prepared",
                    count=len(analogs),
                )
                return analogs
        except requests.RequestException:
            logger.warning(f"Failed to get analog suggestions for {item_name}")
            self._record_audit(
                "ai.suggest_analogs",
                "warning",
                "high",
                0.1,
                "Analog suggestion failed due to request error",
            )
        self._record_audit(
            "ai.suggest_analogs",
            "warning",
            "high",
            0.15,
            "No trustworthy analog suggestions returned",
        )
        return []
    
    def extract_specs_from_text(self, text: str) -> dict:
        """Извлекает технические характеристики из текста с помощью AI."""
        if not text or len(text.strip()) < 50:
            self._record_audit(
                "ai.extract_specs",
                "warning",
                "medium",
                0.2,
                "Specs extraction skipped: source text is too short",
                text_len=len(text or ""),
            )
            return {}
        
        try:
            result = self.client.chat(
                self.SPECS_EXTRACTION_PROMPT,
                self._compose_user_content(text[:8000]),  # Ограничиваем длину текста
                temperature=0.1,
                max_tokens=2000,
                action_name="ai.extract_specs",
            )
            specs = self._normalize_specs_result(result)
            if specs:
                self._record_audit(
                    "ai.extract_specs",
                    "ok" if len(specs) >= 3 else "warning",
                    "low" if len(specs) >= 3 else "medium",
                    0.8 if len(specs) >= 3 else 0.5,
                    "Technical specs extracted",
                    specs=len(specs),
                )
                return specs
        except requests.RequestException as e:
            logger.warning(f"Failed to extract specs with AI: {e}")
            self._record_audit(
                "ai.extract_specs",
                "warning",
                "high",
                0.1,
                "Specs extraction failed due to request error",
            )
        self._record_audit(
            "ai.extract_specs",
            "warning",
            "high",
            0.15,
            "No trustworthy specs extracted",
        )
        return {}
    
    def review_analog(self, analog_name: str, listings: list[dict]) -> AnalogReview:
        """Получает AI-обзор модели-аналога."""
        listings_text = "\n".join(
            f"- {l.get('title', '')} ({l.get('link', '')}) {l.get('snippet', '')}"
            for l in listings
        )
        user_content = f"Модель: {analog_name}\nОбъявления:\n{listings_text}"
        
        try:
            result = self.client.chat(
                self.REVIEW_PROMPT,
                self._compose_user_content(user_content),
                temperature=0.2,
                max_tokens=600,
                action_name="ai.review_analog",
            )
            normalized = self._normalize_review_result(result)
            signals = (
                len(normalized.get("pros", []))
                + len(normalized.get("cons", []))
                + int(bool(normalized.get("note")))
                + int(bool(normalized.get("price_hint")))
            )
            if signals == 0:
                self._record_audit(
                    "ai.review_analog",
                    "warning",
                    "high",
                    0.15,
                    "Analog review rejected: no useful structured evidence",
                    analog=analog_name,
                )
                return {}

            self._record_audit(
                "ai.review_analog",
                "ok" if signals >= 3 else "warning",
                "low" if signals >= 3 else "medium",
                0.8 if signals >= 3 else 0.45,
                "Analog review prepared",
                analog=analog_name,
                signals=signals,
            )
            return normalized
        except requests.RequestException:
            logger.warning(f"Failed to review analog {analog_name}")
            self._record_audit(
                "ai.review_analog",
                "warning",
                "high",
                0.1,
                "Analog review failed due to request error",
                analog=analog_name,
            )
            return {}
    
    def validate_report(self, report: dict) -> ValidationResult:
        """Проверяет рыночный отчет через AI на адекватность."""
        summary = {
            "item": report.get("item"),
            "median_price": report.get("median_price"),
            "mean_price": report.get("mean_price"),
            "market_range": report.get("market_range"),
            "offers_count": len(report.get("offers_used", [])),
        }
        details = json.dumps(summary, ensure_ascii=False, default=str)
        
        try:
            result = self.client.chat(
                self.VALIDATION_PROMPT,
                self._compose_user_content(f"Отчет:\n{details}"),
                temperature=0.1,
                max_tokens=500,
                action_name="ai.validate_report",
            )
            normalized = self._normalize_validation_result(result)
            self._record_audit(
                "ai.validate_report",
                "ok" if normalized.get("is_valid", True) else "warning",
                "low" if normalized.get("is_valid", True) else "medium",
                0.8 if normalized.get("is_valid", True) else 0.45,
                normalized.get("comment", "AI validation completed"),
                offers_count=summary["offers_count"],
            )
            return normalized
        except requests.RequestException:
            logger.warning("Failed to validate report with AI")
            self._record_audit(
                "ai.validate_report",
                "warning",
                "high",
                0.1,
                "AI market validation failed due to request error",
                offers_count=summary["offers_count"],
            )
            return {"is_valid": True, "comment": "AI not available"}
    
    def compare_two_offers(self, offer1: dict, offer2: dict) -> dict:
        """Сравнивает два предложения и определяет, какое лучше."""
        offer1_str = json.dumps(offer1, ensure_ascii=False, default=str, indent=2)
        offer2_str = json.dumps(offer2, ensure_ascii=False, default=str, indent=2)
        
        prompt = self.COMPARE_OFFERS_PROMPT.format(
            offer1=offer1_str,
            offer2=offer2_str
        )
        
        try:
            result = self.client.chat(
                prompt,
                self._compose_user_content("Сравни объявления"),
                temperature=0.2,
                max_tokens=800,
                action_name="ai.compare_two_offers",
            )
            return result or {"winner": 1, "score_1": 5.0, "score_2": 5.0, "reason": "Comparison failed"}
        except requests.RequestException:
            logger.warning("Failed to compare offers")
            return {"winner": 1, "score_1": 5.0, "score_2": 5.0, "reason": "AI unavailable"}
    
    def find_best_offer(self, offers: list[dict]) -> dict:
        """Находит лучшее предложение в списке."""
        if not offers:
            return {"best_index": -1, "best_score": 0.0, "reason": "No offers"}
        
        if len(offers) == 1:
            return {"best_index": 0, "best_score": 8.0, "reason": "Only one offer", "ranking": [{"index": 0, "score": 8.0, "brief_reason": "Single offer"}]}
        
        # Подготавливаем предложения для AI
        offers_list = "\n\n".join([
            f"Объявление {i}:\n{json.dumps(offer, ensure_ascii=False, default=str, indent=2)}"
            for i, offer in enumerate(offers, 1)
        ])
        
        prompt = self.FIND_BEST_OFFER_PROMPT.format(offers_list=offers_list)
        
        try:
            result = self.client.chat(
                prompt,
                self._compose_user_content("Найди лучшее объявление"),
                temperature=0.2,
                max_tokens=1000,
                action_name="ai.find_best_offer",
            )
            if result and "best_index" in result:
                return result
            else:
                # Запасной вариант: возвращаем первое предложение
                return {"best_index": 0, "best_score": 7.0, "reason": "AI parsing failed", "ranking": []}
        except requests.RequestException:
            logger.warning("Failed to find best offer")
            # Запасной вариант: возвращаем первое предложение
            return {"best_index": 0, "best_score": 7.0, "reason": "AI unavailable", "ranking": []}
    
    def compare_best_offers(self, best_original: dict, best_analog: dict, original_name: str, analog_name: str) -> dict:
        """Сравнивает лучшее исходное предложение с лучшим предложением аналога."""
        original_str = json.dumps(best_original, ensure_ascii=False, default=str, indent=2)
        analog_str = json.dumps(best_analog, ensure_ascii=False, default=str, indent=2)
        
        prompt = self.COMPARE_BEST_OFFERS_PROMPT.format(
            original_name=original_name,
            best_original=original_str,
            analog_name=analog_name,
            best_analog=analog_str
        )
        
        try:
            result = self.client.chat(
                prompt,
                self._compose_user_content("Сравни лучшие объявления"),
                temperature=0.2,
                max_tokens=1200,
                action_name="ai.compare_best_offers",
            )
            return result or {
                "winner": "original",
                "original_score": 5.0,
                "analog_score": 5.0,
                "recommendation": "Comparison failed"
            }
        except requests.RequestException:
            logger.warning("Failed to compare best offers")
            return {
                "winner": "original",
                "original_score": 5.0,
                "analog_score": 5.0,
                "recommendation": "AI unavailable"
            }
