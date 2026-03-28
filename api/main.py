import logging
import os
import sys
from pathlib import Path
from typing import Optional

# Add parent directory to path for parser_b import
current_dir = Path(__file__).resolve().parent
parent_dir = current_dir.parent
if str(parent_dir) not in sys.path:
    sys.path.insert(0, str(parent_dir))

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from document_analysis import analyze_document
from parser_b import run_analysis

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Leasing descriptor API",
    description="Рыночный анализ предмета лизинга + аналоги",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

BASE_DIR = Path(__file__).resolve().parent
templates_dir = BASE_DIR / "templates"
static_dir = BASE_DIR / "static"

if static_dir.exists():
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

# CORS настройки (безопаснее для продакшена)
cors_origins = os.getenv("CORS_ORIGINS", "http://localhost:8000,http://127.0.0.1:8000").split(",")
cors_origins = [origin.strip() for origin in cors_origins if origin.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins if cors_origins else ["http://localhost:8000"],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# Rate limiting
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


class DescribeRequest(BaseModel):
    text: str = Field(..., min_length=3, max_length=500, description="Описание предмета лизинга")
    clientPrice: Optional[int] = Field(None, ge=0, le=10**12, description="Цена клиента в рублях")
    useAI: Optional[bool] = Field(True, description="Использовать AI для анализа")
    numResults: Optional[int] = Field(5, ge=1, le=10, description="Количество результатов для поиска")
    
    @field_validator("text")
    @classmethod
    def validate_text(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Текст не может быть пустым")
        return v.strip()


class AnalogDetail(BaseModel):
    name: str
    avg_price_guess: Optional[int] = None
    note: Optional[str] = None
    pros: list[str] = []
    cons: list[str] = []


class MarketReport(BaseModel):
    item: Optional[str] = None
    market_range: Optional[list[int]] = None
    median_price: Optional[float] = None
    mean_price: Optional[int] = None
    client_price: Optional[int] = None
    client_price_ok: Optional[bool] = None
    explanation: Optional[str] = None


class BestOfferAnalysis(BaseModel):
    best_index: Optional[int] = None
    best_score: Optional[float] = None
    reason: Optional[str] = None
    ranking: list[dict] = []


class BestOffersComparison(BaseModel):
    winner: Optional[str] = None
    original_score: Optional[float] = None
    analog_score: Optional[float] = None
    price_comparison: Optional[dict] = None
    pros_original: list[str] = []
    cons_original: list[str] = []
    pros_analog: list[str] = []
    cons_analog: list[str] = []
    recommendation: Optional[str] = None
    use_cases_original: list[str] = []
    use_cases_analog: list[str] = []
    # Ссылки на объявления для сравнения
    original_url: Optional[str] = None
    analog_url: Optional[str] = None
    original_title: Optional[str] = None
    analog_title: Optional[str] = None
    # Форматированные цены
    original_price_formatted: Optional[str] = None
    analog_price_formatted: Optional[str] = None
    comparison_details: Optional[dict] = None
    key_differences: list[str] = []
    sonar_comparison: bool = False  # True если сравнение через Sonar


class DescribeResponse(BaseModel):
    category: Optional[str] = None
    vendor: Optional[str] = None
    model: Optional[str] = None
    price: Optional[int] = None
    currency: Optional[str] = None
    monthly_payment: Optional[int] = None
    year: Optional[int] = None
    condition: Optional[str] = None
    location: Optional[str] = None
    specs: dict = {}
    pros: list[str] = []
    cons: list[str] = []

    market_report: MarketReport = MarketReport()
    sources: list[dict] = []
    
    # Deep analysis results
    best_original_offer: Optional[dict] = None
    best_original_analysis: Optional[BestOfferAnalysis] = None


class DocumentPriceCheck(BaseModel):
    declared_price: Optional[int] = None
    market_median_price: Optional[float] = None
    market_range: Optional[list[int]] = None
    deviation_amount: Optional[int] = None
    deviation_percent: Optional[float] = None
    confirmed: Optional[bool] = None
    verdict: Optional[str] = None


class DocumentAnalyzeResponse(BaseModel):
    file_name: str
    document_type: str
    item_name: Optional[str] = None
    declared_price: Optional[int] = None
    currency: str = "RUB"
    key_characteristics: dict = {}
    price_check: DocumentPriceCheck = DocumentPriceCheck()
    market_report: MarketReport = MarketReport()
    sources: list[dict] = []
    warnings: list[str] = []
    text_preview: Optional[str] = None


def select_primary_offer(offers: list[dict], best_offer: Optional[dict]) -> dict:
    """Pick the most informative offer for top-level item fields."""
    candidates: list[dict] = []

    if isinstance(best_offer, dict) and best_offer:
        candidates.append(best_offer)
    candidates.extend(offer for offer in offers if isinstance(offer, dict))

    if not candidates:
        return {}

    def score(offer: dict) -> tuple[int, int, int, int, int]:
        specs = offer.get("specs")
        specs_count = len(specs) if isinstance(specs, dict) else 0
        return (
            specs_count,
            int(bool(offer.get("price"))),
            int(bool(offer.get("year"))),
            int(bool(offer.get("condition"))),
            int(bool(offer.get("location"))),
        )

    return max(candidates, key=score)


@app.get("/", response_class=HTMLResponse)
async def root() -> HTMLResponse:
    index_path = templates_dir / "index.html"
    if index_path.exists():
        return HTMLResponse(index_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>index.html не найден</h1>", status_code=404)


@app.post("/api/describe", response_model=DescribeResponse)
@limiter.limit("10/minute")
async def describe(request: Request, describe_request: DescribeRequest) -> DescribeResponse:
    """
    Главный эндпоинт API для анализа предмета лизинга.
    
    **Параметры:**
    - **text**: Описание предмета лизинга (3-500 символов)
    - **clientPrice**: Цена клиента в рублях (опционально)
    - **useAI**: Использовать AI для анализа (по умолчанию True)
    - **numResults**: Количество результатов для поиска (1-10, по умолчанию 5)
    
    **Возвращает:**
    - Рыночный анализ с диапазоном цен, медианой, отклонением
    - Список аналогов с детальным сравнением
    - Лучшие предложения с оценками
    
    **Rate Limit:** 10 запросов в минуту на IP адрес
    """
    item_str = describe_request.text
    client_price = describe_request.clientPrice
    use_ai = describe_request.useAI if describe_request.useAI is not None else True
    num_results = describe_request.numResults if describe_request.numResults else 5

    logger.info(f"Запрос анализа: item={item_str[:80]}..., client_price={client_price}, use_ai={use_ai}, num_results={num_results}")

    try:
        try:
            # Запускаем парсер
            analysis = run_analysis(
                item=item_str,
                client_price=client_price,
                use_ai=use_ai,
                num_results=num_results,
            )
        except OverflowError as e:
            # Защита от int too large to convert to float
            logger.warning(f"Overflow в run_analysis: {e}")
            analysis = {
                "item": item_str,
                "offers_used": [],
                "market_report": {
                    "item": item_str,
                    "market_range": None,
                    "median_price": None,
                    "mean_price": None,
                    "client_price": client_price,
                    "client_price_ok": None,
                    "explanation": "Не удалось посчитать диапазон: данные цен некорректны.",
                },
            }

        # Извлекаем нужные части
        market_report = analysis.get("market_report") or {}
        offers_used = analysis.get("offers_used") or []
        analogs_details_raw = analysis.get("analogs_details") or []

        # Все объявления для просмотра
        sources_for_response: list[dict] = []
        for o in offers_used:  # все объявления
            sources_for_response.append(
                {
                    "title": o.get("title"),
                    "source": o.get("source"),
                    "url": o.get("url"),
                    "price_str": o.get("price_str"),
                    "price": o.get("price"),
                    "monthly_payment_str": o.get("monthly_payment_str"),
                    "model": o.get("model"),
                    "year": o.get("year"),
                    "condition": o.get("condition"),
                    "location": o.get("location"),
                }
            )


        # Первое предложение (для базовых полей)
        primary_offer = select_primary_offer(offers_used, market_report.get("best_original_offer"))

        # Преобразуем аналоги в формат, который ожидает фронт
        analogs_for_response = []
        for analog in analogs_details_raw:
            analogs_for_response.append(
                AnalogDetail(
                    name=analog.get("name", "Аналог"),
                    avg_price_guess=analog.get("avg_price_guess"),
                    note=analog.get("note"),
                    pros=analog.get("pros", []),
                    cons=analog.get("cons", []),
                )
            )

        # Deep analysis results
        best_original_offer = market_report.get("best_original_offer")
        best_original_analysis_raw = market_report.get("best_original_analysis", {})
        best_offers_comparison_raw = market_report.get("best_offers_comparison", {})
        
        # Convert best original analysis
        best_original_analysis = None
        if best_original_analysis_raw:
            best_original_analysis = BestOfferAnalysis(
                best_index=best_original_analysis_raw.get("best_index"),
                best_score=best_original_analysis_raw.get("best_score"),
                reason=best_original_analysis_raw.get("reason"),
                ranking=best_original_analysis_raw.get("ranking", [])
            )
        
        # Convert comparisons (includes links to specific offers)
        best_offers_comparison = {}
        for analog_name, comp_data in best_offers_comparison_raw.items():
            best_offers_comparison[analog_name] = BestOffersComparison(
                winner=comp_data.get("winner"),
                original_score=comp_data.get("original_score"),
                analog_score=comp_data.get("analog_score"),
                price_comparison=comp_data.get("price_comparison"),
                pros_original=comp_data.get("pros_original", []),
                cons_original=comp_data.get("cons_original", []),
                pros_analog=comp_data.get("pros_analog", []),
                cons_analog=comp_data.get("cons_analog", []),
                recommendation=comp_data.get("recommendation"),
                use_cases_original=comp_data.get("use_cases_original", []),
                use_cases_analog=comp_data.get("use_cases_analog", []),
                # Ссылки на конкретные объявления
                original_url=comp_data.get("original_url"),
                analog_url=comp_data.get("analog_url"),
                original_title=comp_data.get("original_title"),
                analog_title=comp_data.get("analog_title"),
                # Форматированные цены
                original_price_formatted=comp_data.get("original_price_formatted"),
                analog_price_formatted=comp_data.get("analog_price_formatted"),
                comparison_details=comp_data.get("comparison_details"),
                key_differences=comp_data.get("key_differences", []),
                sonar_comparison=comp_data.get("sonar_comparison", False)
            )
        
        # Собираем ответ
        return DescribeResponse(
            category=primary_offer.get("category"),
            vendor=primary_offer.get("vendor"),
            model=primary_offer.get("model"),
            price=market_report.get("median_price"),
            currency=primary_offer.get("currency", "RUB"),
            monthly_payment=primary_offer.get("monthly_payment"),
            year=primary_offer.get("year"),
            condition=primary_offer.get("condition"),
            location=primary_offer.get("location"),
            specs=primary_offer.get("specs", {}),
            pros=primary_offer.get("pros", []),
            cons=primary_offer.get("cons", []),
            analogs_mentioned=analysis.get("analogs_suggested", []),
            market_report=MarketReport(
                item=market_report.get("item"),
                market_range=market_report.get("market_range"),
                median_price=market_report.get("median_price"),
                mean_price=market_report.get("mean_price"),
                client_price=market_report.get("client_price"),
                client_price_ok=market_report.get("client_price_ok"),
                explanation=market_report.get("explanation"),
            ),
            analogs_details=analogs_for_response,
            sources=sources_for_response,
            best_original_offer=best_original_offer,
            best_original_analysis=best_original_analysis,
            best_offers_comparison=best_offers_comparison,
        )

    except ValueError as e:
        logger.error(f"Ошибка валидации: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Ошибка валидации входных данных: {str(e)}"
        )
    except Exception as e:
        logger.error(f"Ошибка в /api/describe: {e}", exc_info=True)
        
        # Не возвращаем детали ошибки клиенту в продакшене
        error_message = str(e)[:200] if os.getenv("DEBUG", "false").lower() == "true" else "Внутренняя ошибка сервера"
        
        return DescribeResponse(
            category="Ошибка анализа",
            vendor=error_message[:100],
            specs={},
            pros=[],
            cons=[],
            analogs_mentioned=[],
            market_report=MarketReport(
                explanation=f"Ошибка при обработке запроса. Пожалуйста, попробуйте позже."
            ),
            analogs_details=[],
        )


# Проверка обязательных переменных окружения при старте
@app.post("/api/analyze-document", response_model=DocumentAnalyzeResponse)
@limiter.limit("5/minute")
async def analyze_document_endpoint(
    request: Request,
    file: UploadFile = File(...),
    useAI: bool = Form(True),
    numResults: int = Form(5),
) -> DocumentAnalyzeResponse:
    """Analyze uploaded document and compare declared price with market data."""
    if not file.filename:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Не указано имя файла.")

    if numResults < 1 or numResults > 10:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Количество результатов должно быть от 1 до 10.",
        )

    content = await file.read()
    if not content:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Файл пустой.")

    if len(content) > 15 * 1024 * 1024:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Размер файла превышает 15 МБ.",
        )

    logger.info(
        "Запрос анализа документа: file=%s, size=%s bytes, use_ai=%s, num_results=%s",
        file.filename,
        len(content),
        useAI,
        numResults,
    )

    try:
        result = analyze_document(
            file_name=file.filename,
            content=content,
            use_ai=useAI,
            num_results=numResults,
        )
        return DocumentAnalyzeResponse(**result)
    except ValueError as e:
        logger.warning("Ошибка анализа документа %s: %s", file.filename, e)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.error("Ошибка в /api/analyze-document: %s", e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Внутренняя ошибка при анализе документа.",
        )


def check_environment():
    """Проверяет наличие критичных переменных окружения."""
    warnings = []
    
    if not os.getenv("SERPER_API_KEY"):
        warnings.append("⚠️  SERPER_API_KEY не установлен - поиск через Google может не работать")
    
    if not os.getenv("GIGACHAT_AUTH_DATA"):
        warnings.append("⚠️  GIGACHAT_AUTH_DATA не установлен - AI анализ может быть недоступен")
    
    if not os.getenv("PERPLEXITY_API_KEY"):
        warnings.append("⚠️  PERPLEXITY_API_KEY не установлен - поиск аналогов через Sonar недоступен")
    
    if warnings:
        logger.warning("=" * 70)
        for warning in warnings:
            logger.warning(warning)
        logger.warning("=" * 70)
    else:
        logger.info("✅ Все критичные переменные окружения установлены")


@app.on_event("startup")
async def startup_event():
    """Выполняется при старте приложения."""
    check_environment()
    logger.info("🚀 API сервер запущен")


if __name__ == "__main__":
    import uvicorn
    
    check_environment()
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
