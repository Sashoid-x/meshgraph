/* 3D force relaxation for the meshgraph three.js view.
 *
 * The same idea as the d3 simulation in app.js, but in three dimensions: link
 * springs, pairwise repulsion, a gentle pull toward each island's anchor and a
 * position-level collision pass.  A third coordinate only develops from a
 * volumetric seed — with every node flat on z = 0 the forces stay perfectly
 * symmetric and the graph never leaves the plane.
 *
 * Pure functions over plain arrays: no DOM, no d3, no three.js — executed
 * as-is by the QuickJS tests in tests/test_layout_js.py.
 */

"use strict";

/**
 * Single source of truth for the 3D render numbers, mirroring
 * MESHGRAPH_FORCE_PARAMS in layout.js: app.js (via graph3d.js) renders with
 * these, the tests drive the very same constants.
 */
globalThis.MESHGRAPH_FORCE3D_PARAMS = {
  // forces
  linkDistance: 150, // default rest length when a pair carries no own value
  linkStrength: 0.55,
  chargeStrength: -220, // pairwise repulsion; negative = push apart
  anchorStrength: 0.035, // pull toward the island anchor (center when single)
  collisionPad: 34,
  drag: 0.35, // fraction of velocity removed each tick
  maxSpeed: 60, // safety clamp: the O(n^2) charge must not explode
  alphaDecay: 0.02,
  alphaMin: 0.001,
  // seeding
  seedRadius: 170,
  islandSpacing: 340,
};

/**
 * Anchor point per island: a single island sits at the origin (that is the
 * "center" force of the 2D view), several islands spread over a ring in the
 * horizontal xz-plane — each keeps its own patch of space, exactly what the
 * packed cells of the 2D layout do with the viewport.
 *
 * The ring radius is chosen so adjacent anchors are `spacing` apart.
 */
function meshgraphAnchorPoints3D(count, spacing) {
  if (!(count > 1)) return [{ x: 0, y: 0, z: 0 }];
  const points = [];
  const radius = spacing / (2 * Math.sin(Math.PI / count));
  for (let i = 0; i < count; i += 1) {
    const angle = (2 * Math.PI * i) / count;
    points.push({
      x: radius * Math.cos(angle),
      y: 0,
      z: radius * Math.sin(angle),
    });
  }
  return points;
}

/**
 * Starting coordinates for every node.  Previous coordinates win (that is how
 * the view survives re-rendering and how a switch from 2D keeps the layout),
 * but a missing z — 2D nodes never had one — is filled from a golden-angle
 * spiral around the island anchor, so a flat graph gains real depth instead of
 * staying a pancake.  Velocities always start at zero: stale speeds from the
 * last render would kick the scene off-screen.
 *
 * `anchors` is an array parallel to `nodes`; `prevNodes` is a Map(id -> node).
 */
function meshgraphSeedPositions3D(nodes, prevNodes, anchors, params) {
  const golden = Math.PI * (3 - Math.sqrt(5));
  const total = Math.max(nodes.length, 1);
  nodes.forEach((node, i) => {
    const prev = prevNodes ? prevNodes.get(node.id) : null;
    const anchor = (anchors && anchors[i]) || { x: 0, y: 0, z: 0 };
    const radius =
      params.seedRadius * Math.sqrt((i + 0.5) / total);
    const angle = golden * (i + 1);
    const hasPrevXY = prev && isFinite(prev.x) && isFinite(prev.y);
    node.x = hasPrevXY ? prev.x : anchor.x + radius * Math.cos(angle);
    node.y = hasPrevXY ? prev.y : anchor.y + radius * 0.4 * Math.sin(angle * 0.7);
    node.z = prev && isFinite(prev.z) ? prev.z : anchor.z + radius * Math.sin(angle);
    node.vx = 0;
    node.vy = 0;
    node.vz = 0;
  });
}

/**
 * One physics step.  Mutates node positions in place:
 *
 *   1. link springs toward each pair's rest distance;
 *   2. pairwise repulsion (O(n^2) — fine for mesh-sized graphs);
 *   3. weak pull toward the node's island anchor;
 *   4. velocity integration (drag + speed clamp);
 *   5. position-level collision pass so nodes never overlap their radius.
 *
 * `pairs` are `[i, j, restDistance?]` index triples; `anchors` is an array
 * parallel to `nodes`.  `alpha` scales the forces so a fresh scene starts hot
 * and an inherited one can be left cold (alpha = 0 does nothing).
 */
