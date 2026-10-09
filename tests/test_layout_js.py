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


def test_can_freeze_when_refresh_changes_nothing(js):
    # Структура, холст и прошлая симуляция не изменились → свежую симуляцию
    # гасим: иначе перегрев alpha=1 дёргал граф каждое автообновление.
    assert run(js, "meshgraphCanFreeze(true, true, 800, 600, 800, 600)") is True


def test_can_freeze_rejects_changed_structure_or_unsettled_sim(js):
    # Изменение структуры — раскладка должна пересчитаться (симуляция идёт).
    assert run(js, "meshgraphCanFreeze(false, true, 800, 600, 800, 600)") is False
    # Прошлая симуляция ещё в полёте (фильтр поменяли пару секунд назад) —
    # запускаем новую, чтобы layout доуспел доехать, а не замораживали бык.
    assert run(js, "meshgraphCanFreeze(true, false, 800, 600, 800, 600)") is False
    # Первый показ: нет ни прежней симуляции, ни прежнего размера.
    assert run(js, "meshgraphCanFreeze(null, null, 800, 600, undefined, undefined)") is False


def test_can_freeze_rejects_canvas_resize(js):
    # Ресайз: центр/ячейки пересчитываются под новый холст.
    assert run(js, "meshgraphCanFreeze(true, true, 700, 600, 800, 600)") is False
    assert run(js, "meshgraphCanFreeze(true, true, 800, 500, 800, 600)") is False


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
  // Те же константы, что и в прод-рендере (G-P2-2): расхождение параметров
  // между сценарием и app.js больше невозможно.
  const sim = d3
    .forceSimulation(nodes)
    .force("link", d3.forceLink(links).id((d) => d.id)
      .distance(meshgraphLinkDistance))
    .force("charge",
      d3.forceManyBody().strength(MESHGRAPH_FORCE_PARAMS.chargeStrength))
    .force("collision", d3.forceCollide().radius(meshgraphCollisionRadius))
    .force("avoidLinks", meshgraphAvoidLinks(links));
  sim.stop();
  for (let i = 0; i < MESHGRAPH_FORCE_PARAMS.gatherTicks; i++) sim.tick();

  const radii = meshgraphMeasureIslands(
    islands, byId, MESHGRAPH_FORCE_PARAMS.collisionPad
  );
  const cells = meshgraphPlanIslandsFitted(
    radii, 800, 600, MESHGRAPH_FORCE_PARAMS.islandGap
  );
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


def test_refresh_wiring_freezes_and_stays_silent():
    """Фоновое обновление не дёргает граф и не моргает оверлеем.

    Строковые проверки каркаса: renderGraph решает заморозку через
    meshgraphCanFreeze и гасит свежую симуляцию, а автообновление идёт через
    loadGraph({silent: true}) без спиннера поверх холста.
    """
    src = APP_JS.read_text(encoding="utf-8")
    assert "meshgraphCanFreeze(" in src
    # Заморозка унаследованных позиций (иначе перегрев alpha=1 дёргал узлы).
    assert "simulation.alpha(0);" in src
    assert "loadGraph({ silent: true })" in src


# ---------------------------------------------------------------------------
# Shared force parameters and view fitting (G-P2-2)
# ---------------------------------------------------------------------------


def test_force_params_are_shared_by_render_and_scenario(js):
    """Один источник правды: прод-рендер и сценарий читают одни константы."""
    keys = run(js, "Object.keys(MESHGRAPH_FORCE_PARAMS).sort()")
    for expected in (
        "chargeStrength",
        "collisionPad",
        "focusScale",
        "gatherTicks",
        "islandGap",
        "linkDistanceMin",
    ):
        assert expected in keys

    app_js = APP_JS.read_text(encoding="utf-8")
    assert "MESHGRAPH_FORCE_PARAMS" in app_js
    assert "meshgraphLinkDistance" in app_js
    assert "meshgraphSeedPositions" in app_js
    # Магических чисел сил в прод-коде не осталось.
    assert "forceManyBody().strength(-110)" not in app_js
    assert "d.size + 50" not in app_js
    assert "180 - d.strength * 20" not in app_js

    # Сценарий д3 пользуется теми же функциями и константами.
    assert "meshgraphLinkDistance" in _SCENARIO
    assert "MESHGRAPH_FORCE_PARAMS.chargeStrength" in _SCENARIO
    assert run(js, "MESHGRAPH_FORCE_PARAMS.chargeStrength") == -110


