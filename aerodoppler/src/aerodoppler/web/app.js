const state = { analysis: null, history: null, location: null };
const locateOut = document.querySelector("#locate-out");
const audioOut = document.querySelector("#audio-out");
const historyOut = document.querySelector("#history-out");
const compareOut = document.querySelector("#compare-out");

const REQUEST_TIMEOUT_MS = 45000;

async function fetchLimited(url, options) {
  try {
    return await fetch(url, { ...options, signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS) });
  } catch (error) {
    if (error.name === "TimeoutError" || error.name === "AbortError") {
      throw new Error("La petición superó el tiempo de espera. Prueba de nuevo.");
    }
    throw new Error("No hay conexión con el servidor. Comprueba la red e inténtalo otra vez.");
  }
}

async function postJSON(url, payload) {
  const response = await fetchLimited(url, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "Accept": "application/json" },
    body: JSON.stringify(payload),
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.message || "La petición falló.");
  return body;
}

function download(filename, text, type) {
  const blob = new Blob([text], { type });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = filename;
  link.textContent = filename;
  return link;
}

document.querySelector("#locate-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = document.querySelector("#locate-submit");
  const problems = [];
  const audioField = document.querySelector("#locate-audio");
  if (!audioField.files || !audioField.files[0]) problems.push("Elige un archivo de audio.");
  if (!document.querySelector("#locate-when").value) problems.push("Indica la fecha y hora de inicio.");
  const radius = Number(document.querySelector("#locate-radius").value);
  const uncertainty = Number(document.querySelector("#locate-uncertainty").value);
  const altitude = document.querySelector("#locate-alt").value;
  if (!Number.isFinite(radius) || radius < 1 || radius > 40) problems.push("El radio de búsqueda tiene que estar entre 1 y 40 km.");
  if (!Number.isFinite(uncertainty) || uncertainty < 0 || uncertainty > 60) problems.push("La incertidumbre tiene que estar entre 0 y 60 minutos.");
  if (altitude === "" || !Number.isFinite(Number(altitude))) problems.push("Indica la altitud supuesta de la zona.");
  if (problems.length) {
    showLocateMessage(problems.join(" "));
    return;
  }
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  showLocateMessage("Analizando el audio. La zona marcada no es la respuesta.");
  try {
  const body = new FormData();
  body.append("file", audioField.files[0]);
  const analysisResponse = await fetchLimited("/api/analyze", { method: "POST", body, credentials: "same-origin" });
  const analysis = await analysisResponse.json().catch(() => ({}));
  if (!analysisResponse.ok) {
    showLocateMessage(analysis.message || "No se pudo leer el audio.");
    return;
  }
  state.analysis = analysis;
  if (analysis.status !== "accepted") {
    renderLocation({
      status: "no_usable_event",
      message: analysis.message || "El audio no tiene una pista utilizable.",
      hypotheses: [],
      prior_is_result: false,
    });
    return;
  }
  const place = document.querySelector("#locate-place").value;
  showLocateMessage("Consultando trayectorias históricas en una zona más amplia que la búsqueda.");
  let history;
  try {
    history = await postJSON("/api/history", {
      place,
      lat: document.querySelector("#locate-lat").value,
      lon: document.querySelector("#locate-lon").value,
      local_datetime: document.querySelector("#locate-when").value,
      window_min: Math.min(180, uncertainty + 3),
      radius_km: Math.min(120, radius + 25),
      provider: "adsb_aero",
    });
  } catch (error) {
    renderLocation({ status: "incomplete", message: error.message, hypotheses: [], prior_is_result: false });
    return;
  }
  state.history = history;
  if (!history.flights) {
    renderLocation({
      status: "incomplete",
      reason: history.status,
      message: history.message,
      hypotheses: [],
      prior_is_result: false,
      zero_flights_claim: false,
    });
    return;
  }
    showLocateMessage("Puntuando celdas. El centro de la zona no se toma como micrófono.");
  try {
    const found = await postJSON("/api/locate", {
      ridge: { ...analysis.ridge, status: analysis.status },
      flights: history.flights,
      prior: {
        lat: Number(document.querySelector("#locate-lat").value),
        lon: Number(document.querySelector("#locate-lon").value),
        radius_km: radius,
      },
      recording_start_local: document.querySelector("#locate-when").value,
      uncertainty_min: uncertainty,
      altitude_m: document.querySelector("#locate-alt").value,
      history_status: history.status,
    });
    state.location = found;
    renderLocation(found);
  } catch (error) {
    showLocateMessage(error.message || "No se pudo completar la búsqueda.");
  }
  } catch (error) {
    showLocateMessage(error.message || "No se pudo completar la búsqueda.");
  } finally {
    button.disabled = false;
    button.removeAttribute("aria-busy");
  }
});

