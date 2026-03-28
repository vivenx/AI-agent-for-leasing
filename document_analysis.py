import argparse
import io
import json
import logging
import re
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from xml.etree import ElementTree as ET

from parser_b import CONFIG, GigaChatClient, run_analysis

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".txt", ".docx", ".pdf"}
TEXT_PREVIEW_LENGTH = 500
MAX_TEXT_LENGTH = 200_000
MAX_AI_TEXT_LENGTH = 25_000

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


@dataclass
class ExtractedDocumentData:
    """Structured document data extracted from raw file text."""

    file_name: str
    document_type: str
    text: str
    item_name: Optional[str] = None
    declared_price: Optional[int] = None
    currency: str = "RUB"
    characteristics: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def normalize_whitespace(text: str) -> str:
    """Collapse repeated whitespace and trim the string."""

    return re.sub(r"\s+", " ", (text or "")).strip()


def decode_text_content(content: bytes) -> str:
    """Decode plain-text file bytes using common encodings."""

    for encoding in ("utf-8-sig", "utf-8", "cp1251", "utf-16", "latin-1"):
        try:
            decoded = content.decode(encoding)
            if decoded:
                return decoded.lstrip("\ufeff")
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace").lstrip("\ufeff")


def parse_money(value: Any) -> Optional[int]:
    """Convert numeric-like input into a positive integer amount."""

    if value is None:
        return None
    if isinstance(value, (int, float)):
        amount = float(value)
        return int(round(amount)) if amount > 0 else None

    cleaned = str(value).replace("\xa0", " ").strip()
    match = re.search(r"\d[\d\s.,]*", cleaned)
    if not match:
        return None

    numeric = match.group(0).replace(" ", "")
    numeric = numeric.replace(",", ".")
    if numeric.count(".") > 1:
        numeric = numeric.replace(".", "")

    try:
        amount = float(numeric)
    except ValueError:
        digits_only = re.sub(r"[^\d]", "", numeric)
        if not digits_only:
            return None
        amount = float(digits_only)

    return int(round(amount)) if amount > 0 else None


def normalize_currency(value: Any) -> str:
    """Normalize currency markers to a short code used by the API."""

    if not value:
        return "RUB"

    normalized = normalize_whitespace(str(value)).upper()
    if normalized in {"RUR", "РУБ", "РУБ.", "RUBLES"}:
        return "RUB"
    if "USD" in normalized or "$" in normalized:
        return "USD"
    if "EUR" in normalized or "€" in normalized:
        return "EUR"
    if "RUB" in normalized or "₽" in normalized or "РУБ" in normalized:
        return "RUB"
    return normalized[:12] or "RUB"


def stringify_value(value: Any) -> Optional[str]:
    """Convert arbitrary JSON-like values into a readable string."""

    if value is None:
        return None
    if isinstance(value, bool):
        return "Да" if value else "Нет"
    if isinstance(value, (int, float)):
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value)
    if isinstance(value, (list, tuple)):
        parts = [stringify_value(item) for item in value]
        parts = [part for part in parts if part]
        return ", ".join(parts) if parts else None
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)

    normalized = normalize_whitespace(str(value))
    return normalized or None


def extract_docx_text(content: bytes) -> str:
    """Extract visible text from a DOCX archive."""

    parts: list[str] = []
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        xml_names = sorted(
            name
            for name in archive.namelist()
            if name.startswith("word/") and name.endswith(".xml")
        )
        for name in xml_names:
            if not any(key in name for key in ("document", "header", "footer")):
                continue
            data = archive.read(name)
            try:
                root = ET.fromstring(data)
            except ET.ParseError:
                continue
            texts = [normalize_whitespace(node.text or "") for node in root.iter() if node.text]
            part_text = "\n".join(text for text in texts if text)
            if part_text:
                parts.append(part_text)
    return "\n".join(parts)


def extract_pdf_text(content: bytes) -> str:
    """Extract concatenated text from all PDF pages."""

    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ValueError("Для обработки PDF установите зависимость 'pypdf'.") from exc

    reader = PdfReader(io.BytesIO(content))
    pages: list[str] = []
    for page in reader.pages:
        page_text = page.extract_text() or ""
        if page_text.strip():
            pages.append(page_text)
    return "\n".join(pages)


def extract_text_from_document(file_name: str, content: bytes) -> tuple[str, str]:
    """Read supported file types and return extracted text plus document type."""

    suffix = Path(file_name or "").suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise ValueError(
            f"Неподдерживаемый формат файла: {suffix or 'без расширения'}. Поддерживаются: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
        )

    if suffix == ".txt":
        text = decode_text_content(content)
        return text[:MAX_TEXT_LENGTH], "txt"

    if suffix == ".docx":
        text = extract_docx_text(content)
        return text[:MAX_TEXT_LENGTH], "docx"

    if suffix == ".pdf":
        text = extract_pdf_text(content)
        return text[:MAX_TEXT_LENGTH], "pdf"

    raise ValueError(f"Неподдерживаемый формат файла: {suffix}")