def test_fit_cells_fills_the_view_without_upscaling(js):
    cells = "[{tx: 0, ty: 0, r: 100}, {tx: 1800, ty: 900, r: 100}]"
    fit = run(js, f"meshgraphFitCellsTransform({cells}, 800, 600)")

    # Bounding box 2000×1100, центр (900, 450): scale = 0.92·800/2000.
    assert fit["k"] == pytest.approx(0.368)
    assert fit["k"] < 1
    assert fit["cx"] == pytest.approx(900)
    assert fit["cy"] == pytest.approx(450)


def test_fit_cells_never_zooms_a_small_plan_in(js):
    fit = run(js, "meshgraphFitCellsTransform([{tx: 100, ty: 100, r: 50}], 800, 600)")

    assert fit["k"] == 1
    assert fit["cx"] == 100
    assert fit["cy"] == 100


def test_fit_cells_rejects_nothing_to_fit(js):
    assert run(js, "meshgraphFitCellsTransform([], 800, 600)") is None
    assert run(js, "meshgraphFitCellsTransform(null, 800, 600)") is None
    # Одна ячейка нулевого радиуса: ширина и высота вырождены.
    assert (
        run(js, "meshgraphFitCellsTransform([{tx: 0, ty: 0, r: 0}], 800, 600)") is None
    )


def test_fit_bounds_centres_the_content_and_caps_scale(js):
    fit = run(
        js,
        "meshgraphFitBoundsTransform("
        "{x: 0, y: 0, width: 900, height: 450}, 800, 600)",
    )

    # 0.9 / max(900/800, 450/600) = 0.8; центр контента (450, 225) → центр экрана.
    assert fit["scale"] == pytest.approx(0.8)
    assert fit["tx"] == pytest.approx(40)
    assert fit["ty"] == pytest.approx(120)

    # Крошечный контент не раздувается сильнее лимита (2.5).
    tiny = run(
        js,
        "meshgraphFitBoundsTransform({x: 0, y: 0, width: 80, height: 40}, 800, 600)",
    )
    assert tiny["scale"] == pytest.approx(2.5)


def test_fit_bounds_rejects_empty_content(js):
    assert run(js, "meshgraphFitBoundsTransform(null, 800, 600)") is None
    assert (
        run(js, "meshgraphFitBoundsTransform({x: 0, y: 0, width: 0, height: 10},"
                " 800, 600)")
        is None
    )


def test_focus_transform_puts_the_node_in_the_centre(js):
    fit = run(js, "meshgraphFocusTransform(300, 200, 800, 600)")

    assert fit["k"] == pytest.approx(1.6)
    assert fit["tx"] == pytest.approx(-80)
    assert fit["ty"] == pytest.approx(-20)
    # Точка действительно попадает в центр вида.
    assert fit["k"] * 300 + fit["tx"] == pytest.approx(400)
    assert fit["k"] * 200 + fit["ty"] == pytest.approx(300)


def test_seed_positions_inherits_previous_coordinates(js):
    result = run(
        js,
        """(() => {
          const nodes = [{id: 1}, {id: 2}];
          const prev = new Map([[1, {x: 10, y: 20, vx: 3, vy: 4}]]);
          const inherited = meshgraphSeedPositions(nodes, prev, 800, 600);
          return {inherited, nodes};
        })()""",
    )

    assert result["inherited"] == 1
    first, second = result["nodes"]
    assert (first["x"], first["y"]) == (10, 20)
    assert (first["vx"], first["vy"]) == (3, 4)
    # Новый узел посеян на кругу: i = 1 → угол π → x = 400 − 180 ± 30.
    assert 190 <= second["x"] <= 250
    assert 270 <= second["y"] <= 330


