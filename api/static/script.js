const getById = (id) => document.getElementById(id);
const ensureElementById = (id, tagName = "div") => {
  let element = getById(id);
  if (!element) {
    element = document.createElement(tagName);
    element.id = id;
    element.hidden = true;
    document.body.appendChild(element);
  }
  return element;
};
const setTextById = (id, value) => {
  const element = getById(id);
  if (element) element.textContent = value;
};
const setHtmlById = (id, value) => {
  const element = getById(id);
  if (element) element.innerHTML = value;
};
const setDisabled = (element, value) => {
  if (element) element.disabled = value;
};
const form = getById("analyzeForm");
const error = ensureElementById("error");
const placeholder = ensureElementById("placeholder");
const loading = ensureElementById("loading");
const resultContent = ensureElementById("resultContent");
const analogSection = document.querySelector(".analogs-section");
const comparisonSection = getById("bestComparisonSection");
const prevBtn = document.querySelector(".slider-btn.prev") || document.createElement("button");
const nextBtn = document.querySelector(".slider-btn.next") || document.createElement("button");
ensureElementById("useAI", "input");
ensureElementById("deviationValue");
ensureElementById("commentSection");
ensureElementById("analogCard");
ensureElementById("analogCounter");
ensureElementById("sourcesList", "ul");
ensureElementById("allOffersSection");

let currentAnalogIndex = 0;
let analogsData = [];
let loadingInterval = null;
const REQUEST_TIMEOUT_MS = 15 * 60 * 1000;

// ✅ ДОБАВЛЕНЫ: Переменные для управления запросом
let abortController = null;
let timeoutId = null;

if (analogSection) {
  analogSection.style.display = "none";
}
if (comparisonSection) {
  comparisonSection.style.display = "none";
}
setTextById("step2", "Характеристики модели");
setTextById("step4", "Проверка источников");

// ===== ПЕРЕКЛЮЧАТЕЛЬ ИИ =====
document.querySelectorAll(".ai-btn").forEach((btn) => {
  btn.addEventListener("click", (e) => {
    e.preventDefault();
    document.querySelectorAll(".ai-btn").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    const useAIInput = getById("useAI");
    if (useAIInput) {
      useAIInput.value = btn.dataset.value;
    }
  });
});

// ===== АНИМАЦИЯ ЗАГРУЗКИ =====
function startLoadingAnimation() {
  const steps = ["step1", "step2", "step3", "step4"];
  let currentStep = 0;
  // Reset all steps
  steps.forEach(id => {
    const el = document.getElementById(id);
    if (el) {
      el.classList.remove("active", "done");
    }
  });
  // Activate first step
  const firstStep = document.getElementById(steps[0]);
  if (firstStep) firstStep.classList.add("active");

  loadingInterval = setInterval(() => {
    // Mark current as done
    const currentEl = document.getElementById(steps[currentStep]);
    if (currentEl) {
      currentEl.classList.remove("active");
      currentEl.classList.add("done");
    }
    // Move to next
    currentStep++;
    if (currentStep < steps.length) {
      const nextEl = document.getElementById(steps[currentStep]);
      if (nextEl) nextEl.classList.add("active");
    } else {
      // Loop back
      currentStep = 0;
      steps.forEach(id => {
        const el = document.getElementById(id);
        if (el) el.classList.remove("done");
      });
      const firstEl = document.getElementById(steps[0]);
      if (firstEl) firstEl.classList.add("active");
    }
  }, 2500);
}

function stopLoadingAnimation() {
  if (loadingInterval) {
    clearInterval(loadingInterval);
    loadingInterval = null;
  }
}

// ===== ВАЛИДАЦИЯ ФОРМЫ =====
function validateForm() {
  const itemInput = getById("item");
  const clientPriceInput = getById("clientPrice");
  const numResultsInput = getById("numResults");
  if (!itemInput || !clientPriceInput || !numResultsInput || !error) {
    return false;
  }

  const item = itemInput.value.trim();
  const clientPrice = clientPriceInput.value.trim();
  const numResults = parseInt(numResultsInput.value, 10);

  if (!item || item.length < 3) {
    error.textContent = "❌ Описание должно содержать минимум 3 символа";
    error.classList.add("show");
    return false;
  }

  if (item.length > 500) {
    error.textContent = "❌ Описание не должно превышать 500 символов";
    error.classList.add("show");
    return false;
  }

  if (clientPrice) {
    const price = parseInt(clientPrice, 10);
    if (isNaN(price) || price < 0 || price > 10 ** 12) {
      error.textContent = "❌ Цена должна быть числом от 0 до 1 триллиона";
      error.classList.add("show");
      return false;
    }
  }

  if (isNaN(numResults) || numResults < 1 || numResults > 10) {
    error.textContent = "❌ Количество результатов должно быть от 1 до 10";
    error.classList.add("show");
    return false;
  }

  return true;
}

