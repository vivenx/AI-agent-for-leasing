const byId = (id) => document.getElementById(id);

const REQUEST_TIMEOUT_MS = 15 * 60 * 1000;
const SESSION_STORAGE_KEY = "leasing_ai_session_id";

const elements = {
  form: byId("analyzeForm"),
  modeToggle: byId("modeToggle"),
  manualFields: byId("manualFields"),
  documentFields: byId("documentFields"),
  item: byId("item"),
  clientPrice: byId("clientPrice"),
  documentFile: byId("documentFile"),
  documentFileLabel: byId("documentFileLabel"),
  filePicker: byId("filePicker"),
  useAI: byId("useAI"),
  numResults: byId("numResults"),
  submitBtn: byId("submitBtn"),
  error: byId("error"),
  placeholder: byId("placeholder"),
  loading: byId("loading"),
  loadingText: byId("loadingText"),
  resultContent: byId("resultContent"),
  resultTitle: byId("resultTitle"),
  priceLabel: byId("priceLabel"),
  priceValue: byId("priceValue"),
  medianValue: byId("medianValue"),
  rangeValue: byId("rangeValue"),
  deviationValue: byId("deviationValue"),
  commentSection: byId("commentSection"),
  specsSection: byId("specsSection"),
  detailsGrid: byId("detailsGrid"),
  documentSection: byId("documentSection"),
  documentInfoGrid: byId("documentInfoGrid"),
  textPreview: byId("textPreview"),
  warningsSection: byId("warningsSection"),
  warningsList: byId("warningsList"),
  sourcesList: byId("sourcesList"),
  uiFilters: byId("ui-filters"),
  filterMaxPrice: byId("filterMaxPrice"),
  filterLocation: byId("filterLocation"),
  filterYear: byId("filterYear"),
  linkPreviewModal: byId("linkPreviewModal"),
  previewLoading: byId("previewLoading"),
  previewError: byId("previewError"),
  previewImage: byId("previewImage"),
  previewTitle: byId("previewTitle"),
  openOriginalLink: byId("openOriginalLink"),
  closePreviewBtn: byId("closePreviewBtn"),
  closePreviewBtnBottom: byId("closePreviewBtnBottom"),
};

let allSources = []; // Сюда будем сохранять оригинальный список объявлений
let currentMode = "manual";
let abortController = null;
let timeoutId = null;
let requestTimedOut = false;


function applyFilters() {
  const maxPrice = parseFloat(elements.filterMaxPrice?.value) || Infinity;
  const selectedYear = elements.filterYear?.value;
  const selectedLocation = elements.filterLocation?.value;

  const filtered = allSources.filter(source => {
    const priceMatch = (source.price || 0) <= maxPrice;
    const yearMatch = selectedYear === "all" || String(source.year) === selectedYear;
    const locationMatch = selectedLocation === "all" || String(source.location) === selectedLocation;
    return priceMatch && yearMatch && locationMatch;
  });

  renderSources(filtered, false);
}

function init() {
  bindModeToggle();
  bindAiToggle();
  bindFileInput();
  bindForm();
  bindFilters();
  bindPreviewModal();
  setMode("manual");
}

function getOrCreateSessionId() {
  let sessionId = window.localStorage.getItem(SESSION_STORAGE_KEY);
  if (!sessionId) {
    sessionId = crypto.randomUUID();
    window.localStorage.setItem(SESSION_STORAGE_KEY, sessionId);
  }
  return sessionId;
}

function bindModeToggle() {
  elements.modeToggle?.querySelectorAll(".mode-btn").forEach((button) => {
    button.addEventListener("click", () => {
      setMode(button.dataset.mode || "manual");
    });
  });
}

function bindAiToggle() {
  document.querySelectorAll(".ai-btn").forEach((button) => {
    button.addEventListener("click", () => {
      document.querySelectorAll(".ai-btn").forEach((item) => item.classList.remove("active"));
      button.classList.add("active");
      if (elements.useAI) {
        elements.useAI.value = button.dataset.value || "true";
      }
    });
  });
}