def test_seed_positions_reseeds_when_the_previous_one_is_broken(js):
    result = run(
        js,
        """(() => {
          const nodes = [{id: 1}];
          const prev = new Map([[1, {x: NaN, y: 5}]]);
          const inherited = meshgraphSeedPositions(nodes, prev, 800, 600);
          return {inherited, x: nodes[0].x, y: nodes[0].y};
        })()""",
    )

    assert result["inherited"] == 0
    # i = 0 → угол 0: x = 400 + 180 ± 30, y = 300 ± 30.
    assert 550 <= result["x"] <= 610
    assert 270 <= result["y"] <= 330


# ---------------------------------------------------------------------------
# Chat link helpers (meshgraph/static/chatlinks.js)
# ---------------------------------------------------------------------------

CHATLINKS_JS = ROOT / "meshgraph" / "static" / "chatlinks.js"


@pytest.fixture(scope="module")
def links_js() -> "quickjs.Context":
    ctx = quickjs.Context()
    ctx.eval(CHATLINKS_JS.read_text(encoding="utf-8"))
    return ctx


def test_split_links_preserves_text_and_link_order(links_js):
    result = run(
        links_js,
        "meshgraphSplitLinks("
        "'до ссылки https://a.example/x.jpg после\\nи ещё https://b.example/')",
    )
    assert result == [
        {"type": "text", "text": "до ссылки "},
        {"type": "link", "url": "https://a.example/x.jpg"},
        {"type": "text", "text": " после\nи ещё "},
        {"type": "link", "url": "https://b.example/"},
    ]


def test_split_links_keeps_plain_text_as_one_segment(links_js):
    assert run(links_js, "meshgraphSplitLinks('просто текст')") == [
        {"type": "text", "text": "просто текст"}
    ]
    assert run(links_js, "meshgraphSplitLinks('')") == []
    assert run(links_js, "meshgraphSplitLinks(null)") == []


def test_split_links_trims_sentence_punctuation(links_js):
    # Точка после ссылки — часть предложения, не адреса.
    result = run(links_js, "meshgraphSplitLinks('смотри https://a.example/x. конец')")
    assert result[1]["url"] == "https://a.example/x"
    assert result[2]["text"] == ". конец"


def test_split_links_keeps_brackets_the_url_needs(links_js):
    # Закрывающая скобка висит после ссылки — её срезаем…
    wrapped = run(
        links_js, "meshgraphSplitLinks('см. (https://a.example/page) и дальше')"
    )
    assert wrapped[1]["url"] == "https://a.example/page"
    assert wrapped[2]["text"] == ") и дальше"
    # …а скобка, открывающаяся внутри адреса, остаётся его частью.
    balanced = run(links_js, "meshgraphSplitLinks('https://a.example/f(a)')")
    assert balanced == [{"type": "link", "url": "https://a.example/f(a)"}]


def test_collage_grid_adapts_to_the_image_count(links_js):
    assert run(links_js, "meshgraphCollageGrid(1)") == {"shown": 1, "extra": 0}
    assert run(links_js, "meshgraphCollageGrid(4)") == {"shown": 4, "extra": 0}
    assert run(links_js, "meshgraphCollageGrid(6)") == {"shown": 6, "extra": 0}
    # Больше шести — пять плиток и «+N», все картинки открываются в лайтбоксе.
    assert run(links_js, "meshgraphCollageGrid(7)") == {"shown": 5, "extra": 2}
    assert run(links_js, "meshgraphCollageGrid(12)") == {"shown": 5, "extra": 7}


def test_link_host_is_taken_without_path_or_scheme(links_js):
    assert run(links_js, "meshgraphLinkHost('https://meshpic.org/w6i')") == "meshpic.org"
    assert run(links_js, "meshgraphLinkHost('http://192.168.1.5:8080/a')") == "192.168.1.5"


def test_collage_renders_the_resolved_image_not_the_link_page():
    """Страница обменника (junkdata /v/, meshpic /AbC) — это HTML: в img
    Chromium блокирует её как ORB-ответ, и картинка деградирует в чип.
    Ячейка обязана рисовать preview.url, а не ссылку из сообщения."""
    src = APP_JS.read_text(encoding="utf-8")
    assert "meshgraphResolvedImage(" in src
    assert "img.src = item.src;" in src
    # Битая клетка отдаёт клик на страницу-оригинал из сообщения.
    assert "window.open(item.page" in src