function meshgraphForce3DStep(nodes, pairs, anchors, alpha, params) {
  const count = nodes.length;
  if (!count || !(alpha > 0)) return;
  let i;
  let j;
  let dx;
  let dy;
  let dz;
  let dist;
  let dist2;
  let force;

  // Hand-built nodes (tests, external callers) may carry no velocities yet.
  for (i = 0; i < count; i += 1) {
    const node = nodes[i];
    if (!isFinite(node.vx)) node.vx = 0;
    if (!isFinite(node.vy)) node.vy = 0;
    if (!isFinite(node.vz)) node.vz = 0;
  }

  // 1) link springs
  for (let p = 0; p < pairs.length; p += 1) {
    const pair = pairs[p];
    i = pair[0];
    j = pair[1];
    const a = nodes[i];
    const b = nodes[j];
    if (!a || !b) continue;
    dx = b.x - a.x;
    dy = b.y - a.y;
    dz = b.z - a.z;
    dist = Math.sqrt(dx * dx + dy * dy + dz * dz) || 1e-6;
    const rest = typeof pair[2] === "number" ? pair[2] : params.linkDistance;
    force = ((dist - rest) * params.linkStrength * alpha) / dist;
    a.vx += dx * force;
    a.vy += dy * force;
    a.vz += dz * force;
    b.vx -= dx * force;
    b.vy -= dy * force;
    b.vz -= dz * force;
  }

  // 2) pairwise repulsion; distance² clamped so coincident nodes stay finite
  for (i = 0; i < count; i += 1) {
    const a = nodes[i];
    for (j = i + 1; j < count; j += 1) {
      const b = nodes[j];
      dx = a.x - b.x;
      dy = a.y - b.y;
      dz = a.z - b.z;
      dist2 = dx * dx + dy * dy + dz * dz;
      if (dist2 < 1) dist2 = 1;
      dist = Math.sqrt(dist2);
      force = -params.chargeStrength * alpha / dist2; // > 0: away from each other
      const ux = dx / dist;
      const uy = dy / dist;
      const uz = dz / dist;
      a.vx += ux * force;
      a.vy += uy * force;
      a.vz += uz * force;
      b.vx -= ux * force;
      b.vy -= uy * force;
      b.vz -= uz * force;
    }
  }

  // 3) pull toward the island anchor (origin for a single island)
  if (anchors) {
    for (i = 0; i < count; i += 1) {
      const a = nodes[i];
      const anchor = anchors[i];
      if (!anchor) continue;
      a.vx += (anchor.x - a.x) * params.anchorStrength * alpha;
      a.vy += (anchor.y - a.y) * params.anchorStrength * alpha;
      a.vz += (anchor.z - a.z) * params.anchorStrength * alpha;
    }
  }

  // 4) integrate: drag, speed clamp, position update
  const keep = 1 - params.drag;
  for (i = 0; i < count; i += 1) {
    const a = nodes[i];
    a.vx *= keep;
    a.vy *= keep;
    a.vz *= keep;
    const speed = Math.sqrt(a.vx * a.vx + a.vy * a.vy + a.vz * a.vz);
    if (speed > params.maxSpeed) {
      const scale = params.maxSpeed / speed;
      a.vx *= scale;
      a.vy *= scale;
      a.vz *= scale;
    }
    a.x += a.vx;
    a.y += a.vy;
    a.z += a.vz;
  }

  // 5) collisions: half the overlap corrected per node (pairwise again)
  const pad = params.collisionPad;
  if (pad > 0) {
    for (i = 0; i < count; i += 1) {
      const a = nodes[i];
      const ra = (typeof a.size === "number" ? a.size : 10) + pad;
      for (j = i + 1; j < count; j += 1) {
        const b = nodes[j];
        const rb = (typeof b.size === "number" ? b.size : 10) + pad;
        dx = b.x - a.x;
        dy = b.y - a.y;
        dz = b.z - a.z;
        const sum = ra + rb;
        dist2 = dx * dx + dy * dy + dz * dz;
        if (dist2 >= sum * sum) continue;
        dist = Math.sqrt(dist2);
        if (dist < 1e-6) {
          // Exactly coincident: a deterministic axis, no randomness in tests.
          dx = ((i + j) % 3) - 1;
          dy = ((i + j + 1) % 3) - 1;
          dz = ((i + j + 2) % 3) - 1;
          dist = Math.sqrt(dx * dx + dy * dy + dz * dz) || 1;
        }
        const push = (sum - dist) / (2 * dist);
        a.x -= dx * push;
        a.y -= dy * push;
        a.z -= dz * push;
        b.x += dx * push;
        b.y += dy * push;
        b.z += dz * push;
      }
    }
  }
}

/** Alpha after one tick; returns 0 once the scene counts as settled. */
function meshgraphDecayAlpha3D(alpha, params) {
  const next = alpha * (1 - params.alphaDecay);
  return next < params.alphaMin ? 0 : next;
}

/**
 * Relax the scene for at most `ticks` steps starting at alpha = 1 (the
 * settling loop used by the tests and by graph3d.js warm-up runs).  Returns
 * the remaining alpha: 0 means the layout is stable.
 */
function meshgraphRun3D(nodes, pairs, anchors, ticks, params) {
  let alpha = 1;
  for (let t = 0; t < ticks && alpha > 0; t += 1) {
    meshgraphForce3DStep(nodes, pairs, anchors, alpha, params);
    alpha = meshgraphDecayAlpha3D(alpha, params);
  }
  return alpha;
}
