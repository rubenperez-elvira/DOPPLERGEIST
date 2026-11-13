window.__errors = [];
window.addEventListener("error", (event) => { window.__errors.push(String(event.message)); });
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

document.querySelector("#locate-audio").addEventListener("change", async () => {
  const file = document.querySelector("#locate-audio").files[0];
  const note = document.querySelector("#locate-clock-note");
  if (!file) return;
  const found = await clipClockFromFile(file);
  if (!found) {
    note.textContent = "El archivo no trae una hora de grabación. Indica una ventana aproximada. La fecha de modificación del archivo no se usa.";
    return;
  }
  document.querySelector("#locate-when").value = found.localValue;
  note.textContent = found.note;
});

document.querySelector("#locate-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = document.querySelector("#locate-submit");
  const problems = [];
  const audioField = document.querySelector("#locate-audio");
  if (!audioField.files || !audioField.files[0]) problems.push("Elige un archivo de audio.");
  if (!document.querySelector("#locate-when").value) problems.push("Indica la fecha y hora de inicio. Si el archivo no la trae, vale una ventana aproximada.");
  const uncertainty = Number(document.querySelector("#locate-uncertainty").value);
  const altitude = document.querySelector("#locate-alt").value;
  if (!Number.isFinite(uncertainty) || uncertainty < 0 || uncertainty > 3) problems.push("La incertidumbre tiene que estar entre 0 y 3 horas.");
  if (altitude !== "" && !Number.isFinite(Number(altitude))) problems.push("La altitud supuesta no es un número.");
  if (problems.length) {
    showLocateMessage(problems.join(" "));
    return;
  }
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  showLocateMessage("Analizando el audio para averiguar dónde se grabó.");
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
      verdict: "failed",
      verdict_label: "Insuficiente",
      message: analysis.message || "El audio no tiene una pista utilizable.",
      summary: analysis.message || "El audio no tiene una pista utilizable.",
      hypotheses: [],
      prior_is_result: false,
      show_point: false,
    });
    return;
  }
  showLocateMessage("Comprobando si el área por defecto cabe en este servicio.");
  try {
    const ridges = Array.isArray(analysis.ridges) && analysis.ridges.length ? analysis.ridges : [analysis.ridge];
    const payload = {
      ridge: { ...ridges[0], status: analysis.status },
      events: ridges.length > 1 ? ridges.slice(0, 4).map((item) => ({ ...item, status: "accepted" })) : undefined,
      region: currentRegionSpec(),
      recording_start_local: document.querySelector("#locate-when").value,
      uncertainty_h: uncertainty,
    };
    if (altitude !== "") payload.altitude_m = altitude;
    const found = await postJSON("/api/locate", payload);
    state.location = found.job_id ? await pollLocateJob(found.job_id) : found;
    renderLocation(state.location);
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

async function clipClockFromFile(file) {
  const head = new Uint8Array(await file.slice(0, Math.min(file.size, 524288)).arrayBuffer());
  let found = clipClockFromBuffer(head);
  if (!found && file.size > 524288) {
    const tail = new Uint8Array(await file.slice(Math.max(0, file.size - 524288)).arrayBuffer());
    found = clipClockFromBuffer(tail);
  }
  return found;
}

function clipClockFromBuffer(bytes) {
  const view = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  const latin = new TextDecoder("latin1").decode(view);
  const id3 = readId3Stamp(view, latin);
  if (id3) return id3;
  const wav = readWavStamp(view, latin);
  if (wav) return wav;
  return readMvhdStamp(view, latin);
}

function readId3Stamp(view, latin) {
  if (view.length < 10 || latin.slice(0, 3) !== "ID3") return null;
  const frame = latin.indexOf("TDRC");
  if (frame < 10) return null;
  const size = (view[frame + 4] << 24) | (view[frame + 5] << 16) | (view[frame + 6] << 8) | view[frame + 7];
  if (size < 2 || frame + 10 + size > view.length) return null;
  const raw = latin.slice(frame + 11, frame + 10 + size).replace(/\u0000/g, "").trim();
  const parsed = parseStamp(raw);
  if (!parsed) return null;
  return {
    localValue: parsed.value,
    note: "Hora leída de la etiqueta ID3 del archivo, en este navegador. " + parsed.note + " Revísala: a veces es la hora del codificador, no la de la grabación.",
  };
}

function readWavStamp(view, latin) {
  const icrd = latin.indexOf("ICRD");
  if (icrd >= 0 && icrd + 8 < view.length) {
    const size = view[icrd + 4] | (view[icrd + 5] << 8) | (view[icrd + 6] << 16) | (view[icrd + 7] << 24);
    const raw = latin.slice(icrd + 8, icrd + 8 + Math.min(size, 32)).replace(/\u0000/g, "").trim();
    const parsed = parseStamp(raw);
    if (parsed) {
      return { localValue: parsed.value, note: "Hora leída del WAV (ICRD) en este navegador. " + parsed.note };
    }
  }
  const bext = latin.indexOf("bext");
  if (bext >= 0 && bext + 8 + 320 + 18 <= view.length) {
    const date = latin.slice(bext + 8 + 320, bext + 8 + 330);
    const clock = latin.slice(bext + 8 + 330, bext + 8 + 338);
    const parsed = parseStamp(date + "T" + clock);
    if (parsed) {
      return { localValue: parsed.value, note: "Hora leída de la cabecera bext del WAV en este navegador. " + parsed.note };
    }
  }
  return null;
}

function readMvhdStamp(view, latin) {
  const at = latin.indexOf("mvhd");
  if (at < 0 || at + 20 > view.length) return null;
  const version = view[at + 8];
  let seconds = 0;
  if (version === 0) {
    seconds = (view[at + 12] * 2 ** 24) + (view[at + 13] << 16) + (view[at + 14] << 8) + view[at + 15];
  } else if (at + 24 <= view.length) {
    seconds = (view[at + 16] * 2 ** 24) + (view[at + 17] << 16) + (view[at + 18] << 8) + view[at + 19];
  }
  if (!seconds) return null;
  const utc = new Date(Date.UTC(1904, 0, 1) + seconds * 1000);
  if (Number.isNaN(utc.getTime())) return null;
  const madrid = new Date(utc.getTime() + 60 * 60 * 1000);
  const value = madrid.toISOString().slice(0, 16);
  return {
    localValue: value,
    note: "Hora de creación del contenedor MP4, leída en este navegador. Suele ser la del codificador, no la de la grabación. Se muestra en Europe/Madrid (UTC+1, sin horario de verano).",
  };
}

function parseStamp(raw) {
  const text = String(raw || "").trim();
  if (!text) return null;
  const zulu = /Z$|[+-]\d{2}:?\d{2}$/.test(text);
  const match = text.match(/(\d{4})-?(\d{2})-?(\d{2})[T ]?(\d{2})?:?(\d{2})?/);
  if (!match) return null;
  const value = match[1] + "-" + match[2] + "-" + match[3] + "T" + (match[4] || "00") + ":" + (match[5] || "00");
  if (zulu) {
    const utc = new Date(text.endsWith("Z") || text.includes("T") ? text : value + "Z");
    if (Number.isNaN(utc.getTime())) return { value, note: "Sin zona horaria en el archivo; se interpreta como Europe/Madrid." };
    const madrid = new Date(utc.getTime() + 60 * 60 * 1000);
    return { value: madrid.toISOString().slice(0, 16), note: "El archivo trae zona horaria; se muestra en Europe/Madrid (UTC+1, sin horario de verano)." };
  }
  return { value, note: "Sin zona horaria en el archivo; se interpreta como Europe/Madrid." };
}

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
  const labels = { ok: "Compatible", weak: "Débil", ambiguous: "Ambiguo", failed: "Insuficiente" };
  const verdict = result.verdict || (result.status === "ambiguous" ? "ambiguous" : "failed");
  const title = document.createElement("h3");
  title.className = "verdict verdict-" + verdict;
  title.textContent = "Veredicto: " + (result.verdict_label || labels[verdict] || "Insuficiente");
  const message = document.createElement("p");
  message.textContent = result.summary || result.message || "";
  const note = document.createElement("p");
  note.textContent = "Ningún punto se muestra como seguro. Identificación única: no. El centro de búsqueda no es un punto recuperado.";
  card.append(title, message, note);
  if (result.coverage_note) {
    const limit = document.createElement("p");
    limit.className = "warn";
    limit.textContent = result.coverage_note;
    card.append(limit);
  }
  if (result.plan) {
    const cost = document.createElement("p");
    cost.textContent = result.plan.tile_count + " teselas de " + result.plan.tile_radius_km + " km, " + result.plan.queries + " consultas a adsb.aero en el peor caso, cota de red " + Math.round(result.plan.time_bound_s) + " s. " + (result.plan.time_note || "");
    card.append(cost);
    if (result.plan.altitude_note) {
      const altitude = document.createElement("p");
      altitude.textContent = result.plan.altitude_note;
      card.append(altitude);
    }
    if (result.plan.clock_note) {
      const clockNote = document.createElement("p");
      clockNote.textContent = result.plan.clock_note;
      card.append(clockNote);
    }
  }
  if (result.local_mode) {
    const local = document.createElement("p");
    local.textContent = "Modo local, opcional, en tu ordenador y sin cargos de este servicio: " + result.local_mode.plan_command + " y, para ejecutarlo, " + result.local_mode.run_command + ". " + (result.local_mode.note || "");
    card.append(local);
  }
  const regions = result.regions || {};
  const band68 = regions["68"] || {};
  const band90 = regions["90"] || {};
  const regionText = document.createElement("p");
  if (band68.radius_m == null && band90.radius_m == null) {
    regionText.textContent = "No hay región del 68 % ni del 90 %: la evidencia no sostiene una zona.";
  } else {
    regionText.textContent = "Región del 68 %: " + Math.round(band68.radius_m) + " m. Región del 90 %: " + Math.round(band90.radius_m) + " m. No están calibradas con grabaciones reales.";
  }
  card.append(regionText);
  if (result.clock && result.clock.shared && result.clock.offset_s != null) {
    const clock = document.createElement("p");
    const spread = result.clock.spread_68_s == null ? "" : " La dispersión del 68 % del desfase es " + Number(result.clock.spread_68_s).toFixed(1) + " s.";
    clock.textContent = "Desfase de reloj común a los eventos: " + Number(result.clock.offset_s).toFixed(1) + " s, con paso de " + Number(result.clock.step_s).toFixed(1) + " s." + spread;
    card.append(clock);
  } else {
    const clock = document.createElement("p");
    clock.textContent = "Con menos de dos eventos explicados no se separa el lugar del desfase del reloj.";
    card.append(clock);
  }
  if (result.show_point && result.reported_point) {
    const hypothesis = document.createElement("p");
    hypothesis.textContent = "Hipótesis marcada: " + Number(result.reported_point.lat).toFixed(4) + ", " + Number(result.reported_point.lon).toFixed(4) + ". No es una coordenada calibrada.";
    card.append(hypothesis);
  } else {
    const hypothesis = document.createElement("p");
    hypothesis.className = "warn";
    hypothesis.textContent = "No se marca un punto ganador.";
    card.append(hypothesis);
  }
  if (result.resolution_m) {
    const resolution = document.createElement("p");
    resolution.textContent = "Resolución de la rejilla: " + Math.round(result.resolution_m) + " m.";
    card.append(resolution);
  }
  const flags = result.flags || [];
  if (flags.length) {
    const list = document.createElement("ul");
    flags.forEach((flag) => {
      const item = document.createElement("li");
      item.textContent = flag.message || "";
      list.append(item);
    });
    card.append(list);
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
  drawSearch(result);
}

function drawSearch(result) {
  const canvas = document.querySelector("#locate-map");
  if (!canvas) return;
  const hypotheses = (result && result.hypotheses) || [];
  const context = canvas.getContext("2d");
  context.clearRect(0, 0, canvas.width, canvas.height);
  const frame = (result && result.map) || { lat: 40.2, lon: -3.2, radius_km: 750 };
  const lat = Number(frame.lat);
  const lon = Number(frame.lon);
  const radius = Number(frame.radius_km) || 750;
  if (!Number.isFinite(lat) || !Number.isFinite(lon)) return;
  const kx = 111.32 * Math.cos(lat * Math.PI / 180);
  const ky = 110.57;
  const span = radius * 1.2;
  const project = (pointLat, pointLon) => [
    canvas.width / 2 + ((pointLon - lon) * kx) / span * (canvas.width * 0.4),
    canvas.height / 2 - ((pointLat - lat) * ky) / span * (canvas.height * 0.4),
  ];
  const radiusPx = (meters) => (Number(meters) / 1000) / span * canvas.width * 0.4;
  context.strokeStyle = "#314052";
  context.beginPath();
  context.arc(canvas.width / 2, canvas.height / 2, canvas.width * 0.4 * (radius / span), 0, Math.PI * 2);
  context.stroke();
  context.fillStyle = "#8fb4d6";
  context.fillText("Área de búsqueda, no el lugar de la grabación", 16, 24);
  const tiles = (result.plan && result.plan.tiles) || [];
  context.strokeStyle = "#314052";
  tiles.forEach((tile) => {
    const [x, y] = project(tile.lat, tile.lon);
    context.beginPath();
    context.arc(x, y, radiusPx(tile.radius_km * 1000), 0, Math.PI * 2);
    context.stroke();
  });
  const band90 = (result.regions && result.regions["90"]) || {};
  const band68 = (result.regions && result.regions["68"]) || {};
  (band90.cells || []).forEach((item) => {
    const [x, y] = project(item.lat, item.lon);
    context.fillStyle = "rgba(143, 180, 214, 0.9)";
    context.fillRect(x - 3, y - 3, 6, 6);
  });
  (band68.cells || []).forEach((item) => {
    const [x, y] = project(item.lat, item.lon);
    context.fillStyle = "rgba(240, 198, 116, 0.95)";
    context.fillRect(x - 3, y - 3, 6, 6);
  });
  const center = result.show_point && result.reported_point ? result.reported_point : null;
  if (center && Number.isFinite(Number(band90.radius_m))) {
    const [x, y] = project(center.lat, center.lon);
    context.strokeStyle = "#8fb4d6";
    context.setLineDash([6, 4]);
    context.beginPath();
    context.arc(x, y, radiusPx(band90.radius_m), 0, Math.PI * 2);
    context.stroke();
    context.setLineDash([]);
    context.strokeStyle = "#f0c674";
    context.beginPath();
    context.arc(x, y, radiusPx(band68.radius_m), 0, Math.PI * 2);
    context.stroke();
    context.fillStyle = "#f4f7fb";
    context.fillRect(x - 4, y - 4, 8, 8);
    context.fillText("hipótesis", x + 8, y - 8);
  } else {
    hypotheses.forEach((item) => {
      const [x, y] = project(item.lat, item.lon);
      context.fillStyle = "#7dcea0";
      context.fillRect(x - 4, y - 4, 8, 8);
    });
  }
}

const extraIds = [];
let regionCatalog = null;

function currentRegionSpec() {
  const base = document.querySelector("#locate-region").value || "unsure";
  if (!extraIds.length || base === "unsure" || base === "iberia" || String(base).startsWith("cc:")) return base;
  return [base, ...extraIds].join(",");
}

async function loadRegions() {
  let response;
  try {
    response = await fetch("/api/regions", { credentials: "same-origin", headers: { "Accept": "application/json" } });
  } catch (error) {
    window.__errors.push(String(error));
    return;
  }
  if (!response.ok) {
    window.__errors.push("regions " + response.status);
    return;
  }
  regionCatalog = await response.json();
  const select = document.querySelector("#locate-region");
  regionCatalog.groups.forEach((group) => {
    const box = document.createElement("optgroup");
    box.label = group.name;
    group.items.forEach((item) => {
      const option = document.createElement("option");
      option.value = item.id;
      option.textContent = item.name;
      box.append(option);
    });
    select.append(box);
  });
  select.addEventListener("change", () => {
    extraIds.splice(0, extraIds.length);
    refreshRegionPlan();
  });
  document.querySelector("#locate-extra").addEventListener("change", () => {
    const value = document.querySelector("#locate-extra").value;
    if (value && !extraIds.includes(value)) extraIds.push(value);
    document.querySelector("#locate-extra").value = "";
    refreshRegionPlan();
  });
  refreshRegionPlan();
}

async function refreshRegionPlan() {
  const spec = currentRegionSpec();
  const response = await fetch("/api/region-plan?region=" + encodeURIComponent(spec), { credentials: "same-origin", headers: { "Accept": "application/json" } });
  if (!response.ok) return;
  const plan = await response.json();
  const note = document.querySelector("#locate-fit");
  const verb = plan.fits_free_web
    ? "Cabe en este servicio: " + plan.tile_count + " tesela, " + plan.queries + " llamadas, hasta " + Math.round(plan.search_bound_s) + " s. Se buscará solo en " + plan.label + "."
    : "No cabe en este servicio: " + plan.tile_count + " teselas, " + plan.queries + " llamadas, hasta " + Math.round(plan.search_bound_s) + " s. No se buscará solo una parte: hará falta el modo local.";
  note.textContent = verb;
  fillExtraProvinces(spec);
  const chosen = document.querySelector("#locate-chosen");
  chosen.replaceChildren();
  extraIds.forEach((id) => {
    const item = document.createElement("li");
    item.textContent = provinceName(id) + " ";
    const button = document.createElement("button");
    button.type = "button";
    button.className = "secondary";
    button.textContent = "Quitar";
    button.addEventListener("click", () => {
      const index = extraIds.indexOf(id);
      if (index >= 0) extraIds.splice(index, 1);
      refreshRegionPlan();
    });
    item.append(button);
    chosen.append(item);
  });
  drawSearch({ map: plan.map, plan: { tiles: plan.tiles }, hypotheses: [], show_point: false, regions: {} });
}

function fillExtraProvinces(spec) {
  const extra = document.querySelector("#locate-extra");
  extra.replaceChildren();
  const blank = document.createElement("option");
  blank.value = "";
  blank.textContent = "Ninguna más";
  extra.append(blank);
  const base = document.querySelector("#locate-region").value;
  if (!regionCatalog || !base || base === "unsure" || base === "iberia" || String(base).startsWith("cc:")) return;
  const selected = new Set([base, ...extraIds]);
  const neighborIds = new Set();
  selected.forEach((id) => {
    const item = findProvince(id);
    (item && item.neighbors || []).forEach((neighbor) => {
      if (!selected.has(neighbor)) neighborIds.add(neighbor);
    });
  });
  neighborIds.forEach((id) => {
    const option = document.createElement("option");
    option.value = id;
    option.textContent = provinceName(id);
    extra.append(option);
  });
}

function findProvince(id) {
  if (!regionCatalog) return null;
  for (const group of regionCatalog.groups) {
    for (const item of group.items) {
      if (item.id === id) return item;
    }
  }
  return null;
}

function provinceName(id) {
  const item = findProvince(id);
  return item ? item.name : id;
}

async function pollLocateJob(jobId) {
  for (let attempt = 0; attempt < 140; attempt += 1) {
    const response = await fetchLimited("/api/locate-jobs/" + jobId, { credentials: "same-origin", headers: { "Accept": "application/json" } });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.message || "No se pudo seguir la búsqueda.");
    if (body.state === "done") return body.result;
    showLocateMessage((body.phase || "Buscando en la zona elegida.") + " " + (body.tiles_done || 0) + "/" + (body.tiles_total || 1));
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
  throw new Error("La búsqueda sigue en curso más de lo previsto. No se muestra un resultado parcial.");
}

loadRegions();
window.__appReady = true;

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