def test_image_groups_merge_links_separated_by_whitespace(links_js):
    # «url url\nurl» — одна картинка-группа: пробелы и переносы не рвут её.
    result = run(
        links_js,
        """meshgraphImageGroups(
            meshgraphSplitLinks("https://a.example/1 https://a.example/2\\nhttps://a.example/3 конец"),
            () => "image")""",
    )
    assert result == [{
        "start": 0, "end": 5,
        "urls": ["https://a.example/1", "https://a.example/2",
                 "https://a.example/3"],
    }]


def test_real_text_keeps_two_groups_in_order(links_js):
    # Текст между картинками: коллаж, текст, коллаж — очерёдность свята.
    result = run(
        links_js,
        """meshgraphImageGroups(
            meshgraphSplitLinks("https://a.example/1 середина https://a.example/2"),
            () => "image")""",
    )
    assert result == [
        {"start": 0, "end": 1, "urls": ["https://a.example/1"]},
        {"start": 2, "end": 3, "urls": ["https://a.example/2"]},
    ]


def test_non_image_link_breaks_the_group(links_js):
    result = run(
        links_js,
        """meshgraphImageGroups(
            meshgraphSplitLinks("https://a.example/pic/1 https://a.example/page"),
            (url) => url.includes("/pic/") ? "image" : "page")""",
    )
    assert result == [{"start": 0, "end": 2, "urls": ["https://a.example/pic/1"]}]


def test_pending_links_form_no_groups(links_js):
    # Пока превью не пришло — ссылки рендерятся чипами, коллаж появится
    # после перерисовки.
    assert run(
        links_js,
        """meshgraphImageGroups(
            meshgraphSplitLinks("https://a.example/1 https://a.example/2"),
            () => "pending")""",
    ) == []


# ---------------------------------------------------------------------------
# 3D force layout (force3d.js)
# ---------------------------------------------------------------------------

FORCE3D_JS = ROOT / "meshgraph" / "static" / "force3d.js"
GRAPH3D_JS = ROOT / "meshgraph" / "static" / "graph3d.js"
INDEX_HTML = ROOT / "meshgraph" / "templates" / "index.html"


@pytest.fixture(scope="module")
def js3d() -> "quickjs.Context":
    ctx = quickjs.Context()
    ctx.eval(FORCE3D_JS.read_text(encoding="utf-8"))
    return ctx


def test_anchor_points_ring_and_singleton(js3d):
    assert run(js3d, "meshgraphAnchorPoints3D(1, 340)") == [
        {"x": 0, "y": 0, "z": 0}
    ]
    ring = run(js3d, "meshgraphAnchorPoints3D(4, 340)")
    assert len(ring) == 4
    # Все якорья лежат в горизонтальной плоскости, на одном радиусе,
    # соседние — на равном расстоянии (хорда = исходный spacing).
    assert all(p["y"] == 0 for p in ring)
    radii = [(p["x"] ** 2 + p["z"] ** 2) ** 0.5 for p in ring]
    assert max(radii) - min(radii) < 1e-6
    chords = []
    for i in range(4):
        a, b = ring[i], ring[(i + 1) % 4]
        chords.append(((a["x"] - b["x"]) ** 2 + (a["z"] - b["z"]) ** 2) ** 0.5)
    assert max(chords) - min(chords) < 1e-6
    assert abs(chords[0] - 340) < 1e-6


def test_seed_fills_depth_and_is_deterministic(js3d):
    # Без предыдущих координат спираль даёт объём (z не плоский ноль),
    # повторный посев на тех же входах воспроизводит те же координаты.
    result = run(
        js3d,
        """(function () {
            const nodes = Array.from({length: 30}, (_, i) => ({id: i}));
            const anchors = nodes.map(() => ({x: 0, y: 0, z: 0}));
            meshgraphSeedPositions3D(nodes, null, anchors, MESHGRAPH_FORCE3D_PARAMS);
            const first = nodes.map(n => [n.x, n.y, n.z]);
            meshgraphSeedPositions3D(nodes, null, anchors, MESHGRAPH_FORCE3D_PARAMS);
            const same = JSON.stringify(first) === JSON.stringify(
                nodes.map(n => [n.x, n.y, n.z]));
            return {
                same,
                finite: nodes.every(n => isFinite(n.x) && isFinite(n.y) && isFinite(n.z)),
                flat: nodes.every(n => n.z === 0),
            };
        })()""",
    )
    assert result["same"] and result["finite"]
    assert not result["flat"]


