/* Packet flow engine (the glowing dots over the graph).
 *
 * Data comes from /api/packet_routes (replay ▶) or /api/packet_flow (live ✦):
 * every reception is a movement — a direct packet flying sender → gateway,
 * or a traceroute walk node by node.  The same packet id keeps one colour
 * wherever it flies, so duplicate receptions read as a single traveller.
 * This file is pure logic — no DOM, no three.js.  app.js asks for one frame
 * per animation tick and paints it twice: as an SVG layer in 2D and through
 * window.meshgraph3D.renderFlow in 3D.  A frame is a pure function of
 * (plan, elapsed, node coordinates), which is what lets QuickJS test it.
 * Live mode runs several plans at once; batchId keeps their pools apart.
 */

"use strict";

globalThis.MESHGRAPH_FLOW_PARAMS = {
  hopMs: 520, // dot travel along a single leg
  routeStaggerMs: 180, // gap between consecutive routes of a batch
  legFadeMs: 1100, // leg highlight decay after the dot has passed
  maxRoutes: 40, // routes per plan batch
  maxActivePlans: 6, // plans playing at once
  maxInflight: 120, // active + queued cap; live arrivals drop beyond it
  replayMinutes: 30, // window of the replay button
  replayLimit: 200, // server-side cap for the replay window
  livePollMs: 2000, // live feed poll interval
  liveLimit: 60, // server-side cap per live poll
  dotRadius: 4.5,
  haloRadius: 12,
  legWidth: 2.5,
  legHaloWidth: 6,
};

/**
 * Stable hue for a packet id (FNV-1a).
 *
 * The same packet keeps its colour in every part of the graph — that is
 * what ties the duplicate receptions of one packet together — while
 * different packets land on different hues.  Id-less rows fall back to a
 * caller-built "sender:ts" string, so they differ too.
 *
 * @param {number|string} packetId
 * @returns {number} hue in [0, 360)
 */
function meshgraphFlowHue(packetId) {
  const text =
    packetId === undefined || packetId === null ? "" : String(packetId);
  let hash = 2166136261;
  for (let i = 0; i < text.length; i += 1) {
    hash ^= text.charCodeAt(i);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0) % 360;
}

/**
 * Filter the routes down to what is actually on screen and lay them out in
 * time.
 *
 * A movement rides straight between its endpoints: the chat graph may hide
 * the link behind filters, but the packet still moved — only a *missing
 * node* drops a leg (nothing to anchor it to).  A traceroute chain breaks
 * whole when one hop's node is gone.
 *
 * @param {Array} routes /api/packet_routes or /api/packet_flow movements
 * @param {Array} nodeIds ids of the nodes currently in the graph
 * @param {object} params MESHGRAPH_FLOW_PARAMS
 * @param {number} [batchId] namespaces the pool keys — overlapping live
 *   batches keep their own elements instead of fighting for one
 * @returns {{routes: Array, totalMs: number, params: object}}
 */
function meshgraphFlowPlan(routes, nodeIds, params, batchId = 0) {
  const have = new Set(nodeIds);
  const tag = `b${batchId}:`;

  const planned = [];
  routes.forEach((route, index) => {
    if (!have.has(route.sender)) return;
    let legs = [];
    if (route.kind === "direct") {
      legs = (route.legs || [])
        .filter((leg) => have.has(leg.to))
        .map((leg, i) => ({
          key: `${tag}direct-${index}:${i}`,
          from: route.sender,
          to: leg.to,
          snr: leg.snr === undefined ? null : leg.snr,
          at: 0,
        }));
    } else if (route.kind === "traceroute") {
      // Consecutive legs chain: each starts where the previous one ended.
      let cursor = route.sender;
      const chain = [];
      let intact = true;
      for (const leg of route.legs || []) {
        if (!have.has(leg.to)) {
          intact = false;
          break;
        }
        chain.push({ from: cursor, to: leg.to, snr: leg.snr === undefined ? null : leg.snr });
        cursor = leg.to;
      }
      if (intact) {
        legs = chain.map((leg, i) => ({
          ...leg,
          key: `${tag}traceroute-${index}:${i}`,
          at: i * params.hopMs,
        }));
      }
    }
    if (!legs.length) return;
    const durationMs = legs.reduce(
      (max, leg) => Math.max(max, leg.at + params.hopMs),
      0
    );
    planned.push({
      key: `${tag}${route.kind}-${index}`,
      kind: route.kind,
      // Colour identity: same packet id → same hue wherever it flies.
      pid:
        route.packet_id === undefined || route.packet_id === null
          ? `${route.sender}:${route.ts}`
          : route.packet_id,
      ts: route.ts,
      legs,
      startMs: index * params.routeStaggerMs,
      durationMs,
    });
  });

  const lastEnd = planned.reduce(
    (max, route) => Math.max(max, route.startMs + route.durationMs),
    0
  );
  return { routes: planned, totalMs: lastEnd + params.legFadeMs, params };
}

/**
 * Everything visible at `elapsed` ms into a plan.
 *
 * Coordinates are resolved from `nodesById` at call time — the map is
 * re-bound after a re-render, and a node that vanished simply drops its
 * entries instead of drawing through a hole.  Dots ride the leg with a
 * smoothstep easing; finished legs linger as a fading highlight.
 *
 * @returns {{dots: Array, legs: Array, done: boolean}}
 *   dots: {key, x, y, z, from, to, snr, pid}
 *   legs: {key, from, to, snr, pid, ax..bz, phase}  phase 1 = lit, decaying to 0
 */
function meshgraphFlowAt(plan, elapsed, nodesById) {
  const dots = [];
  const legs = [];
  if (!plan) return { dots, legs, done: true };
  const params = plan.params;

  const point = (id) => {
    const node = nodesById.get(id);
    if (!node || !isFinite(node.x) || !isFinite(node.y)) return null;
    return { x: node.x, y: node.y, z: isFinite(node.z) ? node.z : 0 };
  };
  const smooth = (t) => t * t * (3 - 2 * t);

  for (const route of plan.routes) {
    const e = elapsed - route.startMs;
    if (e < 0 || e > route.durationMs + params.legFadeMs) continue;
    for (const leg of route.legs) {
      const le = e - leg.at;
      if (le < 0) continue;
      const a = point(leg.from);
      const b = point(leg.to);
      if (!a || !b) continue;
      const base = {
        key: leg.key,
        from: leg.from,
        to: leg.to,
        snr: leg.snr,
        pid: route.pid,
      };
      if (le <= params.hopMs) {
        const t = smooth(le / params.hopMs);
        legs.push({
          ...base,
          ax: a.x, ay: a.y, az: a.z,
          bx: b.x, by: b.y, bz: b.z,
          phase: 1,
        });
        dots.push({
          ...base,
          x: a.x + (b.x - a.x) * t,
          y: a.y + (b.y - a.y) * t,
          z: a.z + (b.z - a.z) * t,
        });
      } else {
        const phase = 1 - (le - params.hopMs) / params.legFadeMs;
        if (phase > 0) {
          legs.push({
            ...base,
            ax: a.x, ay: a.y, az: a.z,
            bx: b.x, by: b.y, bz: b.z,
            phase,
          });
        }
      }
    }
  }
  return { dots, legs, done: elapsed >= plan.totalMs };
}