function showLocateMessage(text) {
  locateOut.replaceChildren();
  const message = document.createElement("p");
  message.className = "error";
  message.textContent = text;
  locateOut.append(message);
}

function renderLocation(result) {
  const card = document.createElement("div");
  card.className = "card";
  const title = document.createElement("h3");
  title.textContent = "Estado: " + (result.status || "sin estado");
  const message = document.createElement("p");
  message.textContent = result.message || "";
  const note = document.createElement("p");
  note.textContent = "Identificación única: no. El centro de búsqueda no es un punto recuperado.";
  card.append(title, message, note);
  if (result.resolution_m) {
    const resolution = document.createElement("p");
    resolution.textContent = "Resolución de la rejilla: " + Math.round(result.resolution_m) + " m.";
    card.append(resolution);
  }
  const table = document.createElement("table");
  table.innerHTML = "<tr><th>Hipótesis</th><th>Latitud</th><th>Longitud</th><th>RMSE</th></tr>";
  (result.hypotheses || []).forEach((item, index) => {
    const row = document.createElement("tr");
    row.innerHTML = "<td></td><td></td><td></td><td></td>";
    const cells = row.querySelectorAll("td");
    cells[0].textContent = String(index + 1);
    cells[1].textContent = Number(item.lat).toFixed(4);
    cells[2].textContent = Number(item.lon).toFixed(4);
    cells[3].textContent = item.rmse_hz == null ? "" : Number(item.rmse_hz).toFixed(2) + " Hz";
    table.append(row);
  });
  card.append(table);
  if (result.csv) card.append(download("hipotesis.csv", result.csv, "text/csv"));
  card.append(download("busqueda.json", JSON.stringify(result, null, 2), "application/json"));
  locateOut.replaceChildren(card);
  drawSearch(result.hypotheses || []);
}

function drawSearch(hypotheses) {
  const canvas = document.querySelector("#locate-map");
  if (!canvas) return;
  const context = canvas.getContext("2d");
  context.clearRect(0, 0, canvas.width, canvas.height);
  const lat = Number(document.querySelector("#locate-lat").value);
  const lon = Number(document.querySelector("#locate-lon").value);
  const radius = Number(document.querySelector("#locate-radius").value) || 20;
  if (!Number.isFinite(lat) || !Number.isFinite(lon)) return;
  const kx = 111.32 * Math.cos(lat * Math.PI / 180);
  const ky = 110.57;
  const span = radius * 1.2;
  const project = (pointLat, pointLon) => [
    canvas.width / 2 + ((pointLon - lon) * kx) / span * (canvas.width * 0.4),
    canvas.height / 2 - ((pointLat - lat) * ky) / span * (canvas.height * 0.4),
  ];
  context.strokeStyle = "#314052";
  context.beginPath();
  context.arc(canvas.width / 2, canvas.height / 2, canvas.width * 0.4 * (radius / span), 0, Math.PI * 2);
  context.stroke();
  context.fillStyle = "#8fb4d6";
  context.fillText("zona de búsqueda", 16, 24);
  hypotheses.forEach((item, index) => {
    const [x, y] = project(item.lat, item.lon);
    context.fillStyle = index === 0 ? "#f0c674" : "#7dcea0";
    context.fillRect(x - 5, y - 5, 10, 10);
  });
}

document.querySelector("#audio-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  audioOut.textContent = "Analizando en este servidor. El archivo no se conserva.";
  const body = new FormData(event.target);
  const response = await fetch("/api/analyze", { method: "POST", body, credentials: "same-origin" });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    audioOut.textContent = payload.message || "No se pudo analizar.";
    return;
  }
  state.analysis = payload;
  audioOut.innerHTML = "";
  const card = document.createElement("div");
  card.className = "card";
  const title = document.createElement("h3");
  title.textContent = payload.status === "accepted" ? "Pista utilizable para comparar" : "Pista rechazada";
  card.append(title);
  const message = document.createElement("p");
  message.textContent = payload.message;
  card.append(message);
  if (payload.reasons && payload.reasons.length) {
    const reasons = document.createElement("p");
    reasons.textContent = "Motivos: " + payload.reasons.join(", ");
    card.append(reasons);
  }
  if (payload.fit) {
    const fit = document.createElement("p");
    fit.textContent = `RMSE ${payload.fit.rmse_hz.toFixed(2)} Hz, R² ${payload.fit.r2.toFixed(3)}, duración ${payload.fit.duration_s.toFixed(1)} s, f0 ${payload.fit.f0_hz.toFixed(1)} Hz.`;
    card.append(fit);
  }
  if (payload.spectrogram_png_base64) {
    const image = document.createElement("img");
    image.alt = "Espectrograma de la grabación";
    image.src = "data:image/png;base64," + payload.spectrogram_png_base64;
    card.append(image);
  }
  card.append(download("pista.csv", payload.csv || "", "text/csv"));
  card.append(download("analisis.json", JSON.stringify(payload, null, 2), "application/json"));
  audioOut.append(card);
});