def test_seed_inherits_prev_positions_and_zeroes_speeds(js3d):
    # 3D-наследник: x/y/z остаются прежними, старые скорости гаснут —
    # иначе унаследованный импульс увёл бы сцену с экрана.
    result = run(
        js3d,
        """(function () {
            const nodes = [{id: 1}];
            const prev = new Map([[1, {id: 1, x: 5, y: 6, z: 7, vx: 9, vy: 9, vz: 9}]]);
            const anchors = [{x: 0, y: 0, z: 0}];
            meshgraphSeedPositions3D(nodes, prev, anchors, MESHGRAPH_FORCE3D_PARAMS);
            const n = nodes[0];
            return {x: n.x, y: n.y, z: n.z, vx: n.vx, vy: n.vy, vz: n.vz};
        })()""",
    )
    assert result == {"x": 5, "y": 6, "z": 7, "vx": 0, "vy": 0, "vz": 0}


def test_seed_from_flat_2d_prev_gains_depth(js3d):
    # Узлы из 2D не имеют z вовсе (undefined): координаты x/y держим,
    # а глубину донашиваем спиралью — иначе переключение даёт «блин».
    result = run(
        js3d,
        """(function () {
            const nodes = Array.from({length: 12}, (_, i) => ({id: i}));
            const prev = new Map(nodes.map(n => [n.id, {id: n.id, x: 100, y: 50}]));
            const anchors = nodes.map(() => ({x: 0, y: 0, z: 0}));
            meshgraphSeedPositions3D(nodes, prev, anchors, MESHGRAPH_FORCE3D_PARAMS);
            return {
                xyKept: nodes.every(n => n.x === 100 && n.y === 50),
                zFinite: nodes.every(n => isFinite(n.z)),
                someZ: nodes.some(n => Math.abs(n.z) > 1),
            };
        })()""",
    )
    assert result["xyKept"] and result["zFinite"] and result["someZ"]


def test_link_springs_pull_to_rest_distance(js3d):
    # Одиночная связь без конкурирующих сил: растянутая пара сжимается,
    # сжатая — расправляется к естественной длине связи.
    result = run(
        js3d,
        """(function () {
            const p = Object.assign({}, MESHGRAPH_FORCE3D_PARAMS, {
                chargeStrength: 0, anchorStrength: 0, collisionPad: 0});
            const far = [{id: 0, x: 0, y: 0, z: 0}, {id: 1, x: 900, y: 0, z: 0}];
            const near = [{id: 0, x: 0, y: 0, z: 0}, {id: 1, x: 40, y: 0, z: 0}];
            const pairs = [[0, 1]];
            const noAnchors = [null, null];
            for (let i = 0; i < 60; i += 1) {
                meshgraphForce3DStep(far, pairs, noAnchors, 1, p);
                meshgraphForce3DStep(near, pairs, noAnchors, 1, p);
            }
            return {
                far: Math.abs(far[1].x - far[0].x),
                near: Math.abs(near[1].x - near[0].x),
            };
        })()""",
    )
    # alpha держим на 1 — просто несколько десятков шагов подряд.
    assert 100 < result["far"] < 300  # было 900 → потянулось к покою
    assert 60 < result["near"] < 240  # было 40 → расправилось к пок


def test_repulsion_pushes_unlinked_nodes_apart(js3d):
    result = run(
        js3d,
        """(function () {
            const p = Object.assign({}, MESHGRAPH_FORCE3D_PARAMS, {
                anchorStrength: 0, collisionPad: 0});
            const nodes = [{id: 0, x: 0, y: 0, z: 0}, {id: 1, x: 50, y: 0, z: 0}];
            for (let i = 0; i < 30; i += 1) {
                meshgraphForce3DStep(nodes, [], [null, null], 1, p);
            }
            return Math.hypot(nodes[1].x - nodes[0].x, nodes[1].y - nodes[0].y,
                              nodes[1].z - nodes[0].z);
        })()""",
    )
    assert result > 50


