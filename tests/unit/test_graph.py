from __future__ import annotations

from scaletrace.graph import DB_NODE, build_knowledge_graph, endpoint_node, function_node, graph_from_json, graph_to_json, merge_runtime, risk_paths
from scaletrace.graph.html import render_graph_html
from scaletrace.pipeline import run_static_analysis
from scaletrace.schemas.runtime import EndpointRuntime, Measurement, ObservedEdge, RuntimeReport
from tests.conftest import EXAMPLES

SHOP = EXAMPLES / "shop_app"


def _shop_graph():
    static = run_static_analysis(SHOP)
    return static, build_knowledge_graph(static.project, static.report.findings)


def test_nodes_and_edges_for_shop_app():
    static, graph = _shop_graph()
    report_ep = endpoint_node("GET /customers/report")
    handler = function_node("app.routers.customers:customer_report")
    helper = function_node("app.services.orders:orders_for_customer")
    assert graph.has_edge(report_ep, handler, "routes_to")
    assert graph.has_edge(handler, helper, "calls")
    assert graph.edges[handler, helper, "calls"]["in_loop"] is True
    assert graph.has_edge(helper, DB_NODE, "queries")
    assert graph.has_edge(handler, function_node("app.db:get_db"), "depends_on")
    assert graph.has_node("resource:external:external service")
    assert graph.nodes[handler]["rules"] == ["DB-001"]


def test_risk_paths_cover_each_injected_risk():
    _, graph = _shop_graph()
    described = {p.endpoint: set(p.rules) for p in risk_paths(graph)}
    assert described["GET /customers/report"] == {"DB-001"}
    assert described["GET /customers/order-counts"] == {"DB-003"}
    assert {"MEM-001", "MEM-002", "CMP-001"} <= described["GET /catalog/search"]
    assert "GET /customers/order-counts-fast" not in described


def test_json_round_trip_preserves_structure():
    _, graph = _shop_graph()
    restored = graph_from_json(graph_to_json(graph))
    assert set(restored.nodes) == set(graph.nodes)
    assert sorted((u, v, k) for u, v, k in restored.edges(keys=True)) == sorted((u, v, k) for u, v, k in graph.edges(keys=True))


def test_merge_runtime_marks_and_adds_observed_edges():
    _, graph = _shop_graph()
    handler = "app.routers.customers:customer_report"
    helper = "app.services.orders:orders_for_customer"
    unseen = "app.services.orders:order_total"
    runtime = RuntimeReport(
        scaletrace_version="t", repository="shop", app="app.main:app", generated_at="now", status="completed",
        endpoints=[EndpointRuntime(endpoint="GET /customers/report", request_name="r", measurements=[
            Measurement(size=10, requests=1, errors=0, function_calls_per_request={handler: 1, helper: 10},
                        queries_by_function_per_request={helper: 10},
                        call_edges=[ObservedEdge(caller=handler, callee=helper, calls_per_request=10),
                                    ObservedEdge(caller=helper, callee=unseen, calls_per_request=1)]),
        ])],
    )
    merge_runtime(graph, runtime)
    edge = graph.edges[function_node(handler), function_node(helper), "calls"]
    assert edge["observed"] and edge["static"] and edge["observed_calls_per_request"] == 10
    added = graph.edges[function_node(helper), function_node(unseen), "calls"]
    assert added["observed"] and not added["static"]
    assert graph.nodes[function_node(helper)]["observed_queries_per_request"] == 10


def test_html_view_renders():
    static, graph = _shop_graph()
    page = render_graph_html(graph, static.report.findings, "shop")
    assert "vis-network@9.1.9" in page
    assert "customer_report" in page
    assert page.count("</script>") == 2  # the CDN tag and the inline script; embedded data is escaped
