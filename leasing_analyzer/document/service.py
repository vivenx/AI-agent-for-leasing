from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

# Core
from leasing_analyzer.core.config import CONFIG
from leasing_analyzer.core.logging import get_logger

logger = get_logger(__name__)

# Clients (AI)
from leasing_analyzer.clients.gigachat import GigaChatClient

# Document extraction
from leasing_analyzer.document.extractors import (
    extract_text_from_document,
    normalize_ai_payload,
    normalize_whitespace,
    prepare_text_for_ai,
    ExtractedDocumentData,
)
from leasing_analyzer.services.pipeline import run_analysis

TEXT_PREVIEW_LENGTH = 500

DOCUMENT_EXTRACTION_PROMPT = """Ты анализируешь текст документа по лизингу, договору купли-продажи, счету, коммерческому предложению или иному документу на актив.

Верни только валидный JSON без markdown и без пояснений в формате:
{
  "item_name": "строка или null",
  "declared_price": 1234567,
  "currency": "RUB",
  "key_characteristics": {
    "Характеристика": "значение"
  },
  "warnings": ["предупреждение 1"]
}

Правила извлечения:
1. item_name:
- Это именно предмет лизинга / имущество / актив / транспортное средство.
- Не указывай названия сторон договора, банков, поставщиков и компаний, если это не предмет.
- Если известны марка, модель и год, включи их в item_name, например: "BMW X5 2024".
- Если предмет не удаётся определить надёжно, верни null.

2. declared_price:
- Это цена, сопоставимая с рыночной стоимостью самого предмета лизинга.
- Предпочитай стоимость имущества / объекта / предмета договора / цену транспортного средства.
- Не подставляй аванс, ежемесячный платёж, штраф, пеню, выкупной платёж, НДС отдельно, общую сумму платежей по графику, если это не цена самого объекта.
- Если надёжно определить цену нельзя, верни null.

3. currency:
- Используй RUB, USD или EUR.
- Если валюта не указана явно, но документ русскоязычный и сумма в рублях, верни RUB.

4. key_characteristics:
- Включай только характеристики, которые явно есть в тексте документа.
- Примеры: Марка, Модель, Год, VIN, Цвет, Пробег, Двигатель, Мощность, Коробка передач, Привод, Комплектация, Серийный номер.
- Не придумывай характеристики.

5. warnings:
- Краткие предупреждения только если есть неопределённость или конфликт в документе.
- Если предупреждений нет, верни пустой массив.

Если в тексте несколько сумм, выбери ту, которая относится к самому объекту лизинга. Если это невозможно, верни declared_price = null и добавь предупреждение.
"""

def get_gigachat_client() -> GigaChatClient:
    """Create a configured GigaChat client or raise if auth is missing."""

    if not CONFIG.gigachat_auth_data:
        raise ValueError("GIGACHAT_AUTH_DATA не установлен. AI-разбор документа недоступен.")
    return GigaChatClient(CONFIG.gigachat_auth_data)

def parse_document(file_name: str, content: bytes) -> ExtractedDocumentData:
    """Extract text from a file and ask GigaChat to structure document data."""

    text, document_type = extract_text_from_document(file_name, content)
    if not normalize_whitespace(text):
        raise ValueError("Не удалось извлечь текст из документа.")

    ai_text, truncation_warnings = prepare_text_for_ai(text)
    client = get_gigachat_client()

    user_content = (
        f"Имя файла: {file_name}\n"
        f"Тип файла: {document_type}\n"
        "Нужно извлечь структурированные данные из документа.\n\n"
        "Текст документа:\n"
        f"{ai_text}"
    )

    payload = client.chat(
        system_prompt=DOCUMENT_EXTRACTION_PROMPT,
        user_content=user_content,
        temperature=0.1,
        max_tokens=1400,
    )
    if not isinstance(payload, dict):
        raise ValueError("GigaChat не вернул структурированный JSON для анализа документа.")

    extracted = normalize_ai_payload(file_name, document_type, text, payload)
    extracted.warnings = truncation_warnings + extracted.warnings
    return extracted

