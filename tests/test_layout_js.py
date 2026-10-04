"""Island layout helpers (``meshgraph/static/layout.js``) run through QuickJS.

The layout lives in the browser, so these tests execute its pure part in a JS
engine: component detection, radius measurement and the packing invariant that
keeps disconnected parts of the graph from overlapping.  A syntax guard also
compiles ``app.js`` so front-end typos fail the suite without a browser.
"""

from __future__ import annotations

import json
import pathlib

import pytest

quickjs = pytest.importorskip("quickjs")

ROOT = pathlib.Path(__file__).resolve().parents[1]
LAYOUT_JS = ROOT / "meshgraph" / "static" / "layout.js"
APP_JS = ROOT / "meshgraph" / "static" / "app.js"
D3_JS = ROOT / "meshgraph" / "static" / "d3.v7.min.js"


@pytest.fixture(scope="module")
def js() -> "quickjs.Context":
    ctx = quickjs.Context()
    ctx.eval(LAYOUT_JS.read_text(encoding="utf-8"))
    return ctx


def run(js, expression: str):
    """Evaluate a JS expression and bring the result back as Python data."""
    return json.loads(js.eval(f"JSON.stringify({expression})"))


# ---------------------------------------------------------------------------
# Components (what counts as an island)
# ---------------------------------------------------------------------------


def test_components_split_disconnected_parts(js):
    result = run(
        js,
        "meshgraphFindComponents("
        "[{id:1},{id:2},{id:3},{id:4},{id:5},{id:6}],"
        "[{source:1,target:2},{source:2,target:3},{source:5,target:6}])",
    )
    # 1-2-3 цепочка, 4 одинокий, 5-6 пара — и порядок соответствует узлам.
    assert result == [[1, 2, 3], [4], [5, 6]]


def test_components_accept_object_link_endpoints(js):
    # forceLink переводит id в объекты — раскладка должна принимать оба вида.
    result = run(
        js,
        "meshgraphFindComponents([{id:'a'},{id:'b'}],"
        "[{source:{id:'a'},target:{id:'b'}}])",
    )
    assert result == [["a", "b"]]


def test_components_without_links_makes_singletons(js):
    assert run(js, "meshgraphFindComponents([{id:7},{id:8}], [])") == [[7], [8]]
    assert run(js, "meshgraphFindComponents([], [])") == []


def test_extra_connection_merges_islands(js):
    two = run(
        js,
        "meshgraphFindComponents([{id:1},{id:2},{id:3},{id:4}],"
        "[{source:1,target:2},{source:3,target:4}])",
    )
    one = run(
        js,
        "meshgraphFindComponents([{id:1},{id:2},{id:3},{id:4}],"
        "[{source:1,target:2},{source:3,target:4},{source:2,target:3}])",
    )
    assert len(two) == 2
    # Появившаяся связь (в т.ч. непрямая) сразу объединяет части.
    assert len(one) == 1


# ---------------------------------------------------------------------------
# Structure key (may the previous packing plan be reused on re-render?)
# ---------------------------------------------------------------------------


def test_island_key_maps_every_node_to_smallest_id_of_its_part(js):
    result = run(js, "Array.from(meshgraphIslandKey([[3, 4, 5], [1], [7, 8]]))")
    assert result == [[3, 3], [4, 3], [5, 3], [1, 1], [7, 7], [8, 7]]


def test_same_structure_for_unchanged_islands(js):
    # Тот же набор частей (даже в другом порядке) → план упаковки годится.
    assert run(
        js,
        "meshgraphSameStructure("
        "meshgraphIslandKey([[1, 2], [5, 6]]),"
        "meshgraphIslandKey([[6, 5], [2, 1]]))",
    ) is True


def test_same_structure_rejects_changed_membership(js):
    base = "meshgraphIslandKey([[1, 2], [5, 6]])"
    moved = "meshgraphIslandKey([[1, 2, 5], [6]])"  # узел переехал в другую часть
    added = "meshgraphIslandKey([[1, 2], [5, 6], [9]])"  # появилась третья часть
    swapped = "meshgraphIslandKey([[1, 2], [5, 7]])"  # тот же размер, но не те узлы
    assert run(js, f"meshgraphSameStructure({base}, {moved})") is False
    assert run(js, f"meshgraphSameStructure({base}, {added})") is False
    assert run(js, f"meshgraphSameStructure({base}, {swapped})") is False


