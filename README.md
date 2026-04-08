# AI Agent For Leasing

Сервис для проверки рыночной стоимости предмета лизинга и анализа документов. Проект состоит из FastAPI-приложения, веб-интерфейса и Python-пакета `leasing_analyzer` с клиентами AI, поиском, парсингом и расчётом рыночного отчёта.

## Что умеет

- Анализировать предмет лизинга по текстовому описанию через `POST /api/describe`
- Принимать `txt`, `docx`, `pdf` и извлекать из документа предмет, цену и характеристики через `POST /api/analyze-document`
- Искать предложения на рынке, считать диапазон, медиану и отклонение от цены клиента
- Подтягивать аналоги и сравнения через Perplexity Sonar
- Использовать GigaChat для AI-разбора текста, характеристик и вспомогательных сравнений
- Отдавать локальный веб-интерфейс из `api/templates` и `api/static`

## Актуальная структура

```text
.
├── api/
│   ├── main.py
│   ├── templates/
│   │   └── index.html
│   └── static/
│       ├── script.js
│       ├── style.css
│       └── favicon.ico
├── leasing_analyzer/
│   ├── clients/
│   │   ├── ai_analyzer.py
│   │   ├── gigachat.py
│   │   └── sonar.py
│   ├── core/
│   │   ├── config.py
│   │   ├── logging.py
│   │   ├── models.py
│   │   ├── rate_limit.py
│   │   ├── sessions.py
│   │   └── utils.py
│   ├── document/
│   │   ├── extractors.py
│   │   └── service.py
│   ├── parsing/
│   │   ├── avito.py
│   │   ├── base.py
│   │   ├── basic.py
│   │   ├── content_cleaner.py
│   │   └── helpers.py
│   └── services/
│       ├── fetcher.py
│       ├── market.py
│       ├── output.py
│       ├── pipeline.py
│       ├── search.py
│       └── specs.py
├── requirements.txt
├── verify_env.py
├── .env.example
└── README.md
```

## Требования

- Python 3.11+
- Google Chrome
- Доступ в интернет для внешних API и Selenium Manager
- Установленные зависимости:

```bash
pip install -r requirements.txt
```

Важно:

- Для `selenium` нужен доступ к Chrome и ChromeDriver. Если Selenium Manager не может скачать драйвер, анализ рынка будет зависать или падать на инициализации браузера.
- Для PDF-анализа нужен пакет `pypdf`. Он уже указан в `requirements.txt`.

## Настройка `.env`

Скопируйте шаблон:

```bash
copy .env.example .env
```

или вручную создайте `.env` в корне проекта.

Минимальный пример:

```env
SERPER_API_KEY=your_serper_api_key
GIGACHAT_AUTH_DATA=your_gigachat_auth_data
PERPLEXITY_API_KEY=your_perplexity_or_proxy_key

# Опционально для proxy-режима Sonar
PERPLEXITY_BASE_URL=https://api.artemox.com/v1
PERPLEXITY_MODEL=sonar-reasoning-pro

# Опционально
LOG_LEVEL=INFO
CORS_ORIGINS=http://localhost:8000,http://127.0.0.1:8000
```

Роли переменных:

- `SERPER_API_KEY`:
  нужен для поиска объявлений через Serper/Google
- `GIGACHAT_AUTH_DATA`:
  нужен для AI-анализа и обязателен для `POST /api/analyze-document`
- `PERPLEXITY_API_KEY`:
  нужен для Sonar-аналогов и deep-comparison
- `PERPLEXITY_BASE_URL` и `PERPLEXITY_MODEL`:
  используются, если Sonar идёт через proxy

Проверка окружения:

```bash
python verify_env.py
```

## Запуск

Запускать сервер лучше из корня проекта, а не из `api/`.

### Windows PowerShell

```powershell
.\venv\Scripts\python.exe -m uvicorn api.main:app --host 127.0.0.1 --port 8000 --reload
```

### Git Bash

```bash
./venv/Scripts/python.exe -m uvicorn api.main:app --host 127.0.0.1 --port 8000 --reload
```

После запуска:

- UI: `http://127.0.0.1:8000/`
- Swagger: `http://127.0.0.1:8000/docs`
- Healthcheck: `http://127.0.0.1:8000/health`