def calculate_price_check(
    declared_price: Optional[int],
    market_report: dict,
) -> dict:
    """Calculate deviation between declared and market median prices."""

    median_price = market_report.get("median_price")
    market_range = market_report.get("market_range")
    if declared_price is None or median_price in (None, 0):
        return {
            "declared_price": declared_price,
            "market_median_price": median_price,
            "market_range": market_range,
            "deviation_amount": None,
            "deviation_percent": None,
            "confirmed": None,
            "verdict": "Недостаточно данных для проверки цены.",
        }

    deviation_amount = declared_price - float(median_price)
    deviation_percent = round((deviation_amount / float(median_price)) * 100, 2)
    confirmed = market_report.get("client_price_ok")

    if confirmed is True:
        verdict = "Цена подтверждается рыночными данными."
    elif confirmed is False:
        verdict = "Цена не подтверждается рыночными данными."
    else:
        verdict = "Не удалось однозначно подтвердить цену."

    return {
        "declared_price": declared_price,
        "market_median_price": median_price,
        "market_range": market_range,
        "deviation_amount": int(round(deviation_amount)),
        "deviation_percent": deviation_percent,
        "confirmed": confirmed,
        "verdict": verdict,
    }

def analyze_document(
    file_name: str,
    content: bytes,
    use_ai: bool = True,
    num_results: int = 5,
) -> dict:
    """Run full document analysis including AI extraction and market comparison."""

    parsed = parse_document(file_name, content)
    default_explanation = (
        f"Рыночный анализ не выполнен: предмет лизинга определен как '{parsed.item_name}', но поиск не был запущен."
        if parsed.item_name
        else "Рыночный анализ не запускался: GigaChat не смог определить предмет лизинга."
    )

    market_analysis: dict = {
        "item": parsed.item_name,
        "offers_used": [],
        "market_report": {
            "item": parsed.item_name,
            "market_range": None,
            "median_price": None,
            "mean_price": None,
            "client_price": parsed.declared_price,
            "client_price_ok": None,
            "explanation": default_explanation,
        },
    }

    if parsed.item_name:
        try:
            market_analysis = run_analysis(
                item=parsed.item_name,
                client_price=parsed.declared_price,
                use_ai=use_ai,
                num_results=num_results,
            )
        except Exception as exc:
            error_message = str(exc)[:200]
            logger.warning("Market analysis failed for %s: %s", parsed.file_name, exc)
            parsed.warnings.append(f"Рыночный анализ не выполнен: {error_message}")
            market_analysis["market_report"]["explanation"] = (
                f"Предмет лизинга определен как '{parsed.item_name}', "
                f"но рыночный анализ не выполнен: {error_message}"
            )

    market_report = market_analysis.get("market_report") or {}
    offers_used = market_analysis.get("offers_used") or []

    sources = [
        {
            "title": offer.get("title"),
            "source": offer.get("source"),
            "url": offer.get("url"),
            "price": offer.get("price"),
            "price_str": offer.get("price_str"),
            "year": offer.get("year"),
            "condition": offer.get("condition"),
            "location": offer.get("location"),
        }
        for offer in offers_used
    ]

    return {
        "file_name": parsed.file_name,
        "document_type": parsed.document_type,
        "item_name": parsed.item_name,
        "declared_price": parsed.declared_price,
        "currency": parsed.currency,
        "key_characteristics": parsed.characteristics,
        "price_check": calculate_price_check(parsed.declared_price, market_report),
        "market_report": market_report,
        "sources": sources,
        "warnings": parsed.warnings,
        "text_preview": parsed.text[:TEXT_PREVIEW_LENGTH],
    }

def format_price(value: Optional[int | float]) -> str:
    """Format a numeric price for CLI output."""

    if value is None:
        return "не определена"
    return f"{int(round(value)):,} ₽".replace(",", " ")