def test_same_structure_without_previous_render(js):
    # Первый показ (ещё нечего сравнивать) — только свежий план.
    assert run(js, "meshgraphSameStructure(null, meshgraphIslandKey([[1]]))") is False


# ---------------------------------------------------------------------------
# Packing
# ---------------------------------------------------------------------------


def test_single_island_is_centred(js):
    cells = run(js, "meshgraphPlanIslands([120], 800, 600, 20)")
    assert cells == [{"index": 0, "tx": 400, "ty": 300, "r": 120}]


def test_cells_never_overlap(js):
    radii = [180, 95, 40, 130, 60, 210, 33]
    cells = run(js, f"meshgraphPlanIslands({radii}, 700, 500, 50)")
    gap = 50

    assert sorted(c["index"] for c in cells) == list(range(len(radii)))
    for i, a in enumerate(cells):
        for b in cells[i + 1 :]:
            ra = radii[a["index"]]
            rb = radii[b["index"]]
            dist = ((a["tx"] - b["tx"]) ** 2 + (a["ty"] - b["ty"]) ** 2) ** 0.5
            # Инвариант: центры ячеек дальше, чем сумма радиусов + зазор.
            assert dist >= ra + rb + gap - 1e-6, (a, b, dist)


def test_islands_wrap_into_rows_when_they_do_not_fit(js):
    # Каждый остров 200 px + зазор 50 → в 450 px помещаются только два в ряд.
    cells = run(js, "meshgraphPlanIslands([100,100,100,100,100], 450, 400, 50)")
    assert len({c["ty"] for c in cells}) == 3


def test_fitted_plan_prefers_arrangement_that_fits_viewport(js):
    # Широкий экран: два одинаковых острова в ряд, а не колонкой.
    row = run(js, "meshgraphPlanIslandsFitted([300,300], 1000, 400, 20)")
    assert len({c["ty"] for c in row}) == 1
    # Высокий узкий — наоборот, колонкой.
    col = run(js, "meshgraphPlanIslandsFitted([300,300], 500, 900, 20)")
    assert len({c["ty"] for c in col}) == 2


def test_fitted_cells_never_overlap(js):
    radii = [180, 95, 40, 130, 60, 210, 33]
    cells = run(js, f"meshgraphPlanIslandsFitted({radii}, 700, 500, 20)")
    gap = 20

    assert sorted(c["index"] for c in cells) == list(range(len(radii)))
    for i, a in enumerate(cells):
        for b in cells[i + 1 :]:
            ra = radii[a["index"]]
            rb = radii[b["index"]]
            dist = ((a["tx"] - b["tx"]) ** 2 + (a["ty"] - b["ty"]) ** 2) ** 0.5
            assert dist >= ra + rb + gap - 1e-6, (a, b, dist)


# ---------------------------------------------------------------------------
# Radius measurement
# ---------------------------------------------------------------------------


def test_measure_keeps_node_extent_in_radius(js):
    result = run(
        js,
        "meshgraphMeasureIslands([[1,2]],"
        " new Map([[1,{x:0,y:0,size:10}],[2,{x:40,y:0,size:10}]]), 50)",
    )
    # Центроид (20,0): дальняя точка 20 px + размер узла 10 + коллизия 50.
    assert result == [80]


def test_measure_handles_missing_positions(js):
    # Узел без координат не должен ронять раскладку.
    assert run(js, "meshgraphMeasureIslands([[9]], new Map(), 40)") == [70]


# ---------------------------------------------------------------------------
# Repulsion from foreign link lines
# ---------------------------------------------------------------------------


