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
  userSourceUrl: byId("userSourceUrl"),
  addUserSourceBtn: byId("addUserSourceBtn"),
  useOnlyUserSources: byId("useOnlyUserSources"),
  userSourcesList: byId("userSourcesList"),
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
  userSourcesReportSection: byId("userSourcesReportSection"),
  userSourcesReportList: byId("userSourcesReportList"),
  uiFilters: byId("ui-filters"),
  filterMaxPrice: byId("filterMaxPrice"),
  filterLocation: byId("filterLocation"),
  filterYear: byId("filterYear"),
};

let allSources = []; // Сюда будем сохранять оригинальный список объявлений
let currentMode = "manual";
let abortController = null;
let timeoutId = null;
let requestTimedOut = false;
let userSources = [];


function applyFilters() {
  const maxPrice = parseFloat(elements.filterMaxPrice?.value) || Infinity;
  const selectedYear = elements.filterYear?.value;
  const selectedLocation = elements.filterLocation?.value;

  const filtered = allSources.filter(source => {
    const priceMatch = (source.price || 0) <= maxPrice;
    const yearMatch = selectedYear === "all" || String(source.year) === selectedYear;
    
    // Переводим строки в нижний регистр для безопасного поиска
    const cardLocation = String(source.location || "").toLowerCase();
    const filterLocation = String(selectedLocation || "").toLowerCase();
    
    // Проверяем: если выбрано "all" — пропускаем, иначе смотрим, входит ли регион в адрес карточки
    const locationMatch = selectedLocation === "all" || cardLocation.includes(filterLocation);
    
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
  bindUserSources();
  loadUserSources();
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

function bindUserSources() {
  elements.addUserSourceBtn?.addEventListener("click", addUserSource);
  elements.userSourceUrl?.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      addUserSource();
    }
  });
}

async function loadUserSources() {
  try {
    userSources = await requestJson("/api/user-sources", { method: "GET" });
    renderUserSourcesManager();
  } catch (error) {
    renderUserSourcesManager();
  }
}

