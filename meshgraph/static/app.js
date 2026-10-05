/* meshgraph front end: D3 force graph + settings dialog + status polling. */
/* global d3 */

"use strict";

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

const state = {
  graph: null,
  svg: null,
  g: null,
  zoom: null,
  simulation: null,
  width: 0,
  height: 0,
  nodeSel: null,
  linkSel: null,
  indirectSel: null,
  selectedNodeId: null,
  selectedLinkKey: null,
  loading: false,
  islandCount: 0,
  userMoved: false,
  autoFitted: false,
  // Перенос вида между рендерами: трансформация зума, флаг перетаскивания,
  // ключ структуры островов и спланированные ячейки.
  viewTransform: null,
  dragging: false,
  pendingReload: false,
  islandKey: null,
  islandTargets: null,
  // Чат: подпись последнего списка (пропуск неизменных опросов), сам список
  // и метка последнего просмотренного сообщения для счётчика непрочитанных.
  chatSig: null,
  chatMessages: [],
  chatSeenTs: 0,
};

const AUTO_REFRESH_MS = 60000;
const STATUS_POLL_MS = 4000;
// Плановый зазор между острами: коллизия узлов и так держит ~100 px белого
// между частями, ещё 20 px сверху — отделены видно, но и всё на экране.
const ISLAND_GAP = 20;

const snrColor = () => d3.scaleSequential(d3.interpolateRdYlGn).domain([-30, 10]);

const $ = (id) => document.getElementById(id);

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------

function hexId(node) {
  return node.hex_id || "!" + (node.id >>> 0).toString(16).padStart(8, "0");
}

function fmtTime(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  return d.toLocaleString([], {
    day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit",
  });
}

function fmtNum(value) {
  if (value === null || value === undefined) return "—";
  return new Intl.NumberFormat().format(value);
}

// Messages per minute: "0", "1.4", "16" — wide numbers are rounded away.
function fmtRate(value) {
  const rate = Number(value);
  if (!Number.isFinite(rate) || rate === 0) return "0";
  if (rate >= 10) return String(Math.round(rate));
  return rate.toFixed(1);
}

function showOverlay(id, visible) {
  const el = $(id);
  if (el) el.hidden = !visible;
}

function showMessage(title, text) {
  $("messageTitle").textContent = title;
  $("messageText").textContent = text;
  showOverlay("message", true);
}

function hideMessage() {
  showOverlay("message", false);
}

// ---------------------------------------------------------------------------
// Graph loading
// ---------------------------------------------------------------------------

function currentParams() {
  const params = new URLSearchParams();
  const mode = document.querySelector('input[name="mode"]:checked');
  params.set("mode", mode ? mode.value : "traceroute");
  params.set("hours", $("hours").value);
  params.set("min_snr", $("minSnr").value);
  params.set("channel", $("channel").value);
  if ($("includeIndirect").checked) params.set("include_indirect", "true");
  return params;
}

async function loadGraph() {
  if (state.loading) return;
  if (state.dragging) {
    // Не перерисовываем под курсором: узел ещё тянут. Обновление выполнится
    // сразу после окончания перетаскивания (см. обработчик drag end).
    state.pendingReload = true;
    return;
  }
  state.loading = true;
  state.pendingReload = false;
  showOverlay("loading", true);
  hideMessage();

  try {
    const response = await fetch(`/api/graph?${currentParams().toString()}`);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    // Координаты узлов прошлого рендера нужны renderGraph для наследования —
    // снимаем ссылки до замены state.graph.
    const prevNodes =
      state.graph && state.graph.nodes
        ? new Map(state.graph.nodes.map((n) => [n.id, n]))
        : null;
    state.graph = data;
    renderGraph(data, prevNodes);
    updateStats(data);
    restoreSelection();
  } catch (error) {
    console.error("Failed to load graph:", error);
    showMessage("Не удалось загрузить граф", String(error.message || error));
  } finally {
    state.loading = false;
    showOverlay("loading", false);
  }
}

