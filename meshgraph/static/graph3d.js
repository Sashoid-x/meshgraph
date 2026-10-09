/* 3D view of the mesh graph: three.js scene, force3d relaxation, picking.
 *
 * app.js drives everything through `window.meshgraph3D` — this module never
 * reaches into app state itself, it receives a payload (nodes, links, anchors
 * seed data, callbacks) and reports user actions back through those callbacks.
 * Classic dependencies (layout.js, force3d.js) arrive as plain globals;
 * three.js comes through the import map declared in index.html.
 *
 * The renderer, camera and OrbitControls are created once and reused across
 * re-render (auto-refresh runs every minute — a fresh WebGL context per
 * refresh would hit the browser's context limit).
 */

import * as THREE from "three";
import { OrbitControls } from "./OrbitControls.js";

const LABEL_NEAR_DIST = 460; // labels for nodes closer than this to the camera
const CLICK_PX = 5; // pointer travel below this counts as a click, not a drag
const SIM_STEPS_PER_FRAME = 3;
const DIM_OPACITY = 0.16; // matches .dimmed { opacity: .12 } of the 2D view
const DIM_COLOR = 0.18; // colour multiplier for dimmed lines

const view = {
  ready: false,
  running: false,
  renderer: null,
  scene: null,
  camera: null,
  controls: null,
  raycaster: null,
  host: null,
  labelLayer: null,
  nodeGroup: null,
  lines: null,
  indirect: null,
  lineColors: null, // Float32Array base colours of direct links
  indirectDistancesDirty: false,
  records: [], // per node: {node, group, mesh, mat, rimMat, size, label}
  linkRecords: [], // per direct link: {link, key, index, from, to, color}
  indirectRecords: [],
  nodes: [],
  links: [],
  pairs: [],
  anchors: [],
  alpha: 0,
  raf: 0,
  callbacks: {},
  selectedNodeId: null,
  selectedLinkKey: null,
  hoveredNodeId: null,
  hoverActive: false,
  nodeIndex: new Map(),
  pointerDown: null,
  lastHoverAt: 0,
  cameraTween: null,
  cameraReady: false,
  userMovedCamera: false,
  sphereGeo: null,
  rimGeo: null,
  // Маршруты пакетов (кадры из packetflow.js через app.js): собственная
  // группа — disposeObjects её не трогает, анимация переживает пересборку.
  flowGroup: null,
  flowLegs: new Map(), // key → {line}
  flowDots: new Map(), // key → {group, core, glow}
  flowGlowTex: null,
  flowAccent: null, // --accent, перечитывается при смене темы
  flowTheme: null,
  width: 0,
  height: 0,
};

// ---------------------------------------------------------------------------
// Setup / teardown
// ---------------------------------------------------------------------------

function ensureInit(width, height) {
  if (view.ready) {
    measure();
    return true;
  }
  view.host = document.getElementById("graphCanvas");
  if (!view.host) return false;
  try {
    view.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  } catch (error) {
    console.error("3D view unavailable:", error);
    return false;
  }
  view.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  view.renderer.setClearColor(0x000000, 0);
  view.renderer.domElement.classList.add("graph3d-canvas");

  view.scene = new THREE.Scene();
  view.camera = new THREE.PerspectiveCamera(
    50,
    Math.max(width, 1) / Math.max(height, 1),
    1,
    9000
  );
  view.camera.position.set(300, 240, 560);

  view.controls = new OrbitControls(view.camera, view.renderer.domElement);
  view.controls.enableDamping = true;
  view.controls.dampingFactor = 0.08;
  view.controls.minDistance = 60;
  view.controls.maxDistance = 4500;

  view.raycaster = new THREE.Raycaster();
  view.raycaster.params.Line.threshold = 8;
  view.sphereGeo = new THREE.SphereGeometry(1, 18, 12);
  view.rimGeo = new THREE.SphereGeometry(1, 18, 12);

  view.scene.add(new THREE.AmbientLight(0xffffff, 0.95));
  const key = new THREE.DirectionalLight(0xffffff, 1.25);
  key.position.set(1, 1.7, 1.2);
  view.scene.add(key);

  view.labelLayer = document.createElement("div");
  view.labelLayer.className = "label3d-layer";

  const canvas = view.renderer.domElement;
  canvas.addEventListener("pointerdown", (event) => {
    view.pointerDown = { x: event.clientX, y: event.clientY };
  });
  canvas.addEventListener("pointerup", onPointerUp);
  canvas.addEventListener("pointermove", onPointerMove);
  canvas.addEventListener("pointerleave", () => {
    clearHover();
    view.pointerDown = null;
  });
  // A real gesture (drag or wheel) cancels a programmed flight and, like the
  // 2D userMoved flag, disarms future auto-fits.  A plain click does not.
  canvas.addEventListener("wheel", markCameraMoved, { passive: true });

  if (typeof ResizeObserver !== "undefined") {
    const observer = new ResizeObserver(() => measure());
    observer.observe(view.host);
  }

  view.ready = true;
  measure();
  return true;
}

