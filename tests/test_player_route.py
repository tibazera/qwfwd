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


def _post_json(port, path, payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_player_route_happy_path_and_isolation():
    server, port = _start_test_server()
    try:
        a = "10.0.1.1"
        b = "10.0.1.2"
        edges_before = json.dumps(collector.graph.snapshot(), sort_keys=True)

        status, body = _post_json(
            port, f"/player-route?to={b}:30000",
            {"uuid": "22222222-2222-4222-8222-222222222222",
             "samples": [{"ip": a, "port": 30000, "rtt_ms": 15.0}]},
        )
        check("player_route_status_200", status == 200, f"status={status} body={body}")
        if status == 200:
            check("player_route_total_ping", body.get("total_ping_ms") == 215.0, f"body={body}")
            check("player_route_hops", body.get("hops") == 1, f"body={body}")

        edges_after = json.dumps(collector.graph.snapshot(), sort_keys=True)
        check("player_route_isolation", edges_before == edges_after, "graph mutated by /player-route")
    finally:
        server.shutdown()


def test_player_route_rejects_bad_payload():
    server, port = _start_test_server()
    try:
        status, _ = _post_json(port, "/player-route?to=10.0.1.2:30000", {"uuid": "not-a-uuid", "samples": []})
        check("player_route_rejects_missing_samples", status == 400, f"status={status}")

        status, _ = _post_json(
            port, "/player-route?to=10.0.1.2:30000",
            {"uuid": "33333333-3333-4333-8333-333333333333",
             "samples": [{"ip": "203.0.113.9", "port": 1, "rtt_ms": 10.0}]},  # unknown target
        )
        check("player_route_rejects_unknown_target", status == 400, f"status={status}")

        status, _ = _post_json(
            port, "/player-route?to=10.0.1.2:30000",
            {"uuid": "44444444-4444-4444-8444-444444444444",
             "samples": [{"ip": "10.0.1.1", "port": 30000, "rtt_ms": 999999}]},  # out of range, dropped -> no samples left
        )
        check("player_route_all_samples_dropped_yields_404_or_400", status in (400, 404), f"status={status}")
    finally:
        server.shutdown()


def main():
    test_dijkstra_with_extra_edges_uses_injected_edge_without_mutating_graph()
    test_uuid_rate_limited_blocks_after_threshold()
    test_player_targets_returns_known_nodes()
    test_player_route_happy_path_and_isolation()
    test_player_route_rejects_bad_payload()

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
