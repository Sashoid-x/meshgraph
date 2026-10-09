/* Packet route replay engine (the glowing dots over the graph).
 *
 * Data comes from /api/packet_routes: either a fan (one mesh packet heard by
 * several gateways) or a traceroute walk (the packet crawling node by node).
 * This file is pure logic — no DOM, no three.js.  app.js asks for one frame
 * per animation tick and paints it twice: as an SVG layer in 2D and through
 * window.meshgraph3D.renderFlow in 3D.  A frame is a pure function of
 * (plan, elapsed, node coordinates), which is what lets QuickJS test it.
 */

"use strict";

globalThis.MESHGRAPH_FLOW_PARAMS = {
  hopMs: 520, // dot travel along a single leg
  fanSpreadMs: 110, // gap between the legs of one fanned packet
  routeStaggerMs: 180, // gap between consecutive routes of a playback
  legFadeMs: 1100, // leg highlight decay after the dot has passed
  maxRoutes: 40, // cap per playback batch
  replayMinutes: 30, // window of the replay button
  pulseMinutes: 10, // window of the background pulse
  pulsePollMs: 20000,
  dotRadius: 4.5,
  haloRadius: 12,
  legWidth: 2.5,
  legHaloWidth: 6,
};

/**
 * Filter the routes down to what is actually on screen and lay them out in
 * time.
 *
 * Fan legs keep only the spokes whose gateway node *and* edge are present; a
 * traceroute chain is dropped whole when a single hop has no edge — dots
 * must ride real links, never fly over a gap.
 *
 * @param {Array} routes /api/packet_routes result, both kinds mixed
 * @param {Array} nodeIds ids of the nodes currently in the graph
 * @param {Array} linkKeys "a-b" keys of the current direct links
 * @param {object} params MESHGRAPH_FLOW_PARAMS
 * @returns {{routes: Array, totalMs: number, params: object}}
 */
function meshgraphFlowPlan(routes, nodeIds, linkKeys, params) {
  const have = new Set(nodeIds);
  const links = new Set(linkKeys);
  const edge = (a, b) => links.has([a, b].sort((x, y) => x - y).join("-"));

  const planned = [];
  routes.forEach((route, index) => {
    if (!have.has(route.sender)) return;
    let legs = [];
    if (route.kind === "fan") {
      legs = (route.legs || [])
        .filter((leg) => have.has(leg.to) && edge(route.sender, leg.to))
        .map((leg, i) => ({
          key: `${route.kind}-${index}:${i}`,
          from: route.sender,
          to: leg.to,
          snr: leg.snr === undefined ? null : leg.snr,
          at: i * params.fanSpreadMs,
        }));
    } else if (route.kind === "traceroute") {
      // Consecutive legs chain: each starts where the previous one ended.
      let cursor = route.sender;
      const chain = [];
      let intact = true;
      for (const leg of route.legs || []) {
        if (!have.has(leg.to) || !edge(cursor, leg.to)) {
          intact = false;
          break;
        }
        chain.push({ from: cursor, to: leg.to, snr: leg.snr === undefined ? null : leg.snr });
        cursor = leg.to;
      }
      if (intact) {
        legs = chain.map((leg, i) => ({
          ...leg,
          key: `${route.kind}-${index}:${i}`,
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
      key: `${route.kind}-${index}`,
      kind: route.kind,
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
 *   dots: {key, x, y, z, from, to, snr}
 *   legs: {key, from, to, snr, ax..bz, phase}  phase 1 = lit, decaying to 0
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
