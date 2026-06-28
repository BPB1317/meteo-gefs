/**
 * app.js — Météo Probabiliste
 * Données d'ensemble GEFS via Open-Meteo · AuRA & BFC
 */

const API_BASE =
  window.location.hostname === "localhost" || window.location.hostname === "127.0.0.1"
    ? "http://localhost:8000"
    : "";

// ── State global ──────────────────────────────────────────────────────────────

let hourlyChart          = null;
let tempChart            = null;
let rainChart            = null;
let currentHourlyData    = null;  // conservé pour le toggle graphique/tableau
let hourlyView           = "table";
let hourlyDays           = 5;     // nombre de jours affichés dans le tableau

// Carte Leaflet
let leafletMap    = null;
let mapMarkers    = {};      // { cityName: L.Marker }
let mapData       = null;    // réponse de /forecast/map
let currentHourIdx = 0;
let animInterval  = null;
let animSpeed     = 200;     // ms entre chaque frame
let ciMode        = "p10-p90"; // ou "p25-p75"

// ── Initialisation ────────────────────────────────────────────────────────────

document.addEventListener("DOMContentLoaded", () => {
  initMap();
  loadMapData();
  loadCities();
  document.getElementById("citySelect").addEventListener("change", onCityChange);
  document.getElementById("daysSelect").addEventListener("change", onCityChange);
});

// ── Villes ────────────────────────────────────────────────────────────────────

async function loadCities() {
  try {
    const res = await apiFetch("/cities");
    const cities = await res.json();

    const select = document.getElementById("citySelect");
    const regions = [...new Set(cities.map((c) => c.region))].sort();
    regions.forEach((region) => {
      const group = document.createElement("optgroup");
      group.label = region;
      cities
        .filter((c) => c.region === region)
        .sort((a, b) => a.name.localeCompare(b.name, "fr"))
        .forEach((city) => {
          const opt = document.createElement("option");
          opt.value = city.name;
          opt.textContent = city.name;
          group.appendChild(opt);
        });
      select.appendChild(group);
    });

    const defaultCity = cities.find((c) => c.name === "Lyon") || cities[0];
    if (defaultCity) {
      select.value = defaultCity.name;
      await loadForecast(defaultCity.name, 7);
    }
  } catch (err) {
    showError("Impossible de charger les villes : " + err.message);
  }
}

async function onCityChange() {
  const city = document.getElementById("citySelect").value;
  const days = parseInt(document.getElementById("daysSelect").value, 10);
  if (city) await loadForecast(city, days);
}

// ── Données ville ─────────────────────────────────────────────────────────────

async function loadForecast(cityName, days) {
  setLoading(true);
  hideError();

  try {
    const [dailyRes, hourlyRes] = await Promise.all([
      apiFetch(`/forecast?city=${encodeURIComponent(cityName)}&days=${days}`),
      apiFetch(`/forecast/hourly?city=${encodeURIComponent(cityName)}&hours=384`),
    ]);

    if (dailyRes.status === 503) {
      const err = await dailyRes.json();
      showError(err.detail || "Données non disponibles — réessayez dans quelques minutes.");
      return;
    }
    if (!dailyRes.ok) {
      const err = await dailyRes.json();
      throw new Error(err.detail || `Erreur HTTP ${dailyRes.status}`);
    }

    const daily  = await dailyRes.json();
    const hourly = hourlyRes.ok ? await hourlyRes.json() : null;

    renderAll(daily, hourly);
  } catch (err) {
    showError(err.message);
  } finally {
    setLoading(false);
  }
}

// ── Rendu principal ───────────────────────────────────────────────────────────