function bindFileInput() {
  if (!elements.documentFile) return;

  elements.documentFile.addEventListener("change", () => {
    const file = elements.documentFile.files?.[0];
    if (elements.documentFileLabel) {
      elements.documentFileLabel.textContent = file
        ? `${file.name} • ${formatFileSize(file.size)}`
        : "TXT, DOCX, PDF";
    }
  });

  ["dragenter", "dragover"].forEach((eventName) => {
    elements.filePicker?.addEventListener(eventName, (event) => {
      event.preventDefault();
      elements.filePicker.classList.add("active");
    });
  });

  ["dragleave", "drop"].forEach((eventName) => {
    elements.filePicker?.addEventListener(eventName, (event) => {
      event.preventDefault();
      elements.filePicker.classList.remove("active");
    });
  });

  elements.filePicker?.addEventListener("drop", (event) => {
    const files = event.dataTransfer?.files;
    if (!files || files.length === 0 || !elements.documentFile) return;
    elements.documentFile.files = files;
    const file = files[0];
    if (elements.documentFileLabel) {
      elements.documentFileLabel.textContent = `${file.name} • ${formatFileSize(file.size)}`;
    }
  });
}

function bindForm() {
  elements.form?.addEventListener("submit", async (event) => {
    event.preventDefault();
    hideError();

    const numResults = Number(elements.numResults?.value || "5");
    if (!Number.isInteger(numResults) || numResults < 1 || numResults > 10) {
      showError("Количество результатов должно быть от 1 до 10.");
      return;
    }

    if (currentMode === "manual") {
      await submitManual();
      return;
    }

    await submitDocument();
  });
}

function bindFilters() {
  elements.filterMaxPrice?.addEventListener("input", applyFilters);
  elements.clientPrice?.addEventListener("input", applyFilters);

  // Логика открытия/закрытия кастомных рамок по клику
  document.querySelectorAll(".custom-select-trigger").forEach(trigger => {
    trigger.addEventListener("click", (e) => {
      e.stopPropagation();
      const wrapper = trigger.parentElement;
      
      // Закрываем другие открытые выпадашки
      document.querySelectorAll(".custom-select-wrapper").forEach(w => {
        if (w !== wrapper) w.classList.remove("open");
      });
      
      wrapper.classList.toggle("open");
    });
  });

  // Закрытие выпадашек при клике в любое место экрана
  document.addEventListener("click", () => {
    document.querySelectorAll(".custom-select-wrapper").forEach(w => w.classList.remove("open"));
  });
}

function bindPreviewModal() {
  elements.sourcesList?.addEventListener("click", (event) => {
    const link = event.target.closest(".source-title");
    if (!link) return;

    const url = link.getAttribute("href");
    if (!url || url === "#") return;

    event.preventDefault();
    openLinkPreview(url, link.textContent || "Скриншот страницы");
  });

  const closeButtons = [elements.closePreviewBtn, elements.closePreviewBtnBottom];
  closeButtons.forEach((button) => {
    button?.addEventListener("click", () => closeLinkPreview());
  });

  elements.linkPreviewModal?.addEventListener("click", (event) => {
    if (event.target?.getAttribute("data-close-modal") === "true") {
      closeLinkPreview();
    }
  });

  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      closeLinkPreview();
    }
  });
}


function setMode(mode) {
  currentMode = mode;
  const isManual = mode === "manual";

  elements.modeToggle?.querySelectorAll(".mode-btn").forEach((button) => {
    button.classList.toggle("active", button.dataset.mode === mode);
  });

  elements.manualFields?.classList.toggle("hidden", !isManual);
  elements.documentFields?.classList.toggle("hidden", isManual);

  // СКРЫВАЕМ секции в правой панели при смене режима
  elements.specsSection?.classList.add("hidden");    // Скрываем Характеристики
  elements.documentSection?.classList.add("hidden"); // Скрываем Документ
  elements.warningsSection?.classList.add("hidden"); // Скрываем Предупреждения
  elements.resultContent?.classList.remove("show");  // Прячем весь результат

  if (elements.priceLabel) {
    elements.priceLabel.textContent = isManual ? "Цена клиента" : "Цена по документу";
  }
  
  elements.submitBtn.textContent = isManual ? "Начать анализ" : "Загрузить и проанализировать";
}

