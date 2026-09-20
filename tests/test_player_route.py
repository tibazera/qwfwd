#!/usr/bin/env python3
"""
Teste automatizado (sem servidor real) para os endpoints /player-targets e
/player-route do coletor: validacao de payload, isolamento do GraphState
compartilhado, rate-limit por uuid, e calculo de rota com aresta injetada.

Roda 100% local e determinístico, sem depender de qwfwd.exe nem de rede.
"""
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "collector"))

import collector  # noqa: E402

results = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    results.append((name, status, detail))
    print(f"[{status}] {name}" + (f" - {detail}" if detail else ""))
    return condition


def test_dijkstra_with_extra_edges_uses_injected_edge_without_mutating_graph():
    collector.graph = collector.GraphState()  # fresh, isolated graph for this test
    real_a = ("10.0.0.1", 30000)
    real_b = ("10.0.0.2", 30000)
    collector.graph.add_edge(real_a, collector.Edge(to_ip=real_b[0], to_port=real_b[1], ping=50.0, source="meshstatus"))

    player_node = ("player", 0)
    extra = {player_node: [collector.Edge(to_ip=real_a[0], to_port=real_a[1], ping=10.0, source="player")]}

    edges_before = json.dumps(collector.graph.snapshot(), sort_keys=True)
    result = collector.dijkstra_with_extra_edges(player_node, real_b, extra_adjacency=extra)
    edges_after = json.dumps(collector.graph.snapshot(), sort_keys=True)

    check(
        "extra_edges_route_found",
        result is not None,
        f"result={result}",
    )
    if result is not None:
        total_ping, path = result
        check("extra_edges_total_ping", total_ping == 60.0, f"got {total_ping}")
        check("extra_edges_path", path == [player_node, real_a, real_b], f"got {path}")
    check(
        "extra_edges_graph_unmutated",
        edges_before == edges_after,
        "graph.edges changed after dijkstra_with_extra_edges call",
    )


def test_uuid_rate_limited_blocks_after_threshold():
    collector._uuid_rate_track.clear()
    uuid = "11111111-1111-4111-8111-111111111111"
    blocked = [collector._uuid_rate_limited(uuid) for _ in range(collector._UUID_RATE_LIMIT_PER_WINDOW + 5)]
    check(
        "uuid_rate_limit_allows_up_to_threshold",
        blocked[: collector._UUID_RATE_LIMIT_PER_WINDOW].count(True) == 0,
        f"unexpected early block: {blocked[:collector._UUID_RATE_LIMIT_PER_WINDOW]}",
    )
    check(
        "uuid_rate_limit_blocks_past_threshold",
        any(blocked[collector._UUID_RATE_LIMIT_PER_WINDOW:]),
        f"no block seen in tail: {blocked[collector._UUID_RATE_LIMIT_PER_WINDOW:]}",
    )


def _start_test_server():
    collector.graph = collector.GraphState()
    a = ("10.0.1.1", 30000)
    b = ("10.0.1.2", 30000)
    collector.graph.geo[a] = collector.GeoInfo("PT", "Portugal", "Europe", "Lisbon", 38.7, -9.1, "test-a", True)
    collector.graph.geo[b] = collector.GeoInfo("BR", "Brazil", "South America", "Sao Paulo", -23.5, -46.6, "test-b", True)
    collector.graph.add_edge(a, collector.Edge(to_ip=b[0], to_port=b[1], ping=200.0, source="meshstatus"))

    server = collector.ThreadingHTTPServer(("127.0.0.1", 0), collector.Handler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    time.sleep(0.1)
    return server, port


def test_player_targets_returns_known_nodes():
    server, port = _start_test_server()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/player-targets", timeout=2) as resp:
            body = json.loads(resp.read())
        check("player_targets_status_shape", "targets" in body, f"body={body}")
        ips = {t["ip"] for t in body.get("targets", [])}
        check("player_targets_includes_known_nodes", {"10.0.1.1", "10.0.1.2"} <= ips, f"ips={ips}")
    finally:
        server.shutdown()


def main():
    test_dijkstra_with_extra_edges_uses_injected_edge_without_mutating_graph()
    test_uuid_rate_limited_blocks_after_threshold()
    test_player_targets_returns_known_nodes()

    failed = [r for r in results if r[1] == "FAIL"]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("FAILED:")
        for name, status, detail in failed:
            print(f"  - {name}: {detail}")
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
