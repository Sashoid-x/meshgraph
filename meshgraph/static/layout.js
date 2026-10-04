/* Island-aware layout helpers for the meshgraph front end.
 *
 * "Islands" are the connected parts of the graph: without them, disconnected
 * fragments pile up in the middle of the canvas and the network looks like one
 * blob.  The helpers below find the islands, measure them and pack them into
 * non-overlapping cells of the viewport.
 *
 * Pure functions: no DOM, no d3 — executed as-is by the QuickJS unit tests in
 * tests/test_layout_js.py.
 */

"use strict";

/**
 * Connected components of the drawn graph (union-find over node ids).
 * Follows direct links and multi-hop "indirect" connections alike, so an
 * island is genuinely a part with no path to the others.  Link endpoints may
 * be plain ids or already-resolved {id} objects.
 *
 * Returns an array of id arrays; every node belongs to exactly one island,
 * isolated nodes come back as islands of one.
 */
function meshgraphFindComponents(nodes, links) {
  const parent = new Map();

  const find = (id) => {
    let root = id;
    while (parent.get(root) !== root) root = parent.get(root);
    let cur = id;
    while (cur !== root) {
      const next = parent.get(cur);
      parent.set(cur, root);
      cur = next;
    }
    return root;
  };
  const add = (id) => {
    if (id === null || id === undefined) return;
    if (!parent.has(id)) parent.set(id, id);
  };
  const union = (a, b) => {
    const ra = find(a);
    const rb = find(b);
    if (ra !== rb) parent.set(rb, ra);
  };
  const idOf = (v) => (v !== null && typeof v === "object" ? v.id : v);

  for (const node of nodes || []) add(idOf(node));
  for (const link of links || []) {
    const a = idOf(link.source);
    const b = idOf(link.target);
    add(a);
    add(b);
    if (parent.has(a) && parent.has(b)) union(a, b);
  }

  const groups = new Map();
  for (const id of parent.keys()) {
    const root = find(id);
    if (!groups.has(root)) groups.set(root, []);
    groups.get(root).push(id);
  }
  return Array.from(groups.values());
}

/**
 * Radius every island needs so that it fits inside its own cell: the distance
 * of the farthest node from the island centroid plus that node's visual size
 * and the collision padding used by the simulation.
 *
 * `nodeById` is a Map (or a plain object) of node records with x/y positions.
 */
function meshgraphMeasureIslands(components, nodeById, collisionPad) {
  const pad = collisionPad === undefined ? 50 : collisionPad;
  const lookup =
    nodeById instanceof Map
      ? (id) => nodeById.get(id)
      : (id) => nodeById[id];

  return components.map((ids) => {
    const nodes = [];
    for (const id of ids) {
      const n = lookup(id);
      if (n && typeof n.x === "number" && typeof n.y === "number") {
        nodes.push(n);
      }
    }
    if (nodes.length === 0) return pad + 30;

    let cx = 0;
    let cy = 0;
    for (const n of nodes) {
      cx += n.x;
      cy += n.y;
    }
    cx /= nodes.length;
    cy /= nodes.length;

    let maxDist = 0;
    let extent = 0;
    for (const n of nodes) {
      const d = Math.hypot(n.x - cx, n.y - cy);
      if (d > maxDist) maxDist = d;
      const size = typeof n.size === "number" ? n.size : 10;
      if (size + pad > extent) extent = size + pad;
    }
    return maxDist + extent;
  });
}

/**
 * Shelf-pack islands into rows: widest rows first, biggest island at the head
 * of each row.  Pure packing — returns the rows plus the size of the block
 * they form (no viewport knowledge yet).
 */