// ===== ОТПРАВКА ФОРМЫ =====
if (form) {
form.addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!error || !placeholder || !loading || !resultContent) {
    console.error("[ERROR] Required UI elements are missing");
    return;
  }
  error.classList.remove("show");

  if (!validateForm()) {
    return;
  }

  const submitButton = form.querySelector("button[type='submit']") || form.querySelector("button");
  setDisabled(submitButton, true);
  console.log("[DEBUG] Форма отправлена");
  placeholder.classList.add("hidden");
  resultContent.classList.remove("show");
  loading.classList.add("show");
  startLoadingAnimation();

  const item = (getById("item")?.value || "").trim();
  const clientPrice = parseInt(getById("clientPrice")?.value, 10) || null;
  const useAI = getById("useAI")?.value === "true";
  const numResults = parseInt(getById("numResults")?.value, 10) || 5;
  let requestTimedOut = false;

  try {
    // ✅ ИСПРАВЛЕНО: Создание новых controller и timeout для каждого запроса
    abortController = new AbortController();

    // Таймаут 5 минут (300000 мс) на случай долгого анализа
    timeoutId = setTimeout(() => {
      requestTimedOut = true;
      abortController.abort();
      error.textContent = "⏱️ Время ожидания истекло. Попробуйте снова.";
      error.classList.add("show");
    }, REQUEST_TIMEOUT_MS);

    const resp = await fetch("/api/describe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: item, clientPrice, useAI, numResults }),
      signal: abortController.signal,
    });

    clearTimeout(timeoutId);
    timeoutId = null;

    if (!resp.ok) {
      // ✅ ИСПРАВЛЕНО: Показать ошибку сервера
      const errorData = await resp.json().catch(() => ({}));
      throw new Error(errorData.detail || `Ошибка: ${resp.status} ${resp.statusText}`);
    }

    const data = await resp.json();
    console.log("[DEBUG] Данные распарсены:", JSON.stringify(data, null, 2));

    // ✅ Задержка для эффекта
    await new Promise((resolve) => setTimeout(resolve, 1200));

    analogsData = data.analogs_details || [];
    currentAnalogIndex = 0;

    render(data, clientPrice);
    renderSources(data.sources || []);
    renderBestOriginal(data);
    renderBestComparison(data);
    renderAllOffers(data.sources || []);
    stopLoadingAnimation();
    loading.classList.remove("show");
    resultContent.classList.add("show");
    return;

    if (analogsData.length > 0) {
      showAnalog(0);
    } else {
      setHtmlById("analogCard", "<p style='color: var(--muted)'>Аналоги не найдены</p>");
      setDisabled(prevBtn, true);
      setDisabled(nextBtn, true);
      updateAnalogCounter();
    }
    return;

    // ✅ ИСПРАВЛЕНО: Остановка анимации загрузки
    stopLoadingAnimation();
    loading.classList.remove("show");
    console.error("[ERROR] Анализ завершился с ошибкой:", err);
    if (requestTimedOut) {
      error.textContent = "⏱️ Клиентский таймаут истек до ответа сервера. Сервер мог завершить анализ позже.";
      error.classList.add("show");
      return;
    }
    if (err?.name === "AbortError") {
      error.textContent = "❌ Запрос был отменен. Проверьте соединение.";
      error.classList.add("show");
      return;
    }
    error.textContent = `❌ Ошибка: ${err?.message || "Неизвестная ошибка"}`;
    error.classList.add("show");
    return;
    if (false) {
    console.error("[ERROR] Анализ завершился с ошибкой:", err);
    if (requestTimedOut) {
      error.textContent = "⏱️ Клиентский таймаут истек до ответа сервера. Сервер мог завершить анализ позже.";
      error.classList.add("show");
      return;
    }
    if (err?.name === "AbortError") {
      error.textContent = "❌ Запрос был отменен. Проверьте соединение.";
      error.classList.add("show");
      return;
    }
    error.textContent = `❌ Ошибка: ${err?.message || "Неизвестная ошибка"}`;
    error.classList.add("show");
    return;
    }
    resultContent.classList.add("show");

    if (analogsData.length > 0) {
      showAnalog(0);
    } else {
      document.getElementById("analogCard").innerHTML = "<p style='color: var(--muted)'>Аналоги не найдены</p>";
      prevBtn.disabled = true;
      nextBtn.disabled = true;
      updateAnalogCounter();
    }
  } catch (err) {
    stopLoadingAnimation();
    loading.classList.remove("show");
    console.error("[ERROR] Анализ завершился с ошибкой:", err);

    if (requestTimedOut) {
      error.textContent = "⏱️ Клиентский таймаут истек до ответа сервера. Сервер мог завершить анализ позже.";
      error.classList.add("show");
      return;
    }

    if (err?.name === "AbortError") {
      error.textContent = "❌ Запрос был отменен. Проверьте соединение.";
      error.classList.add("show");
      return;
    }

    error.textContent = `❌ Ошибка: ${err?.message || "Неизвестная ошибка"}`;
    error.classList.add("show");
    return;
    stopLoadingAnimation();
    loading.classList.remove("show");

    console.error("[ERROR] Ошибка при анализе:", err);

    if (err.name === "AbortError" && requestTimedOut) {
      error.textContent = "❌ Запрос был отменен. Проверьте соединение.";
    } else if (err.name === "AbortError") {
      error.textContent = `❌ Ошибка: ${err.message || "Неизвестная ошибка"}`;
    }
    error.classList.add("show");
  } finally {
    if (timeoutId) {
      clearTimeout(timeoutId);
      timeoutId = null;
    }
    setDisabled(submitButton, false);
  }
});
}