function renderAll(data, hourlyData) {
  const { forecast, run_time } = data;

  const runDt = new Date(run_time);
  document.getElementById("runInfo").textContent =
    `Run GEFS : ${runDt.toLocaleDateString("fr-FR")} ${runDt.toLocaleTimeString("fr-FR", {
      hour: "2-digit", minute: "2-digit",
    })} UTC`;

  const labels = forecast.map((d) => labelDate(d.date));

  renderSummaryCards(forecast);
  if (hourlyData && hourlyData.forecast && hourlyData.forecast.length > 0) {
    currentHourlyData = hourlyData.forecast;
    renderHourlySection();
  }
  renderTempChart(labels, forecast);
  renderRainChart(labels, forecast);
  renderTable(forecast);

  document.getElementById("content").hidden = false;
}

// ── Cartes résumé ─────────────────────────────────────────────────────────────

function renderSummaryCards(forecast) {
  const container = document.getElementById("summaryCards");
  container.innerHTML = "";

  forecast.slice(0, 5).forEach((day) => {
    const prob = Math.round(day.precip_prob * 100);
    const rainClass = prob >= 60 ? "rain-high" : prob >= 30 ? "rain-med" : "rain-low";
    const rainLabel = prob >= 60 ? `🌧 ${prob}%` : prob >= 30 ? `🌦 ${prob}%` : `☀️ ${prob}%`;

    // Tmax/Tmin si disponibles, sinon temp_max/temp_min
    const tmax = (day.tmax_mean ?? day.temp_max).toFixed(0);
    const tmin = (day.tmin_mean ?? day.temp_min).toFixed(0);

    const card = document.createElement("div");
    card.className = "summary-card";
    card.innerHTML = `
      <div class="sc-date">${shortDate(day.date)}</div>
      <div class="sc-tmax col-hot">${tmax}° max</div>
      <div class="sc-temp">${day.temp_mean.toFixed(0)}° moy</div>
      <div class="sc-tmin col-cold">${tmin}° min</div>
      <div class="sc-rain ${rainClass}">${rainLabel}</div>
    `;
    container.appendChild(card);
  });
}

// ── Section horaire : toggle graphique / tableau ──────────────────────────────

function setHourlyView(mode, btn) {
  hourlyView = mode;
  document.querySelectorAll(".vt-btn").forEach((b) => b.classList.remove("active"));
  btn.classList.add("active");
  document.getElementById("hourlyChartWrap").hidden = (mode !== "chart");
  document.getElementById("hourlyTableWrap").hidden = (mode !== "table");
  if (currentHourlyData) renderHourlySection();
}

function setHourlyDays(btn) {
  document.querySelectorAll("#hourlyDayBtns .ci-btn").forEach((b) => b.classList.remove("active"));
  btn.classList.add("active");
  hourlyDays = parseInt(btn.dataset.days, 10);
  if (currentHourlyData) renderHourlySection();
}

function renderHourlySection() {
  if (!currentHourlyData) return;
  if (hourlyView === "chart") {
    renderHourlyChart(currentHourlyData);
  } else {
    renderHourlyTable(currentHourlyData);
  }
}

// ── Graphique horaire (5 jours) ───────────────────────────────────────────────