function meshgraphPackRows(radii, width, gap) {
  const items = radii
    .map((r, index) => ({ index, r: Math.max(r, 30) }))
    .sort((a, b) => b.r - a.r || a.index - b.index);

  const rows = [];
  let row = [];
  let rowWidth = 0;
  for (const item of items) {
    const w = 2 * item.r;
    if (row.length > 0 && rowWidth + gap + w > width) {
      rows.push({ items: row, width: rowWidth });
      row = [];
      rowWidth = 0;
    }
    rowWidth += (row.length > 0 ? gap : 0) + w;
    row.push(item);
  }
  if (row.length > 0) rows.push({ items: row, width: rowWidth });

  const heights = rows.map(
    (r) => 2 * Math.max.apply(null, r.items.map((i) => i.r)) + gap
  );
  return {
    rows,
    heights,
    gap,
    blockWidth: rows.reduce((m, r) => Math.max(m, r.width), 0),
    blockHeight: heights.reduce((a, b) => a + b, 0),
  };
}

/**
 * Lay a packing out as cells `[{index, tx, ty, r}]` centred in the viewport:
 * the target centre and radius of every island.
 *
 * Invariant (unit-tested): for any two cells the distance between centres is
 * at least `ri + rj + gap`, so the islands can never overlap.
 */
function meshgraphPlaceRows(pack, radii, viewWidth, viewHeight) {
  const centerX = Math.max(viewWidth || 0, 1) / 2;
  const centerY = (viewHeight || 0) / 2;
  const cells = radii.map((r, index) => ({
    index,
    tx: centerX,
    ty: centerY,
    r: Math.max(r, 30),
  }));
  let y = centerY - pack.blockHeight / 2;
  pack.rows.forEach((row, ri) => {
    const rowCenterY = y + pack.heights[ri] / 2;
    let x = centerX - row.width / 2;
    for (const item of row.items) {
      cells[item.index] = {
        index: item.index,
        tx: x + item.r,
        ty: rowCenterY,
        r: item.r,
      };
      x += 2 * item.r + pack.gap;
    }
    y += pack.heights[ri];
  });
  return cells;
}

/** Pack islands into the viewport: rows limited by its width. */
function meshgraphPlanIslands(radii, viewWidth, viewHeight, gap) {
  const g = gap === undefined ? 20 : gap;
  const pack = meshgraphPackRows(radii, Math.max(viewWidth || 0, 1), g);
  return meshgraphPlaceRows(pack, radii, viewWidth, viewHeight);
}

/**
 * Same, but tries several row widths and keeps the arrangement that keeps the
 * block closest to the viewport shape — i.e. the one that needs the least
 * zoom-out to show everything.  Islands separate, but not farther than needed.
 */
function meshgraphPlanIslandsFitted(radii, viewWidth, viewHeight, gap) {
  const g = gap === undefined ? 20 : gap;
  const base = Math.max(viewWidth || 0, 1);
  const height = Math.max(viewHeight || 0, 1);

  let best = null;
  let bestMetric = Infinity;
  for (const factor of [1, 1.5, 2, 3, 0.75, 0.5, 4]) {
    const pack = meshgraphPackRows(radii, base * factor, g);
    const metric = Math.max(pack.blockWidth / base, pack.blockHeight / height);
    if (metric < bestMetric - 1e-9) {
      best = pack;
      bestMetric = metric;
    }
  }
  return meshgraphPlaceRows(best, radii, viewWidth, viewHeight);
}

/**
 * d3-style force that glides every island towards its planned cell.  Each
 * island moves as a unit — the usual link/charge forces keep its internal
 * shape, this one only translates the centroid (12% of the gap per tick, so
 * the parts slide apart visibly instead of teleporting).
 *
 * `targetsById` maps node id → cell from meshgraphPlanIslands.
 */
function meshgraphIslandForce(targetsById) {
  let groups = [];

  function force() {
    for (const group of groups) {
      const nodes = group.nodes;
      if (nodes.length === 0) continue;
      let cx = 0;
      let cy = 0;
      for (const n of nodes) {
        cx += n.x;
        cy += n.y;
      }
      cx /= nodes.length;
      cy /= nodes.length;
      const dx = group.tx - cx;
      const dy = group.ty - cy;
      const k = 0.12;
      for (const n of nodes) {
        n.x += dx * k;
        n.y += dy * k;
      }
    }
  }

  force.initialize = (nodes) => {
    const buckets = new Map();
    for (const n of nodes) {
      const cell = targetsById.get(n.id);
      if (!cell) continue;
      if (!buckets.has(cell)) {
        buckets.set(cell, { tx: cell.tx, ty: cell.ty, nodes: [] });
      }
      buckets.get(cell).nodes.push(n);
    }
    groups = Array.from(buckets.values());
  };

  return force;
}