async function submitManual() {
  const item = (elements.item?.value || "").trim();
  const rawClientPrice = (elements.clientPrice?.value || "").trim();
  const numResults = Number(elements.numResults?.value || "5");
  const clientPrice = rawClientPrice ? Number(rawClientPrice) : null;

  if (!item || item.length < 3) {
    showError("Введите предмет лизинга длиной не менее 3 символов.");
    return;
  }

  if (rawClientPrice && (!Number.isFinite(clientPrice) || clientPrice < 0)) {
    showError("Цена клиента должна быть положительным числом.");
    return;
  }

  startLoading();

  try {
    const data = await requestJson("/api/describe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        text: item,
        clientPrice,
        useAI: elements.useAI?.value === "true",
        numResults,
        sessionId: getOrCreateSessionId(),
      }),
    });
    renderManualResult(data, clientPrice);
  } catch (error) {
    showError(resolveErrorMessage(error));
  } finally {
    stopLoading();
  }
}

async function submitDocument() {
  const file = elements.documentFile?.files?.[0];
  const numResults = Number(elements.numResults?.value || "5");

  if (!file) {
    showError("Выберите документ для анализа.");
    return;
  }

  startLoading();

  try {
    const formData = new FormData();
    formData.append("file", file);
    formData.append("useAI", String(elements.useAI?.value === "true"));
    formData.append("numResults", String(numResults));
    formData.append("sessionId", getOrCreateSessionId());

    const data = await requestJson("/api/analyze-document", {
      method: "POST",
      body: formData,
    });
    renderDocumentResult(data);
  } catch (error) {
    showError(resolveErrorMessage(error));
  } finally {
    stopLoading();
  }
}

async function requestJson(url, options) {
  cleanupPendingRequest();
  abortController = new AbortController();
  requestTimedOut = false;
  timeoutId = window.setTimeout(() => {
    requestTimedOut = true;
    abortController?.abort();
  }, REQUEST_TIMEOUT_MS);

  try {
    const response = await fetch(url, {
      ...options,
      signal: abortController.signal,
    });
    const payload = await response.json().catch(() => ({}));

    if (!response.ok) {
      throw new Error(payload.detail || `Ошибка ${response.status}`);
    }

    return payload;
  } finally {
    cleanupPendingRequest();
  }
}

function cleanupPendingRequest() {
  if (timeoutId) {
    window.clearTimeout(timeoutId);
    timeoutId = null;
  }
  abortController = null;
}

function openLinkPreview(url, title) {
  if (!elements.linkPreviewModal) return;

  // 1. Настраиваем заголовки и оригинальную ссылку
  if (elements.previewTitle) {
    elements.previewTitle.textContent = title || "Предпросмотр страницы";
  }
  if (elements.openOriginalLink) {
    elements.openOriginalLink.href = url;
    elements.openOriginalLink.setAttribute("aria-label", `Открыть оригинал ${title || 'страницы'}`);
  }

  // 2. Сбрасываем состояния: прячем старую картинку/ошибки, включаем новый скелетон
  elements.previewError?.classList.add("hidden");
  elements.previewImage?.classList.add("hidden");
  elements.previewLoading?.classList.remove("hidden");

  // Ставим красивый стартовый текст в индикатор загрузки
  const loadingTextElem = elements.linkPreviewModal.querySelector(".loading-text");
  if (loadingTextElem) {
    loadingTextElem.textContent = "Инициализация безопасного окружения...";
  }

  // 3. Открываем модальное окно (добавляем класс show)
  elements.linkPreviewModal.classList.add("show");
  document.body.style.overflow = "hidden"; // Запрещаем скролл основной страницы под модалкой

  // 4. Запрашиваем скриншот у бэкенда
  fetchPreviewScreenshot(url)
    .then((src) => {
      if (elements.previewImage) {
        elements.previewImage.src = src;
        
        // Ждем, пока браузер физически отрисует картинку из blob-ссылки
        elements.previewImage.onload = () => {
          elements.previewLoading?.classList.add("hidden"); // Тушим скелетон
          elements.previewImage?.classList.remove("hidden"); // Показываем готовый скриншот
        };
      }
    })
    .catch((error) => {
      console.error("Ошибка безопасного превью:", error);
      elements.previewLoading?.classList.add("hidden");
      if (elements.previewError) {
        elements.previewError.textContent = "Система защиты: Не удалось выполнить рендеринг страницы. Вы можете открыть оригинал ссылки на свой страх и риск.";
        elements.previewError.classList.remove("hidden");
      }
    });
}