def test_collision_keeps_node_bodies_apart(js3d):
    # Совпадающие центры: детерминированная ось + половинное исправление
    # позиции разводят узлы ровно на сумму радиусов с отступом.
    result = run(
        js3d,
        """(function () {
            const p = Object.assign({}, MESHGRAPH_FORCE3D_PARAMS, {
                chargeStrength: 0, anchorStrength: 0});
            const nodes = [{id: 0, x: 0, y: 0, z: 0, size: 10},
                           {id: 1, x: 0, y: 0, z: 0, size: 10}];
            for (let i = 0; i < 30; i += 1) {
                meshgraphForce3DStep(nodes, [], [null, null], 1, p);
            }
            return Math.hypot(nodes[1].x - nodes[0].x, nodes[1].y - nodes[0].y,
                              nodes[1].z - nodes[0].z);
        })()""",
    )
    # size 10 + 10 + collisionPad 34 = 54, с допуском на округление.
    assert result >= 54 - 0.01


def test_islands_converge_to_their_own_anchors(js3d):
    # Две части без связей разъезжаются к своим якорьям, а не слипаются.
    result = run(
        js3d,
        """(function () {
            const p = Object.assign({}, MESHGRAPH_FORCE3D_PARAMS, {
                chargeStrength: 0, collisionPad: 0});
            const nodes = [{id: 0, x: 0, y: 0, z: 0}, {id: 1, x: 0, y: 0, z: 0}];
            const anchors = [{x: 1000, y: 0, z: 0}, {x: -1000, y: 0, z: 0}];
            meshgraphRun3D(nodes, [], anchors, 200, p);
            return {
                d0: Math.hypot(nodes[0].x - 1000, nodes[0].y, nodes[0].z),
                d1: Math.hypot(nodes[1].x + 1000, nodes[1].y, nodes[1].z),
            };
        })()""",
    )
    assert result["d0"] < 200 and result["d1"] < 200


def test_alpha_decays_to_zero(js3d):
    result = run(
        js3d,
        """(function () {
            const p = MESHGRAPH_FORCE3D_PARAMS;
            const once = meshgraphDecayAlpha3D(1, p);
            const tiny = meshgraphDecayAlpha3D(p.alphaMin / 2, p);
            const nodes = Array.from({length: 6}, (_, i) => ({id: i, x: i * 40, y: 0, z: 0}));
            const pairs = [[0, 1], [1, 2], [2, 3], [3, 4], [4, 5]];
            const anchors = nodes.map(() => ({x: 0, y: 0, z: 0}));
            const left = meshgraphRun3D(nodes, pairs, anchors, 100000, p);
            return {once, tiny, left,
                    finite: nodes.every(n => isFinite(n.x) && isFinite(n.y)
                                             && isFinite(n.z))};
        })()""",
    )
    assert result["once"] < 1
    assert result["tiny"] == 0
    assert result["left"] == 0  # длинная цепочка доиграла до покоя
    assert result["finite"]


def test_step_with_zero_alpha_moves_nothing(js3d):
    result = run(
        js3d,
        """(function () {
            const p = MESHGRAPH_FORCE3D_PARAMS;
            const nodes = [{id: 0, x: 1, y: 2, z: 3, vx: 0, vy: 0, vz: 0}];
            meshgraphForce3DStep(nodes, [], [null], 0, p);
            return [nodes[0].x, nodes[0].y, nodes[0].z];
        })()""",
    )
    assert result == [1, 2, 3]


# ---------------------------------------------------------------------------
# 3D wiring (module compiles, template and app.js hook it up)
# ---------------------------------------------------------------------------


def test_graph3d_module_compiles():
    """Compile (not run) graph3d.js: imports are stripped, three.js has no
    QuickJS, everything else must parse."""
    src = GRAPH3D_JS.read_text(encoding="utf-8")
    stripped = "\n".join(
        line for line in src.splitlines() if not line.startswith("import ")
    )
    quickjs.Context().eval("(function (){\n" + stripped + "\n})")