def print_document_analysis(result: dict) -> None:
    """Render analysis results in a human-readable CLI format."""

    print("=" * 70)
    print("АНАЛИЗ ДОКУМЕНТА")
    print("=" * 70)
    print(f"Файл: {result.get('file_name')}")
    print(f"Тип: {result.get('document_type')}")

    item_name = result.get("item_name")
    print(f"Предмет лизинга: {item_name or 'не определен'}")
    print(f"Заявленная цена: {format_price(result.get('declared_price'))}")

    characteristics = result.get("key_characteristics") or {}
    if characteristics:
        print("\nКлючевые характеристики:")
        for key, value in characteristics.items():
            print(f"  - {key}: {value}")

    price_check = result.get("price_check") or {}
    market_report = result.get("market_report") or {}

    print("\nПроверка цены:")
    print(f"  - Медиана рынка: {format_price(price_check.get('market_median_price'))}")

    market_range = price_check.get("market_range")
    if market_range and len(market_range) == 2:
        print(
            f"  - Диапазон рынка: {format_price(market_range[0])} .. {format_price(market_range[1])}"
        )

    deviation_amount = price_check.get("deviation_amount")
    deviation_percent = price_check.get("deviation_percent")
    if deviation_amount is not None and deviation_percent is not None:
        sign = "+" if deviation_amount > 0 else ""
        print(
            f"  - Отклонение: {sign}{format_price(deviation_amount)} ({deviation_percent:+.2f}%)"
        )
    else:
        print("  - Отклонение: не рассчитано")

    print(f"  - Вердикт: {price_check.get('verdict') or 'нет данных'}")

    explanation = market_report.get("explanation")
    if explanation:
        print(f"\nКомментарий рынка:\n{explanation}")

    warnings = result.get("warnings") or []
    if warnings:
        print("\nПредупреждения:")
        for warning in warnings:
            print(f"  - {warning}")

    sources = result.get("sources") or []
    if sources:
        print("\nИсточники:")
        for source in sources[:5]:
            title = source.get("title") or "Объявление"
            price_str = source.get("price_str") or format_price(source.get("price"))
            url = source.get("url") or "без ссылки"
            print(f"  - {title}")
            print(f"    Цена: {price_str}")
            print(f"    URL: {url}")

        if len(sources) > 5:
            print(f"  - ... и еще {len(sources) - 5} источников")

def build_arg_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser for document analysis."""

    parser = argparse.ArgumentParser(
        description="CLI для AI-анализа документа и проверки заявленной цены по рынку."
    )
    parser.add_argument("file", help="Путь к документу (.txt, .docx, .pdf)")
    parser.add_argument(
        "--no-ai",
        action="store_true",
        help="Отключить AI-обогащение при рыночном анализе после извлечения данных из документа",
    )
    parser.add_argument(
        "--num-results",
        type=int,
        default=5,
        help="Количество поисковых результатов для рыночного анализа (1-10)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Вывести результат в JSON",
    )
    parser.add_argument(
        "--output",
        help="Сохранить результат в JSON-файл",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry point for document analysis."""

    parser = build_arg_parser()
    args = parser.parse_args(argv)

    file_path = Path(args.file)
    if not file_path.exists() or not file_path.is_file():
        print(f"Файл не найден: {file_path}", file=sys.stderr)
        return 1

    if args.num_results < 1 or args.num_results > 10:
        print("Параметр --num-results должен быть в диапазоне 1..10", file=sys.stderr)
        return 1

    try:
        content = file_path.read_bytes()
        result = analyze_document(
            file_name=file_path.name,
            content=content,
            use_ai=not args.no_ai,
            num_results=args.num_results,
        )
    except Exception as exc:
        print(f"Ошибка анализа документа: {exc}", file=sys.stderr)
        return 1

    if args.output:
        output_path = Path(args.output)
        output_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"JSON сохранен: {output_path}")

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print_document_analysis(result)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