// ===== РЕНДЕР МЕТРИК =====
function render(data, clientPrice) {
  console.log("[DEBUG] Рендеринг данных:", data);
  const setText = (id, value) => {
    const element = document.getElementById(id);
    if (element) {
      element.textContent = value;
    }
  };
  const titleEl = document.getElementById("resultTitle");
  if (titleEl) {
    const itemName = data.vendor && data.model
      ? `${data.vendor} ${data.model}`
      : data.market_report?.item || "Результат анализа";
    titleEl.textContent = `📊 ${itemName}`;
  }

  const marketReport = data.market_report || {};
  const minPrice = marketReport.market_range ? marketReport.market_range[0] : null;
  const maxPrice = marketReport.market_range ? marketReport.market_range[1] : null;
  const medianPrice = marketReport.median_price;

  const formatPrice = (price) => {
    if (!price) return "—";
    return `${price.toLocaleString("ru-RU")} ₽`;
  };

  if (minPrice && maxPrice) {
    setText("rangeValue", `${formatPrice(minPrice)} – ${formatPrice(maxPrice)}`);
  } else {
    setText("rangeValue", "—");
  }

  setText("medianValue", formatPrice(medianPrice));
  setText("clientValue", formatPrice(clientPrice));

  if (clientPrice && medianPrice) {
    const deviation = Math.round(((clientPrice - medianPrice) / medianPrice) * 100);
    const deviationText = deviation > 0 ? `+${deviation}%` : `${deviation}%`;
    const color = Math.abs(deviation) <= 20 ? "#10b981" : "#f43f5e";
    setHtmlById("deviationValue", `<span style="color: ${color}; font-weight: 700;">${deviationText}</span>`);
  } else {
    setTextById("deviationValue", "—");
  }

  let comment = "";
  if (data.vendor || data.model) {
    comment = `<strong style="font-size: 16px;">${data.vendor || ""} ${data.model || ""}</strong>`;
  }
  if (data.category) comment += `<br>📂 <strong>Категория:</strong> ${data.category}`;
  if (data.year) comment += `<br>📅 <strong>Год:</strong> ${data.year}`;
  if (data.condition) comment += `<br>⚙️ <strong>Состояние:</strong> ${data.condition}`;

  if (data.location) {
    comment += `<br><strong>Расположение:</strong> ${data.location}`;
  }

  const specsEntries = Object.entries(data.specs || {}).filter(([, value]) => value);
  if (specsEntries.length > 0) {
    const specsHtml = specsEntries
      .slice(0, 8)
      .map(([key, value]) => `<div style="padding: 10px 12px; background: var(--glass); border-radius: 10px;"><strong>${key}:</strong> ${value}</div>`)
      .join("");
    comment += `<br><br><strong>Характеристики:</strong><div style="display: grid; gap: 8px; margin-top: 10px;">${specsHtml}</div>`;
  }

  if (marketReport.explanation) {
    comment += `<br><br><div style="padding: 12px; background: rgba(59, 130, 246, 0.1); border-radius: 8px; margin-top: 8px;"><strong>💡 Рыночная оценка:</strong><br>${marketReport.explanation}</div>`;
  }

  setHtmlById("commentSection", comment || "Данные загружены");
}