function renderHourlyChart(forecast) {
  const ctx = document.getElementById("hourlyChart").getContext("2d");
  if (hourlyChart) hourlyChart.destroy();

  const now = new Date();
  forecast = forecast.filter(h => new Date(h.time) >= now).slice(0, hourlyDays * 24);

  const labels  = forecast.map((h) => {
    const d = new Date(h.time);
    // Toutes les 6h montrer date+heure, sinon juste l'heure
    const hh = d.getHours();
    if (hh === 0)  return d.toLocaleDateString("fr-FR", { day: "numeric", month: "short" });
    if (hh % 6 === 0) return `${String(hh).padStart(2,"0")}h`;
    return "";
  });

  const p10 = forecast.map((h) => round1(h.t_p10));
  const p25 = forecast.map((h) => round1(h.t_p25));
  const p50 = forecast.map((h) => round1(h.t_p50));
  const p75 = forecast.map((h) => round1(h.t_p75));
  const p90 = forecast.map((h) => round1(h.t_p90));

  hourlyChart = new Chart(ctx, {
    type: "line",
    data: {
      labels,
      datasets: [
        {
          label: "P10",
          data: p10,
          borderWidth: 0,
          pointRadius: 0,
          fill: "+1",
          backgroundColor: "rgba(59,130,246,0.10)",
          tension: 0.3,
        },
        {
          label: "P90",
          data: p90,
          borderWidth: 0,
          pointRadius: 0,
          fill: false,
          tension: 0.3,
        },
        {
          label: "P25",
          data: p25,
          borderWidth: 0,
          pointRadius: 0,
          fill: "+1",
          backgroundColor: "rgba(59,130,246,0.25)",
          tension: 0.3,
        },
        {
          label: "P75",
          data: p75,
          borderWidth: 0,
          pointRadius: 0,
          fill: false,
          tension: 0.3,
        },
        {
          label: "Médiane (P50)",
          data: p50,
          borderColor: "#3b82f6",
          borderWidth: 2,
          pointRadius: 0,
          fill: false,
          tension: 0.3,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: {
          labels: {
            filter: (item) => !["P10","P25","P75","P90"].includes(item.text),
            color: "#94a3b8",
          },
        },
        tooltip: {
          backgroundColor: "#1e293b",
          borderColor: "#334155",
          borderWidth: 1,
          titleColor: "#e2e8f0",
          bodyColor: "#94a3b8",
          callbacks: {
            title: (items) => {
              const h = forecast[items[0].dataIndex];
              return new Date(h.time).toLocaleString("fr-FR", {
                weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
              });
            },
            label: () => null,
            afterBody: (items) => {
              const idx = items[0].dataIndex;
              const h = forecast[idx];
              const precip = Math.round(h.precip_prob * 100);
              return [
                `  P10 : ${p10[idx]?.toFixed(1)} °C`,
                `  P25 : ${p25[idx]?.toFixed(1)} °C`,
                `  Médiane : ${p50[idx]?.toFixed(1)} °C`,
                `  P75 : ${p75[idx]?.toFixed(1)} °C`,
                `  P90 : ${p90[idx]?.toFixed(1)} °C`,
                `  Pluie : ${precip} % (${h.precip_mean?.toFixed(1)} mm)`,
              ];
            },
          },
        },
      },
      scales: {
        x: {
          grid: { color: "rgba(148,163,184,0.06)" },
          ticks: {
            color: "#94a3b8",
            maxRotation: 0,
            autoSkip: false,
            callback: (val, idx) => labels[idx],
          },
        },
        y: {
          title: { display: true, text: "°C", color: "#94a3b8" },
          grid: { color: "rgba(148,163,184,0.08)" },
          ticks: { color: "#94a3b8", callback: (v) => v + "°" },
        },
      },
    },
  });
}

// ── Graphique journalier Tmax/Tmoy/Tmin ──────────────────────────────────────

function renderTempChart(labels, forecast) {
  const ctx = document.getElementById("tempChart").getContext("2d");
  if (tempChart) tempChart.destroy();

  const tmaxP10  = forecast.map((d) => round1(d.tmax_p10  ?? d.temp_min));
  const tmaxMean = forecast.map((d) => round1(d.tmax_mean ?? d.temp_max));
  const tmaxP90  = forecast.map((d) => round1(d.tmax_p90  ?? d.temp_max));
  const tmean    = forecast.map((d) => round1(d.temp_mean));
  const tminP10  = forecast.map((d) => round1(d.tmin_p10  ?? d.temp_min));
  const tminMean = forecast.map((d) => round1(d.tmin_mean ?? d.temp_min));
  const tminP90  = forecast.map((d) => round1(d.tmin_p90  ?? d.temp_max));

  tempChart = new Chart(ctx, {
    type: "line",
    data: {
      labels,
      datasets: [
        // Bande Tmax (P10→P90)
        { label: "_tmaxP10", data: tmaxP10, borderWidth:0, pointRadius:0, fill:"+1", backgroundColor:"rgba(251,146,60,0.12)", tension:0.3 },
        { label: "_tmaxP90", data: tmaxP90, borderWidth:0, pointRadius:0, fill:false, tension:0.3 },
        // Tmax médiane
        { label: "Tmax moy.", data: tmaxMean, borderColor:"#fb923c", borderWidth:2, pointRadius:3,
          pointBackgroundColor:"#fb923c", pointBorderColor:"#0f172a", pointBorderWidth:1.5, fill:false, tension:0.3 },
        // Température moyenne
        { label: "Tmoy.", data: tmean, borderColor:"#a78bfa", borderWidth:2, pointRadius:3,
          pointBackgroundColor:"#a78bfa", pointBorderColor:"#0f172a", pointBorderWidth:1.5, fill:false, tension:0.3,
          borderDash:[4,3] },
        // Bande Tmin (P10→P90)
        { label: "_tminP10", data: tminP10, borderWidth:0, pointRadius:0, fill:"+1", backgroundColor:"rgba(59,130,246,0.12)", tension:0.3 },
        { label: "_tminP90", data: tminP90, borderWidth:0, pointRadius:0, fill:false, tension:0.3 },
        // Tmin médiane
        { label: "Tmin moy.", data: tminMean, borderColor:"#60a5fa", borderWidth:2, pointRadius:3,
          pointBackgroundColor:"#60a5fa", pointBorderColor:"#0f172a", pointBorderWidth:1.5, fill:false, tension:0.3 },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: {
          labels: {
            filter: (item) => !item.text.startsWith("_"),
            color: "#94a3b8",
          },
        },
        tooltip: {
          backgroundColor: "#1e293b",
          borderColor: "#334155",
          borderWidth: 1,
          titleColor: "#e2e8f0",
          bodyColor: "#94a3b8",
          callbacks: {
            label: () => null,
            afterBody: (items) => {
              const idx = items[0].dataIndex;
              return [
                `  Tmax : ${tmaxP10[idx]?.toFixed(1)}–${tmaxP90[idx]?.toFixed(1)} °C (moy: ${tmaxMean[idx]?.toFixed(1)}°)`,
                `  Tmoy : ${tmean[idx]?.toFixed(1)} °C`,
                `  Tmin : ${tminP10[idx]?.toFixed(1)}–${tminP90[idx]?.toFixed(1)} °C (moy: ${tminMean[idx]?.toFixed(1)}°)`,
              ];
            },
          },
        },
      },
      scales: {
        x: {
          grid: { color: "rgba(148,163,184,0.08)" },
          ticks: { color: "#94a3b8", maxRotation: 30 },
        },
        y: {
          title: { display: true, text: "°C", color: "#94a3b8" },
          grid: { color: "rgba(148,163,184,0.08)" },
          ticks: { color: "#94a3b8", callback: (v) => v + "°" },
        },
      },
    },
  });
}

// ── Graphique pluie ───────────────────────────────────────────────────────────

function renderRainChart(labels, forecast) {
  const ctx = document.getElementById("rainChart").getContext("2d");
  if (rainChart) rainChart.destroy();

  const probs = forecast.map((d) => Math.round(d.precip_prob * 100));
  const colors = probs.map((p) =>
    p >= 70 ? "#2563eb" : p >= 40 ? "#60a5fa" : p >= 20 ? "#93c5fd" : "#dbeafe"
  );

  rainChart = new Chart(ctx, {
    type: "bar",
    data: {
      labels,
      datasets: [{ label: "P(pluie > 1mm)", data: probs, backgroundColor: colors, borderRadius: 5, borderSkipped: false }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: "#1e293b",
          borderColor: "#334155",
          borderWidth: 1,
          titleColor: "#e2e8f0",
          bodyColor: "#94a3b8",
          callbacks: {
            label: (ctx) => {
              const d = forecast[ctx.dataIndex];
              return [`  Probabilité : ${ctx.parsed.y} %`, `  Pluie moy. : ${d.precip_mean.toFixed(1)} mm`];
            },
          },
        },
      },
      scales: {
        x: { grid: { display: false }, ticks: { color: "#94a3b8", maxRotation: 30 } },
        y: {
          min: 0, max: 100,
          title: { display: true, text: "%", color: "#94a3b8" },
          grid: { color: "rgba(148,163,184,0.08)" },
          ticks: { color: "#94a3b8", callback: (v) => v + "%" },
        },
      },
    },
  });
}