def test_3d_view_wiring():
    """The toggle, the import map and the module all ship together."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="view2dBtn"' in html
    assert 'id="view3dBtn"' in html
    assert '"imports"' in html and "three.module.min.js" in html
    assert "graph3d.js" in html and "force3d.js" in html
    app = APP_JS.read_text(encoding="utf-8")
    assert "renderGraph3D(" in app and "switchView(" in app
    # Ветка 3D уводит выделение и зум в модуль, а не в d3.
    assert "window.meshgraph3D.setSelection" in app
    assert "window.meshgraph3D.focusNode" in app


# ---------------------------------------------------------------------------
# Packet flow engine (packetflow.js): plan and frames for the glowing dots
# ---------------------------------------------------------------------------

PACKETFLOW_JS = ROOT / "meshgraph" / "static" / "packetflow.js"


@pytest.fixture(scope="module")
def jsflow() -> "quickjs.Context":
    ctx = quickjs.Context()
    ctx.eval(PACKETFLOW_JS.read_text(encoding="utf-8"))
    return ctx


def test_flow_plan_keeps_only_on_screen_fan_legs(jsflow):
    routes = [
        {
            "kind": "fan",
            "sender": 1,
            "packet_id": 7,
            "ts": 100.0,
            "legs": [
                {"to": 2, "snr": 5.0, "rssi": -80, "ts": 100.0},
                {"to": 3, "snr": 2.0, "rssi": -90, "ts": 100.0},
                {"to": 9, "snr": 1.0, "rssi": -70, "ts": 100.0},
            ],
        }
    ]
    plan = run(
        jsflow,
        f"meshgraphFlowPlan({json.dumps(routes)}, [1, 2, 3, 4],"
        " ['1-2', '1-4'], MESHGRAPH_FLOW_PARAMS)",
    )
    # Узла 9 в графе нет, связи 1-3 тоже нет — остаётся одна спица 1-2.
    assert len(plan["routes"]) == 1
    assert [leg["to"] for leg in plan["routes"][0]["legs"]] == [2]
    assert plan["totalMs"] > 0


def test_flow_plan_drops_broken_traceroute_chain(jsflow):
    routes = [
        {
            "kind": "traceroute",
            "sender": 1,
            "packet_id": 5,
            "ts": 100.0,
            "legs": [{"to": 2, "snr": 4.0}, {"to": 3, "snr": None}],
        }
    ]
    ok = run(
        jsflow,
        f"meshgraphFlowPlan({json.dumps(routes)}, [1, 2, 3],"
        " ['1-2', '2-3'], MESHGRAPH_FLOW_PARAMS)",
    )
    # Цепочка: вторая нога начинается там, где кончилась первая.
    legs = ok["routes"][0]["legs"]
    assert [(leg["from"], leg["to"]) for leg in legs] == [(1, 2), (2, 3)]
    # Без среднего ребра маршрут исчезает целиком — шарик не летает над дырой.
    broken = run(
        jsflow,
        f"meshgraphFlowPlan({json.dumps(routes)}, [1, 2, 3],"
        " ['1-3'], MESHGRAPH_FLOW_PARAMS)",
    )
    assert broken["routes"] == []


def test_flow_plan_lays_routes_out_in_time(jsflow):
    routes = [
        {
            "kind": "fan",
            "sender": 1,
            "packet_id": 1,
            "ts": 1.0,
            "legs": [{"to": 2, "snr": 5.0}, {"to": 3, "snr": 5.0}],
        },
        {
            "kind": "traceroute",
            "sender": 1,
            "packet_id": 2,
            "ts": 2.0,
            "legs": [{"to": 3, "snr": None}, {"to": 4, "snr": 2.0}],
        },
    ]
    plan = run(
        jsflow,
        f"meshgraphFlowPlan({json.dumps(routes)}, [1, 2, 3, 4],"
        " ['1-2', '1-3', '3-4'], MESHGRAPH_FLOW_PARAMS)",
    )
    p = plan["params"]
    fan, trace = plan["routes"]
    assert fan["startMs"] == 0
    assert trace["startMs"] == p["routeStaggerMs"]
    # Веер: ноги почти одновременно (небольшой разброс); трассировка: шаги
    # друг за другом в темпе hopMs.
    assert [leg["at"] for leg in fan["legs"]] == [0, p["fanSpreadMs"]]
    assert [leg["at"] for leg in trace["legs"]] == [0, p["hopMs"]]
    assert fan["durationMs"] == p["fanSpreadMs"] + p["hopMs"]
    assert trace["durationMs"] == 2 * p["hopMs"]
    assert (
        plan["totalMs"]
        == trace["startMs"] + trace["durationMs"] + p["legFadeMs"]
    )


def test_flow_frame_interpolates_and_fades(jsflow):
    routes = [
        {
            "kind": "fan",
            "sender": 1,
            "packet_id": 1,
            "ts": 1.0,
            "legs": [{"to": 2, "snr": 5.0}],
        }
    ]
    jsflow.eval(
        "var flowPlan = meshgraphFlowPlan("
        f"{json.dumps(routes)}, [1, 2], ['1-2'], MESHGRAPH_FLOW_PARAMS);"
    )
    jsflow.eval(
        "var flowNodes = new Map([[1, {x: 0, y: 0, z: 0}],"
        " [2, {x: 100, y: 50, z: 20}]]);"
    )
    hop = run(jsflow, "MESHGRAPH_FLOW_PARAMS.hopMs")

    # Старт: шарик в отправителе, нога светится в полную силу.
    frame = run(jsflow, "meshgraphFlowAt(flowPlan, 0, flowNodes)")
    assert len(frame["dots"]) == 1 and len(frame["legs"]) == 1
    assert (frame["dots"][0]["x"], frame["dots"][0]["y"]) == (0, 0)
    assert frame["legs"][0]["phase"] == 1
    assert not frame["done"]

    # Середина хопа — ровно середина отрезка (smoothstep(0.5) = 0.5).
    frame = run(jsflow, f"meshgraphFlowAt(flowPlan, {hop / 2}, flowNodes)")
    dot = frame["dots"][0]
    assert (dot["x"], dot["y"], dot["z"]) == (50, 25, 10)

    # Шарик дошёл (последний кадр хопа — ещё на точке прибытия, дальше его
    # уже нет): точки больше, но нога ещё горит и гаснет.
    frame = run(jsflow, f"meshgraphFlowAt(flowPlan, {hop + 1}, flowNodes)")
    assert frame["dots"] == []
    assert 0 < frame["legs"][0]["phase"] < 1
    assert not frame["done"]

    # Догорело: пустой кадр, план завершён.
    total = run(jsflow, "flowPlan.totalMs")
    frame = run(jsflow, f"meshgraphFlowAt(flowPlan, {total}, flowNodes)")
    assert frame["dots"] == [] and frame["legs"] == []
    assert frame["done"]


def test_flow_frame_drops_legs_whose_node_vanished(jsflow):
    routes = [
        {
            "kind": "fan",
            "sender": 1,
            "packet_id": 1,
            "ts": 1.0,
            "legs": [{"to": 2, "snr": 5.0}],
        }
    ]
    jsflow.eval(
        "var gonePlan = meshgraphFlowPlan("
        f"{json.dumps(routes)}, [1, 2], ['1-2'], MESHGRAPH_FLOW_PARAMS);"
    )
    jsflow.eval("var goneNodes = new Map([[1, {x: 0, y: 0, z: 0}]]);")
    frame = run(jsflow, "meshgraphFlowAt(gonePlan, 100, goneNodes)")
    assert frame["dots"] == [] and frame["legs"] == []


def test_packet_flow_wiring():
    """Кнопки, скрипт, endpoint и 3D-рендер собираются вместе."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="flowReplay"' in html
    assert 'id="flowPulse"' in html
    assert "packetflow.js" in html
    app = APP_JS.read_text(encoding="utf-8")
    assert "/api/packet_routes?" in app
    assert "meshgraphFlowPlan(" in app and "meshgraphFlowAt(" in app
    assert "flowApplyPulse()" in app
    three_d = GRAPH3D_JS.read_text(encoding="utf-8")
    # Экспорт renderFlow в window.meshgraph3D (кадры рисуются из app.js).
    assert "renderFlow," in three_d