// ===== ПОКАЗАТЬ АНАЛОГ =====
function showAnalog(index) {
  if (analogsData.length === 0) {
    document.getElementById("analogCard").innerHTML = "<p style='color: var(--muted)'>Аналоги не найдены</p>";
    prevBtn.disabled = true;
    nextBtn.disabled = true;
    updateAnalogCounter();
    return;
  }

  currentAnalogIndex = Math.max(0, Math.min(index, analogsData.length - 1));
  const analog = analogsData[currentAnalogIndex];

  let html = `<div class="analog-name">🔄 ${analog.name || "Аналог"}</div>`;

  // Sonar badge
  if (analog.sonar_info) {
    html += `<div class="sonar-badge">🤖 Найден через Sonar AI</div>`;
  }

  if (analog.avg_price_guess) {
    const price = analog.avg_price_guess.toLocaleString("ru-RU");
    html += `<div class="analog-price">${price} ₽</div>`;
  }

  // Price range from Sonar
  if (analog.sonar_info && analog.sonar_info.price_range) {
    html += `<div style="font-size: 13px; color: var(--text-secondary); margin: 8px 0; padding: 10px; background: var(--glass); border-radius: 8px;">💰 Диапазон цен: <strong>${analog.sonar_info.price_range}</strong></div>`;
  }

  if (analog.note) {
    html += `<div class="analog-note">📝 ${analog.note}</div>`;
  }

  // Key difference from Sonar
  if (analog.sonar_info && analog.sonar_info.key_difference) {
    html += `<div style="font-size: 13px; color: var(--accent); margin: 10px 0; padding: 12px; background: rgba(16, 185, 129, 0.1); border-radius: 10px; border-left: 4px solid var(--accent);">🔑 <strong>Ключевое отличие:</strong> ${analog.sonar_info.key_difference}</div>`;
  }

  if (analog.pros && analog.pros.length > 0) {
    html += '<div class="analog-pros">';
    html += '<div style="color: #10b981; font-size: 12px; font-weight: 700; margin-bottom: 8px; text-transform: uppercase;">✅ Плюсы</div>';
    html += '<ul class="analog-list">';
    analog.pros.forEach((p) => {
      html += `<li>+ ${p}</li>`;
    });
    html += `</ul></div>`;
  }

  if (analog.cons && analog.cons.length > 0) {
    html += '<div class="analog-cons">';
    html += '<div style="color: #f43f5e; font-size: 12px; font-weight: 700; margin-bottom: 8px; text-transform: uppercase;">❌ Минусы</div>';
    html += '<ul class="analog-list">';
    analog.cons.forEach((c) => {
      html += `<li>- ${c}</li>`;
    });
    html += `</ul></div>`;
  }

  setHtmlById("analogCard", html);
  updateAnalogCounter();
  setDisabled(prevBtn, currentAnalogIndex === 0);
  setDisabled(nextBtn, currentAnalogIndex === analogsData.length - 1);
}

function nextAnalog() {
  showAnalog(currentAnalogIndex + 1);
}

function prevAnalog() {
  showAnalog(currentAnalogIndex - 1);
}