def test_avoid_links_pushes_node_sitting_on_foreign_line(js):
    # Узел 5 стоит точно на чужой линии 1–2 (y=0): сила уводит его в сторону,
    # а концы самой линии 1–2 и узлы чужой линии 3–4 остаются на месте.
    result = run(
        js,
        """(function() {
          const nodes = [
            { id: 1, x: 0, y: 0, size: 10 },
            { id: 2, x: 200, y: 0, size: 10 },
            { id: 3, x: 0, y: 200, size: 10 },
            { id: 4, x: 200, y: 200, size: 10 },
            { id: 5, x: 100, y: 0, size: 10 },
            { id: 6, x: 150, y: 4, size: 10 },
          ];
          const links = [{ source: 1, target: 2 }, { source: 3, target: 4 }];
          const force = meshgraphAvoidLinks(links);
          force.initialize(nodes);
          for (let i = 0; i < 60; i++) force();
          return {
            onLine: Math.abs(nodes[4].y),
            side: nodes[5].y,
            endpoints: [nodes[0].x, nodes[0].y, nodes[1].x, nodes[1].y,
                        nodes[2].x, nodes[2].y],
          };
        })()""",
    )
    # Точно на линии узел вытолкнут до выхода за зону (55 px).
    assert result["onLine"] >= 50
    # Узел у самой кромки уходит В СТОРОНУ, в которой стоял, а не на себя.
    assert result["side"] >= 50
    # Концы линий и чужая линия не сдвинулись: они касаются своих линий
    # или лежат вне зоны.
    assert result["endpoints"] == [0, 0, 200, 0, 0, 200]


def test_avoid_links_leaves_nodes_outside_zone_untouched(js):
    # Узел дальше зоны (55 px) не получает ни толчка.
    result = run(
        js,
        """(function() {
          const nodes = [
            { id: 1, x: 0, y: 0, size: 10 },
            { id: 2, x: 200, y: 0, size: 10 },
            { id: 7, x: 100, y: 80, size: 10 },
          ];
          const force = meshgraphAvoidLinks([{ source: 1, target: 2 }]);
          force.initialize(nodes);
          for (let i = 0; i < 30; i++) force();
          return [nodes[2].x, nodes[2].y];
        })()""",
    )
    assert result == [100, 80]


def test_avoid_links_tolerates_ids_and_degenerate_links(js):
    # Ссылки с id вместо объектов, вырожденный отрезок и узел в зоне —
    # сила не должна падать и рождать NaN.
    result = run(
        js,
        """(function() {
          const nodes = [
            { id: 'a', x: 0, y: 0, size: 10 },
            { id: 'b', x: 0, y: 100, size: 10 },
            { id: 'c', x: 40, y: 50, size: 10 },
          ];
          const links = [{ source: 'a', target: 'b' },
                         { source: 'a', target: 'a' }];
          const force = meshgraphAvoidLinks(links);
          force.initialize(nodes);
          for (let i = 0; i < 40; i++) force();
          return [nodes[0].x, nodes[0].y, nodes[1].x, nodes[1].y,
                  nodes[2].x, nodes[2].y];
        })()""",
    )
    ax, ay, bx, by, cx, cy = result
    # Концы линии стоят на месте, чужой узел c вытолкнут от неё.
    assert (ax, ay) == (0, 0)
    assert (bx, by) == (0, 100)
    # NaN сериализовался бы в null и ронял бы сравнение.
    assert cy == 50 and cx >= 50
    assert all(isinstance(v, (int, float)) for v in result)


# ---------------------------------------------------------------------------
# The whole pipeline against the real d3 forces
# ---------------------------------------------------------------------------

