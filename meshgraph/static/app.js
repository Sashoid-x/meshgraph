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
  // Превью ссылок: url → результат /api/link_preview, множество запросов
  // в полёте и таймер пакетного перерисовывания (превью приходят по одному,
  // пересобирать список на каждый ответ — значит мигать gif-картинками).
  linkPreviews: new Map(),
  linkPreviewPending: new Set(),
  linkPreviewTimer: null,
  // Открытый лайтбокс: список картинок сообщения и текущий индекс.
  lightbox: { urls: [], index: 0 },
  // Маршруты пакетов (packetflow.js): очередь воспроизведения, план текущей
  // партии, привязка координат узлов (переживает перерисовку холста), пул
  // SVG-элементов слоя и состояние фонового пульса.
  flow: {
    playing: false,
    plan: null,
    startedAt: 0,
    raf: 0,
    queue: [],
    nodesById: new Map(),
    loading: false,
    pulseTimer: null,
    lastPulseTs: 0,
    layer: null,
    legEls: new Map(),
    dotEls: new Map(),
  },
};

const AUTO_REFRESH_MS = 60000;
const STATUS_POLL_MS = 4000;
// Плановый зазор между острами: коллизия узлов и так держит ~100 px белого
// между частями, ещё 20 px сверху — отделены видно, но и всё на экране.
const ISLAND_GAP = MESHGRAPH_FORCE_PARAMS.islandGap;

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

/**
 * `silent` — фоновое обновление (автообновление раз в минуту): граф остаётся
 * видимым без оверлея со спиннером, который моргал поверх холста каждые
 * AUTO_REFRESH_MS. Оверлей показывают только пользовательские действия
 * (первичная загрузка, смена фильтров, ресайз).
 */
async function loadGraph({ silent = false } = {}) {
  if (state.loading) return;
  if (state.dragging) {
    // Не перерисовываем под курсором: узел ещё тянут. Обновление выполнится
    // сразу после окончания перетаскивания (см. обработчик drag end).
    state.pendingReload = true;
    return;
  }
  state.loading = true;
  state.pendingReload = false;
  if (!silent) showOverlay("loading", true);

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
    // Пересборка холста не должна рвать играющую анимацию маршрутов: слой и
    // 3D-группа переживают перерисовку, координаты узлов просто
    // перепривязываются к свежим объектам (пакеты едут по свежим x/y/z).
    if (state.flow.playing) {
      state.flow.nodesById = new Map((data.nodes || []).map((n) => [n.id, n]));
    }
    renderGraph(data, prevNodes);
    updateStats(data);
    restoreSelection();
  } catch (error) {
    console.error("Failed to load graph:", error);
    showMessage("Не удалось загрузить граф", String(error.message || error));
  } finally {
    state.loading = false;
    if (!silent) showOverlay("loading", false);
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
  // 3D-режим держит собственный rAF-цикл: пока холст пересобирается, гасим
  // его, иначе рендер писал бы в отсоединённый canvas.
  if (window.meshgraph3D) window.meshgraph3D.deactivate();
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
  // Данные пришли — прежнее сообщение («нет данных» / ошибка загрузки)
  // снимаем здесь, а не в начале loadGraph: фоновое обновление иначе
  // убирало карточку на время fetch и карточка мигала каждую минуту.
  hideMessage();

  const rect = container.node().getBoundingClientRect();
  let width = rect.width || 900;
  let height = rect.height || 600;
  // Прежние размеры холста — для meshgraphCanFreeze (ресайз отменяет
  // заморозку: центр/ячейки нужно пересчитать под новый размер).
  const prevWidth = state.width;
  const prevHeight = state.height;
  state.width = width;
  state.height = height;

  // Трёхмерный вид: собственный рендер (graph3d.js + three.js), силы — из
  // force3d.js. Если WebGL недоступен — возвращаемся в 2D тем же проходом.
  if (viewMode() === "3d") {
    if (renderGraph3D(data, prevNodes, width, height, structureSame)) return;
    setViewMode("2d");
    updateViewButtons();
  }

  const svg = container
    .append("svg")
    .attr("width", width)
    .attr("height", height)
    .attr("viewBox", `0 0 ${width} ${height}`);
  const g = svg.append("g");

  const zoom = d3
    .zoom()
    .scaleExtent([
      MESHGRAPH_FORCE_PARAMS.zoomScaleMin,
      MESHGRAPH_FORCE_PARAMS.zoomScaleMax,
    ])
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
  // координата больше не может испортить вид. Логика целиком в layout.js,
  // чтобы тесты гоняли тот же код (G-P2-2).
  meshgraphSeedPositions(data.nodes, prevNodes, width, height);

  // -- simulation ---------------------------------------------------------
  const nodeById = new Map(data.nodes.map((n) => [n.id, n]));
  // Несколько несвязанных частей → каждая уезжает в свою ячейку упаковки.
  const spreadIslands = islands.length > 1;

  // Заморозка повторного рендера (layout.js: meshgraphCanFreeze). Свежая
  // симуляция всегда стартует с alpha=1 и даже на идентичных данных
  // «дыхала» layout на сотни пикселей каждые AUTO_REFRESH_MS: перегрев
  // повторно раскручивал уже устоявшиеся силы. Если структура, холст и прошлая
  // симуляция не изменились — позиции наследуются и симуляция замораживается
  // (alpha=0), ноль движения. Перетаскивание явно будит её через
  // alphaTarget().restart().
  const prevSim = state.simulation;
  const freeze = meshgraphCanFreeze(
    structureSame,
    !prevSim || prevSim.alpha() <= prevSim.alphaMin(),
    width,
    height,
    prevWidth,
    prevHeight
  );

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
        // Числа — в MESHGRAPH_FORCE_PARAMS (layout.js), их же читает тест.
        .distance(meshgraphLinkDistance)
    )
    .force(
      "charge",
      d3.forceManyBody().strength(MESHGRAPH_FORCE_PARAMS.chargeStrength)
    )
    .force("collision", d3.forceCollide().radius(meshgraphCollisionRadius))
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
      // Ничего не двигаем: ячейки унаследованы, позиции унаследованы, а
      // свежую симуляцию гасим — иначе она тут же раскрутится с alpha=1.
      if (freeze) {
        simulation.stop();
        simulation.alpha(0);
      }
    } else {
      // Части сначала «собираются» на месте, чтобы замер был честным; затем
      // каждая уезжает в свою ячейку упаковки — как единое целое.
      simulation.stop();
      for (let i = 0; i < MESHGRAPH_FORCE_PARAMS.gatherTicks; i += 1) {
        simulation.tick();
      }
      const radii = meshgraphMeasureIslands(
        islands,
        nodeById,
        MESHGRAPH_FORCE_PARAMS.collisionPad
      );
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
    if (freeze) {
      simulation.stop();
      simulation.alpha(0);
    }
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
  // Отрисовка позиций → DOM. Отдельная функция, а не только колбэк tick:
  // замороженная симуляция (автообновление, meshgraphCanFreeze) не тикает
  // никогда, и первый вызов обязан пройти синхронно — иначе узлы и связи
  // остались бы в (0,0).
  const redraw = () => {
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
  };
  simulation.on("tick", redraw);
  redraw();
}