function updateAnalogCounter() {
  const total = analogsData.length;
  const current = analogsData.length > 0 ? currentAnalogIndex + 1 : 0;
  const counterEl = document.getElementById("analogCounter");
  if (counterEl) {
    counterEl.textContent = `${current} / ${total}`;
  }
}

if (prevBtn) prevBtn.addEventListener("click", prevAnalog);
if (nextBtn) nextBtn.addEventListener("click", nextAnalog);

// ===== РЕНДЕР ИСТОЧНИКОВ =====
function renderSources(sources) {
  const list = document.getElementById("sourcesList");
  if (!list) return;

  list.innerHTML = "";

  if (!sources || sources.length === 0) {
    list.innerHTML = '<li style="color: var(--muted);">📭 Источники не найдены</li>';
    return;
  }

  sources.slice(0, 5).forEach((s) => {
    const li = document.createElement("li");
    const title = s.title || "Объявление";
    const src = s.source ? ` <span style="color: var(--muted);">(${s.source})</span>` : "";
    const price = s.price_str ? ` · <span style="color: var(--accent);">${s.price_str}</span>` : "";

    if (s.url) {
      li.innerHTML = `<a href="${s.url}" target="_blank" rel="noopener noreferrer">${title.substring(0, 60)}${title.length > 60 ? '...' : ''}</a>${src}${price}`;
    } else {
      li.textContent = `${title} ${src} ${price}`;
    }
    list.appendChild(li);
  });

  if (sources.length > 5) {
    const li = document.createElement("li");
    li.style.color = "var(--muted)";
    li.textContent = `... и еще ${sources.length - 5} источников`;
    list.appendChild(li);
  }
}

// ===== РЕНДЕР ЛУЧШЕГО ОРИГИНАЛЬНОГО ПРЕДЛОЖЕНИЯ =====
function renderBestOriginal(data) {
  const section = document.getElementById("bestOriginalSection");
  const card = document.getElementById("bestOriginalCard");
  if (!section || !card) return;

  const bestOffer = data.best_original_offer;
  const analysis = data.best_original_analysis;

  if (!bestOffer || !analysis) {
    section.classList.add("hidden");
    return;
  }

  section.classList.remove("hidden");
  let html = `<div class="best-offer-title">🎯 ${bestOffer.title || "Лучшее объявление"}</div>`;
  if (bestOffer.url) {
    html += `<div class="best-offer-url">🔗 <a href="${bestOffer.url}" target="_blank">${bestOffer.url.substring(0, 50)}...</a></div>`;
  }
  if (bestOffer.price_str) {
    html += `<div style="font-size: 16px; margin: 12px 0;">💰 Цена: <strong style="color: var(--accent);">${bestOffer.price_str}</strong></div>`;
  }

  const details = [];
  if (bestOffer.year) details.push(`📅 ${bestOffer.year}`);
  if (bestOffer.condition) details.push(`⚙️ ${bestOffer.condition}`);
  if (details.length) {
    html += `<div style="font-size: 13px; color: var(--text-secondary); margin: 8px 0;">${details.join(' • ')}</div>`;
  }

  const score = analysis.best_score || 0;
  html += `<div class="best-offer-score">⭐ ${score.toFixed(1)}/10</div>`;

  if (analysis.reason) {
    html += `<div class="best-offer-reason">${analysis.reason}</div>`;
  }

  card.innerHTML = html;
}