/**
 * Membership key of the given components: for every node — the smallest id of
 * its island.  Two graphs have the same island structure exactly when their
 * keys agree, and then the previous packing plan is still valid: the parts do
 * not need to move on re-render (refresh, filters, resize).
 */
function meshgraphIslandKey(components) {
  const key = new Map();
  components.forEach((ids) => {
    const smallest = ids.reduce((m, id) => (id < m ? id : m), ids[0]);
    ids.forEach((id) => key.set(id, smallest));
  });
  return key;
}

/**
 * True when two keys describe the same islands — same nodes in the same
 * parts — so the plan saved in `prev` may be reused as-is.  `null` (no
 * previous render) is never the same structure.
 */
function meshgraphSameStructure(prev, next) {
  if (!prev || !next || prev.size !== next.size) return false;
  for (const [id, smallest] of next) {
    if (prev.get(id) !== smallest) return false;
  }
  return true;
}

/**
 * d3-style force: nudges nodes off link lines they are NOT part of, so a node
 * never appears to sit on someone else's connection.  For every node the
 * perpendicular distance to every foreign segment is computed.  Inside the
 * zone the push runs at full strength while the line still touches the node's
 * circle (plus `edge` px of white), then falls off linearly to `clearance`;
 * this is what guarantees a visible gap even for nodes anchored stiffly by
 * several links.  The segment's own endpoints are exempt — they are supposed
 * to touch their line.
 *
 * `links` may hold resolved node objects or plain ids (resolved through the
 * node list); `strength` is the full nudge per offending line per tick,
 * applied directly to positions like meshgraphIslandForce, so the gap holds
 * even when the simulation has cooled down.
 */
function meshgraphAvoidLinks(links, clearance, strength) {
  const clear = clearance === undefined ? 55 : clearance;
  const k = strength === undefined ? 3 : strength;
  const edge = 6;
  let nodes = [];
  let byId = null;

  function resolve(v) {
    if (v && typeof v === "object") return v;
    return (byId && byId.get(v)) || null;
  }

  function force() {
    for (const n of nodes) {
      if (typeof n.x !== "number" || typeof n.y !== "number") continue;
      let fx = 0;
      let fy = 0;
      for (const link of links) {
        const s = resolve(link.source);
        const t = resolve(link.target);
        if (!s || !t || s === n || t === n) continue;
        const vx = t.x - s.x;
        const vy = t.y - s.y;
        const len2 = vx * vx + vy * vy;
        if (!(len2 > 1e-9)) continue;
        const raw = ((n.x - s.x) * vx + (n.y - s.y) * vy) / len2;
        const u = raw < 0 ? 0 : raw > 1 ? 1 : raw;
        const dx = n.x - (s.x + u * vx);
        const dy = n.y - (s.y + u * vy);
        const d = Math.sqrt(dx * dx + dy * dy);
        if (d >= clear) continue;
        const near = Math.min(clear, (typeof n.size === "number" ? n.size : 10) + edge);
        const push =
          d < near ? 1 : (clear - d) / Math.max(1e-3, clear - near);
        if (d > 1e-3) {
          fx += (dx / d) * push;
          fy += (dy / d) * push;
        } else {
          // Точно на линии: направление не определено — толкаем
          // перпендикулярно самой линии.
          const inv = 1 / Math.sqrt(len2);
          fx += vy * inv * push;
          fy += -vx * inv * push;
        }
      }
      n.x += fx * k;
      n.y += fy * k;
    }
  }

  force.initialize = (ns) => {
    nodes = ns;
    byId = new Map(ns.map((n) => [n.id, n]));
  };

  return force;
}