# Mirrors renderGraph(): settle → measure → plan → glide.  The simulation is
# stepped by hand because the JS environment has no timers.
_SCENARIO = """
globalThis.runScenario = function() {
  const nodes = [];
  const links = [];
  for (let i = 1; i <= 5; i++) nodes.push({ id: i, size: 12 });
  for (let i = 100; i <= 101; i++) nodes.push({ id: i, size: 12 });
  for (let i = 200; i <= 202; i++) nodes.push({ id: i, size: 12 });
  for (let i = 1; i < 5; i++) links.push({ source: i, target: i + 1, strength: 4 });
  links.push({ source: 100, target: 101, strength: 4 });
  links.push({ source: 200, target: 201, strength: 4 });
  links.push({ source: 201, target: 202, strength: 4 });

  const byId = new Map(nodes.map((n) => [n.id, n]));
  const islands = meshgraphFindComponents(nodes, links);
  const sim = d3
    .forceSimulation(nodes)
    .force("link", d3.forceLink(links).id((d) => d.id)
      .distance((d) => Math.max(140, 180 - d.strength * 20)))
    .force("charge", d3.forceManyBody().strength(-110))
    .force("collision", d3.forceCollide().radius((d) => d.size + 50))
    .force("avoidLinks", meshgraphAvoidLinks(links));
  sim.stop();
  for (let i = 0; i < 120; i++) sim.tick();

  const radii = meshgraphMeasureIslands(islands, byId, 50);
  const cells = meshgraphPlanIslandsFitted(radii, 800, 600, 20);
  const targets = new Map();
  cells.forEach((c) => islands[c.index].forEach((id) => targets.set(id, c)));
  sim.force("islands", meshgraphIslandForce(targets));
  for (let i = 0; i < 250; i++) sim.tick();

  const centres = islands.map((ids, ci) => {
    let cx = 0;
    let cy = 0;
    ids.forEach((id) => { cx += byId.get(id).x; cy += byId.get(id).y; });
    return {
      cx: cx / ids.length,
      cy: cy / ids.length,
      dev: Math.hypot(cx / ids.length - cells[ci].tx, cy / ids.length - cells[ci].ty),
      r: radii[ci],
    };
  });
  let minGap = Infinity;
  for (let i = 0; i < centres.length; i++) {
    for (let j = i + 1; j < centres.length; j++) {
      const a = centres[i];
      const b = centres[j];
      minGap = Math.min(minGap, Math.hypot(a.cx - b.cx, a.cy - b.cy) - (a.r + b.r));
    }
  }
  // Минимальный зазор от КРАЯ узла до чужой линии: отрицательный = линия
  // проходит внутри кружка узла.
  let minEdge = Infinity;
  for (const n of nodes) for (const l of links) {
    if (l.source === n || l.target === n) continue;
    const vx = l.target.x - l.source.x, vy = l.target.y - l.source.y;
    const len2 = vx * vx + vy * vy;
    if (!len2) continue;
    let u = ((n.x - l.source.x) * vx + (n.y - l.source.y) * vy) / len2;
    u = Math.max(0, Math.min(1, u));
    const d = Math.hypot(n.x - (l.source.x + u * vx), n.y - (l.source.y + u * vy));
    minEdge = Math.min(minEdge, d - n.size);
  }
  return JSON.stringify({ islands: islands.length,
                          maxDev: Math.max(...centres.map((c) => c.dev)),
                          minGap: minGap, minEdge: minEdge });
};
"""


@pytest.fixture(scope="module")
def scenario_ctx() -> "quickjs.Context":
    ctx = quickjs.Context()
    # d3 starts its timer on construction; without a DOM there are no timers,
    # the tests step the simulation manually anyway.
    ctx.eval(
        "globalThis.setTimeout = function(){ return 0; };"
        "globalThis.clearTimeout = function(){};"
        "globalThis.setInterval = function(){ return 0; };"
        "globalThis.clearInterval = function(){};"
    )
    ctx.eval(D3_JS.read_text(encoding="utf-8"))
    ctx.eval(LAYOUT_JS.read_text(encoding="utf-8"))
    ctx.eval(_SCENARIO)
    return ctx


def test_islands_actually_move_apart_with_d3(scenario_ctx):
    result = json.loads(scenario_ctx.eval("runScenario()"))

    # Три несвязанные части: цепочка, пара и тройка.
    assert result["islands"] == 3
    # Каждая часть доехала до своей ячейки…
    assert result["maxDev"] < 1.0
    # …и ни одна пара не перекрывается (запас — планировочный зазор).
    assert result["minGap"] > 0
    # …а ни один узел не оказался на чужой линии связи: до её края остаётся
    # белое (сила держит зазор не меньше 6 px).
    assert result["minEdge"] >= 6


# ---------------------------------------------------------------------------
# Syntax guard for the rest of the front end
# ---------------------------------------------------------------------------


def test_app_js_still_compiles():
    """Compile (not run) app.js: syntax errors fail the suite without a browser."""
    ctx = quickjs.Context()
    ctx.eval("(function(){\n" + APP_JS.read_text(encoding="utf-8") + "\n})")