function closeLinkPreview() {
  if (!elements.linkPreviewModal) return;

  // Закрываем окно, очищаем картинку и возвращаем скролл страницы
  elements.linkPreviewModal.classList.remove("show");
  document.body.style.overflow = ""; 
  
  if (elements.previewImage) {
    elements.previewImage.src = "";
  }
  
  elements.previewError?.classList.add("hidden");
  elements.previewLoading?.classList.add("hidden");
  elements.previewImage?.classList.add("hidden");
}

async function fetchPreviewScreenshot(url) {
  // Важно: твой бэкенд на FastAPI/Flask принимает роут /api/link-preview, так что оставляем его
  const response = await fetch(`/api/link-preview?url=${encodeURIComponent(url)}`);
  if (!response.ok) {
    let detail = `Ошибка ${response.status}`;
    try {
      const payload = await response.json();
      detail = payload.detail || detail;
    } catch {
      // ignore
    }
    throw new Error(detail);
  }

  const blob = await response.blob();
  return URL.createObjectURL(blob);
}

function startLoading() {
  elements.submitBtn.disabled = true;
  elements.error.classList.remove("show");
  elements.placeholder.classList.add("hidden");
  elements.loading.classList.add("show");
}

function stopLoading() {
  elements.submitBtn.disabled = false;
  elements.loading.classList.remove("show");
}

function renderManualResult(data, fallbackClientPrice) {
  const marketReport = data.market_report || {};
  const subject = [data.vendor, data.model, data.year].filter(Boolean).join(" ") || marketReport.item || "Предмет не определен";

  elements.resultTitle.textContent = subject;
  if (elements.priceLabel) {
    elements.priceLabel.textContent = "Цена клиента";
  }
  elements.priceValue.textContent = formatPrice(marketReport.client_price ?? fallbackClientPrice, data.currency);
  elements.medianValue.textContent = formatPrice(marketReport.median_price, data.currency);
  elements.rangeValue.textContent = formatRange(marketReport.market_range, data.currency);
  elements.deviationValue.textContent = formatDeviationFromPrices(
    marketReport.client_price ?? fallbackClientPrice,
    marketReport.median_price,
    data.currency
  );
  const commentText = buildMarketCommentText({
    explanation: marketReport.explanation,
    clientPrice: marketReport.client_price ?? fallbackClientPrice,
    medianPrice: marketReport.median_price,
    currency: data.currency,
  });
  elements.commentSection.innerHTML = `
    <strong>${escapeHtml(marketReport.client_price_ok === true ? "Цена в рынке" : marketReport.client_price_ok === false ? "Цена вне рынка" : "Недостаточно данных")}</strong>
    <div style="margin-top: 8px;">${escapeHtml(commentText || "Комментарий рынка отсутствует.")}</div>
  `;

  renderSources(data.sources || []);

  elements.specsSection.classList.add("hidden");
  elements.documentSection.classList.add("hidden");
  elements.warningsSection.classList.add("hidden");
  elements.resultContent.classList.add("show");
}

