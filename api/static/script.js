const byId = (id) => document.getElementById(id);

const REQUEST_TIMEOUT_MS = 15 * 60 * 1000;

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
  subjectValue: byId("subjectValue"),
  priceValue: byId("priceValue"),
  medianValue: byId("medianValue"),
  deviationValue: byId("deviationValue"),
  commentSection: byId("commentSection"),
  detailsGrid: byId("detailsGrid"),
  documentSection: byId("documentSection"),
  documentInfoGrid: byId("documentInfoGrid"),
  textPreview: byId("textPreview"),
  warningsSection: byId("warningsSection"),
  warningsList: byId("warningsList"),
  sourcesList: byId("sourcesList"),
};

let currentMode = "manual";
let abortController = null;
let timeoutId = null;
let requestTimedOut = false;

function init() {
  bindModeToggle();
  bindAiToggle();
  bindFileInput();
  bindForm();
  setMode("manual");
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

function setMode(mode) {
  currentMode = mode;
  const isManual = mode === "manual";

  elements.modeToggle?.querySelectorAll(".mode-btn").forEach((button) => {
    button.classList.toggle("active", button.dataset.mode === mode);
  });

  elements.manualFields?.classList.toggle("hidden", !isManual);
  elements.documentFields?.classList.toggle("hidden", isManual);
  elements.documentSection?.classList.toggle("hidden", isManual);
  elements.submitBtn.textContent = isManual ? "Начать анализ" : "Загрузить и проанализировать";
  elements.loadingText.textContent = isManual
    ? "Анализируем рынок..."
    : "Обрабатываем документ и проверяем рынок...";
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
  const details = {
    Категория: data.category,
    Год: data.year,
    Состояние: data.condition,
    Локация: data.location,
    Валюта: data.currency,
    ...normalizeObject(data.specs || {}),
  };

  elements.resultTitle.textContent = subject;
  elements.subjectValue.textContent = subject;
  elements.priceValue.textContent = formatPrice(marketReport.client_price ?? fallbackClientPrice, data.currency);
  elements.medianValue.textContent = formatPrice(marketReport.median_price, data.currency);
  elements.deviationValue.textContent = formatDeviation(
    marketReport.client_price ?? fallbackClientPrice,
    marketReport.median_price,
    data.currency
  );
  elements.commentSection.innerHTML = `
    <strong>${escapeHtml(marketReport.client_price_ok === true ? "Цена в рынке" : marketReport.client_price_ok === false ? "Цена вне рынка" : "Недостаточно данных")}</strong>
    <div style="margin-top: 8px;">${escapeHtml(marketReport.explanation || "Комментарий рынка отсутствует.")}</div>
  `;

  renderDetails(elements.detailsGrid, details, "Характеристики появятся после анализа");
  renderSources(data.sources || []);

  elements.documentSection.classList.add("hidden");
  elements.warningsSection.classList.add("hidden");
  elements.resultContent.classList.add("show");
}

function renderDocumentResult(data) {
  const priceCheck = data.price_check || {};
  const marketReport = data.market_report || {};
  const subject = data.item_name || marketReport.item || "Предмет не определен";

  elements.resultTitle.textContent = subject;
  elements.subjectValue.textContent = subject;
  elements.priceValue.textContent = formatPrice(data.declared_price, data.currency);
  elements.medianValue.textContent = formatPrice(priceCheck.market_median_price, data.currency);
  elements.deviationValue.textContent = formatDeviation(priceCheck.deviation_amount, priceCheck.market_median_price, data.currency, priceCheck.deviation_percent);
  elements.commentSection.innerHTML = `
    <strong>${escapeHtml(priceCheck.verdict || "Нет итогового вывода")}</strong>
    <div style="margin-top: 8px;">${escapeHtml(marketReport.explanation || "Комментарий рынка отсутствует.")}</div>
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

function renderSources(sources) {
  if (!sources.length) {
    elements.sourcesList.innerHTML = '<div class="empty-note">Источники не найдены</div>';
    return;
  }

  elements.sourcesList.innerHTML = sources
    .map((source) => {
      const meta = [source.source, source.year, source.condition, source.location].filter(Boolean);
      const title = source.title || "Источник";
      const price = source.price_str || formatPrice(source.price);
      const url = source.url || "#";

      return `
        <div class="source-card">
          <div class="source-card-head">
            <a class="source-title" href="${escapeAttribute(url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(title)}</a>
            <div class="source-price">${escapeHtml(price)}</div>
          </div>
          <div class="source-meta">
            ${meta.map((item) => `<span>${escapeHtml(String(item))}</span>`).join("")}
          </div>
        </div>
      `;
    })
    .join("");
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

function formatDeviation(clientPrice, medianPrice, currency = "RUB", percentOverride = null) {
  const client = Number(clientPrice);
  const median = Number(medianPrice);
  const percent = Number(percentOverride);

  if (Number.isFinite(percent) && Number.isFinite(client)) {
    const amount = `${client > 0 ? "+" : ""}${formatPrice(client, currency)}`;
    return `${amount} · ${percent > 0 ? "+" : ""}${percent.toFixed(1)}%`;
  }

  if (!Number.isFinite(client) || !Number.isFinite(median) || median === 0) return "—";

  const diff = client - median;
  const diffPercent = (diff / median) * 100;
  return `${diff > 0 ? "+" : ""}${formatPrice(diff, currency)} · ${diffPercent > 0 ? "+" : ""}${diffPercent.toFixed(1)}%`;
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