async function addUserSource() {
  const url = (elements.userSourceUrl?.value || "").trim();
  if (!url) {
    showError("Введите ссылку на конкретную страницу модели или предложения.");
    return;
  }

  try {
    const source = await requestJson("/api/user-sources", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    userSources = userSources.filter((item) => item.id !== source.id);
    userSources.push(source);
    elements.userSourceUrl.value = "";
    hideError();
    renderUserSourcesManager();
  } catch (error) {
    showError(resolveErrorMessage(error));
  }
}

async function deleteUserSource(sourceId) {
  try {
    await requestJson(`/api/user-sources/${encodeURIComponent(sourceId)}`, { method: "DELETE" });
    userSources = userSources.filter((source) => source.id !== sourceId);
    renderUserSourcesManager();
  } catch (error) {
    showError(resolveErrorMessage(error));
  }
}

function renderUserSourcesManager() {
  if (!elements.userSourcesList) return;
  if (!userSources.length) {
    elements.userSourcesList.innerHTML = '<div class="empty-note">Ссылки пока не добавлены</div>';
    return;
  }

  elements.userSourcesList.innerHTML = userSources
    .map((source) => `
      <div class="user-source-row">
        <div class="user-source-main">
          <a href="${escapeAttribute(source.url)}" target="_blank">${escapeHtml(source.url)}</a>
          <span>${escapeHtml(formatUserSourceStatus(source.status))}</span>
        </div>
        <button type="button" class="icon-btn" data-delete-source="${escapeAttribute(source.id)}" title="Удалить">×</button>
      </div>
    `)
    .join("");

  elements.userSourcesList.querySelectorAll("[data-delete-source]").forEach((button) => {
    button.addEventListener("click", () => deleteUserSource(button.dataset.deleteSource));
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

  if (elements.useOnlyUserSources?.checked && userSources.length === 0) {
    showError("Добавьте хотя бы одну пользовательскую ссылку или отключите режим только пользовательских источников.");
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
        useOnlyUserSources: Boolean(elements.useOnlyUserSources?.checked),
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
  renderUserSourcesReport(marketReport.user_sources || []);
  syncUserSourceStatuses(marketReport.user_sources || []);

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
  renderUserSourcesReport(marketReport.user_sources || []);
  syncUserSourceStatuses(marketReport.user_sources || []);

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

  const customerPrice = parseFloat(elements.clientPrice?.value) || 0;

  elements.sourcesList.innerHTML = sources
    .map((source) => {
      const currentPrice = source.price || 0;
      let priceClass = "price-normal"; // По умолчанию зеленый [-10% ; 10%]

      if (customerPrice > 0 && currentPrice > 0) {
        // Находим разницу в процентах
        const percentDiff = ((currentPrice - customerPrice) / customerPrice) * 100;
        // Берем модуль числа (убираем минус, если цена объявления меньше цены клиента)
        const absDiff = Math.abs(percentDiff);

        if (absDiff > 20) {
          priceClass = "price-danger";   // Отклонение больше 20% в любую сторону (Красный)
        } else if (absDiff > 10) {
          priceClass = "price-warning";  // Отклонение от 10% до 20% в любую сторону (Желтый)
        }
        // Если absDiff <= 10, то остается "price-normal" (Зеленый)
      }

      const title = source.title || "Источник";
      const priceStr = source.price_str || formatPrice(source.price);
      const url = source.url || "#";
      const meta = [source.source, source.year, source.location].filter(Boolean);

      return `
        <div class="source-card">
          <div class="source-card-head">
            <a class="source-title" href="${url}" target="_blank">${escapeHtml(title)}</a>
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

function renderUserSourcesReport(sources) {
  if (!elements.userSourcesReportSection || !elements.userSourcesReportList) return;
  if (!sources || sources.length === 0) {
    elements.userSourcesReportSection.classList.add("hidden");
    elements.userSourcesReportList.innerHTML = '<div class="empty-note">Пользовательские источники не использовались</div>';
    return;
  }

  elements.userSourcesReportSection.classList.remove("hidden");
  elements.userSourcesReportList.innerHTML = sources
    .map((source) => {
      const foundData = source.found_data || {};
      const offers = Array.isArray(foundData.offers) ? foundData.offers : [];
      const dataText = [
        `предложений: ${foundData.offers_count || 0}`,
        `цен: ${foundData.prices_count || 0}`,
      ].join(", ");
      return `
        <div class="source-card user-source-report-card">
          <div class="source-card-head">
            <a class="source-title" href="${escapeAttribute(source.url)}" target="_blank">${escapeHtml(source.url)}</a>
            <div class="source-price">${escapeHtml(formatUserSourceStatus(source.status))}</div>
          </div>
          <div class="source-meta">
            <span>${escapeHtml(dataText)}</span>
            <span>${source.participated_in_calculation ? "участвовал в расчёте" : "не участвовал в расчёте"}</span>
          </div>
          <div class="user-source-reason">${escapeHtml(source.reason || "Причина не указана.")}</div>
          ${offers.length ? `<div class="user-source-found">${offers.map((offer) => escapeHtml([offer.title, offer.price_str || formatPrice(offer.price), offer.year].filter(Boolean).join(" · "))).join("<br>")}</div>` : ""}
        </div>
      `;
    })
    .join("");
}

function syncUserSourceStatuses(reportSources) {
  if (!Array.isArray(reportSources) || reportSources.length === 0) return;
  const byId = new Map(reportSources.map((source) => [source.id, source]));
  userSources = userSources.map((source) => byId.get(source.id) || source);
  renderUserSourcesManager();
}

function formatUserSourceStatus(status) {
  const labels = {
    pending: "ожидает обработки",
    success: "успешно",
    error: "ошибка",
    insufficient_data: "данных недостаточно",
  };
  return labels[status] || status || "ожидает обработки";
}

function syncUserSourceStatuses(reportSources) {
  if (!Array.isArray(reportSources) || reportSources.length === 0) return;
  const byId = new Map(reportSources.map((source) => [source.id, source]));
  userSources = userSources.map((source) => byId.get(source.id) || source);
  renderUserSourcesManager();
}

function formatUserSourceStatus(status) {
  const labels = {
    pending: "ожидает обработки",
    success: "успешно",
    error: "ошибка",
    insufficient_data: "данных недостаточно",
  };
  return labels[status] || status || "ожидает обработки";
}

// 1. Универсальная функция инициализации и наполнения кастомного селекта
function initCustomSelect(wrapperId, triggerId, optionsBoxId, hiddenInputId, dataArray, defaultText) {
  const wrapper = document.getElementById(wrapperId);
  const trigger = document.getElementById(triggerId);
  const optionsBox = document.getElementById(optionsBoxId);
  const hiddenInput = document.getElementById(hiddenInputId);

  if (!wrapper || !optionsBox || !hiddenInput || !trigger) return;

  wrapper.style.display = "block";
  optionsBox.innerHTML = ""; // Очищаем старые пункты

  // Создаем дефолтный пункт (Все регионы / Все годы)
  const defaultOpt = document.createElement("div");
  defaultOpt.className = "custom-option selected";
  defaultOpt.textContent = defaultText;
  defaultOpt.dataset.value = "all";
  optionsBox.appendChild(defaultOpt);

  // Навешиваем клик на дефолтный пункт
  defaultOpt.addEventListener("click", (e) => handleOptionClick(e, defaultOpt, wrapper, trigger, hiddenInput));

  // Наполняем селект уникальными данными из массива
  dataArray.forEach(item => {
    const opt = document.createElement("div");
    opt.className = "custom-option";
    opt.textContent = item;
    opt.dataset.value = item;
    optionsBox.appendChild(opt);

    // Навешиваем клик на каждый созданный пункт
    opt.addEventListener("click", (e) => handleOptionClick(e, opt, wrapper, trigger, hiddenInput));
  });
}

// 2. Вспомогательная функция обработки клика по пункту списка
function handleOptionClick(e, option, wrapper, trigger, hiddenInput) {
  e.stopPropagation();

  const optionsBox = option.parentElement;
  
  // Меняем активный класс у элементов списка
  optionsBox.querySelectorAll(".custom-option").forEach(o => o.classList.remove("selected"));
  option.classList.add("selected");

  // Меняем текст на триггере и пишем значение в скрытый инпут
  trigger.querySelector("span").textContent = option.textContent;
  hiddenInput.value = option.dataset.value;

  // Закрываем рамку и запускаем фильтрацию
  wrapper.classList.remove("open");
  applyFilters();
}

// 3. Функции подготовки данных (вызываются при рендере источников)
function setupYearFilter(sources) {
  const years = [...new Set(sources.map(s => s.year).filter(Boolean))].sort((a, b) => b - a);
  initCustomSelect("wrapperYear", "triggerYear", "optionsYear", "filterYear", years, "Все годы");
}

// Функция-помощник, которая оставляет только область/край/город
function cleanLocationName(locationStr) {
  if (!locationStr) return "";
  
  let loc = String(locationStr).trim();

  // Обработка городов федерального значения
  if (loc.includes("Москва")) return "Москва";
  if (loc.includes("Санкт-Петербург") || loc.includes("Спб") || loc.includes("СПб")) return "Санкт-Петербург";
  if (loc.includes("Севастополь")) return "Севастополь";

  // Если адрес длинный и разделен запятыми
  if (loc.includes(",")) {
    const parts = loc.split(",");
    // Ищем ту часть, где есть упоминание области, края или республики
    const regionPart = parts.find(p => 
      /обл|край|респ|автономный|ао/i.test(p)
    );
    // Если нашли — берем её, если нет — забираем самую первую часть адреса
    loc = regionPart ? regionPart.trim() : parts[0].trim();
  }

  // Делаем первую букву заглавной для красоты
  return loc.charAt(0).toUpperCase() + loc.slice(1);
}

function setupLocationFilter(sources) {
  // Прогоняем каждый адрес через очиститель
  const cleanedLocations = sources
    .map(s => cleanLocationName(s.location))
    .filter(Boolean); // выкидываем пустые строки, если они есть

  // Убираем дубликаты (чтобы не было три раза "Свердловская обл.") и сортируем
  const uniqueLocations = [...new Set(cleanedLocations)].sort();

  // Передаем чистый массив в кастомный селект
  initCustomSelect("wrapperLocation", "triggerLocation", "optionsLocation", "filterLocation", uniqueLocations, "Все регионы");
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