function renderDocumentResult(data) {
  const priceCheck = data.price_check || {};
  const marketReport = data.market_report || {};
  const subject = data.item_name || marketReport.item || "Предмет не определен";

  elements.resultTitle.textContent = subject;
  if (elements.priceLabel) {
    elements.priceLabel.textContent = "Цена по документу";
  }
  elements.priceValue.textContent = formatPrice(data.declared_price, data.currency);
  elements.medianValue.textContent = formatPrice(priceCheck.market_median_price, data.currency);
  elements.rangeValue.textContent = formatRange(priceCheck.market_range, data.currency);
  elements.deviationValue.textContent = formatDeviationAmount(
    priceCheck.deviation_amount,
    priceCheck.deviation_percent,
    data.currency
  );
  const commentText = buildMarketCommentText({
    explanation: marketReport.explanation,
    clientPrice: data.declared_price,
    medianPrice: priceCheck.market_median_price,
    currency: data.currency,
    includeDeviation: false,
  });
  elements.commentSection.innerHTML = `
    <strong>${escapeHtml(priceCheck.verdict || "Нет итогового вывода")}</strong>
    <div style="margin-top: 8px;">${escapeHtml(commentText || "Комментарий рынка отсутствует.")}</div>
  `;

  renderDetails(elements.detailsGrid, data.key_characteristics || {}, "Характеристики не найдены");
  renderDetails(
    elements.documentInfoGrid,
    {
      Файл: data.file_name,
      Тип: (data.document_type || "").toUpperCase(),
      Валюта: data.currency,
      "Диапазон рынка": formatRange(priceCheck.market_range, data.currency),
    },
    "Информация о документе появится после анализа"
  );
  elements.textPreview.textContent = data.text_preview || "—";

  if (data.warnings?.length) {
    elements.warningsSection.classList.remove("hidden");
    elements.warningsList.innerHTML = data.warnings
      .map((warning) => `<li>${escapeHtml(String(warning))}</li>`)
      .join("");
  } else {
    elements.warningsSection.classList.add("hidden");
    elements.warningsList.innerHTML = "";
  }

  renderSources(data.sources || []);

  elements.specsSection.classList.remove("hidden");
  elements.documentSection.classList.remove("hidden");
  elements.resultContent.classList.add("show");
}

function renderDetails(container, data, emptyText) {
  const entries = Object.entries(normalizeObject(data)).filter(([, value]) => value && value !== "—");
  if (entries.length === 0) {
    container.innerHTML = `<div class="empty-note">${escapeHtml(emptyText)}</div>`;
    return;
  }

  container.innerHTML = entries
    .map(
      ([key, value]) => `
        <div class="detail-card">
          <div class="detail-card-label">${escapeHtml(key)}</div>
          <div class="detail-card-value">${escapeHtml(String(value))}</div>
        </div>
      `
    )
    .join("");
}

function renderSources(sources, setupFilters = true) {
  // Получаем контейнер фильтров
  const filtersCont = document.getElementById("ui-filters");

  if (!sources || sources.length === 0) {
    // Если предложений нет (или массив пустой) — принудительно прячем фильтры
    filtersCont?.classList.add("hidden");
  } else {
    // Если предложения пришли — убираем hidden и показываем блок
    filtersCont?.classList.remove("hidden");
  }

  if (setupFilters) {
    allSources = sources;
    setupLocationFilter(sources);
    setupYearFilter(sources);
  }

  // Получаем цену клиента из инпута
  const customerPrice = parseFloat(elements.clientPrice?.value) || 0;

  elements.sourcesList.innerHTML = sources
    .map((source) => {
      const currentPrice = source.price || 0;
      let priceClass = "price-normal"; // По умолчанию зеленый

      if (customerPrice > 0 && currentPrice > 0) {
        const diff = (currentPrice - customerPrice) / customerPrice;

        if (diff > 0.25) {
          priceClass = "price-danger";  // Больше чем на 25% дороже
        } else if (diff > 0.10) {
          priceClass = "price-warning"; // Больше чем на 10% дороже
        }
      }

      const title = source.title || "Источник";
      const priceStr = source.price_str || formatPrice(source.price);
      const url = source.url || "#";
      const meta = [source.source, source.year, source.location].filter(Boolean);

      return `
        <div class="source-card">
          <div class="source-card-head">
            <a class="source-title source-link-preview" href="${url}" data-tooltip="Открыть безопасный предпросмотр">
              ${escapeHtml(title)}
            </a>
            <div class="source-price ${priceClass}">${escapeHtml(priceStr)}</div>
          </div>
          <div class="source-meta">
            ${meta.map(m => `<span>${escapeHtml(String(m))}</span>`).join("")}
          </div>
        </div>
      `;
    })
    .join("");

  bindPreviewModal(); 
}