function updateStats(data) {
  const stats = data.stats || {};
  $("stNodes").textContent = fmtNum(stats.nodes);
  $("stLinks").textContent = fmtNum(stats.links);
  // Разрозненность сети: сколько несвязанных частей на холсте.
  $("stIslands").textContent =
    state.islandCount > 0 ? fmtNum(state.islandCount) : "—";
  $("stIslands").classList.toggle("many", state.islandCount > 1);
  $("stGateways").textContent = fmtNum(stats.gateways);
  $("stPackets").textContent = fmtNum(
    stats.packets_analyzed ?? stats.receptions_analyzed ?? 0
  );
  $("stRelayed").textContent =
    stats.receptions_relayed === undefined || stats.receptions_relayed === null
      ? "—"
      : fmtNum(stats.receptions_relayed);
  $("stUpdated").textContent = new Date().toLocaleTimeString();
  // SQL-лимит мог отбросить старшие строки окна — говорим об этом явно.
  const truncated = $("stTruncated");
  truncated.hidden = !stats.truncated;
  truncated.textContent = stats.truncated
    ? `⚠ Строк больше лимита: показаны свежайшие ${fmtNum(stats.packets_analyzed)} пакетов`
    : "";
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

function renderGraph(data, prevNodes = null) {
  const container = d3.select("#graphCanvas");
  container.selectAll("*").remove();
  state.nodeSel = null;
  state.linkSel = null;
  state.indirectSel = null;
  state.userMoved = false;
  state.autoFitted = false;
  // Повторный рендер (обновление раз в минуту, фильтры, ресайз) не должен
  // сбрасывать вид: keepView запрещает авто-подгонку, а координаты узлов и
  // зум берутся из прошлого рендера.
  const keepView = !!state.viewTransform;

  // Острова: связные части графа (прямые + непрямые связи). Разъезжаются по
  // разным зонам холста, чтобы разрозненность сети была видна сразу.
  const islands = meshgraphFindComponents(
    data.nodes || [],
    (data.links || []).concat(data.indirect_connections || [])
  );
  state.islandCount =
    data.nodes && data.nodes.length ? islands.length : 0;

  // Ключ структуры (layout.js): если он не изменился, прежние ячейки
  // упаковки остаются в силе и части при обновлении не двигаются.
  const islandKey = meshgraphIslandKey(islands);
  const structureSame = meshgraphSameStructure(state.islandKey, islandKey);
  state.islandKey = islandKey;

  // Наследование координат узла из прошлого рендера — иначе посев как раньше.
  const inherit = (node) => {
    const prev = prevNodes && prevNodes.get(node.id);
    if (!prev || typeof prev.x !== "number" || Number.isNaN(prev.x)) {
      return false;
    }
    node.x = prev.x;
    node.y = prev.y;
    if (typeof prev.vx === "number") {
      node.vx = prev.vx;
      node.vy = prev.vy;
    }
    return true;
  };

  if (!data.nodes || data.nodes.length === 0) {
    state.islandKey = new Map();
    state.islandTargets = null;
    const hints = {
      rssi: "За это время шлюзы не приняли ни одного прямого пакета. Увеличьте период или проверьте подключение к брокеру.",
      combined:
        "Нет ни трассировок, ни прямых приёмов за выбранный период. Увеличьте период либо проверьте подключение к брокеру и ключи каналов в настройках.",
      traceroute:
        "Пакеты traceroute не найдены. Возможно, в сети их никто не шлёт — переключитесь на режим «RSSI-приёмы», либо проверьте ключ шифрования канала в настройках.",
    };
    showMessage(
      "Нет данных за выбранный период",
      hints[data.mode] || hints.traceroute
    );
    return;
  }

  const rect = container.node().getBoundingClientRect();
  let width = rect.width || 900;
  let height = rect.height || 600;
  state.width = width;
  state.height = height;

  const svg = container
    .append("svg")
    .attr("width", width)
    .attr("height", height)
    .attr("viewBox", `0 0 ${width} ${height}`);
  const g = svg.append("g");

  const zoom = d3
    .zoom()
    .scaleExtent([0.05, 10])
    .on("zoom", (event) => {
      g.attr("transform", event.transform);
      state.viewTransform = event.transform;
      // Программные трансформации (sourceEvent === null) пользователем не считаются.
      if (event.sourceEvent) state.userMoved = true;
    });
  svg.call(zoom);
  // Вид переносится между рендерами: возвращаем прежнюю трансформацию, а не
  // identity — обновление раз в минуту больше не сбрасывает зум и панораму.
  svg.call(zoom.transform, keepView ? state.viewTransform : d3.zoomIdentity);

  state.svg = svg;
  state.g = g;
  state.zoom = zoom;

  const color = snrColor();

  // Посев: уже показанные узлы остаются на своих местах, новые появляются по
  // кругу. Координаты MapReport на раскладку не влияют — одна ошибочная
  // координата больше не может испортить вид.
  data.nodes.forEach((node, i) => {
    if (inherit(node)) return;
    const angle = (2 * Math.PI * i) / Math.max(data.nodes.length, 1);
    const radius = Math.min(width, height) * 0.3;
    node.x = width / 2 + Math.cos(angle) * radius + (Math.random() - 0.5) * 60;
    node.y = height / 2 + Math.sin(angle) * radius + (Math.random() - 0.5) * 60;
  });

  // -- simulation ---------------------------------------------------------
  const nodeById = new Map(data.nodes.map((n) => [n.id, n]));
  // Несколько несвязанных частей → каждая уезжает в свою ячейку упаковки.
  const spreadIslands = islands.length > 1;

  let simulation = d3
    .forceSimulation(data.nodes)
    .force(
      "link",
      d3
        .forceLink(data.links)
        .id((d) => d.id)
        // Короткие связи (140–160 px) и слабое отталкивание — подобрано на
        // реальных данных: части компактнее почти вдвое (масштаб подгонки
        // ~0.5 на 1200×650), а минимум ~100 px белого держит коллизия.
        .distance((d) => Math.max(140, 180 - d.strength * 20))
    )
    .force("charge", d3.forceManyBody().strength(-110))
    .force("collision", d3.forceCollide().radius((d) => d.size + 50))
    // Узел не сидит на чужой линии связи: отталкивание от чужих сегментов
    // (свои две линии не считаются) даёт видимый зазор до края узла.
    .force(
      "avoidLinks",
      meshgraphAvoidLinks(data.links.concat(data.indirect_connections || []))
    );

  if (spreadIslands) {
    // Структура не изменилась и вид уже был настроен → ячейки и позиции
    // остаются прежними: обновление ничего не двигает и не подгоняет.
    const reuseTargets = keepView && structureSame && !!state.islandTargets;
    let targets = null;

    if (reuseTargets) {
      targets = state.islandTargets;
    } else {
      // Части сначала «собираются» на месте, чтобы замер был честным; затем
      // каждая уезжает в свою ячейку упаковки — как единое целое.
      simulation.stop();
      for (let i = 0; i < 120; i += 1) simulation.tick();
      const radii = meshgraphMeasureIslands(islands, nodeById, 50);
      const cells = meshgraphPlanIslandsFitted(radii, width, height, ISLAND_GAP);
      targets = new Map();
      cells.forEach((cell) => {
        islands[cell.index].forEach((id) => targets.set(id, cell));
      });
      state.islandTargets = targets;
      // Вид сразу показывает место назначения: части уезжают в поле зрения,
      // а не за край экрана. Масштаб никогда не больше 1. Прежний зум
      // пользователя (keepView) при этом не перебиваем.
      if (!keepView) fitToCells(cells);
    }

    // Место каждой части задают ячейки упаковки, поэтому центральная сила не
    // нужна: она лишь сдвигала готовые части относительно своих ячеек и
    // портит точность подгонки вида.
    simulation.force("islands", meshgraphIslandForce(targets));
    if (!reuseTargets) simulation.alpha(0.5).restart();
  } else {
    // Одна связная сеть — обычные силы, центр держит её на экране.
    simulation.force("center", d3.forceCenter(width / 2, height / 2));
  }

  state.simulation = simulation;

  // Пока части разрознены — показываем весь рисунок целиком. Если пользователь
  // уже сам двигал вид или вид унаследован с прошлого рендера — не трогаем.
  simulation.on("end", () => {
    if (
      spreadIslands &&
      !keepView &&
      !state.userMoved &&
      !state.autoFitted &&
      state.simulation === simulation
    ) {
      state.autoFitted = true;
      fitToContent();
    }
  });

  // -- links --------------------------------------------------------------
  // Непрямые связи приходят с id: переводим в объекты узлов, иначе d3 ждал бы
  // ссылки (как у forceLink) и рисовал линии в точке (0,0).
  const toNode = (v) =>
    (v !== null && typeof v === "object" ? v : nodeById.get(v)) || v;
  (data.indirect_connections || []).forEach((d) => {
    d.source = toNode(d.source);
    d.target = toNode(d.target);
  });

  const indirect = g
    .append("g")
    .attr("class", "indirect-links")
    .selectAll("line")
    .data(data.indirect_connections || [])
    .join("line")
    .attr("class", "link indirect")
    .attr("stroke", "#7d8794")
    .attr("stroke-width", 1.5)
    .attr("stroke-dasharray", "6 5")
    .attr("stroke-opacity", 0.45)
    .style("cursor", "pointer")
    .on("mouseenter", (event, d) => showIndirectTip(event, d))
    .on("mouseleave", hideTip);

  const link = g
    .append("g")
    .attr("class", "links")
    .selectAll("line")
    .data(data.links)
    .join("line")
    .attr("class", "link")
    .attr("stroke", (d) => (d.avg_snr === null || d.avg_snr === undefined ? "#8b949e" : color(d.avg_snr)))
    .attr("stroke-width", (d) => Math.max(1, d.strength))
    .attr("stroke-opacity", 0.8)
    .style("cursor", "pointer")
    .on("mouseenter", (event, d) => showLinkTip(event, d))
    .on("mouseleave", hideTip)
    .on("click", (event, d) => {
      event.stopPropagation();
      const key = linkKey(d);
      select(state.selectedLinkKey === key ? null : d, null);
    });

  state.linkSel = link;
  state.indirectSel = indirect;

  // -- nodes --------------------------------------------------------------
  const node = g
    .append("g")
    .attr("class", "nodes")
    .selectAll("g")
    .data(data.nodes)
    .join("g")
    .attr("class", "node")
    .style("cursor", "pointer")
    .call(
      d3
        .drag()
        .on("start", (event, d) => {
          state.userMoved = true;
          state.dragging = true;
          if (!event.active) simulation.alphaTarget(0.25).restart();
          d.fx = d.x;
          d.fy = d.y;
        })
        .on("drag", (event, d) => {
          d.fx = event.x;
          d.fy = event.y;
        })
        .on("end", (event, d) => {
          state.dragging = false;
          if (!event.active) simulation.alphaTarget(0);
          d.fx = null;
          d.fy = null;
          // Обновление, отложенное на время перетаскивания, выполняем здесь:
          // позиции и вид всё равно унаследуются — ничего не сорвётся.
          if (state.pendingReload) {
            state.pendingReload = false;
            window.setTimeout(() => loadGraph(), 400);
          }
        })
    )
    .on("mouseenter", (event, d) => showNodeTip(event, d))
    .on("mouseleave", hideTip)
    .on("click", (event, d) => {
      event.stopPropagation();
      select(null, state.selectedNodeId === d.id ? null : d);
    });

  node
    .append("circle")
    .attr("r", (d) => d.size)
    .attr("fill", (d) =>
      d.avg_snr === null || d.avg_snr === undefined ? "#9fb3c8" : color(d.avg_snr)
    )
    .attr("stroke", (d) => (d.is_gateway ? "#ffd166" : "#ffffff"))
    .attr("stroke-width", (d) => (d.is_gateway ? 3 : 2));

  node
    .append("text")
    .attr("class", (d) => "node-label" + (d.is_gateway ? " node-gateway-label" : ""))
    .attr("text-anchor", "middle")
    .attr("dy", (d) => d.size + 14)
    .text((d) => d.name);

  state.nodeSel = node;

  svg.on("click", (event) => {
    if (event.target === svg.node()) select(null, null);
  });

  // -- tick ----------------------------------------------------------------
  simulation.on("tick", () => {
    link
      .attr("x1", (d) => d.source.x)
      .attr("y1", (d) => d.source.y)
      .attr("x2", (d) => d.target.x)
      .attr("y2", (d) => d.target.y);
    indirect
      .attr("x1", (d) => d.source.x)
      .attr("y1", (d) => d.source.y)
      .attr("x2", (d) => d.target.x)
      .attr("y2", (d) => d.target.y);
    node.attr("transform", (d) => `translate(${d.x},${d.y})`);
  });
}

// ---------------------------------------------------------------------------
// Selection & highlighting
// ---------------------------------------------------------------------------

function linkKey(link) {
  const s = typeof link.source === "object" ? link.source.id : link.source;
  const t = typeof link.target === "object" ? link.target.id : link.target;
  return [s, t].sort((a, b) => a - b).join("-");
}

function select(link, node) {
  state.selectedLinkKey = link ? linkKey(link) : null;
  state.selectedNodeId = node ? node.id : null;

  if (!state.nodeSel) return;

  if (!link && !node) {
    state.nodeSel.classed("dimmed", false);
    if (state.linkSel) state.linkSel.classed("dimmed", false);
    if (state.indirectSel) state.indirectSel.classed("dimmed", false);
    $("detailsPanel").hidden = true;
    return;
  }

  if (node) {
    const neighbours = new Set([node.id]);
    (state.graph.links || []).forEach((l) => {
      const s = typeof l.source === "object" ? l.source.id : l.source;
      const t = typeof l.target === "object" ? l.target.id : l.target;
      if (s === node.id) neighbours.add(t);
      if (t === node.id) neighbours.add(s);
    });

    state.nodeSel.classed("dimmed", (d) => !neighbours.has(d.id));
    if (state.linkSel) {
      state.linkSel.classed("dimmed", (d) => {
        const s = typeof d.source === "object" ? d.source.id : d.source;
        const t = typeof d.target === "object" ? d.target.id : d.target;
        return s !== node.id && t !== node.id;
      });
    }
    if (state.indirectSel) state.indirectSel.classed("dimmed", true);
    renderNodeDetails(node);
    return;
  }

  if (link) {
    const key = linkKey(link);
    state.nodeSel.classed("dimmed", (d) => {
      const s = typeof link.source === "object" ? link.source.id : link.source;
      const t = typeof link.target === "object" ? link.target.id : link.target;
      return d.id !== s && d.id !== t;
    });
    if (state.linkSel) state.linkSel.classed("dimmed", (d) => linkKey(d) !== key);
    if (state.indirectSel) state.indirectSel.classed("dimmed", true);
    renderLinkDetails(link);
  }
}

function restoreSelection() {
  if (!state.graph) return;
  if (state.selectedNodeId !== null) {
    const found = (state.graph.nodes || []).find((n) => n.id === state.selectedNodeId);
    if (found) select(null, found);
    else {
      state.selectedNodeId = null;
      $("detailsPanel").hidden = true;
    }
  }
}

function detailRow(label, value) {
  return `<div class="kv"><span>${label}</span><span>${value}</span></div>`;
}

function renderNodeDetails(node) {
  const body = $("detailsBody");
  body.innerHTML =
    `<div class="title">${escapeHtml(node.name)}</div>` +
    `<div class="hexid">${hexId(node)}</div>` +
    detailRow("Роль", escapeHtml(node.role || "—")) +
    detailRow("Тип", node.is_gateway ? "Шлюз" : "Узел") +
    detailRow("Пакетов", fmtNum(node.packet_count)) +
    detailRow("Связей", fmtNum(node.connections)) +
    detailRow("Средний SNR", node.avg_snr === null || node.avg_snr === undefined ? "—" : `${node.avg_snr} дБ`) +
    detailRow("Средний RSSI", node.avg_rssi === null || node.avg_rssi === undefined ? "—" : `${node.avg_rssi} дБм`) +
    (node.location
      ? detailRow(
          "Координаты",
          `${node.location.latitude.toFixed(5)}, ${node.location.longitude.toFixed(5)}`
        )
      : "") +
    detailRow("Последний раз", fmtTime(node.last_seen));
  $("detailsPanel").hidden = false;
}

function renderLinkDetails(link) {
  const s = typeof link.source === "object" ? link.source : null;
  const t = typeof link.target === "object" ? link.target : null;
  const body = $("detailsBody");
  body.innerHTML =
    `<div class="title">${escapeHtml(s ? s.name : "?")} ↔ ${escapeHtml(t ? t.name : "?")}</div>` +
    detailRow("Тип", link.type === "indirect" ? "Непрямая" : "Прямая") +
    detailRow("SNR", link.avg_snr === null || link.avg_snr === undefined ? "—" : `${link.avg_snr} дБ`) +
    detailRow("RSSI", link.avg_rssi === null || link.avg_rssi === undefined ? "—" : `${link.avg_rssi} дБм`) +
    (link.hop_count ? detailRow("Хопов", fmtNum(link.hop_count)) : "") +
    detailRow("Наблюдений", fmtNum(link.packet_count || link.path_count)) +
    detailRow("Последний раз", fmtTime(link.last_seen));
  $("detailsPanel").hidden = false;
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

// ---------------------------------------------------------------------------
// Tooltips
// ---------------------------------------------------------------------------

function placeTip(event) {
  const tip = $("tooltip");
  const wrap = $("graphCanvas").parentElement.getBoundingClientRect();
  const tw = tip.offsetWidth;
  const th = tip.offsetHeight;
  let left = event.clientX - wrap.left + 14;
  let top = event.clientY - wrap.top + 14;
  if (left + tw > wrap.width - 8) left = event.clientX - wrap.left - tw - 14;
  if (top + th > wrap.height - 8) top = event.clientY - wrap.top - th - 14;
  tip.style.left = `${Math.max(4, left)}px`;
  tip.style.top = `${Math.max(4, top)}px`;
}

function showTip(event, html) {
  const tip = $("tooltip");
  tip.innerHTML = html;
  tip.hidden = false;
  placeTip(event);
}

function hideTip() {
  $("tooltip").hidden = true;
}

function showNodeTip(event, d) {
  showTip(
    event,
    `<div class="t-title">${escapeHtml(d.name)}${d.is_gateway ? " 📡" : ""}</div>` +
      `<div class="t-row"><span>id</span><b>${hexId(d)}</b></div>` +
      `<div class="t-row"><span>Пакетов</span><b>${fmtNum(d.packet_count)}</b></div>` +
      `<div class="t-row"><span>Связей</span><b>${fmtNum(d.connections)}</b></div>` +
      `<div class="t-row"><span>SNR</span><b>${
        d.avg_snr === null || d.avg_snr === undefined ? "—" : d.avg_snr + " дБ"
      }</b></div>` +
      `<div class="t-row"><span>RSSI</span><b>${
        d.avg_rssi === null || d.avg_rssi === undefined ? "—" : d.avg_rssi + " дБм"
      }</b></div>`
  );
}

function showLinkTip(event, d) {
  const s = typeof d.source === "object" ? d.source.name : d.source;
  const t = typeof d.target === "object" ? d.target.name : d.target;
  const source = Array.isArray(d.modes)
    ? `<div class="t-row"><span>Источник</span><b>${
        d.modes.length > 1
          ? "трассировка + приёмы"
          : d.modes[0] === "traceroute"
            ? "трассировки"
            : "прямые приёмы"
      }</b></div>`
    : "";
  showTip(
    event,
    `<div class="t-title">${escapeHtml(s)} ↔ ${escapeHtml(t)}</div>` +
      source +
      `<div class="t-row"><span>SNR</span><b>${
        d.avg_snr === null || d.avg_snr === undefined ? "—" : d.avg_snr + " дБ"
      }</b></div>` +
      `<div class="t-row"><span>RSSI</span><b>${
        d.avg_rssi === null || d.avg_rssi === undefined ? "—" : d.avg_rssi + " дБм"
      }</b></div>` +
      `<div class="t-row"><span>Наблюдений</span><b>${fmtNum(d.packet_count)}</b></div>` +
      `<div class="t-row"><span>Последний раз</span><b>${fmtTime(d.last_seen)}</b></div>`
  );
}

function showIndirectTip(event, d) {
  const s = typeof d.source === "object" ? d.source.name : d.source;
  const t = typeof d.target === "object" ? d.target.name : d.target;
  showTip(
    event,
    `<div class="t-title">${escapeHtml(s)} ⇢ ${escapeHtml(t)}</div>` +
      `<div class="t-row"><span>Через хопов</span><b>${fmtNum(d.hop_count)}</b></div>` +
      `<div class="t-row"><span>Путей</span><b>${fmtNum(d.path_count)}</b></div>`
  );
}

// ---------------------------------------------------------------------------
// Zoom controls / search
// ---------------------------------------------------------------------------

function zoomBy(factor) {
  if (!state.svg || !state.zoom) return;
  state.svg.transition().duration(220).call(state.zoom.scaleBy, factor);
}

// Показать план упаковки островов целиком (по известным ячейкам, ещё до
// движения частей). Масштаб не больше 1: если всё помещается — вид не трогаем.
function fitToCells(cells) {
  if (!state.svg || !state.zoom || !cells || !cells.length) return;
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (const cell of cells) {
    minX = Math.min(minX, cell.tx - cell.r);
    maxX = Math.max(maxX, cell.tx + cell.r);
    minY = Math.min(minY, cell.ty - cell.r);
    maxY = Math.max(maxY, cell.ty + cell.r);
  }
  const bw = maxX - minX;
  const bh = maxY - minY;
  if (!bw || !bh) return;
  const k = Math.min(
    1,
    (0.92 * state.width) / bw,
    (0.92 * state.height) / bh
  );
  const cx = (minX + maxX) / 2;
  const cy = (minY + maxY) / 2;
  state.svg.call(
    state.zoom.transform,
    d3.zoomIdentity
      .translate(state.width / 2, state.height / 2)
      .scale(k)
      .translate(-cx, -cy)
  );
}

function fitToContent() {
  if (!state.svg || !state.zoom || !state.g) return;
  const bounds = state.g.node().getBBox();
  if (!bounds.width || !bounds.height) return;
  const width = state.width;
  const height = state.height;
  const scale = Math.min(
    2.5,
    0.9 / Math.max(bounds.width / width, bounds.height / height)
  );
  const tx = width / 2 - scale * (bounds.x + bounds.width / 2);
  const ty = height / 2 - scale * (bounds.y + bounds.height / 2);
  state.svg
    .transition()
    .duration(350)
    .call(state.zoom.transform, d3.zoomIdentity.translate(tx, ty).scale(scale));
}

function focusOnNode(target) {
  if (!state.svg || !state.zoom) return;
  const k = 1.6;
  state.svg
    .transition()
    .duration(450)
    .call(
      state.zoom.transform,
      d3.zoomIdentity
        .translate(state.width / 2, state.height / 2)
        .scale(k)
        .translate(-target.x, -target.y)
    );
}

function runSearch(query) {
  const box = $("searchResults");
  box.innerHTML = "";
  const q = query.trim().toLowerCase();
  if (!q || !state.graph) return;

  const matches = (state.graph.nodes || [])
    .filter((n) => {
      return (
        (n.name || "").toLowerCase().includes(q) ||
        hexId(n).toLowerCase().includes(q) ||
        String(n.id).includes(q)
      );
    })
    .slice(0, 10);

  if (!matches.length) {
    box.innerHTML = '<div class="hint">Ничего не найдено</div>';
    return;
  }

  matches.forEach((node) => {
    const el = document.createElement("div");
    el.className = "search-result";
    el.innerHTML = `<strong>${escapeHtml(node.name)}</strong><span>${hexId(node)} · ${
      node.connections
    } связей</span>`;
    el.addEventListener("click", () => {
      focusOnNode(node);
      select(null, node);
      box.innerHTML = "";
      $("nodeSearch").value = "";
    });
    box.appendChild(el);
  });
}

// ---------------------------------------------------------------------------
// Chat: channel text messages in a foldable, draggable window
// ---------------------------------------------------------------------------

const CHAT_POLL_MS = 10000;
// Palette for author names — hashed from the node id so the colour stays the
// same between polls.
const CHAT_NAME_COLORS = [
  "#e06c75", "#61afef", "#98c379", "#e5c07b",
  "#c678dd", "#56b6c2", "#d19a66", "#7f848e",
];

function storeGet(key) {
  try { return localStorage.getItem(key); } catch { return null; }
}

function storeSet(key, value) {
  try { localStorage.setItem(key, value); } catch { /* приватный режим */ }
}

function loadJSON(key) {
  try { return JSON.parse(storeGet(key)); } catch { return null; }
}

function saveJSON(key, value) {
  storeSet(key, JSON.stringify(value));
}

function clampInt(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function chatColor(nodeId) {
  const text = String(nodeId ?? "?");
  let hash = 0;
  for (let i = 0; i < text.length; i++) {
    hash = (hash * 31 + text.charCodeAt(i)) >>> 0;
  }
  return CHAT_NAME_COLORS[hash % CHAT_NAME_COLORS.length];
}

// Текст только через textContent: сообщения приходят от чужих устройств.
function chatEl(tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text !== undefined) el.textContent = text;
  return el;
}

function fmtChatTime(ts) {
  return new Date(ts * 1000).toLocaleTimeString([], {
    hour: "2-digit", minute: "2-digit",
  });
}

function chatDayKey(ts) {
  const d = new Date(ts * 1000);
  return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
}

function chatDayLabel(ts) {
  const day = new Date(ts * 1000);
  const midnight = (d) =>
    new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const days = (midnight(new Date()) - midnight(day)) / 86400000;
  if (days === 0) return "Сегодня";
  if (days === 1) return "Вчера";
  return day.toLocaleDateString([], {
    day: "numeric", month: "long", year: "numeric",
  });
}

async function loadChat() {
  if (document.hidden) return;
  try {
    const params = new URLSearchParams({ hours: $("hours").value });
    const channel = $("channel").value;
    if (channel) params.set("channel", channel);
    const response = await fetch(`/api/chat?${params}`);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    $("chatHead").title = `Обновлено в ${fmtChatTime(data.generated_at)}`;
    renderChat(data.messages || []);
  } catch (err) {
    $("chatHead").title = `Обновление чата не удалось: ${err.message}`;
  }
}

function chatSignature(messages) {
  return JSON.stringify(
    messages.map((m) => [m.id, m.ts, m.name, m.text, m.image, m.reply_to, m.reactions, m.phantom])
  );
}

function chatReplyEl(target) {
  const quote = chatEl("div", "chat-reply");
  if (target.text !== undefined && target.text !== null) {
    const name = chatEl("span", "chat-reply-name", target.name || "");
    name.style.color = chatColor(target.from);
    quote.style.borderLeftColor = chatColor(target.from);
    quote.appendChild(name);
    quote.appendChild(chatEl("span", "chat-reply-text", target.text));
    quote.title = "Перейти к сообщению";
    quote.addEventListener("click", () => jumpToChatMessage(target.packet_id));
  } else {
    quote.classList.add("missing");
    quote.appendChild(chatEl("span", "chat-reply-name", "Сообщение недоступно"));
    quote.appendChild(
      chatEl("span", "chat-reply-text", `id пакета ${target.packet_id}`)
    );
    // Если на недоступное сообщение есть реакции — под ними фантом, и
    // цитата прокручивает к нему; без фантома клик бездействует.
    const anchored = (state.chatMessages || []).some(
      (m) => m.phantom && m.packet_id === target.packet_id
    );
    if (anchored) {
      quote.classList.add("clickable");
      quote.title = "Перейти к сообщению";
      quote.addEventListener("click", () => jumpToChatMessage(target.packet_id));
    }
  }
  return quote;
}

function appendChatReactions(bubble, reactions) {
  if (!reactions || !reactions.length) return;
  const chips = chatEl("div", "chat-reactions");
  for (const reaction of reactions) {
    const label = reaction.count > 1
      ? `${reaction.emoji} ${reaction.count}`
      : reaction.emoji;
    const chip = chatEl("span", "chat-reaction", label);
    chip.title = (reaction.names || []).join(", ");
    chips.appendChild(chip);
  }
  bubble.appendChild(chips);
}

// Картинка пиксель-арта: рисуем на canvas целыми пикселями (без сглаживания),
// цвета берём из палитры пакета — она своя у каждой картинки и не зависит
// от темы страницы; рамка вокруг — обычная, живёт на CSS-переменных.
function pixelArtEl(image) {
  const palette =
    MESHGRAPH_PIXEL_PALETTES[image.theme] || MESHGRAPH_PIXEL_PALETTES[0];
  const scale = meshgraphPixelScale(
    image.w, image.h,
    MESHGRAPH_PIXEL_MAX_W, MESHGRAPH_PIXEL_MAX_H, MESHGRAPH_PIXEL_MAX_SCALE
  );
  const wrap = chatEl("div", "chat-pixelart");
  const canvas = document.createElement("canvas");
  canvas.width = image.w * scale;
  canvas.height = image.h * scale;
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = palette.bg;
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  const pixels = meshgraphDecodePixels(image);
  ctx.fillStyle = palette.fg;
  for (let y = 0; y < image.h; y++) {
    for (let x = 0; x < image.w; x++) {
      if (pixels[y * image.w + x]) {
        ctx.fillRect(x * scale, y * scale, scale, scale);
      }
    }
  }
  // Сетка как в прошивке: линия внизу и справа каждого пикселя, но только
  // при масштабе ≥ 3 — на мелком она бы лишь пачкала картинку.
  if (image.grid && scale >= 3) {
    ctx.fillStyle = palette.fg;
    for (let y = 1; y <= image.h; y++) {
      ctx.fillRect(0, y * scale - 1, image.w * scale, 1);
    }
    for (let x = 1; x <= image.w; x++) {
      ctx.fillRect(x * scale - 1, 0, 1, image.h * scale);
    }
  }
  wrap.appendChild(canvas);
  wrap.title = `${image.w}×${image.h} · ${palette.name}`;
  return wrap;
}

function chatMessageEl(msg) {
  const row = chatEl("div", "chat-msg");
  if (msg.packet_id) row.dataset.pid = String(msg.packet_id);
  const main = chatEl("div", "chat-main");

  if (msg.phantom) {
    // Фантом недостающего сообщения: серая заглушка вместо него, а реакции
    // на нём висят так же, как на настоящем сообщении.
    row.classList.add("chat-phantom");
    const bubble = chatEl("div", "chat-bubble");
    bubble.appendChild(
      chatEl("div", "chat-phantom-text", "Сообщение недоступно")
    );
    bubble.appendChild(
      chatEl("div", "chat-phantom-id", `id пакета ${msg.packet_id}`)
    );
    appendChatReactions(bubble, msg.reactions);
    main.appendChild(bubble);
    row.appendChild(main);
    return row;
  }

  const initial =
    (msg.name || "?").replace(/^!/, "").trim().charAt(0).toUpperCase() || "?";
  const avatar = chatEl("div", "chat-av", initial);
  avatar.style.background = chatColor(msg.from);
  row.appendChild(avatar);

  const meta = chatEl("div", "chat-meta");
  const name = chatEl("span", "chat-name", msg.name || "?");
  name.style.color = chatColor(msg.from);
  meta.appendChild(name);
  meta.appendChild(chatEl("span", "chat-time", fmtChatTime(msg.ts)));
  main.appendChild(meta);

  const bubble = chatEl("div", "chat-bubble");
  if (msg.reply_to) bubble.appendChild(chatReplyEl(msg.reply_to));
  if (msg.image) bubble.appendChild(pixelArtEl(msg.image));
  else bubble.appendChild(chatEl("div", "chat-text", msg.text));
  if (msg.emoji_only) bubble.classList.add("chat-emoji-only");
  appendChatReactions(bubble, msg.reactions);

  main.appendChild(bubble);
  row.appendChild(main);
  return row;
}

function jumpToChatMessage(packetId) {
  const list = $("chatList");
  const target = list.querySelector(`[data-pid="${packetId}"]`);
  if (!target) return;
  target.scrollIntoView({ block: "center", behavior: "smooth" });
  target.classList.remove("chat-flash");
  void target.offsetWidth;  // перезапуск анимации подсветки
  target.classList.add("chat-flash");
}

function renderChat(messages) {
  const signature = chatSignature(messages);
  if (signature === state.chatSig) return;  // список прежний — DOM не трогаем
  const first = state.chatSig === null;
  state.chatSig = signature;
  state.chatMessages = messages;

  const list = $("chatList");
  const body = $("chatBody");  // именно он прокручивается, список растёт в нём
  const keepScroll = body.scrollTop;
  const atBottom = body.scrollHeight - keepScroll - body.clientHeight < 48;
  list.replaceChildren();

  if (!messages.length) {
    list.appendChild(chatEl("div", "chat-empty", "Сообщений пока нет"));
  }
  let lastDay = null;
  for (const msg of messages) {
    const day = chatDayKey(msg.ts);
    if (day !== lastDay) {
      list.appendChild(chatEl("div", "chat-date", chatDayLabel(msg.ts)));
      lastDay = day;
    }
    list.appendChild(chatMessageEl(msg));
  }

  // Первый показ и «прилипание» к низу: в мессенджере снизу свежие сообщения.
  if (first || atBottom) body.scrollTop = body.scrollHeight;
  else body.scrollTop = keepScroll;

  updateChatBadge(messages);
}

function markChatSeen() {
  const messages = state.chatMessages;
  const last = messages.length ? messages[messages.length - 1].ts : 0;
  if (last > state.chatSeenTs) {
    state.chatSeenTs = last;
    storeSet("meshgraph.chat.seen", String(last));
  }
  $("chatBadge").hidden = true;
}

function updateChatBadge(messages) {
  const badge = $("chatBadge");
  if (!$("chatWindow").classList.contains("collapsed")) {
    markChatSeen();  // окно открыто — всё и так на виду
    return;
  }
  // Фантом — не сообщение: реакции на него не должны будить счётчик,
  // как и реакции на настоящие сообщения.
  const unseen = messages.filter((m) => !m.phantom && m.ts > state.chatSeenTs).length;
  badge.hidden = unseen === 0;
  badge.textContent = String(unseen);
}

function setChatFolded(folded, persist) {
  const win = $("chatWindow");
  win.classList.toggle("collapsed", folded);
  const button = $("chatCollapse");
  button.textContent = folded ? "▴" : "▾";
  button.title = folded ? "Развернуть чат" : "Свернуть чат";
  button.setAttribute("aria-label", button.title);
  if (persist) storeSet("meshgraph.chat.folded", folded ? "1" : "");
}

function clampChatWindow() {
  const win = $("chatWindow");
  if (!win.style.left) return;  // ещё не перетаскивали — держится углом
  const left = clampInt(parseInt(win.style.left, 10) || 0, 0,
    Math.max(0, window.innerWidth - win.offsetWidth));
  const top = clampInt(parseInt(win.style.top, 10) || 0, 0,
    Math.max(0, window.innerHeight - win.offsetHeight));
  win.style.left = `${left}px`;
  win.style.top = `${top}px`;
}

function initChat() {
  const win = $("chatWindow");
  const head = $("chatHead");

  state.chatSeenTs = Number(storeGet("meshgraph.chat.seen")) || 0;
  setChatFolded(storeGet("meshgraph.chat.folded") === "1", false);

  // Сохранённая позиция; без неё окно стоит в правом нижнем углу (CSS).
  const saved = loadJSON("meshgraph.chat.pos");
  if (saved && Number.isFinite(saved.left) && Number.isFinite(saved.top)) {
    win.style.left = `${saved.left}px`;
    win.style.top = `${saved.top}px`;
    win.style.right = "auto";
    win.style.bottom = "auto";
    clampChatWindow();
  }

  $("chatCollapse").addEventListener("click", () => {
    const folded = !win.classList.contains("collapsed");
    setChatFolded(folded, true);
    if (!folded) markChatSeen();
    clampChatWindow();
  });

  // Перетаскивание за заголовок — Pointer Events, работает и мышью, и пальцем.
  let drag = null;
  head.addEventListener("pointerdown", (event) => {
    if (event.target.closest("button")) return;
    const rect = win.getBoundingClientRect();
    drag = { dx: event.clientX - rect.left, dy: event.clientY - rect.top };
    win.classList.add("dragging");
    head.setPointerCapture(event.pointerId);
    event.preventDefault();
  });
  head.addEventListener("pointermove", (event) => {
    if (!drag) return;
    const left = clampInt(event.clientX - drag.dx, 0,
      Math.max(0, window.innerWidth - win.offsetWidth));
    const top = clampInt(event.clientY - drag.dy, 0,
      Math.max(0, window.innerHeight - win.offsetHeight));
    win.style.left = `${left}px`;
    win.style.top = `${top}px`;
    win.style.right = "auto";
    win.style.bottom = "auto";
  });
  const stopDrag = () => {
    if (!drag) return;
    drag = null;
    win.classList.remove("dragging");
    saveJSON("meshgraph.chat.pos", {
      left: parseInt(win.style.left, 10) || 0,
      top: parseInt(win.style.top, 10) || 0,
    });
  };
  head.addEventListener("pointerup", stopDrag);
  head.addEventListener("pointercancel", stopDrag);

  window.addEventListener("resize", clampChatWindow);
}

// ---------------------------------------------------------------------------
// Status polling
// ---------------------------------------------------------------------------

async function pollStatus() {
  try {
    const response = await fetch("/api/status");
    if (!response.ok) return;
    const status = await response.json();

    const dot = $("statusDot");
    dot.className = "dot " + (status.connected ? "on" : "off");
    $("statusText").textContent = status.connected
      ? `подключено к ${status.broker}`
      : `нет связи: ${status.broker}`;

    const stats = status.stats || {};
    const packets = stats.messages || 0;
    const dbPackets = (status.db && status.db.packets) || 0;
    const rateLine =
      stats.rate_1m === undefined ? "" : ` · ${fmtRate(stats.rate_1m)}/мин`;
    $("statusCounters").textContent = `${fmtNum(packets)} получено · ${fmtNum(
      dbPackets
    )} в базе${rateLine}`;

    // Hover on the status chip: broker plus how fast the messages come in.
    $("mqttStatus").title =
      `Состояние MQTT-подключения\n` +
      `Брокер: ${status.broker}\n` +
      `Скорость: ${fmtRate(stats.rate_1m)}/мин за последнюю минуту\n` +
      `Средняя за 5 мин: ${fmtRate(stats.rate_5m)}/мин\n` +
      `Получено: ${fmtNum(packets)} · в базе: ${fmtNum(dbPackets)}`;

    if (!status.connected && !state.graph) {
      showMessage(
        "Нет подключения к MQTT-брокеру",
        `Сейчас: ${status.broker}. Откройте настройки и укажите адрес своего брокера — данные начнут появляться сразу после подключения.`
      );
    }
  } catch (error) {
    // The UI keeps working offline from the last successful poll.
  }
}

async function loadChannels() {
  try {
    const response = await fetch("/api/channels");
    if (!response.ok) return;
    const payload = await response.json();
    const select = $("channel");
    const current = select.value;
    while (select.options.length > 1) select.remove(1);
    (payload.channels || []).forEach((name) => {
      const option = document.createElement("option");
      option.value = name;
      option.textContent = name;
      select.appendChild(option);
    });
    select.value = current;
  } catch (error) {
    /* non-critical */
  }
}

// ---------------------------------------------------------------------------
// Settings dialog
// ---------------------------------------------------------------------------

const SETTINGS_FIELDS = [
  "mqtt_broker_address", "mqtt_port", "mqtt_username", "mqtt_password",
  "mqtt_topic_prefix", "mqtt_topic_suffix", "mqtt_client_id",
  "mqtt_tls", "mqtt_tls_insecure",
  "decryption_keys", "default_graph_mode", "default_hours",
  "retention_hours", "graph_packet_limit",
];

const BOOLEAN_FIELDS = new Set(["mqtt_tls", "mqtt_tls_insecure"]);

const FIELD_TO_INPUT = {
  mqtt_broker_address: "setBroker",
  mqtt_port: "setPort",
  mqtt_username: "setUser",
  mqtt_password: "setPass",
  mqtt_topic_prefix: "setPrefix",
  mqtt_topic_suffix: "setSuffix",
  mqtt_client_id: "setClientId",
  mqtt_tls: "setTls",
  mqtt_tls_insecure: "setTlsInsecure",
  decryption_keys: "setKeys",
  default_graph_mode: "setMode",
  default_hours: "setHours",
  retention_hours: "setRetention",
  graph_packet_limit: "setLimit",
};

/** Live preview of what the worker will subscribe to (and a wildcard warning). */
function updateTopicPreview() {
  const preview = $("topicPreview");
  if (!preview) return;
  const topic = ($("setPrefix").value || "") + ($("setSuffix").value || "");
  preview.textContent = topic || "—";
  const warn = $("topicWarn");
  const hasWildcard = topic.includes("#") || topic.includes("+");
  const message =
    topic && !hasWildcard
      ? " — без wildcard (`/#`) подписка ничего не найдёт."
      : "";
  warn.textContent = message;
  warn.classList.toggle("bad", Boolean(message));
}

function openSettings() {
  $("settingsModal").hidden = false;
  $("formErrors").hidden = true;
  $("settingsHint").textContent = "Изменения применяются сразу: соединение переустанавливается.";
  fetch("/api/settings")
    .then((r) => r.json())
    .then((settings) => {
      SETTINGS_FIELDS.forEach((field) => {
        const input = $(FIELD_TO_INPUT[field]);
        if (!input) return;
        let value = settings[field];
        if (field === "mqtt_password") {
          value = value || "";
          input.placeholder = settings.mqtt_password_set
            ? "пароль задан — введите новый, чтобы изменить"
            : "необязательно";
        }
        if (BOOLEAN_FIELDS.has(field)) {
          input.checked = Boolean(value);
          return;
        }
        input.value = value === null || value === undefined ? "" : value;
      });
      updateTopicPreview();
      setTimeout(() => $("setBroker").focus(), 50);
    })
    .catch((error) => showFormErrors([`Не удалось прочитать настройки: ${error}`]));
}

function closeSettings() {
  $("settingsModal").hidden = true;
}

function showFormErrors(errors) {
  const box = $("formErrors");
  if (!errors || !errors.length) {
    box.hidden = true;
    return;
  }
  box.innerHTML = `<ul>${errors.map((e) => `<li>${escapeHtml(e)}</li>`).join("")}</ul>`;
  box.hidden = false;
}

async function saveSettings() {
  const payload = {};
  SETTINGS_FIELDS.forEach((field) => {
    const input = $(FIELD_TO_INPUT[field]);
    if (!input) return;
    if (BOOLEAN_FIELDS.has(field)) {
      payload[field] = input.checked;
      return;
    }
    let value = input.value;
    if (["mqtt_port", "default_hours", "retention_hours", "graph_packet_limit"].includes(field)) {
      value = parseInt(value, 10);
      if (Number.isNaN(value)) return;
    }
    payload[field] = value;
  });

  const saveBtn = $("settingsSave");
  saveBtn.disabled = true;
  try {
    const response = await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const result = await response.json();
    if (!response.ok || !result.ok) {
      showFormErrors(result.errors || [`Ошибка HTTP ${response.status}`]);
      return;
    }
    showFormErrors([]);
    $("settingsHint").textContent = "Сохранено. Переподключение к брокеру…";
    setTimeout(() => {
      closeSettings();
      pollStatus();
      loadChannels();
      loadGraph();
    }, 600);
  } catch (error) {
    showFormErrors([String(error)]);
  } finally {
    saveBtn.disabled = false;
  }
}

// ---------------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------------

function applyFilters() {
  // Непрямые связи считает traceroute-часть: в RSSI-режиме их нет,
  // в комбинированном — есть (галочка скрыта только для чистого RSSI).
  const mode = document.querySelector('input[name="mode"]:checked')?.value;
  $("indirectWrap").style.display = mode === "rssi" ? "none" : "";
  loadGraph();
  loadChat();  // период и канал действуют и для чата
}

// ---------------------------------------------------------------------------
// Sidebar: sections fold on title click
// ---------------------------------------------------------------------------

function setSectionCollapsed(section, collapsed, persist) {
  section.classList.toggle("collapsed", collapsed);
  const title = section.querySelector(".panel-title");
  if (title) title.setAttribute("aria-expanded", collapsed ? "false" : "true");
  if (persist && section.id) {
    storeSet(`meshgraph.side.${section.id}`, collapsed ? "1" : "");
  }
}

function initSidebar() {
  document.querySelectorAll(".sidebar .panel").forEach((section) => {
    // Компактные разделы («Поиск узла», «Выделено») не сворачиваются.
    if (section.hasAttribute("data-nofold")) return;
    const title = section.querySelector(".panel-title");
    if (!title) return;
    title.setAttribute("role", "button");
    title.setAttribute("tabindex", "0");
    setSectionCollapsed(
      section,
      Boolean(section.id) && storeGet(`meshgraph.side.${section.id}`) === "1",
      false
    );
    const toggle = () =>
      setSectionCollapsed(section, !section.classList.contains("collapsed"), true);
    title.addEventListener("click", toggle);
    title.addEventListener("keydown", (event) => {
      // Enter и Пробел — «нажатие» role=button; Пробел иначе скроллит страницу.
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        toggle();
      }
    });
  });
}

const THEME_KEY = "meshgraph.theme";

function currentTheme() {
  return document.documentElement.dataset.theme === "light" ? "light" : "dark";
}

/** Переключает тему. Атрибут на <html> меняется мгновенно — CSS-переменные
 *  перекрашивают страницу без перерисовки графа. persist=true — запомнить
 *  выбор в localStorage (иначе страница продолжит следовать системе). */
function applyTheme(theme, persist) {
  document.documentElement.dataset.theme = theme;
  const btn = $("themeBtn");
  if (btn) {
    const label = theme === "dark" ? "Светлая тема" : "Тёмная тема";
    btn.title = label;
    btn.setAttribute("aria-label", label);
  }
  if (persist) storeSet(THEME_KEY, theme);
}

function initTheme() {
  applyTheme(currentTheme());
  $("themeBtn").addEventListener("click", () => {
    applyTheme(currentTheme() === "dark" ? "light" : "dark", true);
  });
  /* Пока пользователь сам ничего не выбирал — страница следует за системой. */
  try {
    window.matchMedia("(prefers-color-scheme: light)").addEventListener("change", (event) => {
      if (storeGet(THEME_KEY) === null) applyTheme(event.matches ? "light" : "dark");
    });
  } catch { /* старые браузеры без addEventListener на matchMedia */ }
}

function init() {
  initTheme();
  initSidebar();
  initChat();

  ["applyBtn"].forEach((id) => $(id).addEventListener("click", applyFilters));

  document.querySelectorAll('input[name="mode"]').forEach((radio) => {
    radio.addEventListener("change", applyFilters);
  });

  ["hours", "minSnr", "channel", "includeIndirect"].forEach((id) => {
    $(id).addEventListener("change", applyFilters);
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && event.target.tagName === "SELECT") {
      event.preventDefault();
      applyFilters();
    }
    if (event.key === "Escape") {
      if (!$("settingsModal").hidden) closeSettings();
      else select(null, null);
    }
  });

  $("clearSelection").addEventListener("click", () => select(null, null));
  $("nodeSearch").addEventListener("input", (e) => runSearch(e.target.value));

  $("zoomIn").addEventListener("click", () => zoomBy(1.35));
  $("zoomOut").addEventListener("click", () => zoomBy(1 / 1.35));
  $("zoomFit").addEventListener("click", fitToContent);

  $("settingsBtn").addEventListener("click", openSettings);
  $("messageSettings").addEventListener("click", openSettings);
  $("settingsClose").addEventListener("click", closeSettings);
  $("settingsCancel").addEventListener("click", closeSettings);
  $("settingsSave").addEventListener("click", saveSettings);
  ["setPrefix", "setSuffix"].forEach((id) =>
    $(id).addEventListener("input", updateTopicPreview)
  );
  $("settingsModal").addEventListener("click", (event) => {
    if (event.target === $("settingsModal")) closeSettings();
  });

  window.addEventListener("resize", () => {
    if (!state.graph) return;
    clearTimeout(window.__resizeTimer);
    window.__resizeTimer = setTimeout(() => loadGraph(), 250);
  });

  applyFilters();
  loadChannels();
  pollStatus();
  setInterval(pollStatus, STATUS_POLL_MS);
  setInterval(loadChat, CHAT_POLL_MS);
  setInterval(() => {
    if (document.hidden) return;
    if (!$("settingsModal").hidden) return;
    loadChannels();
    loadGraph();
  }, AUTO_REFRESH_MS);
}

document.addEventListener("DOMContentLoaded", init);