## API

### `GET /`

Отдаёт локальный веб-интерфейс.

### `GET /health`

Простой healthcheck:

```json
{"status": "ok"}
```

### `POST /api/describe`

Анализирует предмет лизинга по текстовому описанию.

Пример запроса:

```json
{
  "text": "BMW X5 2024",
  "clientPrice": 8000000,
  "useAI": true,
  "numResults": 5
}
```

Пример `curl`:

```bash
curl -X POST "http://127.0.0.1:8000/api/describe" \
  -H "Content-Type: application/json" \
  -d "{\"text\":\"BMW X5 2024\",\"clientPrice\":8000000,\"useAI\":true,\"numResults\":5}"
```

Что возвращает:

- агрегированный `market_report`
- список `sources`
- подобранные `analogs_details`
- `best_original_offer` и `best_offers_comparison`, если deep-analysis отработал

### `POST /api/analyze-document`

Принимает файл и строит документный и рыночный отчёт.

Поддерживаемые форматы:

- `.txt`
- `.docx`
- `.pdf`

Ограничения:

- размер файла до 15 МБ
- для корректного результата нужен `GIGACHAT_AUTH_DATA`

Пример `curl`:

```bash
curl -X POST "http://127.0.0.1:8000/api/analyze-document" \
  -F "file=@contract.docx" \
  -F "useAI=true" \
  -F "numResults=5"
```

Что возвращает:

- `item_name`
- `declared_price`
- `currency`
- `key_characteristics`
- `price_check`
- `market_report`
- `sources`
- `warnings`
- `text_preview`

## Использование как Python API

Текущий программный entrypoint находится в `leasing_analyzer.services.pipeline`.

```python
from leasing_analyzer.services.pipeline import run_analysis

result = run_analysis(
    item="BMW X5 2024",
    client_price=8_000_000,
    use_ai=True,
    num_results=5,
)
```

Для документного анализа:

```python
from leasing_analyzer.document.service import analyze_document

with open("contract.docx", "rb") as f:
    result = analyze_document(
        file_name="contract.docx",
        content=f.read(),
        use_ai=True,
        num_results=5,
    )
```

## Как устроен поток обработки

### Анализ предмета

1. `api.main` принимает запрос
2. `leasing_analyzer.services.pipeline.run_analysis()` запускает основной pipeline
3. `services.search` собирает поисковые URL и предложения
4. `services.fetcher` тянет страницы через Selenium
5. `parsing.*` извлекают данные объявлений
6. `services.market` считает рынок, аналоги и сравнения
7. `clients.gigachat` и `clients.sonar` подключаются, если включён AI и заданы ключи

### Анализ документа

1. `api.main` принимает файл
2. `document.extractors` извлекает текст
3. `document.service` отправляет текст в GigaChat
4. После извлечения предмета запускается обычный рыночный pipeline

## Логи и диагностика

Логирование настраивается в:

- `leasing_analyzer/core/logging.py`

Уровень логов:

```env
LOG_LEVEL=INFO
```

Если сервер не стартует или UI открывается, но анализ не идёт, проверьте:

- загрузились ли ключи из `.env`
- запускаете ли сервер из корня проекта
- доступен ли Chrome
- может ли Selenium Manager скачать драйвер
- есть ли доступ в интернет к Serper, GigaChat и Perplexity

## Известные ограничения

- Анализ рынка зависит от Selenium и внешних сайтов
- Без `SERPER_API_KEY` поиск будет ограничен
- Без `GIGACHAT_AUTH_DATA` document-analysis не работает
- Без `PERPLEXITY_API_KEY` не будет Sonar-аналогов и части deep-analysis
- Если TLS для GigaChat оставлен с `verify=False`, это рабочий, но небезопасный режим

## Что устарело

В репозитории больше не стоит ориентироваться на старые инструкции про:

- `parser_b.py` как основной entrypoint
- `document_analysis.py` как отдельный CLI-модуль
- запуск `uvicorn main:app` из `api/` как основной рекомендуемый способ

Актуальная точка входа для сервера:

```bash
python -m uvicorn api.main:app --reload
```