function setupYearFilter(sources) {
  const wrapper = document.getElementById("wrapperYear");
  const trigger = document.getElementById("triggerYear");
  const optionsBox = document.getElementById("optionsYear");
  const hiddenInput = document.getElementById("filterYear");
  if (!wrapper || !optionsBox || !hiddenInput) return;

  wrapper.style.display = "block";

  const years = [...new Set(sources.map(s => s.year).filter(Boolean))].sort((a, b) => b - a);

  let optionsHtml = `<div class="custom-option selected" data-value="all">Все годы</div>`;
  
  if (sources.length > 0 && years.length > 0) {
    years.forEach(year => {
      optionsHtml += `<div class="custom-option" data-value="${year}">${year}</div>`;
    });
  }
  
  optionsBox.innerHTML = optionsHtml;
  initCustomOptions(wrapper, trigger, optionsBox, hiddenInput);
}

function setupLocationFilter(sources) {
  const wrapper = document.getElementById("wrapperLocation");
  const trigger = document.getElementById("triggerLocation");
  const optionsBox = document.getElementById("optionsLocation");
  const hiddenInput = document.getElementById("filterLocation");
  if (!wrapper || !optionsBox || !hiddenInput) return;

  wrapper.style.display = "block";

  const locations = [...new Set(sources.map(s => s.location).filter(Boolean))].sort();

  let optionsHtml = `<div class="custom-option selected" data-value="all">Все регионы</div>`;
  
  if (sources.length > 0 && locations.length > 0) {
    locations.forEach(loc => {
      optionsHtml += `<div class="custom-option" data-value="${loc}">${escapeHtml(String(loc))}</div>`;
    });
  }
  
  optionsBox.innerHTML = optionsHtml;
  initCustomOptions(wrapper, trigger, optionsBox, hiddenInput);
}

// Вспомогательная функция для обработки выбора в кастомном списке
function initCustomOptions(wrapper, trigger, optionsBox, hiddenInput) {
  optionsBox.querySelectorAll(".custom-option").forEach(option => {
    option.addEventListener("click", (e) => {
      e.stopPropagation();
      
      // Меняем активный класс у элементов списка
      optionsBox.querySelectorAll(".custom-option").forEach(o => o.classList.remove("selected"));
      option.classList.add("selected");
      
      // Меняем текст на триггере и пишем значение в скрытый инпут
      trigger.querySelector("span").textContent = option.textContent;
      hiddenInput.value = option.dataset.value;
      
      // Закрываем рамку и запускаем фильтрацию
      wrapper.classList.remove("open");
      applyFilters();
    });
  });
}