document.querySelector("#preset").addEventListener("click", () => {
  document.querySelector("#place").value = "Cabrerizos";
  document.querySelector("#lat").value = "40.97786";
  document.querySelector("#lon").value = "-5.60845";
  document.querySelector("#when").value = "2026-11-07T23:30";
  document.querySelector("#window").value = "30";
  document.querySelector("#radius").value = "80";
  document.querySelector("#provider").value = "adsb_aero";
  document.querySelector("#record-start").value = "2026-11-07T23:30";
});

document.querySelector("#history-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  historyOut.textContent = "Consultando el proveedor seleccionado.";
  const form = new FormData(event.target);
  const payload = Object.fromEntries(form.entries());
  try {
    const result = await postJSON("/api/history", payload);
    state.history = result;
    renderHistory(result);
  } catch (error) {
    historyOut.textContent = error.message;
  }
});

document.querySelector("#live").addEventListener("click", async () => {
  historyOut.textContent = "Consultando el tráfico en vivo. Esto no es el 7 de noviembre.";
  try {
    const result = await postJSON("/api/live", {
      lat: document.querySelector("#lat").value,
      lon: document.querySelector("#lon").value,
      radius_nm: 43,
    });
    const card = document.createElement("div");
    card.className = "card";
    const title = document.createElement("h3");
    title.textContent = "En vivo, no historia";
    const message = document.createElement("p");
    message.textContent = result.message || "";
    const count = document.createElement("p");
    count.textContent = "Aeronaves en la respuesta: " + (result.count ?? "sin cifra") + ".";
    card.append(title, message, count);
    historyOut.replaceChildren(card);
  } catch (error) {
    historyOut.textContent = error.message;
  }
});

function renderHistory(result) {
  const card = document.createElement("div");
  card.className = "card";
  const title = document.createElement("h3");
  title.textContent = "Estado: " + result.status;
  card.append(title);
  const message = document.createElement("p");
  message.textContent = result.message || "";
  card.append(message);
  const utc = document.createElement("p");
  utc.textContent = `UTC pedido: ${result.requested.start_utc} → ${result.requested.end_utc}. Cobertura: ${result.coverage}.`;
  card.append(utc);
  const table = document.createElement("table");
  table.innerHTML = "<tr><th>Indicativo</th><th>Tipo</th><th>Clase</th><th>Vértice más cercano</th></tr>";
  for (const flight of result.flights || []) {
    const near = flight.nearest_vertex_in_window;
    const distance = near ? (near.ground_m / 1000).toFixed(1) + " km" : "sin vértice";
    const outside = flight.vertices_outside_requested_radius ? " (vértices fuera del radio)" : "";
    const row = document.createElement("tr");
    row.innerHTML = `<td></td><td></td><td></td><td></td>`;
    const cells = row.querySelectorAll("td");
    cells[0].textContent = flight.callsign || flight.icao24 || "";
    cells[1].textContent = flight.icao_type || "";
    cells[2].textContent = flight.engine_class || "";
    cells[3].textContent = distance + outside;
    table.append(row);
  }
  card.append(table);
  if (result.csv) card.append(download("vuelos.csv", result.csv, "text/csv"));
  card.append(download("historia.json", JSON.stringify(result, null, 2), "application/json"));
  historyOut.replaceChildren(card);
  drawMap(result.flights || []);
}

document.querySelector("#elevation").addEventListener("click", async () => {
  try {
    const result = await postJSON("/api/elevation", {
      lat: document.querySelector("#lat").value,
      lon: document.querySelector("#lon").value,
    });
    document.querySelector("#observer-alt").value = String(Math.round(result.alt_m));
    compareOut.textContent = result.note;
  } catch (error) {
    compareOut.textContent = error.message;
  }
});

