from __future__ import annotations

import io
import json
import re
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


SUPPORTED_EXTENSIONS = {".txt", ".docx", ".pdf"}
TEXT_PREVIEW_LENGTH = 500
MAX_TEXT_LENGTH = 200_000
MAX_AI_TEXT_LENGTH = 25_000


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
    except ModuleNotFoundError as exc:
        raise ValueError(
            "Для обработки PDF требуется установленный пакет 'pypdf'."
        ) from exc

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