// ---------------------------------------------------------------------------
// 3D view mode (toggle 2D/3D, three.js render bridge)
// ---------------------------------------------------------------------------

const VIEW_MODE_KEY = "meshgraph.view";

function viewMode() {
  return storeGet(VIEW_MODE_KEY) === "3d" ? "3d" : "2d";
}

function setViewMode(mode) {
  storeSet(VIEW_MODE_KEY, mode);
}

function updateViewButtons() {
  const mode = viewMode();
  if (!$("view2dBtn") || !$("view3dBtn")) return;
  $("view2dBtn").classList.toggle("is-active", mode === "2d");
  $("view3dBtn").classList.toggle("is-active", mode === "3d");
}

/**
 * Switch between the views without refetching: the same data re-renders in
 * the other dimension, node coordinates carry over (a 3D scene keeps its
 * depth when falling back to the flat canvas).
 */
function switchView(mode) {
  if (mode === viewMode()) return;
  setViewMode(mode);
  updateViewButtons();
  if (!state.graph) return;
  const prevNodes = new Map(state.graph.nodes.map((n) => [n.id, n]));
  renderGraph(state.graph, prevNodes);
  restoreSelection();
}

/**
 * Hand the graph to window.meshgraph3D.  Returns false when the module or
 * WebGL is missing — renderGraph then falls through to the 2D path.
 *
 * Unlike d3, three.js needs resolved link endpoints up front: the details
 * panel and the tooltips read link.source.name directly.
 */
