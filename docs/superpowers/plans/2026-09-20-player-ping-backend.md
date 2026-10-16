# Player Ping Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add two HTTP endpoints to `collector/collector.py` — `/player-targets` (candidate server list for a player) and `/player-route` (route calculated from player-reported ping samples) — without ever writing player samples into the shared `GraphState`.

**Architecture:** Both endpoints live in the existing `Handler.do_GET`/new `do_POST` of `collector/collector.py`, reusing `graph` (read-only), `dijkstra()`, `parse_addr_param()`, `_geo_to_dict()`, and `_rate_limited()` already in the file. `/player-route` builds a per-request adjacency snapshot (existing graph edges + synthetic player edges) and calls a new `dijkstra_with_extra_edges()` helper — `dijkstra()` itself stays untouched so every existing endpoint keeps its current behavior byte-for-byte.

**Tech Stack:** Python 3, stdlib only (`http.server`, `json`, `heapq`) — matches the rest of `collector/collector.py`. No new dependency.

**Spec:** `docs/superpowers/specs/2026-09-20-player-ping-app-design.md`

## Global Constraints

- No new dependency — stdlib only, matching the rest of `collector/collector.py`.
- Never write player-reported samples into `graph` (the shared `GraphState`) — isolation by design, per spec.
- Reuse `_rate_limited(ip)` (per-IP) for both new endpoints; add a separate per-UUID limiter for `/player-route` only.
- `/player-route` samples: reject `rtt_ms` outside `0 < rtt_ms <= 2000` (drop the sample, don't reject the whole request); cap `samples` list at 50 entries (reject the whole request if exceeded); every sample's `(ip, port)` must already exist as a key in `graph.edges` or `graph.geo` (reject unknown targets — this endpoint is not a generic ping oracle).
- Error responses stay minimal (`{"error": "..."}`, no stack traces, no per-field validation detail) — matches existing endpoints' style in `collector.py`.
- Tests are standalone `assert`-based scripts matching `tests/test_mesh_protocol.py`'s style (no pytest in this repo) — run directly with `python`, exit non-zero on failure.

---

## File Structure

- **Modify: `collector/collector.py`**
  - Add `dijkstra_with_extra_edges()` near existing `dijkstra()` (~line 402): same algorithm, but takes an optional `extra_adjacency: dict[tuple, list[Edge]]` merged into the snapshot before running.
  - Add `_uuid_rate_limited(uuid)` near existing `_rate_limited()` (~line 502): same sliding-window-counter pattern, separate tracking dict.
  - Add `GET /player-targets` handling in `Handler.do_GET` (~near `/routes-to`).
  - Add `Handler.do_POST` (doesn't exist yet) handling `POST /player-route`.
- **Create: `tests/test_player_route.py`** — standalone assert-based script, no live qwfwd process needed (pure in-process test against the collector's Python functions + a spun-up `HTTPServer` on a test port), matching `tests/test_mesh_protocol.py`'s `check()`/`results` pattern.

---

### Task 1: `dijkstra_with_extra_edges()` — routing with injected player edges, graph untouched

**Files:**
- Modify: `collector/collector.py` (add function after `dijkstra()`, ~line 475)
- Test: `tests/test_player_route.py` (new file)

**Interfaces:**
- Consumes: `Edge` dataclass (existing, `collector.py:89`), `graph.edges` (existing `GraphState.edges`, read via `graph.lock`).
- Produces: `dijkstra_with_extra_edges(start: tuple[str,int], end: tuple[str,int], extra_adjacency: dict[tuple[str,int], list[Edge]] | None = None) -> tuple[float, list[tuple[str,int]]] | None` — same return shape as `dijkstra()`. Task 3 and Task 4 call this with a synthetic player node as `start`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_player_route.py`:

```python
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


def main():
    test_dijkstra_with_extra_edges_uses_injected_edge_without_mutating_graph()

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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python tests/test_player_route.py`
Expected: `AttributeError: module 'collector' has no attribute 'dijkstra_with_extra_edges'`

- [ ] **Step 3: Write minimal implementation**

In `collector/collector.py`, immediately after the existing `dijkstra()` function (after its closing `return raw_ping[end_state], path` at line 474), add:

```python
def dijkstra_with_extra_edges(
    start: tuple[str, int],
    end: tuple[str, int],
    extra_adjacency: dict[tuple[str, int], list[Edge]] | None = None,
) -> tuple[float, list[tuple[str, int]]] | None:
    """Same algorithm as dijkstra(), but merges extra_adjacency (e.g. a
    synthetic player node's self-measured edges) into the snapshot before
    running. Never touches `graph` - extra_adjacency lives only for the
    duration of this call, so player-reported samples never become part of
    the shared mesh (see spec: isolation by design, no poisoning surface)."""
    with graph.lock:
        adjacency = {k: list(v) for k, v in graph.edges.items()}
    if extra_adjacency:
        for node, edges in extra_adjacency.items():
            adjacency.setdefault(node, []).extend(edges)

    start_state = (start, 0)
    dist: dict[tuple[tuple[str, int], int], float] = {start_state: 0.0}
    raw_ping: dict[tuple[tuple[str, int], int], float] = {start_state: 0.0}
    prev: dict[tuple[tuple[str, int], int], tuple[tuple[str, int], int]] = {}
    visited: set[tuple[tuple[str, int], int]] = set()
    pq: list[tuple[float, tuple[str, int], int]] = [(0.0, start, 0)]
    end_state: tuple[tuple[str, int], int] | None = None

    while pq:
        d, node, hops = heapq.heappop(pq)
        state = (node, hops)
        if state in visited:
            continue
        visited.add(state)
        if node == end:
            end_state = state
            break
        if node[0] == end[0]:
            local_end_state = (end, hops + 1)
            dist[local_end_state] = d
            raw_ping[local_end_state] = raw_ping[state]
            prev[local_end_state] = state
            end_state = local_end_state
            break
        if hops >= ROUTE_MAX_HOPS:
            continue
        for edge in adjacency.get(node, []):
            if edge.age_seconds > ROUTE_MAX_EDGE_AGE_SECONDS:
                continue
            neighbor = (edge.to_ip, edge.to_port)
            next_state = (neighbor, hops + 1)
            jitter = float(edge.jitter or 0)
            loss = float(edge.loss_pct or 0)
            edge_cost = (edge.ping + ROUTE_JITTER_WEIGHT * jitter
                         + ROUTE_LOSS_WEIGHT_MS * loss)
            if neighbor != end:
                edge_cost += ROUTE_RELAY_PENALTY_MS
            nd = d + edge_cost
            if next_state not in dist or nd < dist[next_state]:
                dist[next_state] = nd
                raw_ping[next_state] = raw_ping[state] + edge.ping
                prev[next_state] = state
                heapq.heappush(pq, (nd, neighbor, hops + 1))

    if end_state is None:
        return None

    path = [end_state[0]]
    seen = {end_state}
    state = end_state
    while state != start_state:
        nxt = prev.get(state)
        if nxt is None or nxt in seen:
            return None
        seen.add(nxt)
        path.append(nxt[0])
        state = nxt
    path.reverse()
    return raw_ping[end_state], path
```

Note: `extra_adjacency` edges skip the `age_seconds` staleness check implicitly because `Edge.age_seconds` defaults to `0` — a freshly-built synthetic edge is always "fresh".

- [ ] **Step 4: Run test to verify it passes**

Run: `python tests/test_player_route.py`
Expected: `3/3 passed`

- [ ] **Step 5: Commit**

```bash
git add collector/collector.py tests/test_player_route.py
git commit -m "feat(mesh): add dijkstra_with_extra_edges for player-injected routes

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 2: `_uuid_rate_limited()` — per-UUID rate limiting

**Files:**
- Modify: `collector/collector.py` (add function after `_rate_limited()`, ~line 511)
- Test: `tests/test_player_route.py` (extend)

**Interfaces:**
- Consumes: nothing new (same sliding-window pattern as existing `_rate_limited`).
- Produces: `_uuid_rate_limited(uuid: str) -> bool` — Task 4's `/player-route` handler calls this alongside the existing `_rate_limited(ip)`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_player_route.py`, a new test function (called from `main()`):

```python
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
```

And add the call in `main()`:

```python
def main():
    test_dijkstra_with_extra_edges_uses_injected_edge_without_mutating_graph()
    test_uuid_rate_limited_blocks_after_threshold()
    ...
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python tests/test_player_route.py`
Expected: `AttributeError: module 'collector' has no attribute '_uuid_rate_track'`

- [ ] **Step 3: Write minimal implementation**

In `collector/collector.py`, immediately after the existing `_rate_limited()` function (after line 510), add:

```python
_UUID_RATE_LIMIT_PER_WINDOW = 10  # requests/window/uuid - tighter than per-IP
                                    # since one NAT'd IP can legitimately host
                                    # several players, but one uuid is one app
                                    # instance and has no reason to burst
_uuid_rate_lock = threading.Lock()
_uuid_rate_track: dict[str, tuple[float, int]] = {}


def _uuid_rate_limited(uuid: str) -> bool:
    now = time.time()
    with _uuid_rate_lock:
        window_start, count = _uuid_rate_track.get(uuid, (now, 0))
        if now - window_start >= _RATE_LIMIT_WINDOW:
            window_start, count = now, 0
        count += 1
        _uuid_rate_track[uuid] = (window_start, count)
        return count > _UUID_RATE_LIMIT_PER_WINDOW
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python tests/test_player_route.py`
Expected: `5/5 passed`

- [ ] **Step 5: Commit**

```bash
git add collector/collector.py tests/test_player_route.py
git commit -m "feat(mesh): add per-uuid rate limiting for player telemetry

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 3: `GET /player-targets` endpoint

**Files:**
- Modify: `collector/collector.py` (add handling inside `Handler.do_GET`, near the existing `/routes-to` block ~line 717)
- Test: `tests/test_player_route.py` (extend)

**Interfaces:**
- Consumes: `graph.geo` (existing `GraphState.geo`, `dict[tuple[str,int], GeoInfo]`), `graph.edges` (existing), `_geo_to_dict()` (existing, `collector.py:912`), `_rate_limited()` (existing).
- Produces: HTTP `GET /player-targets?ip=<optional>` → JSON `{"targets": [{"ip": str, "port": int, "geo": <_geo_to_dict output or null>}]}`. The C# app (Task 5 of the app plan) consumes this shape directly.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_player_route.py`:

```python
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
```

Add the call in `main()`:

```python
def main():
    test_dijkstra_with_extra_edges_uses_injected_edge_without_mutating_graph()
    test_uuid_rate_limited_blocks_after_threshold()
    test_player_targets_returns_known_nodes()
    ...
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python tests/test_player_route.py`
Expected: `player_targets_status_shape` FAILs (404 `{"error": "not found"}` from the catch-all).

- [ ] **Step 3: Write minimal implementation**

In `collector/collector.py`, inside `Handler.do_GET`, add this block right before the existing `if parsed.path == "/snapshot":` line (~859), so it sits alongside the other GET routes:

```python
        if parsed.path == "/player-targets":
            # Candidate list for a player's client-side ping app: every
            # node we currently know geo for (proxies + plain game
            # servers), capped so a player app never has to probe the
            # full ~354-node universe on every request (spec: "só
            # servidores/proxies relevantes pro jogador").
            with graph.lock:
                targets = [
                    {"ip": ip, "port": port, "geo": _geo_to_dict(info)}
                    for (ip, port), info in graph.geo.items()
                ]
            targets = targets[:200]
            self._send_json({"targets": targets})
            return

```

Note: region-based filtering by request IP is deferred — `targets[:200]` is the simplest correct cap for v1 (ponytail: no geo-distance filter yet, add when `/player-targets` payload size or player complaints justify it).

- [ ] **Step 4: Run test to verify it passes**

Run: `python tests/test_player_route.py`
Expected: `7/7 passed`

- [ ] **Step 5: Commit**

```bash
git add collector/collector.py tests/test_player_route.py
git commit -m "feat(mesh): add GET /player-targets endpoint

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 4: `POST /player-route` endpoint

**Files:**
- Modify: `collector/collector.py` (add `Handler.do_POST`, plus a `_read_json_body` helper)
- Test: `tests/test_player_route.py` (extend)

**Interfaces:**
- Consumes: `dijkstra_with_extra_edges()` (Task 1), `_rate_limited()` (existing), `_uuid_rate_limited()` (Task 2), `graph.edges`/`graph.geo` (existing, for target validation), `_geo_to_dict()` (existing), `parse_addr_param()` (existing, reused for the optional `to` query param).
- Produces: HTTP `POST /player-route?to=ip:port` with JSON body `{"uuid": str, "samples": [{"ip": str, "port": int, "rtt_ms": float}, ...]}` → `200 {"to": str, "total_ping_ms": float, "hops": int, "path": [str], "path_geo": [...]}`, or `400`/`404`/`429` on error. Nothing else consumes this in the backend; the C# app (app plan, Task 6) is the only caller.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_player_route.py`:

```python
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
            check("player_route_hops", body.get("hops") == 2, f"body={body}")

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
```

Add both calls in `main()`:

```python
def main():
    test_dijkstra_with_extra_edges_uses_injected_edge_without_mutating_graph()
    test_uuid_rate_limited_blocks_after_threshold()
    test_player_targets_returns_known_nodes()
    test_player_route_happy_path_and_isolation()
    test_player_route_rejects_bad_payload()
    ...
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python tests/test_player_route.py`
Expected: connection error or 501 (no `do_POST` defined yet, `BaseHTTPRequestHandler` default returns 501).

- [ ] **Step 3: Write minimal implementation**

In `collector/collector.py`, add a small body-reading helper right before `class Handler` (~line 513), and add `do_POST` as a method on `Handler` right after `do_GET` ends (after its final `self._send_json({"error": "not found"}, status=404)` at line 909, still inside the class — so indent at the same level as `do_GET`):

Helper (before `class Handler`):

```python
def _read_json_body(handler: BaseHTTPRequestHandler, max_bytes: int = 65536) -> dict | None:
    try:
        length = int(handler.headers.get("Content-Length", "0"))
    except ValueError:
        return None
    if length <= 0 or length > max_bytes:
        return None
    try:
        raw = handler.rfile.read(length)
        return json.loads(raw)
    except (json.JSONDecodeError, OSError):
        return None


_UUID_PATTERN_LEN = 36  # "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"


def _valid_uuid(value: object) -> bool:
    if not isinstance(value, str) or len(value) != _UUID_PATTERN_LEN:
        return False
    parts = value.split("-")
    return len(parts) == 5 and [len(p) for p in parts] == [8, 4, 4, 4, 12]
```

`do_POST` method (inside `class Handler`, same indentation as `do_GET`):

```python
    def do_POST(self) -> None:
        if _rate_limited(self.client_address[0]):
            self._send_json({"error": "rate limited"}, status=429)
            return

        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)

        if parsed.path != "/player-route":
            self._send_json({"error": "not found"}, status=404)
            return

        to_addr = parse_addr_param(qs.get("to", [""])[0])
        if not to_addr:
            self._send_json({"error": "usage: POST /player-route?to=ip:port"}, status=400)
            return

        body = _read_json_body(self)
        if body is None:
            self._send_json({"error": "invalid or missing JSON body"}, status=400)
            return

        uuid = body.get("uuid")
        if not _valid_uuid(uuid):
            self._send_json({"error": "missing or malformed uuid"}, status=400)
            return

        if _uuid_rate_limited(uuid):
            self._send_json({"error": "rate limited"}, status=429)
            return

        samples = body.get("samples")
        if not isinstance(samples, list) or not samples or len(samples) > 50:
            self._send_json({"error": "samples must be a non-empty list, max 50 entries"}, status=400)
            return

        with graph.lock:
            known_nodes = set(graph.edges.keys()) | set(graph.geo.keys())

        player_node = ("player", 0)
        extra_edges: list[Edge] = []
        for sample in samples:
            if not isinstance(sample, dict):
                continue
            ip = sample.get("ip")
            port = sample.get("port")
            rtt_ms = sample.get("rtt_ms")
            if not isinstance(ip, str) or not isinstance(port, int):
                continue
            if not isinstance(rtt_ms, (int, float)) or not (0 < rtt_ms <= 2000):
                continue
            target = (ip, port)
            if target not in known_nodes:
                continue
            extra_edges.append(Edge(to_ip=ip, to_port=port, ping=float(rtt_ms), source="player"))

        if not extra_edges:
            self._send_json({"error": "no valid samples (all rejected or targets unknown)"}, status=400)
            return

        result = dijkstra_with_extra_edges(player_node, to_addr, extra_adjacency={player_node: extra_edges})
        if result is None:
            self._send_json({"error": "no route found", "to": qs.get("to", [""])[0]}, status=404)
            return

        total_ping, path = result
        path = path[1:]  # drop the synthetic player_node from the response path
        with graph.lock:
            path_geo = [_geo_to_dict(graph.geo.get(addr)) for addr in path]
        self._send_json(
            {
                "to": qs.get("to", [""])[0],
                "total_ping_ms": total_ping,
                "hops": len(path) - 1,
                "path": [f"{ip}:{port}" for ip, port in path],
                "path_geo": path_geo,
            }
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python tests/test_player_route.py`
Expected: `13/13 passed`

- [ ] **Step 5: Commit**

```bash
git add collector/collector.py tests/test_player_route.py
git commit -m "feat(mesh): add POST /player-route endpoint

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 5: Wire endpoints into startup log line

**Files:**
- Modify: `collector/collector.py:939-943` (the `print` in `main()`)

**Interfaces:**
- Consumes: nothing.
- Produces: nothing consumed by other tasks — this is a one-line documentation/observability fix so `/player-targets` and `/player-route` show up in the same startup banner as every other endpoint.

- [ ] **Step 1: Update the print statement**

In `collector/collector.py`, change:

```python
    print(
        "[collector] serving on :8730 "
        "(/route, /routes-to, /top-routes, /estimate-route, /compare, /client-ping, /snapshot, /geo, /health)"
    )
```

to:

```python
    print(
        "[collector] serving on :8730 "
        "(/route, /routes-to, /top-routes, /estimate-route, /compare, /client-ping, /snapshot, /geo, /health, "
        "/player-targets, /player-route)"
    )
```

- [ ] **Step 2: Verify by running the collector briefly**

Run: `cd collector && timeout 3 python collector.py || true` (or on Windows PowerShell, start it and Ctrl+C after the banner prints)
Expected: banner line includes `/player-targets, /player-route`.

- [ ] **Step 3: Commit**

```bash
git add collector/collector.py
git commit -m "chore(mesh): list player-targets/player-route in startup banner

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Final Verification

- [ ] Run the full test file once more end to end: `python tests/test_player_route.py` — expect all checks `PASS`, exit code `0`.
- [ ] Run existing test suites to confirm no regression: `python tests/test_mesh_protocol.py` (requires `QWFWD_EXE` built; skip if binary unavailable and note it) and any other `tests/test_*.py` that don't require the compiled binary.
- [ ] Manually smoke-test against a live-running collector (`python collector/collector.py`) with `curl`:
  - `curl "http://127.0.0.1:8730/player-targets"` → non-empty `targets` list once a collection cycle has run.
  - `curl -X POST "http://127.0.0.1:8730/player-route?to=<known ip:port>" -d '{"uuid":"<uuid4>","samples":[{"ip":"<known ip>","port":<known port>,"rtt_ms":20}]}'` → `200` with a `path`.