function normalizeObject(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function formatPrice(value, currency = "RUB") {
  const amount = Number(value);
  if (!Number.isFinite(amount)) return "—";

  if (currency === "USD") {
    return new Intl.NumberFormat("ru-RU", { style: "currency", currency: "USD", maximumFractionDigits: 0 }).format(amount);
  }
  if (currency === "EUR") {
    return new Intl.NumberFormat("ru-RU", { style: "currency", currency: "EUR", maximumFractionDigits: 0 }).format(amount);
  }

  return `${new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 0 }).format(amount)} ₽`;
}

function formatRange(range, currency = "RUB") {
  if (!Array.isArray(range) || range.length !== 2) return "—";
  return `${formatPrice(range[0], currency)} — ${formatPrice(range[1], currency)}`;
}

function buildMarketCommentText({
  explanation,
  clientPrice,
  medianPrice,
  currency = "RUB",
  includeDeviation = true,
}) {
  const parts = [];
  const deviation = formatDeviationFromPrices(clientPrice, medianPrice, currency);

  if (includeDeviation && deviation !== "—") {
    parts.push(`Отклонение от медианы: ${deviation}.`);
  }

  const cleanedExplanation = cleanMarketExplanation(explanation);
  if (cleanedExplanation) {
    parts.push(cleanedExplanation);
  }

  return parts.join(" ").trim();
}

function cleanMarketExplanation(explanation) {
  const localized = localizeMarketExplanation(explanation);
  if (!localized) return "";

  const sentences = splitSentences(localized)
    .map((sentence) => sentence.trim())
    .filter(Boolean)
    .filter((sentence) => !isRedundantMarketSentence(sentence));

  return sentences.join(" ");
}

function localizeMarketExplanation(explanation) {
  if (!explanation) return "";

  return String(explanation)
    .replace(/\bnot confirmed\b/gi, "не подтверждена")
    .replace(/\bconfirmed\b/gi, "подтверждена")
    .replace(/\bMarket range\b/gi, "Диапазон рынка")
    .replace(/\bClient price\b/gi, "Цена клиента")
    .replace(/\bmedian\b/gi, "медиана")
    .replace(/\bNo prices collected\b/gi, "Не удалось собрать данные по ценам")
    .replace(/\s+/g, " ")
    .trim();
}

function splitSentences(text) {
  return text.match(/[^.!?]+[.!?]?/g) || [];
}

function isRedundantMarketSentence(sentence) {
  return /^(Диапазон рынка|Цена клиента)\b/i.test(sentence.trim());
}

function formatDeviationFromPrices(clientPrice, medianPrice, currency = "RUB") {
  const client = Number(clientPrice);
  const median = Number(medianPrice);

  if (!Number.isFinite(client) || !Number.isFinite(median) || median === 0) return "—";

  const diff = client - median;
  const diffPercent = (diff / median) * 100;
  return formatDeviationAmount(diff, diffPercent, currency);
}

function formatDeviationAmount(amountValue, percentValue, currency = "RUB") {
  const amount = normalizeNearZero(Number(amountValue));
  const percent = normalizeNearZero(Number(percentValue));

  if (!Number.isFinite(amount) || !Number.isFinite(percent)) return "—";

  return `${amount > 0 ? "+" : ""}${formatPrice(amount, currency)} · ${formatSignedPercent(percent)}`;
}

function formatSignedPercent(value) {
  const numeric = normalizeNearZero(Number(value));
  if (!Number.isFinite(numeric)) return "—";
  return `${numeric > 0 ? "+" : ""}${numeric.toFixed(2)}%`;
}

function normalizeNearZero(value, epsilon = 0.000001) {
  if (!Number.isFinite(value)) return value;
  return Math.abs(value) < epsilon ? 0 : value;
}

function formatFileSize(bytes) {
  const size = Number(bytes);
  if (!Number.isFinite(size)) return "";
  if (size < 1024) return `${size} Б`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} КБ`;
  return `${(size / (1024 * 1024)).toFixed(1)} МБ`;
}

function showError(message) {
  elements.error.textContent = message;
  elements.error.classList.add("show");
}

function hideError() {
  elements.error.textContent = "";
  elements.error.classList.remove("show");
}

function resolveErrorMessage(error) {
  if (requestTimedOut || error?.name === "AbortError") {
    return "Время ожидания ответа истекло. Попробуйте еще раз.";
  }
  return error?.message || "Не удалось выполнить анализ.";
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function escapeAttribute(value) {
  return escapeHtml(value);
}

init();