function renderGraph3D(data, prevNodes, width, height, structureSame) {
  // The d3 simulation keeps ticking on its own — stop it, its ticks would
  // keep mutating detached DOM.
  if (state.simulation) {
    state.simulation.stop();
    state.simulation = null;
  }
  state.islandTargets = null;
  state.viewTransform = null;
  if (!window.meshgraph3D) return false;

  const nodeById = new Map(data.nodes.map((n) => [n.id, n]));
  const resolve = (v) =>
    (v !== null && typeof v === "object" ? v : nodeById.get(v)) || v;
  data.links.forEach((l) => {
    l.source = resolve(l.source);
    l.target = resolve(l.target);
  });
  (data.indirect_connections || []).forEach((l) => {
    l.source = resolve(l.source);
    l.target = resolve(l.target);
  });

  return window.meshgraph3D.render({
    nodes: data.nodes,
    links: data.links,
    indirect: data.indirect_connections || [],
    width,
    height,
    structureSame,
    prevNodes,
    selectedNodeId: state.selectedNodeId,
    selectedLinkKey: state.selectedLinkKey,
    color: snrColor(),
    linkKey,
    callbacks: {
      onNodeClick: (node, event) =>
        select(null, state.selectedNodeId === node.id ? null : node),
      onLinkClick: (link, event) => {
        const key = linkKey(link);
        select(state.selectedLinkKey === key ? null : link, null);
      },
      onNodeHover: (node, event) => showNodeTip(event, node),
      onLinkHover: (link, event) => showLinkTip(event, link),
      onIndirectHover: (link, event) => showIndirectTip(event, link),
      onLeave: hideTip,
      onBlankClick: () => select(null, null),
    },
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

  // В 3D-режиме подсветку ведёт модуль (материалы/цвета), панель деталей —
  // та же самая, что и в 2D.
  if (viewMode() === "3d" && window.meshgraph3D) {
    window.meshgraph3D.setSelection(
      state.selectedNodeId,
      state.selectedLinkKey
    );
    if (!link && !node) {
      $("detailsPanel").hidden = true;
      return;
    }
    if (node) renderNodeDetails(node);
    else renderLinkDetails(link);
    return;
  }

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
  if (viewMode() === "3d" && window.meshgraph3D) {
    window.meshgraph3D.zoom(factor);
    return;
  }
  if (!state.svg || !state.zoom) return;
  state.svg.transition().duration(220).call(state.zoom.scaleBy, factor);
}

// Показать план упаковки островов целиком (по известным ячейкам, ещё до
// движения частей). Масштаб не больше 1: если всё помещается — вид не трогаем.
function fitToCells(cells) {
  if (!state.svg || !state.zoom) return;
  const fit = meshgraphFitCellsTransform(cells, state.width, state.height);
  if (!fit) return;
  state.svg.call(
    state.zoom.transform,
    d3.zoomIdentity
      .translate(state.width / 2, state.height / 2)
      .scale(fit.k)
      .translate(-fit.cx, -fit.cy)
  );
}

function fitToContent() {
  if (viewMode() === "3d" && window.meshgraph3D) {
    window.meshgraph3D.fit();
    return;
  }
  if (!state.svg || !state.zoom || !state.g) return;
  const fit = meshgraphFitBoundsTransform(
    state.g.node().getBBox(),
    state.width,
    state.height
  );
  if (!fit) return;
  state.svg
    .transition()
    .duration(350)
    .call(
      state.zoom.transform,
      d3.zoomIdentity.translate(fit.tx, fit.ty).scale(fit.scale)
    );
}

function focusOnNode(target) {
  if (viewMode() === "3d" && window.meshgraph3D) {
    window.meshgraph3D.focusNode(target.id);
    return;
  }
  if (!state.svg || !state.zoom) return;
  const fit = meshgraphFocusTransform(
    target.x,
    target.y,
    state.width,
    state.height
  );
  state.svg
    .transition()
    .duration(450)
    .call(
      state.zoom.transform,
      d3.zoomIdentity.translate(fit.tx, fit.ty).scale(fit.k)
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
  else appendChatSegments(bubble, msg);
  if (msg.emoji_only) bubble.classList.add("chat-emoji-only");
  appendChatReactions(bubble, msg.reactions);

  main.appendChild(bubble);
  row.appendChild(main);
  return row;
}

// ---------------------------------------------------------------------------
// Chat links: previews, image collages, lightbox
// ---------------------------------------------------------------------------

// Пузырь со ссылками: текст и ссылки идут в исходном порядке, соседние
// картинки (даже разделённые пробелами) складываются в один коллаж.
function appendChatSegments(bubble, msg) {
  const segments = meshgraphSplitLinks(msg.text || "");
  const groups = meshgraphImageGroups(segments, meshgraphPreviewKind);
  let groupIndex = 0;
  let i = 0;
  while (i < segments.length) {
    const group =
      groupIndex < groups.length && groups[groupIndex].start === i
        ? groups[groupIndex]
        : null;
    if (group) {
      bubble.appendChild(
        chatCollageEl(group.urls.map((url) => ({
          // Страница обменника (…/v/…) — не картинка: браузер заблокировал бы
          // её как ORB-ответ, поэтому рисуем раскрытую preview.url.
          src: meshgraphResolvedImage(url),
          page: url,
        })))
      );
      i = group.end;
      groupIndex += 1;
      continue;
    }
    const segment = segments[i];
    if (segment.type === "text") {
      bubble.appendChild(chatEl("div", "chat-text", segment.text));
    } else {
      bubble.appendChild(chatLinkEl(segment.url));
    }
    i += 1;
  }
}

function meshgraphPreviewKind(url) {
  const preview = state.linkPreviews.get(url);
  return preview ? preview.kind : "pending";
}

// Картинка, которую реально можно рисовать: у превью это итоговый адрес
// (после редиректа или из og:image), а не сама ссылка из сообщения.
function meshgraphResolvedImage(url) {
  const preview = state.linkPreviews.get(url);
  return preview && preview.kind === "image" && preview.url ? preview.url : url;
}

// Ссылка: до прихода превью — компактный чип с хостом, после — карточка
// с заголовком и описанием (или картинка — её рисует коллаж выше).
function chatLinkEl(url) {
  scheduleLinkPreview(url);
  const preview = state.linkPreviews.get(url);
  if (preview && preview.kind === "page") return chatCardEl(url, preview);
  const chip = chatEl("a", "chat-link-chip");
  chip.href = url;
  chip.target = "_blank";
  chip.rel = "noopener noreferrer";
  chip.title = url;
  chip.appendChild(chatEl("span", "chat-link-host", meshgraphLinkHost(url)));
  chip.appendChild(chatEl("span", "chat-link-ext", "↗"));
  return chip;
}

function chatCardEl(url, preview) {
  const card = chatEl("a", "chat-link-card");
  card.href = preview.url || url;
  card.target = "_blank";
  card.rel = "noopener noreferrer";
  if (preview.image) {
    const thumb = document.createElement("img");
    thumb.className = "chat-link-thumb";
    thumb.src = preview.image;
    thumb.loading = "lazy";
    thumb.decoding = "async";
    thumb.alt = "";
    thumb.addEventListener("error", () => thumb.remove());
    card.appendChild(thumb);
  }
  const body = chatEl("div", "chat-link-body");
  body.appendChild(
    chatEl("div", "chat-link-title", preview.title || meshgraphLinkHost(url))
  );
  if (preview.description) {
    body.appendChild(chatEl("div", "chat-link-desc", preview.description));
  }
  body.appendChild(
    chatEl("div", "chat-link-site", preview.site || meshgraphLinkHost(url))
  );
  card.appendChild(body);
  return card;
}

// Коллаж соседних картинок: клетки-квадраты (1 — картинка целиком),
// по клику — лайтбокс на весь список. Протухшая ссылка (обменники живут
// недолго) превращается в чип на страницу-оригинал, а не в пустую плитку.
function chatCollageEl(items) {
  const gallery = items.map((item) => item.src);
  const grid = meshgraphCollageGrid(items.length);
  const wrap = chatEl("div", "chat-collage");
  wrap.dataset.n = String(grid.shown + (grid.extra ? 1 : 0));
  items.slice(0, grid.shown).forEach((item, index) => {
    const cell = chatEl("button", "chat-collage-cell");
    cell.type = "button";
    cell.title = "Показать";
    const img = document.createElement("img");
    img.src = item.src;
    img.loading = "lazy";
    img.decoding = "async";
    img.alt = "Картинка из ссылки";
    img.addEventListener("error", () => {
      cell.dataset.broken = "1";
      cell.classList.add("chat-collage-broken");
      cell.title = item.page;
      img.remove();
      cell.appendChild(chatEl("span", "", meshgraphLinkHost(item.page)));
    });
    cell.appendChild(img);
    cell.addEventListener("click", () => {
      if (cell.dataset.broken === "1") {
        window.open(item.page, "_blank", "noopener");
        return;
      }
      openLightbox(gallery, index);
    });
    wrap.appendChild(cell);
  });
  if (grid.extra) {
    const more = chatEl("button", "chat-collage-more", `+${grid.extra}`);
    more.type = "button";
    more.title = "Показать все картинки";
    more.addEventListener("click", () => openLightbox(gallery, grid.shown));
    wrap.appendChild(more);
  }
  return wrap;
}

// Запрос превью один раз на url; ответы приходят по одному, поэтому
// перерисовка чата пакуется в один таймер — без мигания картинок.
function scheduleLinkPreview(url) {
  if (state.linkPreviews.has(url) || state.linkPreviewPending.has(url)) return;
  state.linkPreviewPending.add(url);
  fetch(`/api/link_preview?url=${encodeURIComponent(url)}`)
    .then((response) => (response.ok ? response.json() : null))
    .then((data) => {
      state.linkPreviewPending.delete(url);
      state.linkPreviews.set(
        url,
        data && data.ok ? data.preview : { kind: "error" }
      );
      queueChatRerender();
    })
    .catch(() => {
      state.linkPreviewPending.delete(url);
      state.linkPreviews.set(url, { kind: "error" });
      queueChatRerender();
    });
}

function queueChatRerender() {
  if (state.linkPreviewTimer) return;
  state.linkPreviewTimer = setTimeout(() => {
    state.linkPreviewTimer = null;
    if (state.chatMessages.length) renderChat(state.chatMessages, true);
  }, 300);
}

// ---------------------------------------------------------------------------
// Lightbox: карусель картинок одного сообщения (gif остаётся анимированным)
// ---------------------------------------------------------------------------

function openLightbox(urls, index) {
  state.lightbox = { urls, index };
  $("lightbox").hidden = false;
  document.body.classList.add("lightbox-open");
  updateLightbox();
}

function updateLightbox() {
  const { urls, index } = state.lightbox;
  const url = urls[index];
  $("lightboxError").hidden = true;
  $("lightboxImg").hidden = false;
  $("lightboxImg").src = url;
  $("lightboxCounter").textContent = `${index + 1} / ${urls.length}`;
  $("lightboxPrev").hidden = urls.length < 2;
  $("lightboxNext").hidden = urls.length < 2;
  $("lightboxSource").href = url;
  // Соседние кадры грузим заранее — перелистывание не ждёт сети.
  [index - 1, index + 1].forEach((i) => {
    if (urls[i]) new Image().src = urls[i];
  });
}

function lightboxStep(delta) {
  const total = state.lightbox.urls.length;
  if (total < 2) return;
  state.lightbox.index = (state.lightbox.index + delta + total) % total;
  updateLightbox();
}

function closeLightbox() {
  $("lightbox").hidden = true;
  document.body.classList.remove("lightbox-open");
  state.lightbox = { urls: [], index: 0 };
}

function lightboxIsOpen() {
  return !$("lightbox").hidden;
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

function renderChat(messages, force) {
  const signature = chatSignature(messages);
  // force — превью ссылок изменились: состав сообщений прежний, но картинки
  // и карточки в пузырях уже другие.
  if (!force && signature === state.chatSig) return;  // список прежний — DOM не трогаем
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

    updateServerSwitch(status);
    updateConnectionStates();

    const dot = $("statusDot");
    dot.className = "dot " + (status.connected ? "on" : "off");
    $("statusText").textContent = status.connected
      ? `подключено к ${status.broker}`
      : `нет связи: ${status.broker}`;

    const conn = status.connection;
    const connCell = $("stConnection");
    if (conn && connCell) {
      connCell.textContent = conn.name || conn.id || "—";
      connCell.title = conn.db_file || "";
    }

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
  "connection_name",
  "mqtt_broker_address", "mqtt_port", "mqtt_username", "mqtt_password",
  "mqtt_topic_prefix", "mqtt_topic_suffix", "mqtt_client_id",
  "mqtt_tls", "mqtt_tls_insecure",
  "decryption_keys", "default_graph_mode", "default_hours",
  "retention_hours", "graph_packet_limit",
];

const BOOLEAN_FIELDS = new Set(["mqtt_tls", "mqtt_tls_insecure"]);

// Поля, уезжающие на сервер вместе с новым подключением. Держать в синхроне
// с PROFILE_FIELDS в meshgraph/config.py: глобальные настройки (период,
// хранение, лимиты) при создании подключения не отправляются.
const CONNECTION_FIELDS = new Set([
  "mqtt_broker_address", "mqtt_port", "mqtt_username", "mqtt_password",
  "mqtt_topic_prefix", "mqtt_topic_suffix", "mqtt_client_id",
  "mqtt_tls", "mqtt_tls_insecure", "decryption_keys", "connection_name",
]);

const FIELD_TO_INPUT = {
  connection_name: "setConnName",
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

// Активное подключение, режим «новое подключение» и последний ответ
// /api/settings — модалка живёт между открытиями, состояние восстанавливается
// при каждом открытии (applySettings).
let activeConnectionId = "";
let newConnectionMode = false;
let lastSettings = null;

// Пароль настроек живёт в памяти страницы до перезагрузки: он открывает
// только модалку настроек — график/чат и переключение серверов ему не нужны.
let settingsPassword = null;
// Последний /api/status: состояния серверов для списка и переключателя.
let lastStatusConnections = [];
let serverSwitchSignature = null;

function settingsHeaders(extra) {
  const headers = Object.assign({}, extra);
  if (settingsPassword !== null) headers["X-Settings-Password"] = settingsPassword;
  return headers;
}

async function settingsFetch(path, options) {
  // Запрос к защищённому эндпоинту: 401/403 сбрасывают сохранённый пароль
  // и открывают панель разблокировки, чтобы ввести его заново.
  const response = await fetch(
    path,
    Object.assign({}, options, {
      headers: settingsHeaders((options && options.headers) || {}),
    })
  );
  if (response.status === 401 || response.status === 403) {
    settingsPassword = null;
    if (!$("settingsModal").hidden) showUnlockPanel(true);
    throw new Error("Настройки защищены паролем — введите пароль.");
  }
  return response;
}

function showUnlockPanel(visible) {
  $("unlockPanel").hidden = !visible;
  $("settingsForm").hidden = visible;
  $("settingsSave").disabled = visible;
  if (visible) {
    showUnlockErrors([]);
    setTimeout(() => $("unlockPassword").focus(), 50);
  }
}

function showUnlockErrors(errors) {
  const box = $("unlockErrors");
  if (!errors || !errors.length) {
    box.hidden = true;
    return;
  }
  box.innerHTML = `<ul>${errors.map((e) => `<li>${escapeHtml(e)}</li>`).join("")}</ul>`;
  box.hidden = false;
}

async function unlockSettings() {
  const value = $("unlockPassword").value;
  if (!value) {
    showUnlockErrors(["Введите пароль."]);
    return;
  }
  try {
    const response = await fetch("/api/settings", {
      headers: { "X-Settings-Password": value },
    });
    if (response.status === 401) {
      showUnlockErrors(["Неверный пароль."]);
      return;
    }
    if (!response.ok) {
      showUnlockErrors([`Ошибка HTTP ${response.status}`]);
      return;
    }
    settingsPassword = value;
    $("unlockPassword").value = "";
    showUnlockPanel(false);
    applySettings(await response.json());
    setTimeout(() => $("setBroker").focus(), 50);
  } catch (error) {
    showUnlockErrors([String(error)]);
  }
}

function applySettings(settings) {
  lastSettings = settings;
  activeConnectionId = settings.active_connection || "";
  newConnectionMode = false;
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
  populateConnectionSelect(settings);
  renderConnectionList(settings);
  updatePasswordUi(settings);
  updateTopicPreview();
}

function populateConnectionSelect(settings) {
  const select = $("setConnection");
  if (!select) return;
  select.innerHTML = "";
  (settings.connections || []).forEach((profile) => {
    const option = document.createElement("option");
    option.value = profile.id;
    option.textContent = profile.name || `${profile.broker}:${profile.port}`;
    select.appendChild(option);
  });
  const create = document.createElement("option");
  create.value = "__new__";
  create.textContent = "+ Новое подключение…";
  select.appendChild(create);
  const wanted = newConnectionMode ? "__new__" : activeConnectionId;
  select.value = wanted;
  if (select.value !== wanted && settings.active_connection) {
    select.value = settings.active_connection;
  }
  updateConnectionDbHint(settings);
}

function updateConnectionDbHint(settings) {
  const file = $("connDbFile");
  if (file) {
    file.textContent = newConnectionMode
      ? "новая база будет создана после сохранения"
      : settings.db_file || "—";
  }
  const del = $("connDelete");
  if (del) del.disabled = (settings.connections || []).length <= 1;
}

// ---------------------------------------------------------------------------
// Manager list, per-server states and the public header switcher
// ---------------------------------------------------------------------------

function renderConnectionList(settings) {
  // «Менеджер серверов» внутри настроек: у каждой строки — тумблер фонового
  // сбора; клик по имени переключает активное подключение (как и селект).
  const box = $("connList");
  if (!box) return;
  box.innerHTML = "";
  const profiles = settings.connections || [];
  if (!profiles.length) return;

  const head = document.createElement("div");
  head.className = "conn-list-head";
  head.textContent = "Серверы — вкл/выкл фонового сбора:";
  box.appendChild(head);

  profiles.forEach((profile) => {
    const row = document.createElement("div");
    row.className =
      "conn-row" + (profile.id === settings.active_connection ? " active" : "");
    row.dataset.id = profile.id;

    const main = document.createElement("button");
    main.type = "button";
    main.className = "conn-row-main";
    main.title = `${profile.broker}:${profile.port} · ${profile.topic}\nБаза: ${profile.db_file}`;
    const name = document.createElement("span");
    name.className = "conn-row-name";
    name.textContent = profile.name || profile.id;
    const meta = document.createElement("span");
    meta.className = "conn-row-meta";
    meta.textContent = `${profile.broker}:${profile.port} · ${profile.topic}`;
    main.append(name, meta);
    main.addEventListener("click", () => {
      if (!newConnectionMode && profile.id === activeConnectionId) return;
      switchConnection(profile.id);
    });

    const state = document.createElement("span");
    state.className = "conn-row-state";
    state.dataset.id = profile.id;
    state.title = profile.enabled
      ? "состояние подключения"
      : "выключен — данные не собираются";

    const toggle = document.createElement("label");
    toggle.className = "switch";
    toggle.title = profile.enabled
      ? "Сбор данных включён — нажмите, чтобы выключить"
      : "Сбор данных выключен — нажмите, чтобы включить";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = Boolean(profile.enabled);
    input.addEventListener("change", () => toggleConnection(profile.id, input.checked));
    const knob = document.createElement("span");
    toggle.append(input, knob);

    row.append(main, state, toggle);
    box.appendChild(row);
  });
  updateConnectionStates();
}

function updateConnectionStates() {
  // Точки состояния в списке — из последнего опроса статуса (без
  // перерисовки строк, чтобы не сбивать фокус в открытой форме).
  const box = $("connList");
  if (!box) return;
  box.querySelectorAll(".conn-row").forEach((row) => {
    const dot = row.querySelector(".conn-row-state");
    if (!dot) return;
    const info = lastStatusConnections.find((c) => c.id === row.dataset.id);
    dot.className = "conn-row-state";
    if (!info) {
      dot.title = "";
      return;
    }
    dot.classList.toggle("on", Boolean(info.connected));
    dot.classList.toggle("off", !info.connected);
    dot.title = info.error
      ? info.error
      : info.connected
        ? "подключено"
        : info.enabled
          ? "нет связи"
          : "выключено";
  });
}

function updateServerSwitch(status) {
  // Публичный переключатель в шапке: только включённые серверы (плюс
  // активный, чтобы выбор не пропадал, если его выключили).
  const select = $("serverSwitch");
  if (!select) return;
  const all = status.connections || [];
  lastStatusConnections = all;
  const list = all.filter((c) => c.enabled || c.active);
  const signature = list.map((c) => `${c.id}:${c.name}`).join("\u0001");
  if (signature !== serverSwitchSignature) {
    serverSwitchSignature = signature;
    select.innerHTML = "";
    list.forEach((c) => {
      const option = document.createElement("option");
      option.value = c.id;
      option.textContent = c.name || c.id;
      select.appendChild(option);
    });
  }
  const activeId = status.connection ? status.connection.id : "";
  if (activeId && select.value !== activeId) select.value = activeId;
  select.title =
    "Серверы:\n" +
    all
      .map((c) => {
        const mark = c.connected ? "●" : "○";
        const off = c.enabled ? "" : " (выкл)";
        const err = c.error ? ` — ${c.error}` : "";
        return `${mark} ${c.name || c.id}${off}${err}`;
      })
      .join("\n");
}

async function switchFromHeader(pid) {
  // Переключение без пароля: сервер сам разрешит только включённые цели.
  try {
    const response = await fetch("/api/connections/select", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: pid }),
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok || !result.ok) {
      showMessage(
        "Не удалось переключиться",
        (result.errors || [`Ошибка HTTP ${response.status}`]).join("\n")
      );
      return;
    }
    activeConnectionId = result.settings.active_connection || pid;
    lastSettings = result.settings;
    if (!$("settingsModal").hidden && settingsPassword !== null) {
      applySettings(result.settings);
    }
    reloadGraphData();
  } catch (error) {
    showMessage("Не удалось переключиться", String(error));
  }
}

async function toggleConnection(pid, enabled) {
  // Вкл/выкл сервера в менеджере: выключенный клиент у воркера снимается,
  // история остаётся в его базе и включается обратно тем же переключателем.
  try {
    const beforeActive = activeConnectionId;
    const response = await settingsFetch(
      `/api/connections/${encodeURIComponent(pid)}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled }),
      }
    );
    const result = await response.json();
    if (!response.ok || !result.ok) {
      showFormErrors(result.errors || [`Ошибка HTTP ${response.status}`]);
      if (lastSettings) applySettings(lastSettings);  // вернуть тумблеры
      return;
    }
    showFormErrors([]);
    const profile = (result.settings.connections || []).find((p) => p.id === pid);
    const label = (profile && profile.name) || pid;
    applySettings(result.settings);
    $("settingsHint").textContent = enabled
      ? `«${label}» включён: подключение установится за секунду, сбор начнётся.`
      : `«${label}» выключен: сбор остановлен, история в его базе сохранена.`;
    pollStatus();
    if (result.settings.active_connection !== beforeActive) reloadGraphData();
  } catch (error) {
    showFormErrors([String(error)]);
  }
}

function updatePasswordUi(settings) {
  const set = Boolean(settings.settings_password_set);
  const input = $("setSettingsPassword");
  if (!input) return;
  input.placeholder = set
    ? "пароль задан — введите новый, чтобы сменить"
    : "не задан — настройки открыты";
  const clear = $("passwordClear");
  if (clear) clear.hidden = !set;
  const save = $("passwordSave");
  if (save) save.textContent = set ? "Сменить пароль" : "Задать пароль";
  const hint = $("passwordHint");
  if (hint) {
    hint.textContent = set
      ? "Пароль спрашивается при открытии настроек и хранится до перезагрузки страницы. Граф, чат и переключение между включёнными серверами работают без него."
      : "Пароль не задан — настройки открыты. Задайте его, чтобы ограничить управление серверами и настройками.";
  }
}

async function postSettingsPassword(value) {
  try {
    const response = await settingsFetch("/api/settings/password", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: value }),
    });
    const result = await response.json();
    if (!response.ok || !result.ok) {
      showFormErrors(result.errors || [`Ошибка HTTP ${response.status}`]);
      return;
    }
    showFormErrors([]);
    // Новый пароль — сразу в память страницы: старый в заголовке не пройдёт.
    settingsPassword = value || null;
    $("setSettingsPassword").value = "";
    applySettings(result.settings);
    $("settingsHint").textContent = value
      ? "Пароль сохранён: при следующем открытии настроек его спросят."
      : "Пароль снят — настройки открыты без него.";
  } catch (error) {
    showFormErrors([String(error)]);
  }
}

function saveSettingsPassword() {
  const value = $("setSettingsPassword").value;
  if (!value) {
    showFormErrors(["Введите новый пароль (не короче 4 символов)."]);
    return;
  }
  postSettingsPassword(value);
}

function clearSettingsPassword() {
  const confirmed = window.confirm(
    "Снять пароль с настроек?\n\nК ним будет доступ без него — любым, у кого есть ссылка."
  );
  if (!confirmed) return;
  postSettingsPassword("");
}

function enterNewConnectionMode() {
  // «Новое подключение» начинается с копии текущего: брокер, порт, TLS,
  // учётные данные и ключи уже на месте — меняется только нужное (чаще
  // всего топик), и у нового подключения появляется своя база. Идентичная
  // копия на сервере не плодится: система переключит на существующее.
  newConnectionMode = true;
  $("setConnName").value = "";
  const file = $("connDbFile");
  if (file) file.textContent = "новая база будет создана после сохранения";
  const del = $("connDelete");
  if (del) del.disabled = true;
  showFormErrors([]);
  $("settingsHint").textContent =
    "Копия текущего подключения: измените нужное (например, топик) и сохраните — новое подключение получит свою базу данных.";
  updateTopicPreview();
  setTimeout(() => $("setSuffix").focus(), 50);
}

async function switchConnection(pid) {
  const saveBtn = $("settingsSave");
  saveBtn.disabled = true;
  try {
    const response = await fetch("/api/connections/select", {
      method: "POST",
      headers: settingsHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ id: pid }),
    });
    const result = await response.json().catch(() => ({}));
    if (response.status === 401 || response.status === 403) {
      // Выключенный сервер без пароля недоступен — предложить разблокировку.
      settingsPassword = null;
      showUnlockPanel(true);
      return;
    }
    if (!response.ok || !result.ok) {
      showFormErrors(result.errors || [`Ошибка HTTP ${response.status}`]);
      // Переключение не вышло — вернуть селектор к серверному состоянию.
      fetch("/api/settings", { headers: settingsHeaders() })
        .then((r) => (r.ok ? r.json() : null))
        .then((settings) => {
          if (settings) applySettings(settings);
        })
        .catch(() => {});
      return;
    }
    showFormErrors([]);
    applySettings(result.settings);
    $("settingsHint").textContent =
      `Подключено: ${result.settings.connection_name}. Граф, каналы и чат читаются из базы этого подключения.`;
    reloadGraphData();
  } catch (error) {
    showFormErrors([String(error)]);
  } finally {
    saveBtn.disabled = false;
  }
}

async function onConnectionChange() {
  const select = $("setConnection");
  if (select.value === "__new__") {
    enterNewConnectionMode();
    return;
  }
  if (!select.value || select.value === activeConnectionId) return;
  await switchConnection(select.value);
}

async function deleteConnection() {
  if (!activeConnectionId || !lastSettings) return;
  const profile = (lastSettings.connections || []).find(
    (p) => p.id === activeConnectionId
  );
  const label = (profile && profile.name) || activeConnectionId;
  const dbFile = (profile && profile.db_file) || lastSettings.db_file;
  const confirmed = window.confirm(
    `Удалить подключение «${label}»?\n\n` +
    `Если оно активное — переключимся на следующее подключение.\n` +
    `Файл базы данных ${dbFile} останется на диске: данные не удалятся.`
  );
  if (!confirmed) return;
  try {
    const response = await settingsFetch(
      `/api/connections/${encodeURIComponent(activeConnectionId)}`,
      { method: "DELETE" }
    );
    const result = await response.json();
    if (!response.ok || !result.ok) {
      showFormErrors(result.errors || [`Ошибка HTTP ${response.status}`]);
      return;
    }
    showFormErrors([]);
    applySettings(result.settings);
    $("settingsHint").textContent =
      "Подключение удалено, файл базы сохранён. Смотрим оставшееся.";
    reloadGraphData();
  } catch (error) {
    showFormErrors([String(error)]);
  }
}

function reloadGraphData() {
  // Всё, что читается из базы активного подключения, надо обновить целиком.
  pollStatus();
  loadChannels();
  loadGraph();
  loadChat();
}

function openSettings() {
  $("settingsModal").hidden = false;
  $("formErrors").hidden = true;
  $("settingsHint").textContent = "Изменения применяются сразу: соединение переустанавливается.";
  const loaded = (settings) => {
    showUnlockPanel(false);
    applySettings(settings);
    setTimeout(() => $("setBroker").focus(), 50);
  };
  if (settingsPassword !== null) {
    settingsFetch("/api/settings")
      .then((r) => r.json())
      .then(loaded)
      .catch((error) => showFormErrors([`Не удалось прочитать настройки: ${error}`]));
    return;
  }
  // Пароль ещё не вводили: пробуем открыть без него — 401 приходит, только
  // если он вообще задан (иначе это первый запуск: раздел «Безопасность»).
  fetch("/api/settings")
    .then((r) => {
      if (r.status === 401) {
        showUnlockPanel(true);
        return null;
      }
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return r.json();
    })
    .then((settings) => {
      if (settings) loaded(settings);
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

  // В режиме «новое подключение» на сервер уезжают только поля профиля:
  // глобальные настройки отправляются обычным сохранением настроек.
  const wasNew = newConnectionMode;
  const body = wasNew
    ? Object.fromEntries(
        Object.entries(payload).filter(([key]) => CONNECTION_FIELDS.has(key))
      )
    : payload;
  const endpoint = wasNew ? "/api/connections" : "/api/settings";

  const saveBtn = $("settingsSave");
  saveBtn.disabled = true;
  try {
    const response = await settingsFetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const result = await response.json();
    if (!response.ok || !result.ok) {
      showFormErrors(result.errors || [`Ошибка HTTP ${response.status}`]);
      return;
    }
    showFormErrors([]);
    const beforeId = lastSettings ? lastSettings.active_connection : "";
    const beforeCount = lastSettings ? (lastSettings.connections || []).length : 0;
    const afterCount = (result.settings.connections || []).length;
    applySettings(result.settings);
    if (!wasNew) {
      $("settingsHint").textContent = "Сохранено. Переподключение к брокеру…";
    } else if (afterCount > beforeCount) {
      $("settingsHint").textContent = "Создано и подключено. Переподключение к брокеру…";
    } else if (result.settings.active_connection !== beforeId) {
      $("settingsHint").textContent =
        "Такое подключение уже было — переключились на него, ничего не создано.";
    } else {
      $("settingsHint").textContent = "Это подключение уже есть — ничего не создано.";
    }
    setTimeout(() => {
      closeSettings();
      reloadGraphData();
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

// ---------------------------------------------------------------------------
// Sidebar: the whole panel collapses to the left edge (hamburger in topbar)
// ---------------------------------------------------------------------------

// Держать в синхроне с @media (max-width: 900px) в style.css и inline-скриптом
// в index.html: на узких экранах панель — выдвижной ящик поверх графа.
const SIDEBAR_MOBILE_QUERY = "(max-width: 900px)";
const SIDEBAR_KEY = "meshgraph.sidebar.collapsed";

function sidebarIsMobile() {
  return window.matchMedia(SIDEBAR_MOBILE_QUERY).matches;
}

function setSidebarCollapsed(collapsed, persist) {
  document
    .querySelector(".layout")
    .classList.toggle("sidebar-collapsed", collapsed);
  const btn = $("sidebarBtn");
  if (btn) {
    btn.setAttribute("aria-expanded", collapsed ? "false" : "true");
    const title = collapsed
      ? "Показать боковую панель"
      : "Свернуть боковую панель";
    btn.title = title;
    btn.setAttribute("aria-label", title);
  }
  // Выбор запоминаем только для десктопа: мобильный ящик всегда стартует
  // закрытым и не должен менять десктопное предпочтение.
  if (persist && !sidebarIsMobile()) {
    storeSet(SIDEBAR_KEY, collapsed ? "1" : "");
  }
  // Ширина холста изменилась → перерисовка под неё (существующий обработчик
  // resize сам делает паузу 250 мс — анимация успевает закончиться).
  // Мобильный ящик лежит поверх графа и холст не трогает — без лишнего
  // запроса.
  if (!sidebarIsMobile()) window.dispatchEvent(new Event("resize"));
}

function initSidebarToggle() {
  // Стартовое состояние inline-скрипт в index.html уже поставил без вспышки —
  // здесь синхронизируем aria/title и вешаем обработчики.
  const layout = document.querySelector(".layout");
  setSidebarCollapsed(layout.classList.contains("sidebar-collapsed"), false);

  $("sidebarBtn").addEventListener("click", () =>
    setSidebarCollapsed(!layout.classList.contains("sidebar-collapsed"), true)
  );
  // Тап по затемнению закрывает мобильный ящик (на десктопе его нет).
  $("sidebarBackdrop").addEventListener("click", () =>
    setSidebarCollapsed(true, true)
  );
  // Переход через границу экрана: на мобильных ящик всегда закрыт, на
  // десктопе возвращается сохранённое состояние.
  try {
    window.matchMedia(SIDEBAR_MOBILE_QUERY).addEventListener("change", () => {
      setSidebarCollapsed(
        sidebarIsMobile() || storeGet(SIDEBAR_KEY) === "1",
        false
      );
    });
  } catch { /* старые браузеры без addEventListener на matchMedia */ }
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

// ---------------------------------------------------------------------------
// Маршруты пакетов: светящиеся шарики вдоль рёбер (движок packetflow.js)
// ---------------------------------------------------------------------------
//
// Данные — /api/packet_routes: веер «один пакет — несколько шлюзов» и
// цепочки трассировок. Один rAF-цикл обслуживает оба вида: кадр рисуется и
// в SVG-слой (2D), и в 3D-группу (graph3d.renderFlow) одновременно —
// переключение вида посреди воспроизведения не требует перезапуска.

const FLOW_PULSE_KEY = "meshgraph.flowPulse";
const FLOW_REPLAY_TITLE = "Повторить маршруты свежих пакетов";
const FLOW_EMPTY_TITLE = "Маршрутов за последнее время не найдено";
const SVG_NS = "http://www.w3.org/2000/svg";

function flowParams() {
  return globalThis.MESHGRAPH_FLOW_PARAMS;
}

/** Общий источник данных для кнопки повтора и фонового пульса. */
async function fetchPacketRoutes(minutes) {
  const response = await fetch(`/api/packet_routes?minutes=${minutes}`);
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const payload = await response.json();
  return payload.routes || [];
}

/**
 * Поставить маршруты в очередь и запустить цикл, если он стоит.
 * replace — кнопка повтора: текущее воспроизведение сбрасывается.
 */
function flowStart(routes, { replace = false } = {}) {
  if (!routes.length) return;
  const flow = state.flow;
  if (replace) {
    if (flow.playing) flowStop();
    flow.queue = [];
  }
  flow.queue.push(...routes);
  flowEnsure();
}

function flowEnsure() {
  const flow = state.flow;
  const params = flowParams();
  // params нет только при кэше старого шаблона без packetflow.js — тогда
  // кнопок нет и без этого, но и стартер не должен падать.
  if (flow.playing || !flow.queue.length || !params) return;
  if (typeof globalThis.meshgraphFlowPlan !== "function") return;
  const batch = flow.queue.splice(0, params.maxRoutes);
  const graph = state.graph;
  flow.nodesById = new Map(
    ((graph && graph.nodes) || []).map((node) => [node.id, node])
  );
  const linkKeys = ((graph && graph.links) || []).map(linkKey);
  const plan = globalThis.meshgraphFlowPlan(
    batch,
    [...flow.nodesById.keys()],
    linkKeys,
    params
  );
  if (!plan.routes.length) return; // всё отфильтровано — показывать нечего
  flow.plan = plan;
  flow.startedAt = performance.now();
  flow.playing = true;
  flow.raf = requestAnimationFrame(flowTick);
}

function flowTick() {
  const flow = state.flow;
  if (!flow.playing) return;
  const elapsed = performance.now() - flow.startedAt;
  const frame = globalThis.meshgraphFlowAt(flow.plan, elapsed, flow.nodesById);
  flowPaint(frame);
  if (frame.done) {
    flowStop();
    // Пульс мог накидать маршруты, пока партия играла.
    flowEnsure();
    return;
  }
  flow.raf = requestAnimationFrame(flowTick);
}

function flowStop() {
  const flow = state.flow;
  if (flow.raf) cancelAnimationFrame(flow.raf);
  flow.raf = 0;
  flow.playing = false;
  flow.plan = null;
  // Пустой кадр гасит накопленные элементы в обоих рендерах.
  flowPaint({ dots: [], legs: [], done: true });
}

function flowPaint(frame) {
  drawFlow2D(frame);
  // Оба рендера получают каждый кадр: переключение вида посреди игры не
  // требует ни перезапуска, ни логики «кто сейчас рисует».  Цвет —
  // акцентный (CSS-переменная), он выделяется и на светлом холсте, и на
  // тёмном поверх зелёно-жёлтых связей.
  if (window.meshgraph3D) window.meshgraph3D.renderFlow(frame);
}

// --- 2D: слой SVG поверх связей ------------------------------------------

function flowLayerEnsure() {
  const flow = state.flow;
  if (flow.layer && flow.layer.parentNode) return true;
  // Слой отсоединён (холст пересобрали) — его пул умер вместе с ним.
  flow.layer = null;
  flow.legEls = new Map();
  flow.dotEls = new Map();
  // state.g — d3-выборка: живой узел смотрим через .node() (3D-холст и
  // пустота дают отсоединённый или отсутствующий узел).
  const g = state.g && state.g.node();
  if (!g || !g.parentNode) return false;
  flow.layer = state.g.append("g").attr("class", "flow-layer").node();
  return true;
}

function flowLineElements() {
  const halo = document.createElementNS(SVG_NS, "line");
  halo.setAttribute("class", "flow-leg-halo");
  const core = document.createElementNS(SVG_NS, "line");
  core.setAttribute("class", "flow-leg");
  return { halo, core };
}

function flowDotElements() {
  const params = flowParams();
  const group = document.createElementNS(SVG_NS, "g");
  const halo = document.createElementNS(SVG_NS, "circle");
  halo.setAttribute("class", "flow-dot-halo");
  halo.setAttribute("r", params.haloRadius);
  const core = document.createElementNS(SVG_NS, "circle");
  core.setAttribute("class", "flow-dot");
  core.setAttribute("r", params.dotRadius);
  group.append(halo, core);
  return { group, halo, core };
}

function drawFlow2D(frame) {
  const flow = state.flow;
  if (flow.layer && !flow.layer.parentNode) {
    flow.layer = null;
    flow.legEls = new Map();
    flow.dotEls = new Map();
  }
  const drawable = frame.legs.length || frame.dots.length;
  if (!drawable && !flow.layer) return; // нечего рисовать — и рисовать некуда
  if (drawable && !flowLayerEnsure()) return;

  // Рёбра: широкое полупрозрачное «сияние» + яркая сердцевина.
  const wantedLegs = new Map(frame.legs.map((leg) => [leg.key, leg]));
  for (const [key, rec] of [...flow.legEls]) {
    if (wantedLegs.has(key)) continue;
    rec.halo.remove();
    rec.core.remove();
    flow.legEls.delete(key);
  }
  wantedLegs.forEach((leg, key) => {
    let rec = flow.legEls.get(key);
    if (!rec) {
      rec = flowLineElements();
      flow.layer.append(rec.halo, rec.core);
      flow.legEls.set(key, rec);
    }
    // Цвет — из CSS (var(--accent)): и тень-контур в светлой теме, и
    // свечение в тёмной задаются стилями, а не кадром.
    for (const el of [rec.halo, rec.core]) {
      el.setAttribute("x1", leg.ax);
      el.setAttribute("y1", leg.ay);
      el.setAttribute("x2", leg.bx);
      el.setAttribute("y2", leg.by);
    }
    rec.halo.setAttribute("opacity", (0.3 * leg.phase).toFixed(3));
    rec.core.setAttribute("opacity", (0.95 * leg.phase).toFixed(3));
  });

  // Шарики: ореол + ядро, позиция берётся из кадра.
  const wantedDots = new Map(frame.dots.map((dot) => [dot.key, dot]));
  for (const [key, rec] of [...flow.dotEls]) {
    if (wantedDots.has(key)) continue;
    rec.group.remove();
    flow.dotEls.delete(key);
  }
  wantedDots.forEach((dot, key) => {
    let rec = flow.dotEls.get(key);
    if (!rec) {
      rec = flowDotElements();
      flow.layer.append(rec.group);
      flow.dotEls.set(key, rec);
    }
    rec.halo.setAttribute("cx", dot.x);
    rec.halo.setAttribute("cy", dot.y);
    rec.core.setAttribute("cx", dot.x);
    rec.core.setAttribute("cy", dot.y);
  });
}

// --- Кнопки и фоновый пульс ------------------------------------------------

/** ▶ — проиграть свежие маршруты заново. */
async function flowReplay() {
  const flow = state.flow;
  if (flow.loading) return;
  flow.loading = true;
  try {
    const routes = await fetchPacketRoutes(flowParams().replayMinutes);
    if (!routes.length) {
      // Тишина после клика выглядит как поломка — подсказкой на кнопке.
      const btn = $("flowReplay");
      if (btn) {
        btn.title = FLOW_EMPTY_TITLE;
        setTimeout(() => {
          btn.title = FLOW_REPLAY_TITLE;
        }, 4000);
      }
      return;
    }
    flowStart(routes, { replace: true });
  } catch (error) {
    console.error("Failed to load packet routes:", error);
  } finally {
    flow.loading = false;
  }
}

/** ✦ — переключить фоновый пульс (состояние переживает перезагрузку). */
function flowTogglePulse() {
  storeSet(FLOW_PULSE_KEY, storeGet(FLOW_PULSE_KEY) === "1" ? "0" : "1");
  flowApplyPulse();
}

function flowApplyPulse() {
  const btn = $("flowPulse");
  if (!btn) return;
  const on = storeGet(FLOW_PULSE_KEY) === "1";
  btn.classList.toggle("is-active", on);
  const flow = state.flow;
  if (on && !flow.pulseTimer) {
    flow.pulseTimer = setInterval(flowPulseTick, flowParams().pulsePollMs);
    flowPulseTick();
  } else if (!on && flow.pulseTimer) {
    clearInterval(flow.pulseTimer);
    flow.pulseTimer = null;
  }
}

/** Свежая порция пульса: только то, что в эфире не игралось. */
async function flowPulseTick() {
  if (!state.graph) return;
  try {
    const routes = await fetchPacketRoutes(flowParams().pulseMinutes);
    const fresh = routes.filter((route) => route.ts > state.flow.lastPulseTs);
    if (!fresh.length) return;
    state.flow.lastPulseTs = Math.max(...fresh.map((route) => route.ts));
    flowStart(fresh);
  } catch (error) {
    console.error("Packet flow pulse failed:", error);
  }
}

function init() {
  initTheme();
  initSidebar();
  initSidebarToggle();
  initChat();

  document.querySelectorAll('input[name="mode"]').forEach((radio) => {
    radio.addEventListener("change", applyFilters);
  });

  ["hours", "minSnr", "channel", "includeIndirect"].forEach((id) => {
    $(id).addEventListener("change", applyFilters);
  });

  document.addEventListener("keydown", (event) => {
    // Лайтбокс поверх всего: Escape его закрывает, стрелки листают.
    if (lightboxIsOpen()) {
      if (event.key === "Escape") closeLightbox();
      else if (event.key === "ArrowLeft") lightboxStep(-1);
      else if (event.key === "ArrowRight") lightboxStep(1);
      return;
    }
    if (event.key === "Escape") {
      if (!$("settingsModal").hidden) closeSettings();
      // Мобильный ящик — тоже оверлей: первый Escape его закрывает…
      else if (
        sidebarIsMobile() &&
        !document.querySelector(".layout").classList.contains("sidebar-collapsed")
      ) {
        setSidebarCollapsed(true, false);
      }
      // …и только потом снимается выделение.
      else select(null, null);
    }
  });

  $("clearSelection").addEventListener("click", () => select(null, null));
  $("nodeSearch").addEventListener("input", (e) => runSearch(e.target.value));

  // Переключатель 2D/3D. Пустая проверка — на случай кэшированного шаблона
  // без кнопок (статика обновляется раньше, чем перечитается index.html).
  if ($("view2dBtn") && $("view3dBtn")) {
    $("view2dBtn").addEventListener("click", () => switchView("2d"));
    $("view3dBtn").addEventListener("click", () => switchView("3d"));
    updateViewButtons();
  }

  // Маршруты пакетов: повтор и фоновый пульс. Пустая проверка — как у
  // переключателя вида: кэшированный шаблон может быть без новых кнопок.
  if ($("flowReplay") && $("flowPulse")) {
    $("flowReplay").addEventListener("click", flowReplay);
    $("flowPulse").addEventListener("click", flowTogglePulse);
    flowApplyPulse();
  }

  // Лайтбокс: крестик, стрелки, клик по фону, свайп и битая картинка.
  $("lightboxClose").addEventListener("click", closeLightbox);
  $("lightboxPrev").addEventListener("click", () => lightboxStep(-1));
  $("lightboxNext").addEventListener("click", () => lightboxStep(1));
  $("lightboxStage").addEventListener("click", (event) => {
    if (event.target === $("lightboxStage")) closeLightbox();
  });
  $("lightboxImg").addEventListener("error", () => {
    $("lightboxImg").hidden = true;
    $("lightboxError").hidden = false;
  });
  let lightboxSwipeX = null;
  $("lightbox").addEventListener(
    "touchstart",
    (event) => {
      lightboxSwipeX =
        event.touches.length === 1 ? event.touches[0].clientX : null;
    },
    { passive: true }
  );
  $("lightbox").addEventListener(
    "touchend",
    (event) => {
      if (lightboxSwipeX === null) return;
      const dx = event.changedTouches[0].clientX - lightboxSwipeX;
      lightboxSwipeX = null;
      if (Math.abs(dx) >= 48) lightboxStep(dx < 0 ? 1 : -1);
    },
    { passive: true }
  );

  $("zoomIn").addEventListener("click", () => zoomBy(1.35));
  $("zoomOut").addEventListener("click", () => zoomBy(1 / 1.35));
  $("zoomFit").addEventListener("click", fitToContent);

  $("settingsBtn").addEventListener("click", openSettings);
  $("messageSettings").addEventListener("click", openSettings);
  $("settingsClose").addEventListener("click", closeSettings);
  $("settingsCancel").addEventListener("click", closeSettings);
  $("settingsSave").addEventListener("click", saveSettings);
  $("setConnection").addEventListener("change", onConnectionChange);
  $("connDelete").addEventListener("click", deleteConnection);
  $("unlockBtn").addEventListener("click", unlockSettings);
  $("unlockPassword").addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      unlockSettings();
    }
  });
  $("serverSwitch").addEventListener("change", (event) => {
    const pid = event.target.value;
    if (!pid || pid === activeConnectionId) return;
    switchFromHeader(pid);
  });
  $("passwordSave").addEventListener("click", saveSettingsPassword);
  $("passwordClear").addEventListener("click", clearSettingsPassword);
  // Enter в поле настроек не должен перезагружать страницу (неявная
  // отправка формы): сохранение — только кнопкой «Сохранить».
  $("settingsForm").addEventListener("submit", (event) => event.preventDefault());
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
    // Фоновое обновление — без оверлея со спиннером: граф не моргает
    // каждые AUTO_REFRESH_MS (см. loadGraph({silent})).
    loadGraph({ silent: true });
  }, AUTO_REFRESH_MS);
}

document.addEventListener("DOMContentLoaded", init);