def prepare_text_for_ai(text: str) -> tuple[str, list[str]]:
    """Trim long documents to the AI input limit and return related warnings."""

    normalized = text.strip()
    warnings: list[str] = []
    if len(normalized) > MAX_AI_TEXT_LENGTH:
        normalized = normalized[:MAX_AI_TEXT_LENGTH]
        warnings.append(
            f"Текст документа был обрезан до первых {MAX_AI_TEXT_LENGTH} символов для AI-анализа."
        )
    return normalized, warnings


def get_gigachat_client() -> GigaChatClient:
    """Create a configured GigaChat client or raise if auth is missing."""

    if not CONFIG.gigachat_auth_data:
        raise ValueError("GIGACHAT_AUTH_DATA не установлен. AI-разбор документа недоступен.")
    return GigaChatClient(CONFIG.gigachat_auth_data)


def first_non_empty(mapping: dict[str, Any], keys: tuple[str, ...]) -> Any:
    """Return the first non-empty value for the provided keys."""

    for key in keys:
        value = mapping.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def normalize_characteristics(raw_value: Any) -> dict[str, str]:
    """Normalize AI-returned characteristics into a flat string dictionary."""

    if not raw_value:
        return {}

    if isinstance(raw_value, dict):
        result: dict[str, str] = {}
        for key, value in raw_value.items():
            key_text = stringify_value(key)
            value_text = stringify_value(value)
            if key_text and value_text:
                result[key_text] = value_text
        return result

    if isinstance(raw_value, list):
        result: dict[str, str] = {}
        simple_items: list[str] = []
        for item in raw_value:
            if isinstance(item, dict):
                name = stringify_value(
                    first_non_empty(item, ("name", "key", "title", "label", "characteristic"))
                )
                value = stringify_value(first_non_empty(item, ("value", "text", "content")))
                if name and value:
                    result[name] = value
                continue

            value = stringify_value(item)
            if value:
                simple_items.append(value)

        for index, value in enumerate(simple_items, start=1):
            result[f"Характеристика {index}"] = value
        return result

    return {}


def get_characteristic_value(characteristics: dict[str, str], aliases: tuple[str, ...]) -> Optional[str]:
    """Find a characteristic value by trying several label aliases."""

    normalized_aliases = {normalize_whitespace(alias).lower() for alias in aliases}
    for key, value in characteristics.items():
        if normalize_whitespace(key).lower() in normalized_aliases:
            return value
    return None


def build_item_name_from_ai_fields(payload: dict[str, Any], characteristics: dict[str, str]) -> Optional[str]:
    """Build a final item name from explicit AI fields or key characteristics."""

    explicit_item = stringify_value(
        first_non_empty(
            payload,
            (
                "item_name",
                "leasing_item",
                "subject",
                "subject_of_leasing",
                "asset_name",
                "object_name",
                "item",
            ),
        )
    )
    if explicit_item:
        return explicit_item

    brand = stringify_value(
        first_non_empty(payload, ("brand", "vendor", "make"))
    ) or get_characteristic_value(characteristics, ("Марка", "Бренд", "Производитель"))
    model = stringify_value(
        first_non_empty(payload, ("model", "asset_model"))
    ) or get_characteristic_value(characteristics, ("Модель",))
    year = stringify_value(
        first_non_empty(payload, ("year", "manufacture_year", "production_year"))
    ) or get_characteristic_value(characteristics, ("Год", "Год выпуска", "Год производства"))

    parts = [part for part in (brand, model, year) if part]
    return normalize_whitespace(" ".join(parts)) if parts else None


def normalize_ai_payload(file_name: str, document_type: str, text: str, payload: dict[str, Any]) -> ExtractedDocumentData:
    """Map raw GigaChat JSON into the internal extracted-document structure."""

    root = payload
    for wrapper_key in ("result", "data", "document"):
        wrapped = root.get(wrapper_key)
        if isinstance(wrapped, dict):
            root = wrapped
            break

    characteristics = normalize_characteristics(
        first_non_empty(root, ("key_characteristics", "characteristics", "specs"))
    )

    item_name = build_item_name_from_ai_fields(root, characteristics)
    declared_price = parse_money(
        first_non_empty(root, ("declared_price", "object_price", "asset_price", "price"))
    )
    currency = normalize_currency(first_non_empty(root, ("currency", "price_currency")))

    raw_warnings = first_non_empty(root, ("warnings", "notes", "issues"))
    warnings: list[str] = []
    if isinstance(raw_warnings, list):
        warnings = [text for item in raw_warnings if (text := stringify_value(item))]
    elif isinstance(raw_warnings, str):
        warning_text = stringify_value(raw_warnings)
        if warning_text:
            warnings = [warning_text]

    if not item_name:
        warnings.append("GigaChat не смог уверенно определить предмет лизинга.")
    if declared_price is None:
        warnings.append("GigaChat не смог уверенно определить цену предмета лизинга.")

    return ExtractedDocumentData(
        file_name=file_name,
        document_type=document_type,
        text=text,
        item_name=item_name,
        declared_price=declared_price,
        currency=currency,
        characteristics=characteristics,
        warnings=warnings,
    )


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