// ===== РЕНДЕР СРАВНЕНИЯ ЛУЧШИХ ПРЕДЛОЖЕНИЙ =====
function renderBestComparison(data) {
  const section = document.getElementById("bestComparisonSection");
  const content = document.getElementById("bestComparisonContent");
  if (!section || !content) return;

  const comparisons = data.best_offers_comparison;

  if (!comparisons || Object.keys(comparisons).length === 0) {
    section.classList.add("hidden");
    return;
  }

  section.classList.remove("hidden");
  content.innerHTML = "";

  for (const [analogName, comp] of Object.entries(comparisons)) {
    const div = document.createElement("div");
    div.className = "comparison-item";
    let winnerText = comp.winner === "original" ? "🏆 Оригинал лучше" :
      comp.winner === "analog" ? "🏆 Аналог лучше" : "🤝 Равные";

    let html = `<div class="comparison-header">`;
    html += `<div style="font-size: 15px;"><strong>Оригинал</strong> <span style="color: var(--muted);">vs</span> <strong>${analogName}</strong></div>`;
    html += `<div class="comparison-winner">${winnerText}</div>`;
    html += `</div>`;

    // Sonar badge
    if (comp.sonar_comparison) {
      html += `<div class="sonar-badge" style="margin-bottom: 16px;">🤖 Сравнение через Perplexity Sonar AI</div>`;
    }

    // Scores
    html += `<div class="comparison-scores">`;
    html += `<div class="comparison-score">📊 Оригинал: <strong>${(comp.original_score || 7).toFixed(1)}</strong>/10</div>`;
    html += `<div class="comparison-score">📊 Аналог: <strong>${(comp.analog_score || 7).toFixed(1)}</strong>/10</div>`;
    html += `</div>`;

    // Links to offers
    if (comp.original_url || comp.analog_url) {
      html += `<div class="comparison-links">`;
      if (comp.original_url) {
        html += `<div class="offer-link">`;
        html += `<strong>🔗 Оригинал:</strong><br>`;
        html += `<a href="${comp.original_url}" target="_blank">${(comp.original_title || comp.original_url).substring(0, 60)}...</a>`;
        if (comp.original_price_formatted) {
          html += ` <span style="color: var(--accent); font-weight: 600;">(${comp.original_price_formatted})</span>`;
        }
        html += `</div>`;
      }
      if (comp.analog_url) {
        html += `<div class="offer-link">`;
        html += `<strong>🔗 Аналог:</strong><br>`;
        html += `<a href="${comp.analog_url}" target="_blank">${(comp.analog_title || comp.analog_url).substring(0, 60)}...</a>`;
        if (comp.analog_price_formatted) {
          html += ` <span style="color: var(--accent); font-weight: 600;">(${comp.analog_price_formatted})</span>`;
        }
        html += `</div>`;
      }
      html += `</div>`;
    }

    // Price comparison
    if (comp.price_comparison) {
      const pc = comp.price_comparison;
      const origPrice = comp.original_price_formatted || (pc.original_price ? pc.original_price.toLocaleString("ru-RU") + " ₽" : "—");
      const analogPrice = comp.analog_price_formatted || (pc.analog_price ? pc.analog_price.toLocaleString("ru-RU") + " ₽" : "—");
      const diff = pc.price_diff || (pc.difference_percent ? `${pc.difference_percent > 0 ? "+" : ""}${pc.difference_percent.toFixed(1)}%` : "");

      html += `<div class="comparison-price">`;
      html += `<strong>💰 Сравнение цен:</strong><br>`;
      html += `<div style="margin-top: 10px; display: grid; grid-template-columns: 1fr 1fr; gap: 12px;">`;
      html += `<div style="padding: 10px; background: var(--glass); border-radius: 8px;">📌 Оригинал<br><strong style="font-size: 16px;">${origPrice}</strong></div>`;
      html += `<div style="padding: 10px; background: var(--glass); border-radius: 8px;">📌 Аналог<br><strong style="font-size: 16px;">${analogPrice}</strong></div>`;
      html += `</div>`;
      if (diff) {
        const diffColor = String(diff).includes("дешевле") ? "var(--accent)" : String(diff).includes("дороже") ? "var(--danger)" : "var(--text)";
        html += `<div style="margin-top: 12px; padding: 10px; background: var(--glass); border-radius: 8px; text-align: center;"><strong style="color: ${diffColor}; font-size: 15px;">📊 ${diff}</strong></div>`;
      }
      html += `</div>`;
    }

    // Ключевые различия
    if (comp.key_differences && comp.key_differences.length > 0) {
      html += `<div class="key-differences">`;
      html += `<h4>🔑 Ключевые отличия:</h4>`;
      html += `<ul>`;
      comp.key_differences.slice(0, 4).forEach(diff => {
        html += `<li>${diff}</li>`;
      });
      html += `</ul></div>`;
    }

    html += `<div class="comparison-pros-cons">`;
    if (comp.pros_original && comp.pros_original.length > 0) {
      html += `<div class="comparison-pros">
        <h4 style="color: var(--accent);">✓ Плюсы оригинала</h4>
        <ul>`;
      comp.pros_original.slice(0, 3).forEach(p => {
        html += `<li>${p}</li>`;
      });
      html += `</ul></div>`;
    }
    if (comp.pros_analog && comp.pros_analog.length > 0) {
      html += `<div class="comparison-pros">
        <h4 style="color: var(--accent);">✓ Плюсы аналога</h4>
        <ul>`;
      comp.pros_analog.slice(0, 3).forEach(p => {
        html += `<li>${p}</li>`;
      });
      html += `</ul></div>`;
    }
    if (comp.cons_original && comp.cons_original.length > 0) {
      html += `<div class="comparison-cons">
        <h4 style="color: var(--danger);">❌ Минусы оригинала</h4>
        <ul>`;
      comp.cons_original.slice(0, 3).forEach(c => {
        html += `<li>- ${c}</li>`;
      });
      html += `</ul></div>`;
    }
    if (comp.cons_analog && comp.cons_analog.length > 0) {
      html += `<div class="comparison-cons">
        <h4 style="color: var(--danger);">✗ Минусы аналога</h4>
        <ul>`;
      comp.cons_analog.slice(0, 3).forEach(c => {
        html += `<li>- ${c}</li>`;
      });
      html += `</ul></div>`;
    }
    html += `</div>`;

    if (comp.recommendation) {
      html += `<div class="comparison-recommendation">
        <strong>Рекомендация:</strong><br>${comp.recommendation}
      </div>`;
    }

    div.innerHTML = html;
    content.appendChild(div);
  }
}