/** Match the drawing buffer and projection to the container box. */
function measure() {
  if (!view.ready || !view.host) return;
  const rect = view.host.getBoundingClientRect();
  const width = Math.max(1, Math.round(rect.width));
  const height = Math.max(1, Math.round(rect.height));
  if (width === view.width && height === view.height) return;
  view.width = width;
  view.height = height;
  view.renderer.setSize(width, height, false);
  view.camera.aspect = width / height;
  view.camera.updateProjectionMatrix();
}

/** Stop the animation loop (app.js calls this before rebuilding the view). */
function deactivate() {
  view.running = false;
  if (view.raf) cancelAnimationFrame(view.raf);
  view.raf = 0;
}

/** Drop scene objects and their GPU resources before a rebuild. */
function disposeObjects() {
  [view.nodeGroup, view.lines, view.indirect].forEach((obj) => {
    if (!obj) return;
    obj.traverse((child) => {
      if (child.material) child.material.dispose();
      if (
        child.geometry &&
        child.geometry !== view.sphereGeo &&
        child.geometry !== view.rimGeo
      ) {
        child.geometry.dispose();
      }
    });
    view.scene.remove(obj);
  });
  view.nodeGroup = null;
  view.lines = null;
  view.indirect = null;
  view.lineColors = null;
  view.records = [];
  view.linkRecords = [];
  view.indirectRecords = [];
  if (view.labelLayer) view.labelLayer.textContent = "";
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

function idOf(endpoint) {
  return endpoint && typeof endpoint === "object" ? endpoint.id : endpoint;
}

function markCameraMoved() {
  view.cameraTween = null;
  view.userMovedCamera = true;
}

/**
 * Rebuild the whole scene from a payload:
 *   {nodes, links, indirect, color, linkKey, structureSame, prevNodes,
 *    selectedNodeId, selectedLinkKey, callbacks}
 * Returns false when WebGL is unavailable (app.js falls back to the 2D view).
 */
function render(payload) {
  if (!ensureInit(payload.width, payload.height)) return false;
  if (view.renderer.domElement.parentNode !== view.host) {
    view.host.appendChild(view.renderer.domElement);
    view.host.appendChild(view.labelLayer);
  }
  measure();
  disposeObjects();

  view.nodes = payload.nodes;
  view.links = payload.links;
  view.callbacks = payload.callbacks || {};
  view.selectedNodeId = payload.selectedNodeId ?? null;
  view.selectedLinkKey = payload.selectedLinkKey ?? null;
  view.hoveredNodeId = null;
  view.hoverActive = false;
  view.nodeIndex = new Map(payload.nodes.map((node) => [node.id, node]));

  // Islands → one anchor per node, so disconnected parts keep their own
  // patch of space (the role of the packed cells in the 2D layout).
  const islands = globalThis.meshgraphFindComponents(
    payload.nodes,
    payload.links.concat(payload.indirect)
  );
  const islandIndex = new Map();
  islands.forEach((ids, index) => ids.forEach((id) => islandIndex.set(id, index)));
  const anchorPoints = globalThis.meshgraphAnchorPoints3D(
    islands.length,
    globalThis.MESHGRAPH_FORCE3D_PARAMS.islandSpacing
  );
  const byId = new Map(payload.nodes.map((node, index) => [node.id, index]));
  view.anchors = payload.nodes.map(
    (node) => anchorPoints[islandIndex.get(node.id) || 0] || anchorPoints[0]
  );

  globalThis.meshgraphSeedPositions3D(
    payload.nodes,
    payload.prevNodes,
    view.anchors,
    globalThis.MESHGRAPH_FORCE3D_PARAMS
  );

  // Rest lengths reuse the shared 2D formula — graphs read at the same scale
  // in both views.  Only direct links take part in the forces, mirroring
  // forceLink(data.links); indirect edges merely affect island detection.
  view.pairs = [];
  payload.links.forEach((link) => {
    const i = byId.get(idOf(link.source));
    const j = byId.get(idOf(link.target));
    if (i === undefined || j === undefined) return;
    view.pairs.push([i, j, globalThis.meshgraphLinkDistance(link)]);
  });

  // A cold start only when the structure is new *or* the previous view was
  // flat (2D): flat coordinates need the spiral z-seed to relax into depth.
  const prevWas3D =
    payload.prevNodes &&
    Array.from(payload.prevNodes.values()).some((node) => isFinite(node.z));
  view.alpha = payload.structureSame && prevWas3D ? 0 : 1;

  buildNodes(payload);
  buildLines(payload);
  applySelection();

  if (!view.cameraReady) {
    fitCamera(false);
    view.cameraReady = true;
  } else if (!payload.structureSame && !view.userMovedCamera) {
    fitCamera(true);
  }

  startLoop();
  return true;
}

function buildNodes(payload) {
  view.nodeGroup = new THREE.Group();
  view.records = payload.nodes.map((node, index) => {
    const size = typeof node.size === "number" ? node.size : 10;
    const fill =
      node.avg_snr === null || node.avg_snr === undefined
        ? "#9fb3c8"
        : payload.color(node.avg_snr);
    const mat = new THREE.MeshLambertMaterial({
      color: fill,
      transparent: true,
      opacity: 1,
    });
    const mesh = new THREE.Mesh(view.sphereGeo, mat);
    mesh.scale.setScalar(size);
    mesh.userData.nodeIndex = index;
    const rimColor = node.is_gateway ? "#ffd166" : "#ffffff";
    const rimMat = new THREE.MeshLambertMaterial({
      color: rimColor,
      side: THREE.BackSide,
      transparent: true,
      opacity: 1,
    });
    const rim = new THREE.Mesh(view.rimGeo, rimMat);
    rim.scale.setScalar(size + (node.is_gateway ? 3 : 2));
    const group = new THREE.Group();
    group.position.set(node.x || 0, node.y || 0, node.z || 0);
    group.add(mesh, rim);
    view.nodeGroup.add(group);

    const label = document.createElement("div");
    label.className = "label3d" + (node.is_gateway ? " is-gateway" : "");
    label.textContent = node.name;
    label.hidden = true;
    view.labelLayer.appendChild(label);

    return { node, group, mesh, mat, rimMat, size, label };
  });
  view.scene.add(view.nodeGroup);
}

function buildLines(payload) {
  // Direct links: one LineSegments buffer, per-vertex colours (SNR gradient
  // exactly as the 2D stroke).  WebGL keeps line width at 1px, so link
  // strength shows through colour rather than thickness.
  const count = payload.links.length;
  const positions = new Float32Array(count * 6);
  const colors = new Float32Array(count * 6);
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
  geometry.setAttribute("color", new THREE.BufferAttribute(colors, 3));
  const material = new THREE.LineBasicMaterial({
    vertexColors: true,
    transparent: true,
    opacity: 0.8,
  });
  view.lines = new THREE.LineSegments(geometry, material);
  view.lines.frustumCulled = false;

  const color = payload.color;
  view.linkRecords = payload.links.map((link, index) => {
    const snr = link.avg_snr;
    const css =
      snr === null || snr === undefined ? "#8b949e" : color(snr);
    const base = new THREE.Color(css);
    colors[index * 6 + 0] = base.r;
    colors[index * 6 + 1] = base.g;
    colors[index * 6 + 2] = base.b;
    colors[index * 6 + 3] = base.r;
    colors[index * 6 + 4] = base.g;
    colors[index * 6 + 5] = base.b;
    return {
      link,
      key: payload.linkKey(link),
      index,
      from: idOf(link.source),
      to: idOf(link.target),
      base,
    };
  });
  view.lineColors = colors;
  view.scene.add(view.lines);

  // Indirect (multi-hop) links: dashed, dimmer, never part of selection
  // highlighting — same semantics as .link.indirect in the 2D view.
  const indirectCount = payload.indirect.length;
  const iPositions = new Float32Array(indirectCount * 6);
  const iGeometry = new THREE.BufferGeometry();
  iGeometry.setAttribute("position", new THREE.BufferAttribute(iPositions, 3));
  const iMaterial = new THREE.LineDashedMaterial({
    color: "#7d8794",
    transparent: true,
    opacity: 0.45,
    dashSize: 6,
    gapSize: 5,
  });
  view.indirect = new THREE.LineSegments(iGeometry, iMaterial);
  view.indirect.frustumCulled = false;
  view.indirectRecords = payload.indirect.map((link, index) => ({
    link,
    index,
    from: idOf(link.source),
    to: idOf(link.target),
  }));
  view.indirect.computeLineDistances();
  view.scene.add(view.indirect);

  syncPositions();
}

/** Write simulation positions into meshes and line buffers. */
function syncPositions() {
  view.records.forEach((record) => {
    const node = record.node;
    record.group.position.set(node.x, node.y, node.z);
  });
  if (view.lines) {
    const position = view.lines.geometry.getAttribute("position");
    view.linkRecords.forEach((record, index) => {
      const from = nodeById(record.from);
      const to = nodeById(record.to);
      if (!from || !to) return;
      position.setXYZ(index * 2, from.x, from.y, from.z);
      position.setXYZ(index * 2 + 1, to.x, to.y, to.z);
    });
    position.needsUpdate = true;
  }
  if (view.indirect) {
    const position = view.indirect.geometry.getAttribute("position");
    view.indirectRecords.forEach((record, index) => {
      const from = nodeById(record.from);
      const to = nodeById(record.to);
      if (!from || !to) return;
      position.setXYZ(index * 2, from.x, from.y, from.z);
      position.setXYZ(index * 2 + 1, to.x, to.y, to.z);
    });
    position.needsUpdate = true;
    view.indirect.computeLineDistances();
  }
}

function nodeById(id) {
  return view.nodeIndex.get(id) || null;
}

// ---------------------------------------------------------------------------
// Selection & labels
// ---------------------------------------------------------------------------

/**
 * Apply the current selection (set through setSelection): dim everything
 * unrelated, emphasise the selected node — the exact semantics of select()
 * in app.js for the 2D view.
 */
function applySelection() {
  const selectedNode = view.selectedNodeId;
  const selectedKey = view.selectedLinkKey;
  const active = selectedNode !== null || selectedKey !== null;

  let neighbours = null;
  let endpoints = null;
  if (selectedNode !== null) {
    neighbours = new Set([selectedNode]);
    view.links.forEach((link) => {
      const from = idOf(link.source);
      const to = idOf(link.target);
      if (from === selectedNode) neighbours.add(to);
      if (to === selectedNode) neighbours.add(from);
    });
  } else if (selectedKey !== null) {
    const link = view.linkRecords.find((record) => record.key === selectedKey);
    if (link) endpoints = new Set([link.from, link.to]);
  }

  view.records.forEach((record) => {
    let on = true;
    if (neighbours) on = neighbours.has(record.node.id);
    else if (endpoints) on = endpoints.has(record.node.id);
    record.mat.opacity = on ? 1 : DIM_OPACITY;
    record.rimMat.opacity = on ? 1 : DIM_OPACITY;
    record.group.scale.setScalar(record.node.id === selectedNode ? 1.18 : 1);
  });

  if (view.lines) {
    const colors = view.lines.geometry.getAttribute("color");
    view.linkRecords.forEach((record) => {
      let on = true;
      if (neighbours) on = record.from === selectedNode || record.to === selectedNode;
      else if (endpoints) on = record.key === selectedKey;
      const k = on ? 1 : DIM_COLOR;
      colors.setXYZ(
        record.index * 2,
        record.base.r * k,
        record.base.g * k,
        record.base.b * k
      );
      colors.setXYZ(
        record.index * 2 + 1,
        record.base.r * k,
        record.base.g * k,
        record.base.b * k
      );
    });
    colors.needsUpdate = true;
  }
  if (view.indirect) {
    view.indirect.material.opacity = active ? 0.08 : 0.45;
  }
}

function setSelection(nodeId, linkKey) {
  view.selectedNodeId = nodeId ?? null;
  view.selectedLinkKey = linkKey ?? null;
  if (view.records.length) applySelection();
}

/** Project labels to screen; show gateways, selection, hover and near nodes. */
function updateLabels() {
  if (!view.records.length) return;
  const camera = view.camera;
  const halfW = view.width / 2;
  const halfH = view.height / 2;
  const tanHalf = Math.tan((camera.fov * Math.PI) / 360);
  const projected = new THREE.Vector3();
  view.records.forEach((record) => {
    const node = record.node;
    const distance = camera.position.distanceTo(record.group.position);
    const visible =
      node.is_gateway ||
      node.id === view.selectedNodeId ||
      node.id === view.hoveredNodeId ||
      distance < LABEL_NEAR_DIST;
    if (!visible) {
      record.label.hidden = true;
      return;
    }
    projected.copy(record.group.position).project(camera);
    if (projected.z > 1) {
      record.label.hidden = true;
      return;
    }
    // Offset by the sphere's *screen* radius — like dy = size + 14 inside
    // the scaled <g> of the 2D view, so a close zoom never drops the text
    // onto the node itself.
    const screenRadius = (record.size * halfH) / (tanHalf * distance);
    const x = projected.x * halfW + halfW;
    const y = -projected.y * halfH + halfH + screenRadius + 10;
    record.label.hidden = false;
    record.label.style.transform =
      `translate3d(${x.toFixed(1)}px, ${y.toFixed(1)}px, 0)` +
      " translate(-50%, 0)";
  });
}

// ---------------------------------------------------------------------------
// Picking / pointer interaction
// ---------------------------------------------------------------------------

function setPointer(event) {
  const rect = view.renderer.domElement.getBoundingClientRect();
  return new THREE.Vector2(
    ((event.clientX - rect.left) / rect.width) * 2 - 1,
    -((event.clientY - rect.top) / rect.height) * 2 + 1
  );
}

function pickNode(event) {
  view.raycaster.setFromCamera(setPointer(event), view.camera);
  const hits = view.raycaster.intersectObjects(
    view.records.map((record) => record.mesh),
    false
  );
  if (!hits.length) return null;
  return view.records[hits[0].object.userData.nodeIndex] || null;
}

function pickLink(event) {
  if (!view.lines && !view.indirect) return null;
  view.raycaster.setFromCamera(setPointer(event), view.camera);
  const targets = [view.lines, view.indirect].filter(Boolean);
  const hits = view.raycaster.intersectObjects(targets, false);
  if (!hits.length) return null;
  const hit = hits[0];
  if (hit.object === view.lines) {
    const record = view.linkRecords[hit.index];
    return record ? { link: record.link, indirect: false } : null;
  }
  const record = view.indirectRecords[hit.index];
  return record ? { link: record.link, indirect: true } : null;
}

function onPointerUp(event) {
  const down = view.pointerDown;
  view.pointerDown = null;
  if (!down) return;
  const travel = Math.hypot(event.clientX - down.x, event.clientY - down.y);
  if (travel > CLICK_PX) return; // that was a camera drag, not a click
  const nodeHit = pickNode(event);
  if (nodeHit) {
    view.callbacks.onNodeClick(nodeHit.node, event);
    return;
  }
  const link = pickLink(event);
  if (link) {
    if (!link.indirect) view.callbacks.onLinkClick(link.link, event);
    return; // indirect links show a tip in 2D but change no selection
  }
  view.callbacks.onBlankClick();
}

function onPointerMove(event) {
  if (view.pointerDown) {
    const travel = Math.hypot(
      event.clientX - view.pointerDown.x,
      event.clientY - view.pointerDown.y
    );
    if (travel > CLICK_PX) markCameraMoved();
  }
  const now = performance.now();
  if (now - view.lastHoverAt < 50) return;
  view.lastHoverAt = now;
  const nodeHit = pickNode(event);
  if (nodeHit) {
    view.hoveredNodeId = nodeHit.node.id;
    showHover();
    view.renderer.domElement.style.cursor = "pointer";
    view.callbacks.onNodeHover(nodeHit.node, event);
    return;
  }
  const link = pickLink(event);
  if (link) {
    view.hoveredNodeId = null;
    showHover();
    view.renderer.domElement.style.cursor = "pointer";
    if (link.indirect) view.callbacks.onIndirectHover(link.link, event);
    else view.callbacks.onLinkHover(link.link, event);
    return;
  }
  clearHover();
}

function showHover() {
  view.hoverActive = true;
}

function clearHover() {
  if (!view.hoverActive) return;
  view.hoverActive = false;
  view.hoveredNodeId = null;
  if (view.renderer) view.renderer.domElement.style.cursor = "";
  if (view.callbacks.onLeave) view.callbacks.onLeave();
}

// ---------------------------------------------------------------------------
// Camera moves
// ---------------------------------------------------------------------------

function viewDirection() {
  const dir = view.camera.position.clone().sub(view.controls.target);
  if (dir.lengthSq() < 1e-6) dir.set(0.4, 0.45, 0.8);
  return dir.normalize();
}

function tweenCamera(target, position, duration) {
  if (duration <= 0) {
    view.camera.position.copy(position);
    view.controls.target.copy(target);
    view.cameraTween = null;
    return;
  }
  view.cameraTween = {
    fromPos: view.camera.position.clone(),
    fromTarget: view.controls.target.clone(),
    toPos: position.clone(),
    toTarget: target.clone(),
    started: performance.now(),
    duration,
  };
}

function stepTween() {
  const tween = view.cameraTween;
  let k = (performance.now() - tween.started) / tween.duration;
  if (k >= 1) {
    k = 1;
    view.cameraTween = null;
  }
  const eased = k < 0.5 ? 2 * k * k : 1 - ((-2 * k + 2) ** 2) / 2;
  view.camera.position.lerpVectors(tween.fromPos, tween.toPos, eased);
  view.controls.target.lerpVectors(tween.fromTarget, tween.toTarget, eased);
}

/** Frame all nodes (the ⤢ button and the initial view). */
function fitCamera(tween) {
  if (!view.records.length) return;
  const box = new THREE.Box3();
  view.records.forEach((record) => box.expandByObject(record.group));
  const sphere = box.getBoundingSphere(new THREE.Sphere());
  const radius = Math.max(sphere.radius, 60);
  const fov = (view.camera.fov * Math.PI) / 180;
  const hFov = 2 * Math.atan(Math.tan(fov / 2) * view.camera.aspect);
  const angle = Math.min(fov, hFov);
  const distance = (radius / Math.sin(angle / 2)) * 1.15;
  const direction = viewDirection();
  tweenCamera(
    sphere.center,
    sphere.center.clone().add(direction.multiplyScalar(distance)),
    tween ? 350 : 0
  );
}

/** Fly to a node (search results in the sidebar). */
function focusNode(id) {
  const record = view.records.find((item) => item.node.id === id);
  if (!record) return;
  const target = record.group.position.clone();
  // Enough distance to see the node *and* its neighbourhood: flying to
  // size × 14 fills the screen with a single sphere.
  const distance = Math.max(record.size * 20, 300);
  const position = target.clone().add(viewDirection().multiplyScalar(distance));
  tweenCamera(target, position, 450);
}

/** Dolly along the view direction; factor > 1 moves closer. */
function zoom(factor) {
  const offset = view.camera.position.clone().sub(view.controls.target);
  const length = offset.length();
  const next = Math.min(
    view.controls.maxDistance,
    Math.max(view.controls.minDistance, length / factor)
  );
  offset.setLength(next);
  view.camera.position.copy(view.controls.target).add(offset);
}

// ---------------------------------------------------------------------------
// Маршруты пакетов: светящиеся шарики вдоль рёбер (кадры app.js)
// ---------------------------------------------------------------------------

const FLOW_DOT_RADIUS = 5; // world units — половина узла по умолчанию
const FLOW_GLOW_SCALE = 34; // спрайт-ореол вокруг шарика

/** Мягкий радиальный градиент для спрайтов: белый в центре, прозрачный по краю. */
function flowGlowTexture() {
  if (view.flowGlowTex) return view.flowGlowTex;
  const canvas = document.createElement("canvas");
  canvas.width = 64;
  canvas.height = 64;
  const ctx = canvas.getContext("2d");
  const gradient = ctx.createRadialGradient(32, 32, 0, 32, 32, 32);
  gradient.addColorStop(0, "rgba(255,255,255,1)");
  gradient.addColorStop(0.35, "rgba(255,255,255,0.55)");
  gradient.addColorStop(1, "rgba(255,255,255,0)");
  ctx.fillStyle = gradient;
  ctx.fillRect(0, 0, 64, 64);
  view.flowGlowTex = new THREE.CanvasTexture(canvas);
  return view.flowGlowTex;
}

/** Акцентный цвет темы (--accent): шарикам и рёбрам нужен контраст с графом. */
function flowAccentColor() {
  const theme = document.documentElement.getAttribute("data-theme") || "dark";
  if (!view.flowAccent || view.flowTheme !== theme) {
    view.flowTheme = theme;
    view.flowAccent =
      getComputedStyle(document.documentElement)
        .getPropertyValue("--accent")
        .trim() || "#4c8dff";
  }
  return view.flowAccent;
}

/**
 * Перерисовать пул анимации по свежему кадру packetflow.js (координаты в
 * кадре уже разрешены движком).  Рёбра — тонкие линии с плавающей
 * прозрачностью; шарики — ядро плюс спрайт-ореол, оба в акцентном цвете.
 * Ореол аддитивен в тёмной теме и обычный в светлой: на белом фоне
 * аддитивный свет выжигает в ноль.  Смешивание пересчитывается каждый кадр —
 * тема может смениться посреди воспроизведения.
 */
function renderFlow(frame) {
  if (!view.ready || !view.scene) return;
  const hasFrame = frame.legs.length || frame.dots.length;
  if (!hasFrame && !view.flowGroup) return;
  if (!view.flowGroup) {
    view.flowGroup = new THREE.Group();
    view.scene.add(view.flowGroup);
  }
  const accent = flowAccentColor();
  const blending =
    document.documentElement.getAttribute("data-theme") === "light"
      ? THREE.NormalBlending
      : THREE.AdditiveBlending;

  const wantedLegs = new Map(frame.legs.map((leg) => [leg.key, leg]));
  for (const [key, rec] of view.flowLegs) {
    if (wantedLegs.has(key)) continue;
    view.flowGroup.remove(rec.line);
    rec.line.geometry.dispose();
    rec.line.material.dispose();
    view.flowLegs.delete(key);
  }
  wantedLegs.forEach((leg, key) => {
    let rec = view.flowLegs.get(key);
    if (!rec) {
      const geometry = new THREE.BufferGeometry();
      geometry.setAttribute(
        "position",
        new THREE.BufferAttribute(new Float32Array(6), 3)
      );
      const material = new THREE.LineBasicMaterial({
        transparent: true,
        depthWrite: false,
      });
      const line = new THREE.Line(geometry, material);
      line.frustumCulled = false;
      line.renderOrder = 9;
      view.flowGroup.add(line);
      rec = { line };
      view.flowLegs.set(key, rec);
    }
    const attr = rec.line.geometry.getAttribute("position");
    attr.setXYZ(0, leg.ax, leg.ay, leg.az);
    attr.setXYZ(1, leg.bx, leg.by, leg.bz);
    attr.needsUpdate = true;
    rec.line.material.color.set(accent);
    rec.line.material.opacity = 0.95 * leg.phase;
  });

  const wantedDots = new Map(frame.dots.map((dot) => [dot.key, dot]));
  for (const [key, rec] of view.flowDots) {
    if (wantedDots.has(key)) continue;
    view.flowGroup.remove(rec.group);
    rec.core.material.dispose();
    rec.glow.material.dispose();
    view.flowDots.delete(key);
  }
  wantedDots.forEach((dot, key) => {
    let rec = view.flowDots.get(key);
    if (!rec) {
      const core = new THREE.Mesh(
        view.sphereGeo,
        new THREE.MeshBasicMaterial({ transparent: true })
      );
      core.scale.setScalar(FLOW_DOT_RADIUS);
      const glow = new THREE.Sprite(
        new THREE.SpriteMaterial({
          map: flowGlowTexture(),
          transparent: true,
          depthWrite: false,
        })
      );
      glow.scale.setScalar(FLOW_GLOW_SCALE);
      const group = new THREE.Group();
      group.add(core, glow);
      view.flowGroup.add(group);
      rec = { group, core, glow };
      view.flowDots.set(key, rec);
    }
    rec.group.position.set(dot.x, dot.y, dot.z);
    // Цвет шарика — по id пакета (как в 2D); ноги остаются акцентными.
    const color = dot.color || accent;
    rec.core.material.color.set(color);
    rec.glow.material.color.set(color);
    rec.glow.material.blending = blending;
    rec.core.material.blending = blending;
  });
}

// ---------------------------------------------------------------------------
// Animation loop
// ---------------------------------------------------------------------------

function startLoop() {
  if (view.running) return;
  view.running = true;
  view.raf = requestAnimationFrame(loop);
}

function loop() {
  if (!view.running) return;
  view.raf = requestAnimationFrame(loop);
  if (view.alpha > 0) {
    for (let step = 0; step < SIM_STEPS_PER_FRAME && view.alpha > 0; step += 1) {
      globalThis.meshgraphForce3DStep(
        view.nodes,
        view.pairs,
        view.anchors,
        view.alpha,
        globalThis.MESHGRAPH_FORCE3D_PARAMS
      );
      view.alpha = globalThis.meshgraphDecayAlpha3D(
        view.alpha,
        globalThis.MESHGRAPH_FORCE3D_PARAMS
      );
    }
    syncPositions();
  }
  if (view.cameraTween) stepTween();
  view.controls.update();
  updateLabels();
  view.renderer.render(view.scene, view.camera);
}

window.meshgraph3D = {
  render,
  deactivate,
  setSelection,
  focusNode,
  fit: () => fitCamera(true),
  zoom,
  renderFlow,
  /**
   * Пул анимации маршрутов + экранные координаты первого шарика
   * (e2e-проверки: кадр дорисован и попадает в видимую область?).
   */
  flowStats: () => {
    const first = view.flowDots.values().next().value;
    let sample = null;
    if (first) {
      const p = first.group.position.clone().project(view.camera);
      sample = {
        x: Math.round((p.x * 0.5 + 0.5) * view.width),
        y: Math.round((-p.y * 0.5 + 0.5) * view.height),
        z: Math.round(p.z * 1000) / 1000,
      };
    }
    return { dots: view.flowDots.size, legs: view.flowLegs.size, sample };
  },
  isRunning: () => view.running,
  /** Screen position of a node (canvas-relative px) — e2e tests and hooks. */
  projectNode(id) {
    const record = view.records.find((item) => item.node.id === id);
    if (!record) return null;
    const p = record.group.position.clone().project(view.camera);
    return {
      x: (p.x * 0.5 + 0.5) * view.width,
      y: (-p.y * 0.5 + 0.5) * view.height,
      z: p.z,
    };
  },
};
