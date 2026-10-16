#!/usr/bin/env python3
"""
Teste automatizado para o cache de samples por uuid usado por
/player-route-cached (Task 3): armazenamento, leitura, e expiração por
TTL. Roda 100% local e determinístico, sem depender de qwfwd.exe nem de
rede.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "collector"))

import collector  # noqa: E402

results = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    results.append((name, status, detail))
    print(f"[{status}] {name}" + (f" - {detail}" if detail else ""))
    return condition


def test_store_and_get_roundtrip():
    collector._player_samples.clear()
    uuid = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    samples = [{"ip": "10.0.0.1", "port": 30000, "rtt_ms": 42.0}]

    collector._store_player_samples(uuid, samples)
    got = collector._get_cached_samples(uuid)

    check("cache_roundtrip_returns_stored_samples", got == samples, f"got={got}")


def test_get_unknown_uuid_returns_none():
    collector._player_samples.clear()
    got = collector._get_cached_samples("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    check("cache_unknown_uuid_returns_none", got is None, f"got={got}")


def test_get_expired_entry_returns_none():
    collector._player_samples.clear()
    uuid = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    samples = [{"ip": "10.0.0.1", "port": 30000, "rtt_ms": 42.0}]
    # simulate an entry stored PLAYER_SAMPLES_TTL_SECONDS + 1 ago
    stale_timestamp = collector.time.time() - collector.PLAYER_SAMPLES_TTL_SECONDS - 1
    with collector._player_samples_lock:
        collector._player_samples[uuid] = (stale_timestamp, samples)

    got = collector._get_cached_samples(uuid)
    check("cache_expired_entry_returns_none", got is None, f"got={got}")


def test_store_overwrites_previous_entry_for_same_uuid():
    collector._player_samples.clear()
    uuid = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
    collector._store_player_samples(uuid, [{"ip": "10.0.0.1", "port": 30000, "rtt_ms": 10.0}])
    collector._store_player_samples(uuid, [{"ip": "10.0.0.2", "port": 30000, "rtt_ms": 20.0}])

    got = collector._get_cached_samples(uuid)
    check(
        "cache_store_overwrites_same_uuid",
        got == [{"ip": "10.0.0.2", "port": 30000, "rtt_ms": 20.0}],
        f"got={got}",
    )


def test_post_player_route_populates_cache():
    collector._player_samples.clear()
    collector.graph = collector.GraphState()
    a = ("10.0.2.1", 30000)
    b = ("10.0.2.2", 30000)
    collector.graph.add_edge(a, collector.Edge(to_ip=b[0], to_port=b[1], ping=100.0, source="meshstatus"))

    server = collector.ThreadingHTTPServer(("127.0.0.1", 0), collector.Handler)
    port = server.server_address[1]
    t = collector.threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    collector.time.sleep(0.1)

    try:
        uuid = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
        req_body = collector.json.dumps(
            {"uuid": uuid, "samples": [{"ip": "10.0.2.1", "port": 30000, "rtt_ms": 5.0}]}
        ).encode()
        req = collector.urllib.request.Request(
            f"http://127.0.0.1:{port}/player-route?to=10.0.2.2:30000",
            data=req_body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with collector.urllib.request.urlopen(req, timeout=2) as resp:
            resp.read()

        cached = collector._get_cached_samples(uuid)
        check(
            "post_player_route_populates_cache",
            cached == [{"ip": "10.0.2.1", "port": 30000, "rtt_ms": 5.0}],
            f"cached={cached}",
        )
    finally:
        server.shutdown()


def test_cached_route_endpoint_happy_path():
    collector._player_samples.clear()
    collector.graph = collector.GraphState()
    a = ("10.0.3.1", 30000)
    b = ("10.0.3.2", 30000)
    collector.graph.add_edge(a, collector.Edge(to_ip=b[0], to_port=b[1], ping=100.0, source="meshstatus"))

    uuid = "ffffffff-ffff-4fff-8fff-ffffffffffff"
    collector._store_player_samples(uuid, [{"ip": "10.0.3.1", "port": 30000, "rtt_ms": 5.0}])

    server = collector.ThreadingHTTPServer(("127.0.0.1", 0), collector.Handler)
    port = server.server_address[1]
    t = collector.threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    collector.time.sleep(0.1)

    try:
        with collector.urllib.request.urlopen(
            f"http://127.0.0.1:{port}/player-route-cached?uuid={uuid}&to=10.0.3.2:30000", timeout=2
        ) as resp:
            status = resp.status
            body = collector.json.loads(resp.read())
        check("cached_route_status_200", status == 200, f"status={status} body={body}")
        check("cached_route_total_ping", body.get("total_ping_ms") == 105.0, f"body={body}")
        check("cached_route_hops", body.get("hops") == 1, f"body={body}")
    finally:
        server.shutdown()


def test_cached_route_unknown_uuid_returns_404():
    collector._player_samples.clear()
    collector.graph = collector.GraphState()

    server = collector.ThreadingHTTPServer(("127.0.0.1", 0), collector.Handler)
    port = server.server_address[1]
    t = collector.threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    collector.time.sleep(0.1)

    try:
        try:
            collector.urllib.request.urlopen(
                f"http://127.0.0.1:{port}/player-route-cached?uuid=00000000-0000-4000-8000-000000000000&to=10.0.3.2:30000",
                timeout=2,
            )
            check("cached_route_unknown_uuid_404", False, "expected HTTPError, got success")
        except collector.urllib.error.HTTPError as e:
            check("cached_route_unknown_uuid_404", e.code == 404, f"status={e.code}")
    finally:
        server.shutdown()


def test_cached_route_malformed_params_return_400():
    server = collector.ThreadingHTTPServer(("127.0.0.1", 0), collector.Handler)
    port = server.server_address[1]
    t = collector.threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    collector.time.sleep(0.1)

    try:
        try:
            collector.urllib.request.urlopen(
                f"http://127.0.0.1:{port}/player-route-cached?uuid=not-a-uuid&to=10.0.3.2:30000", timeout=2
            )
            check("cached_route_bad_uuid_400", False, "expected HTTPError, got success")
        except collector.urllib.error.HTTPError as e:
            check("cached_route_bad_uuid_400", e.code == 400, f"status={e.code}")

        try:
            collector.urllib.request.urlopen(
                f"http://127.0.0.1:{port}/player-route-cached?uuid=00000000-0000-4000-8000-000000000000&to=not-an-addr",
                timeout=2,
            )
            check("cached_route_bad_addr_400", False, "expected HTTPError, got success")
        except collector.urllib.error.HTTPError as e:
            check("cached_route_bad_addr_400", e.code == 400, f"status={e.code}")
    finally:
        server.shutdown()


def main():
    test_store_and_get_roundtrip()
    test_get_unknown_uuid_returns_none()
    test_get_expired_entry_returns_none()
    test_store_overwrites_previous_entry_for_same_uuid()
    test_post_player_route_populates_cache()
    test_cached_route_endpoint_happy_path()
    test_cached_route_unknown_uuid_returns_404()
    test_cached_route_malformed_params_return_400()

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