// ===== РЕНДЕР ВСЕХ ОБЪЯВЛЕНИЙ =====
function renderAllOffers(sources) {
  const toggleBtn = document.getElementById("toggleAllOffers");
  const section = document.getElementById("allOffersSection");
  const list = document.getElementById("allOffersList");

  if (!toggleBtn || !section || !list) return;

  if (!sources || sources.length === 0) {
    toggleBtn.style.display = "none";
    return;
  }

  toggleBtn.style.display = "flex";
  list.innerHTML = "";

  sources.forEach((offer, index) => {
    const div = document.createElement("div");
    div.className = "offer-item";

    let html = `<div class="offer-item-header">
      <span class="offer-number">${index + 1}</span>`;

    if (offer.url) {
      html += `<a href="${offer.url}" target="_blank" rel="noopener noreferrer" class="offer-title-link">${(offer.title || "Объявление").substring(0, 50)}${(offer.title || "").length > 50 ? '...' : ''}</a>`;
    } else {
      html += `<span class="offer-title">${offer.title}</span>`;
    }
    html += `</div>`;

    html += `<div class="offer-item-details">`;
    if (offer.source) html += `<span class="offer-source">${offer.source}</span>`;
    if (offer.price_str) html += `<span class="offer-price">${offer.price_str}</span>`;
    if (offer.monthly_payment_str) html += `<span class="offer-payment">${offer.monthly_payment_str}</span>`;
    if (offer.year) html += `<span class="offer-year">${offer.year}</span>`;
    if (offer.condition) html += `<span class="offer-condition">${offer.condition}</span>`;
    if (offer.location) html += `<span class="offer-location">${offer.location}</span>`;
    html += `</div>`;

    div.innerHTML = html;
    list.appendChild(div);
  });

  const titleEl = document.getElementById("allOffersTitle");
  if (titleEl) {
    titleEl.textContent = `📋 Все объявления (${sources.length})`;
  }
}

// ===== ОБРАБОТЧИК КНОПКИ ПЕРЕКЛЮЧЕНИЯ =====
const mainToggleBtn = document.getElementById("toggleAllOffers");
if (mainToggleBtn) {
  mainToggleBtn.onclick = () => {
    const section = document.getElementById("allOffersSection");
    const isHidden = section.classList.contains("hidden");

    if (isHidden) {
      section.classList.remove("hidden");
      mainToggleBtn.innerHTML = "📋 Скрыть объявления";
    } else {
      section.classList.add("hidden");
      mainToggleBtn.innerHTML = `📋 Показать все объявления`;
    }
  };
}

// ===== KEYBOARD NAVIGATION =====
document.addEventListener("keydown", (e) => {
  if (e.key === "ArrowLeft") {
    prevAnalog();
  } else if (e.key === "ArrowRight") {
    nextAnalog();
  }
});