document.querySelector("#compare-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.history || !state.history.flights) {
    compareOut.textContent = "Primero hace falta una consulta histórica con trayectorias.";
    return;
  }
  compareOut.textContent = "Calculando distancias, retardo y residuos.";
  const ridge = state.analysis && state.analysis.ridge ? state.analysis.ridge : null;
  try {
    const result = await postJSON("/api/compare", {
      observer: {
        lat: Number(document.querySelector("#lat").value),
        lon: Number(document.querySelector("#lon").value),
        alt_m: document.querySelector("#observer-alt").value,
      },
      flights: state.history.flights,
      ridge,
      recording_start_local: document.querySelector("#record-start").value,
      window_start_unix: state.history.requested ? Date.parse(state.history.requested.start_utc) / 1000 : null,
      window_end_unix: state.history.requested ? Date.parse(state.history.requested.end_utc) / 1000 : null,
      f0_hz: document.querySelector("#f0").value || (state.analysis && state.analysis.fit ? state.analysis.fit.f0_hz : null),
    });
    renderCompare(result);
  } catch (error) {
    compareOut.textContent = error.message;
  }
});

function renderCompare(result) {
  const card = document.createElement("div");
  card.className = "card";
  const title = document.createElement("h3");
  title.textContent = "Estado: " + result.status;
  card.append(title);
  const message = document.createElement("p");
  message.className = "warn";
  message.textContent = result.message + " Identificación única: no.";
  card.append(message);
  const note = document.createElement("p");
  note.textContent = result.datum_note;
  card.append(note);
  const table = document.createElement("table");
  table.innerHTML = "<tr><th>Vuelo</th><th>Distancia</th><th>Retardo</th><th>RMSE</th><th>Ranking</th></tr>";
  for (const flight of result.flights || []) {
    const row = document.createElement("tr");
    row.innerHTML = "<td></td><td></td><td></td><td></td><td></td>";
    const cells = row.querySelectorAll("td");
    cells[0].textContent = flight.callsign || flight.icao24 || "";
    cells[1].textContent = flight.closest_m == null ? "" : (flight.closest_m / 1000).toFixed(1) + " km";
    cells[2].textContent = flight.propagation_delay_s == null ? "" : flight.propagation_delay_s.toFixed(1) + " s";
    cells[3].textContent = flight.rmse_hz == null ? "" : flight.rmse_hz.toFixed(2) + " Hz";
    cells[4].textContent = flight.rank_status || flight.geometry_status || "";
    table.append(row);
  }
  card.append(table);
  card.append(download("comparacion.csv", result.csv || "", "text/csv"));
  card.append(download("comparacion.json", JSON.stringify(result, null, 2), "application/json"));
  compareOut.replaceChildren(card);
  drawMap(state.history.flights || [], result);
}

function drawMap(flights, comparison) {
  const canvas = document.querySelector("#map");
  const context = canvas.getContext("2d");
  context.clearRect(0, 0, canvas.width, canvas.height);
  const lat = Number(document.querySelector("#lat").value);
  const lon = Number(document.querySelector("#lon").value);
  const radius = Number(document.querySelector("#radius").value) || 80;
  if (!Number.isFinite(lat) || !Number.isFinite(lon)) return;
  const kx = 111.32 * Math.cos(lat * Math.PI / 180);
  const ky = 110.57;
  const span = radius * 1.25;
  const project = (pointLat, pointLon) => [
    canvas.width / 2 + ((pointLon - lon) * kx) / span * (canvas.width * 0.42),
    canvas.height / 2 - ((pointLat - lat) * ky) / span * (canvas.height * 0.42),
  ];
  context.strokeStyle = "#314052";
  context.beginPath();
  context.arc(canvas.width / 2, canvas.height / 2, canvas.width * 0.42 * (radius / span), 0, Math.PI * 2);
  context.stroke();
  context.fillStyle = "#7dcea0";
  const observer = project(lat, lon);
  context.fillRect(observer[0] - 4, observer[1] - 4, 8, 8);
  flights.forEach((flight, index) => {
    context.beginPath();
    context.strokeStyle = flight.acoustic_eligible ? "#f0c674" : "#8fb4d6";
    (flight.points || []).forEach((point, order) => {
      const [x, y] = project(point.lat, point.lon);
      if (order === 0) context.moveTo(x, y);
      else context.lineTo(x, y);
    });
    context.stroke();
    context.fillStyle = "#f4f7fb";
    context.fillText((flight.callsign || flight.icao24 || String(index)).slice(0, 8), observer[0], 24 + index * 16);
  });
  if (comparison) {
    context.fillStyle = "#b7c3d1";
    context.fillText(comparison.status, 16, canvas.height - 20);
  }
}

document.querySelector("#logout").addEventListener("click", async () => {
  await fetch("/logout", { method: "POST", credentials: "same-origin" });
  window.location.assign("/login");
});