// ── Tableau ───────────────────────────────────────────────────────────────────

function renderTable(forecast) {
  const tbody = document.querySelector("#forecastTable tbody");
  tbody.innerHTML = "";

  forecast.forEach((day) => {
    const prob = Math.round(day.precip_prob * 100);
    const rainEmoji = prob >= 70 ? "🌧" : prob >= 40 ? "🌦" : prob >= 15 ? "🌤" : "☀️";
    const probClass = prob >= 60 ? "rain-high" : prob >= 30 ? "rain-med" : "rain-low";
    const tmax = (day.tmax_mean ?? day.temp_max).toFixed(1);
    const tmin = (day.tmin_mean ?? day.temp_min).toFixed(1);

    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${fullDate(day.date)}</td>
      <td class="col-hot"><strong>${tmax} °C</strong></td>
      <td>${day.temp_mean.toFixed(1)} °C</td>
      <td class="col-cold"><strong>${tmin} °C</strong></td>
      <td class="${probClass}">${rainEmoji} ${prob} %</td>
      <td>${day.precip_mean.toFixed(1)} mm</td>
      <td class="col-muted">${day.member_count}</td>
    `;
    tbody.appendChild(tr);
  });
}

// ── Tableau horaire style Meteociel ───────────────────────────────────────────

function renderHourlyTable(forecast) {
  const wrap = document.getElementById("hourlyTableWrap");

  // Supprimer les heures passées, limiter au nombre de jours sélectionné
  const now = new Date();
  const rows = forecast
    .filter(h => new Date(h.time) >= now)
    .slice(0, hourlyDays * 24);

  // Grouper par jour
  const byDay = {};
  const dayOrder = [];
  rows.forEach((h) => {
    const d = new Date(h.time);
    const key = d.toLocaleDateString("fr-FR", { weekday: "short", day: "numeric", month: "short" });
    if (!byDay[key]) { byDay[key] = []; dayOrder.push(key); }
    byDay[key].push(h);
  });

  let html = `
    <table class="hourly-table">
      <thead>
        <tr>
          <th>Jour</th>
          <th>Heure</th>
          <th>Temp.</th>
          <th>Intervalle</th>
          <th>P(pluie)</th>
          <th>Pluie</th>
        </tr>
      </thead>
      <tbody>`;

  dayOrder.forEach((day) => {
    const hours = byDay[day];
    hours.forEach((h, i) => {
      const d    = new Date(h.time);
      const hStr = String(d.getHours()).padStart(2, "0") + "h";
      const temp = Math.round(h.t_p50 ?? 0);
      const bg   = tempToColor(temp);
      const fg   = (temp <= 12 || temp >= 33) ? "#fff" : "#0f172a";

      const lo = ciMode === "p25-p75"
        ? Math.round(h.t_p25 ?? h.t_p10)
        : Math.round(h.t_p10 ?? 0);
      const hi = ciMode === "p25-p75"
        ? Math.round(h.t_p75 ?? h.t_p90)
        : Math.round(h.t_p90 ?? 0);

      const prob      = Math.round((h.precip_prob ?? 0) * 100);
      const rainMm    = (h.precip_mean ?? 0).toFixed(1);
      const probClass = prob >= 60 ? "rain-high" : prob >= 30 ? "rain-med" : "";
      const rainDisp  = prob >= 15 ? `${rainMm} mm` : "—";

      const dayCell = i === 0
        ? `<td class="ht-day" rowspan="${hours.length}">${day}</td>`
        : "";

      html += `
        <tr>
          ${dayCell}
          <td class="ht-hour">${hStr}</td>
          <td class="ht-temp" style="background:${bg};color:${fg}">${temp} °C</td>
          <td class="ht-range">${lo}° – ${hi}°</td>
          <td class="ht-prob ${probClass}">${prob} %</td>
          <td class="ht-rain">${rainDisp}</td>
        </tr>`;
    });
  });

  html += "</tbody></table>";
  wrap.innerHTML = html;
}

// ══════════════════════════════════════════════════════════════════════════════
// CARTE ANIMÉE LEAFLET
// ══════════════════════════════════════════════════════════════════════════════

function initMap() {
  leafletMap = L.map("leafletMap", {
    center: [45.9, 5.0],
    zoom: 7,
    zoomControl: true,
  });

  // Tuiles CartoDB sombre — cohérent avec le thème dark de l'app
  L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", {
    attribution: '© <a href="https://www.openstreetmap.org/copyright">OSM</a> © <a href="https://carto.com">CARTO</a>',
    subdomains: "abcd",
    maxZoom: 19,
  }).addTo(leafletMap);
}

async function loadMapData() {
  try {
    const res = await apiFetch("/forecast/map");
    if (!res.ok) return; // Données pas encore disponibles
    mapData = await res.json();

    if (mapData && mapData.cities && mapData.cities.length > 0) {
      buildMapMarkers();
      updateMapForHour(0);
      document.getElementById("timeSlider").max = mapData.times.length - 1;
    }
  } catch (err) {
    // La carte reste vide jusqu'à la prochaine tentative — pas bloquant
    console.warn("Map data unavailable:", err.message);
  }
}

function buildMapMarkers() {
  // Supprimer les anciens marqueurs
  Object.values(mapMarkers).forEach((m) => leafletMap.removeLayer(m));
  mapMarkers = {};

  mapData.cities.forEach((city) => {
    const marker = L.marker([city.lat, city.lon], {
      icon: makeTempIcon(0, "#888"),
    }).addTo(leafletMap);

    marker.on("click", () => openCityPopup(city, currentHourIdx, marker));
    mapMarkers[city.name] = marker;
  });
}

function updateMapForHour(idx) {
  if (!mapData || !mapData.cities.length) return;

  idx = Math.max(0, Math.min(idx, mapData.times.length - 1));
  currentHourIdx = idx;

  // Mettre à jour le slider et le label de temps
  document.getElementById("timeSlider").value = idx;
  const t = new Date(mapData.times[idx]);
  document.getElementById("mapTimeLabel").textContent =
    t.toLocaleString("fr-FR", { weekday:"short", day:"numeric", month:"short", hour:"2-digit", minute:"2-digit" });

  mapData.cities.forEach((city) => {
    const temp = city.t_p50[idx];
    if (temp == null) return;
    const color = tempToColor(temp);
    const marker = mapMarkers[city.name];
    if (marker) {
      marker.setIcon(makeTempIcon(Math.round(temp), color));
    }
  });
}

function openCityPopup(city, idx, marker) {
  const t = new Date(mapData.times[idx]);
  const timeStr = t.toLocaleString("fr-FR", { weekday:"short", hour:"2-digit", minute:"2-digit" });

  const lo = ciMode === "p25-p75" ? city.t_p25[idx] : city.t_p10[idx];
  const hi = ciMode === "p25-p75" ? city.t_p75[idx] : city.t_p90[idx];
  const med = city.t_p50[idx];
  const precip = Math.round((city.precip_prob[idx] ?? 0) * 100);
  const ciLabel = ciMode === "p25-p75" ? "P25–P75" : "P10–P90";

  marker.bindPopup(`
    <div class="map-popup">
      <strong>${city.name}</strong>
      <div class="mp-time">${timeStr}</div>
      <div class="mp-temp">${med != null ? Math.round(med) + "°C" : "—"}</div>
      <div class="mp-ci">${ciLabel} : ${lo != null ? Math.round(lo) : "?"}° – ${hi != null ? Math.round(hi) : "?"}°</div>
      <div class="mp-rain">🌧 ${precip}% de pluie</div>
    </div>
  `, { className: "leaflet-dark-popup" }).openPopup();
}

function makeTempIcon(tempRounded, color) {
  return L.divIcon({
    className: "",
    html: `<div class="city-marker" style="background:${color}">${tempRounded}°</div>`,
    iconSize: [48, 24],
    iconAnchor: [24, 12],
    popupAnchor: [0, -14],
  });
}

function tempToColor(t) {
  if (t <= 10) return "#3b82f6";
  if (t <= 15) return interpolateColor("#3b82f6", "#34d399", (t - 10) / 5);
  if (t <= 20) return interpolateColor("#34d399", "#a3e635", (t - 15) / 5);
  if (t <= 25) return interpolateColor("#a3e635", "#facc15", (t - 20) / 5);
  if (t <= 30) return interpolateColor("#facc15", "#fb923c", (t - 25) / 5);
  if (t <= 35) return interpolateColor("#fb923c", "#ef4444", (t - 30) / 5);
  if (t <= 40) return interpolateColor("#ef4444", "#991b1b", (t - 35) / 5);
  return "#991b1b";
}

function interpolateColor(hex1, hex2, t) {
  const c1 = hexToRgb(hex1), c2 = hexToRgb(hex2);
  const r = Math.round(c1.r + (c2.r - c1.r) * t);
  const g = Math.round(c1.g + (c2.g - c1.g) * t);
  const b = Math.round(c1.b + (c2.b - c1.b) * t);
  return `rgb(${r},${g},${b})`;
}

function hexToRgb(hex) {
  const n = parseInt(hex.replace("#", ""), 16);
  return { r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255 };
}

// ── Contrôles animation ───────────────────────────────────────────────────────

function togglePlay() {
  if (animInterval) {
    pauseAnimation();
  } else {
    playAnimation();
  }
}

function playAnimation() {
  if (!mapData) return;
  document.getElementById("btnPlay").textContent = "⏸ Pause";
  animInterval = setInterval(() => {
    const next = (currentHourIdx + 1) % mapData.times.length;
    updateMapForHour(next);
  }, animSpeed);
}

function pauseAnimation() {
  clearInterval(animInterval);
  animInterval = null;
  document.getElementById("btnPlay").textContent = "▶ Animation";
}

function stepMap(delta) {
  pauseAnimation();
  updateMapForHour(currentHourIdx + delta);
}

function onSlider(val) {
  pauseAnimation();
  updateMapForHour(parseInt(val, 10));
}

function setSpeed(ms) {
  animSpeed = parseInt(ms, 10);
  if (animInterval) {
    pauseAnimation();
    playAnimation();
  }
}

function setCIMode(btn) {
  document.querySelectorAll(".ci-btn").forEach((b) => b.classList.remove("active"));
  btn.classList.add("active");
  ciMode = btn.dataset.ci;
}

// ── Utilitaires ───────────────────────────────────────────────────────────────

async function apiFetch(path) {
  return fetch(API_BASE + path);
}

function labelDate(dateStr) {
  const d = new Date(dateStr + "T12:00:00");
  return d.toLocaleDateString("fr-FR", { weekday: "short", day: "numeric", month: "short" });
}

function shortDate(dateStr) {
  const d = new Date(dateStr + "T12:00:00");
  return d.toLocaleDateString("fr-FR", { weekday: "short", day: "numeric" });
}

function fullDate(dateStr) {
  const d = new Date(dateStr + "T12:00:00");
  return d.toLocaleDateString("fr-FR", { weekday: "long", day: "numeric", month: "long" });
}

function round1(v) {
  return v != null ? Math.round(v * 10) / 10 : null;
}

function setLoading(show) {
  document.getElementById("loading").hidden = !show;
  if (show) {
    document.getElementById("content").hidden = true;
  } else {
    document.getElementById("loading").hidden = true;
  }
}

function showError(msg) {
  const el = document.getElementById("errorMsg");
  el.textContent = "⚠ " + msg;
  el.hidden = false;
}

function hideError() {
  document.getElementById("errorMsg").hidden = true;
}
