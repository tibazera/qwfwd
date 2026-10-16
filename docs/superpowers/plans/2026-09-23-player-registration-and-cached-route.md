# Player Registration + Cached Route Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the public site reuse ping samples the app already sends to the collector, via a persistent per-uuid cache and a nick/country/city registration that links the site and the app to the same stable uuid.

**Architecture:** Collector gains an in-memory TTL cache keyed by uuid (populated by the existing `POST /player-route`, read by a new `GET /player-route-cached`) and a small JSON-file-backed player registry (`POST /player-register`, `POST /player-link`). The app imports the site-issued uuid on link instead of the site adopting the app's uuid, so the uuid never changes after registration. The end-to-end route math (`dijkstra_with_extra_edges`) is untouched.

**Tech Stack:** Python 3 stdlib (`http.server`, `json`, `uuid`, `secrets`, `threading`) for the collector; C#/.NET 8 + Avalonia for the app; vanilla JS in `index.html` for the site.

**Spec:** `docs/superpowers/specs/2026-09-23-player-registration-and-cached-route-design.md`

## Global Constraints

- Cache TTL: 120 seconds (`PLAYER_SAMPLES_TTL_SECONDS`), per spec section "Coletor: cache de samples por uuid".
- Link code TTL: 15 minutes (`PLAYER_LINK_CODE_TTL_SECONDS`), single-use, per spec section "Coletor: registro de jogador".
- Link code format: 6 numeric digits, per spec.
- Nick: 1-24 chars; country/city: 1-64 chars each, free text, per spec.
- No new dependencies: registry persists to a flat JSON file (`collector/players.json`), no database.
- uuid format matches existing `_valid_uuid` (36-char, 5 dash-separated groups of 8-4-4-4-12) — reuse that function, don't write a second validator.
- The site's uuid (issued at registration) is the one and only stable identity; the app overwrites its local uuid to match on link, never the reverse.
- Tests follow the existing repo convention: standalone script with `check()`/`main()`, no pytest (see `tests/test_player_route.py`).
- No ranking/leaderboard UI, no authentication — out of scope per spec.

## Review Focus

- **Cache read after TTL expiry returns 404, not stale data** — a person who scanned 3 minutes ago and closed the app should get the honest "no cached samples" so the site falls through to STUN/estimate, not a wrong stale ping.
- **Link code reuse (double-redeem) must fail the second time** — someone accidentally clicking "Vincular" twice, or an attacker replaying a captured code, must not silently re-link or leak the registration to a second uuid.
- **Malformed/missing uuid on the new GET endpoint must 400, not 500** — same posture as every other unauthenticated endpoint in this file; a crash here takes down the whole `ThreadingHTTPServer` request thread pool's error budget.
- **`/player-register` with an empty or whitespace-only nick must be rejected**, not stored as a blank registration that then shows up confusingly in future UI.
- **Concurrent registration + link across two threads must not corrupt `players.json`** — `ThreadingHTTPServer` serves both endpoints on separate threads; the read-modify-write to the file needs the same lock discipline as the in-memory dict, or two near-simultaneous registrations can lose one.

---

## Task 1: Extract shared route-from-samples helper (refactor, no behavior change)

**Files:**
- Modify: `collector/collector.py` (extract from existing `do_POST` handler, lines ~1120-1192)
- Test: `tests/test_player_route.py` (existing test must still pass unmodified — this task is a pure refactor)

**Interfaces:**
- Produces: `_route_from_samples(uuid: str, samples: list, to_addr: tuple[str, int]) -> dict` — returns the same dict shape currently built inline in `do_POST`'s `/player-route` branch: `{"to": ..., "total_ping_ms": ..., "hops": ..., "path": [...], "path_geo": [...]}` on success, or `None` if no valid samples / no route found (caller decides the HTTP status for each `None` case, since POST and the future cached-GET return different statuses for "no samples" vs "no route").

This task exists so Task 3 (`/player-route-cached`) doesn't duplicate the ~30 lines of sample validation + dijkstra call. Extracting it first, under the existing test suite, is the safe order — Task 2 (cache write) and Task 3 (cache read) both build on top of an already-verified-unchanged behavior.

- [ ] **Step 1: Read the current `do_POST` handler to confirm line range**

Run: `grep -n "if parsed.path != \"/player-route\"" collector/collector.py`

Expected: a single match inside `do_POST`, confirming the branch to extract starts right after this line and ends before the final `self._send_json(...)` call that builds the success response.

- [ ] **Step 2: Run the existing test suite to capture the current passing baseline**

Run: `python tests/test_player_route.py`
Expected: `5/5 passed` (all tests currently in the file pass before any change).

- [ ] **Step 3: Write `_route_from_samples`, extracting the validation+dijkstra logic**

Add this function above the `Handler` class (right after `dijkstra_with_extra_edges`, since it's the direct caller):

```python
def _route_from_samples(
    uuid: str, samples: list, to_addr: tuple[str, int]
) -> tuple[dict | None, str | None]:
    """Validates samples against the known-node set and runs
    dijkstra_with_extra_edges from a synthetic player node. Returns
    (result_dict, None) on success, or (None, error_reason) where
    error_reason is "no_samples" (nothing valid to route from) or
    "no_route" (valid samples, but dijkstra found no path) - callers
    map these to different HTTP statuses depending on context (POST
    vs. cached GET use the same reasons but slightly different
    response bodies)."""
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
        return None, "no_samples"

    result = dijkstra_with_extra_edges(player_node, to_addr, extra_adjacency={player_node: extra_edges})
    if result is None:
        return None, "no_route"

    total_ping, path = result
    path = path[1:]  # drop the synthetic player_node from the response path
    with graph.lock:
        path_geo = [_geo_to_dict(graph.geo.get(addr)) for addr in path]
    return {
        "total_ping_ms": total_ping,
        "hops": len(path) - 1,
        "path": [f"{ip}:{port}" for ip, port in path],
        "path_geo": path_geo,
    }, None
```

- [ ] **Step 4: Rewrite the `/player-route` branch of `do_POST` to call the helper**

Find this block in `do_POST` (the full `/player-route` branch after uuid/rate-limit checks):

```python
        samples = body.get("samples")
        if not isinstance(samples, list) or not samples or len(samples) > PLAYER_ROUTE_MAX_SAMPLES:
            self._send_json(
                {"error": f"samples must be a non-empty list, max {PLAYER_ROUTE_MAX_SAMPLES} entries"},
                status=400,
            )
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

Replace it with:

```python
        samples = body.get("samples")
        if not isinstance(samples, list) or not samples or len(samples) > PLAYER_ROUTE_MAX_SAMPLES:
            self._send_json(
                {"error": f"samples must be a non-empty list, max {PLAYER_ROUTE_MAX_SAMPLES} entries"},
                status=400,
            )
            return

        result, error = _route_from_samples(uuid, samples, to_addr)
        if error == "no_samples":
            self._send_json({"error": "no valid samples (all rejected or targets unknown)"}, status=400)
            return
        if error == "no_route":
            self._send_json({"error": "no route found", "to": qs.get("to", [""])[0]}, status=404)
            return

        result["to"] = qs.get("to", [""])[0]
        self._send_json(result)
```

- [ ] **Step 5: Run the test suite to confirm the refactor didn't change behavior**

Run: `python tests/test_player_route.py`
Expected: `5/5 passed` (identical to Step 2's baseline).

- [ ] **Step 6: Commit**

```bash
git add collector/collector.py
git commit -m "refactor(mesh): extract _route_from_samples from /player-route POST handler

Pure extraction, no behavior change (test suite unchanged, still
5/5 passing) - sets up the shared logic Task 3's cached GET endpoint
will reuse instead of duplicating validation+dijkstra."
```

---

## Task 2: Add per-uuid sample cache, written by POST /player-route

**Files:**
- Modify: `collector/collector.py`
- Test: `tests/test_player_route_cache.py` (new file)

**Interfaces:**
- Consumes: `_valid_uuid` (existing), `graph.lock` pattern (existing)
- Produces: `_player_samples: dict[str, tuple[float, list]]` (module-level dict, uuid -> (timestamp, samples)), `_player_samples_lock: threading.Lock`, `PLAYER_SAMPLES_TTL_SECONDS = 120` (constant), `_store_player_samples(uuid: str, samples: list) -> None`, `_get_cached_samples(uuid: str) -> list | None` (returns None if missing or expired — Task 3 consumes this).

- [ ] **Step 1: Write the failing test for cache store/retrieve/expiry**

Create `tests/test_player_route_cache.py`:

```python
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


def main():
    test_store_and_get_roundtrip()
    test_get_unknown_uuid_returns_none()
    test_get_expired_entry_returns_none()
    test_store_overwrites_previous_entry_for_same_uuid()

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

- [ ] **Step 2: Run the test to verify it fails**

Run: `python tests/test_player_route_cache.py`
Expected: `AttributeError: module 'collector' has no attribute '_player_samples'` (or similar — the cache doesn't exist yet).

- [ ] **Step 3: Add the cache constant, storage, and lock**

In `collector.py`, right after the existing `PLAYER_ROUTE_MAX_SAMPLES = 500` line, add:

```python
# TTL for the per-uuid cache written by POST /player-route and read by
# GET /player-route-cached - ping changes fast, so stale data past this
# window is worse than falling through to the site's other fallbacks
# (local bridge / STUN / geographic estimate).
PLAYER_SAMPLES_TTL_SECONDS = 120
```

Then, right after the `_uuid_rate_track: dict[str, tuple[float, int]] = {}` line (near `_uuid_rate_limited`), add:

```python
_player_samples_lock = threading.Lock()
_player_samples: dict[str, tuple[float, list]] = {}


def _store_player_samples(uuid: str, samples: list) -> None:
    with _player_samples_lock:
        _player_samples[uuid] = (time.time(), samples)


def _get_cached_samples(uuid: str) -> list | None:
    with _player_samples_lock:
        entry = _player_samples.get(uuid)
    if entry is None:
        return None
    timestamp, samples = entry
    if time.time() - timestamp > PLAYER_SAMPLES_TTL_SECONDS:
        return None
    return samples
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python tests/test_player_route_cache.py`
Expected: `4/4 passed`

- [ ] **Step 5: Wire `_store_player_samples` into the existing `/player-route` POST handler**

In `do_POST`, find the line (added by Task 1's refactor):

```python
        result, error = _route_from_samples(uuid, samples, to_addr)
```

Add one line right before it:

```python
        _store_player_samples(uuid, samples)
        result, error = _route_from_samples(uuid, samples, to_addr)
```

(Storing unconditionally, even if `_route_from_samples` later returns `no_samples`/`no_route` — the raw samples are still valid pings worth caching for a future `to` target, even if this particular `to` had no route.)

- [ ] **Step 6: Write a test confirming POST /player-route populates the cache**

Add this test function to `tests/test_player_route_cache.py`, before `def main():`:

```python
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
```

And add the call to `main()`:

```python
def main():
    test_store_and_get_roundtrip()
    test_get_unknown_uuid_returns_none()
    test_get_expired_entry_returns_none()
    test_store_overwrites_previous_entry_for_same_uuid()
    test_post_player_route_populates_cache()
    ...
```

- [ ] **Step 7: Run the full test to verify it passes**

Run: `python tests/test_player_route_cache.py`
Expected: `5/5 passed`

- [ ] **Step 8: Run the pre-existing test suite to confirm no regression**

Run: `python tests/test_player_route.py`
Expected: `5/5 passed`

- [ ] **Step 9: Commit**

```bash
git add collector/collector.py tests/test_player_route_cache.py
git commit -m "feat(mesh): cache player ping samples by uuid with TTL

POST /player-route now also stores the raw samples under the
caller's uuid (120s TTL, in-memory dict + lock). Read side
(_get_cached_samples) added but not yet exposed via HTTP - Task 3
wires the GET endpoint."
```

---

## Task 3: Add GET /player-route-cached endpoint

**Files:**
- Modify: `collector/collector.py` (`do_GET` handler)
- Test: `tests/test_player_route_cache.py` (extend)

**Interfaces:**
- Consumes: `_get_cached_samples` (Task 2), `_route_from_samples` (Task 1), `_valid_uuid` (existing), `parse_addr_param` (existing), `_rate_limited` (existing)
- Produces: `GET /player-route-cached?uuid=<uuid>&to=<ip:port>` HTTP endpoint

- [ ] **Step 1: Write the failing test for the new endpoint**

Add to `tests/test_player_route_cache.py`, before `def main():`:

```python
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
```

Add the three calls to `main()`, after `test_post_player_route_populates_cache()`:

```python
    test_cached_route_endpoint_happy_path()
    test_cached_route_unknown_uuid_returns_404()
    test_cached_route_malformed_params_return_400()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python tests/test_player_route_cache.py`
Expected: failures on the three new checks (404 with "not found" body, since the route doesn't exist yet).

- [ ] **Step 3: Add the endpoint to `do_GET`**

In `collector.py`, find the end of `do_GET` — the final fallback line:

```python
        self._send_json({"error": "not found"}, status=404)
```

(this is the line right before `def do_POST`). Insert the new branch immediately before it:

```python
        if parsed.path == "/player-route-cached":
            uuid_param = qs.get("uuid", [""])[0]
            if not _valid_uuid(uuid_param):
                self._send_json({"error": "missing or malformed uuid"}, status=400)
                return
            to_addr = parse_addr_param(qs.get("to", [""])[0])
            if not to_addr:
                self._send_json({"error": "usage: /player-route-cached?uuid=...&to=ip:port"}, status=400)
                return

            samples = _get_cached_samples(uuid_param)
            if samples is None:
                self._send_json({"error": "no cached samples"}, status=404)
                return

            result, error = _route_from_samples(uuid_param, samples, to_addr)
            if error == "no_samples":
                self._send_json({"error": "no valid cached samples"}, status=404)
                return
            if error == "no_route":
                self._send_json({"error": "no route found", "to": qs.get("to", [""])[0]}, status=404)
                return

            result["to"] = qs.get("to", [""])[0]
            self._send_json(result)
            return

        self._send_json({"error": "not found"}, status=404)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python tests/test_player_route_cache.py`
Expected: `8/8 passed`

- [ ] **Step 5: Run the pre-existing test suite to confirm no regression**

Run: `python tests/test_player_route.py`
Expected: `5/5 passed`

- [ ] **Step 6: Update the collector's endpoint listing in the startup message**

In `main()`, find:

```python
        "(/route, /routes-to, /top-routes, /estimate-route, /compare, /client-ping, /snapshot, /geo, /health, "
        "/player-targets, /player-route)"
```

Replace with:

```python
        "(/route, /routes-to, /top-routes, /estimate-route, /compare, /client-ping, /snapshot, /geo, /health, "
        "/player-targets, /player-route, /player-route-cached, /player-register, /player-link)"
```

- [ ] **Step 7: Commit**

```bash
git add collector/collector.py tests/test_player_route_cache.py
git commit -m "feat(mesh): add GET /player-route-cached endpoint

Reads samples a player's app already POSTed under their uuid
(Task 2's cache) and recomputes the route on demand, so the site can
get a real route without the app being open and responding to a
local fetch at click time. 404 on missing/expired cache - callers
fall through to their existing STUN/estimate path."
```

---

## Task 4: Player registration and link-code registry (collector)

**Files:**
- Create: `collector/players.json` (created at runtime, not committed — add to `.gitignore`)
- Modify: `collector/collector.py`, `.gitignore`
- Test: `tests/test_player_register.py` (new file)

**Interfaces:**
- Consumes: `_valid_uuid` (existing), `threading.Lock` pattern (existing)
- Produces: `_players: dict[str, dict]`, `_players_lock: threading.Lock`, `_pending_links: dict[str, tuple[float, str]]` (code -> (issued_at, uuid)), `PLAYER_LINK_CODE_TTL_SECONDS = 900`, `_load_players() -> dict`, `_save_players() -> None`, `_valid_nick(value) -> bool`, `_valid_place(value) -> bool` (shared by country/city — same 1-64 char free-text rule), `POST /player-register`, `POST /player-link` HTTP endpoints.

- [ ] **Step 1: Write the failing test for registration + link + double-redeem rejection**

Create `tests/test_player_register.py`:

```python
#!/usr/bin/env python3
"""
Teste automatizado para /player-register e /player-link: geracao de
uuid+codigo, validacao de payload, resgate de codigo (uso unico), e
expiracao. Usa um arquivo players.json temporario por teste - nunca toca
o arquivo real do coletor. Roda 100% local e deterministico.
"""
import json
import os
import sys
import tempfile
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


def _reset_registry():
    """Points the module's registry at a fresh temp file and clears
    in-memory state, so tests never touch the real collector/players.json
    and never see state left over from a previous test."""
    tmp_dir = tempfile.mkdtemp()
    collector.PLAYERS_FILE_PATH = os.path.join(tmp_dir, "players.json")
    collector._players.clear()
    collector._pending_links.clear()


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


def _start_test_server():
    server = collector.ThreadingHTTPServer(("127.0.0.1", 0), collector.Handler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    time.sleep(0.1)
    return server, port


def test_register_happy_path_returns_uuid_and_code():
    _reset_registry()
    server, port = _start_test_server()
    try:
        status, body = _post_json(
            port, "/player-register", {"nick": "tibazera", "country": "Brazil", "city": "Fortaleza"}
        )
        check("register_status_200", status == 200, f"status={status} body={body}")
        check("register_returns_valid_uuid", collector._valid_uuid(body.get("uuid")), f"body={body}")
        check("register_returns_6digit_code", body.get("link_code", "").isdigit() and len(body.get("link_code", "")) == 6, f"body={body}")
    finally:
        server.shutdown()


def test_register_rejects_blank_nick():
    _reset_registry()
    server, port = _start_test_server()
    try:
        status, body = _post_json(port, "/player-register", {"nick": "   ", "country": "Brazil", "city": "Fortaleza"})
        check("register_rejects_blank_nick", status == 400, f"status={status} body={body}")
    finally:
        server.shutdown()


def test_register_rejects_missing_fields():
    _reset_registry()
    server, port = _start_test_server()
    try:
        status, body = _post_json(port, "/player-register", {"nick": "tibazera"})
        check("register_rejects_missing_country_city", status == 400, f"status={status} body={body}")
    finally:
        server.shutdown()


def test_link_resolves_code_to_registered_uuid():
    _reset_registry()
    server, port = _start_test_server()
    try:
        _, reg_body = _post_json(port, "/player-register", {"nick": "tibazera", "country": "Brazil", "city": "Fortaleza"})
        code = reg_body["link_code"]
        registered_uuid = reg_body["uuid"]

        status, link_body = _post_json(port, "/player-link", {"link_code": code})
        check("link_status_200", status == 200, f"status={status} body={link_body}")
        check("link_returns_same_uuid", link_body.get("uuid") == registered_uuid, f"link_body={link_body}")
        check("link_returns_nick", link_body.get("nick") == "tibazera", f"link_body={link_body}")
    finally:
        server.shutdown()


def test_link_code_is_single_use():
    _reset_registry()
    server, port = _start_test_server()
    try:
        _, reg_body = _post_json(port, "/player-register", {"nick": "tibazera", "country": "Brazil", "city": "Fortaleza"})
        code = reg_body["link_code"]

        status1, _ = _post_json(port, "/player-link", {"link_code": code})
        status2, body2 = _post_json(port, "/player-link", {"link_code": code})
        check("link_first_use_succeeds", status1 == 200, f"status1={status1}")
        check("link_second_use_rejected", status2 == 404, f"status2={status2} body={body2}")
    finally:
        server.shutdown()


def test_link_unknown_code_returns_404():
    _reset_registry()
    server, port = _start_test_server()
    try:
        status, body = _post_json(port, "/player-link", {"link_code": "999999"})
        check("link_unknown_code_404", status == 404, f"status={status} body={body}")
    finally:
        server.shutdown()


def test_link_expired_code_returns_404():
    _reset_registry()
    server, port = _start_test_server()
    try:
        _, reg_body = _post_json(port, "/player-register", {"nick": "tibazera", "country": "Brazil", "city": "Fortaleza"})
        code = reg_body["link_code"]

        # simulate the code being issued PLAYER_LINK_CODE_TTL_SECONDS + 1 ago
        with collector._pending_links_lock:
            _, uuid = collector._pending_links[code]
            collector._pending_links[code] = (time.time() - collector.PLAYER_LINK_CODE_TTL_SECONDS - 1, uuid)

        status, body = _post_json(port, "/player-link", {"link_code": code})
        check("link_expired_code_404", status == 404, f"status={status} body={body}")
    finally:
        server.shutdown()


def main():
    test_register_happy_path_returns_uuid_and_code()
    test_register_rejects_blank_nick()
    test_register_rejects_missing_fields()
    test_link_resolves_code_to_registered_uuid()
    test_link_code_is_single_use()
    test_link_unknown_code_returns_404()
    test_link_expired_code_returns_404()

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

- [ ] **Step 2: Run the test to verify it fails**

Run: `python tests/test_player_register.py`
Expected: `AttributeError` or connection/404 errors — none of `/player-register`, `/player-link`, `PLAYERS_FILE_PATH`, `_pending_links`, `_pending_links_lock` exist yet.

- [ ] **Step 3: Add imports and registry state**

At the top of `collector.py`, add to the existing import block (after `import threading`):

```python
import os
import secrets
import uuid as uuid_lib
```

Right after the `_player_samples`/`_player_samples_lock` block added in Task 2, add:

```python
# Player registration registry - flat JSON file, no database (see spec:
# volume is human-registration-rate, not per-request-rate). Loaded once
# at import time; PLAYERS_FILE_PATH is a module-level variable (not a
# constant) so tests can point it at a temp file without touching the
# real collector/players.json.
PLAYERS_FILE_PATH = os.path.join(os.path.dirname(__file__), "players.json")
PLAYER_LINK_CODE_TTL_SECONDS = 900  # 15 minutes, single-use

_players_lock = threading.Lock()
_players: dict[str, dict] = {}

_pending_links_lock = threading.Lock()
_pending_links: dict[str, tuple[float, str]] = {}  # code -> (issued_at, uuid)


def _load_players() -> dict:
    try:
        with open(PLAYERS_FILE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_players() -> None:
    with open(PLAYERS_FILE_PATH, "w", encoding="utf-8") as f:
        json.dump(_players, f)


def _valid_nick(value: object) -> bool:
    return isinstance(value, str) and 1 <= len(value.strip()) <= 24


def _valid_place(value: object) -> bool:
    return isinstance(value, str) and 1 <= len(value.strip()) <= 64


def _generate_link_code() -> str:
    while True:
        code = f"{secrets.randbelow(1_000_000):06d}"
        with _pending_links_lock:
            if code not in _pending_links:
                return code


def _resolve_link_code(code: str) -> str | None:
    """Single-use: removes the code from _pending_links on any lookup
    (found-but-expired or found-and-valid), so a captured code can never
    be redeemed twice even if the caller retries after a network blip."""
    with _pending_links_lock:
        entry = _pending_links.pop(code, None)
    if entry is None:
        return None
    issued_at, uuid_str = entry
    if time.time() - issued_at > PLAYER_LINK_CODE_TTL_SECONDS:
        return None
    return uuid_str
```

- [ ] **Step 4: Load the registry at startup**

In `main()`, right after the `print("[collector] running initial collection cycle...")` line, add:

```python
    global _players
    _players = _load_players()
```

(`global` is required here since `main()` reassigns the module-level `_players` name rather than mutating it in place.)

- [ ] **Step 5: Add the `/player-register` and `/player-link` branches to `do_POST`**

Find the top of `do_POST`:

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
```

Replace the last two lines (the `/player-route` guard) with a dispatch that also handles the two new paths, keeping the existing `/player-route` body below unchanged:

```python
        if parsed.path == "/player-register":
            self._handle_player_register()
            return

        if parsed.path == "/player-link":
            self._handle_player_link()
            return

        if parsed.path != "/player-route":
            self._send_json({"error": "not found"}, status=404)
            return
```

Then add the two handler methods to the `Handler` class, right after `do_POST` ends (before `_geo_to_dict`, i.e. right after the `/player-route` branch's closing `self._send_json(result)`):

```python
    def _handle_player_register(self) -> None:
        body = _read_json_body(self)
        if body is None:
            self._send_json({"error": "invalid or missing JSON body"}, status=400)
            return

        nick = body.get("nick")
        country = body.get("country")
        city = body.get("city")
        if not _valid_nick(nick) or not _valid_place(country) or not _valid_place(city):
            self._send_json(
                {"error": "nick (1-24 chars), country and city (1-64 chars each) are required"},
                status=400,
            )
            return

        new_uuid = str(uuid_lib.uuid4())
        with _players_lock:
            _players[new_uuid] = {
                "nick": nick.strip(),
                "country": country.strip(),
                "city": city.strip(),
                "registered_at": time.time(),
            }
            _save_players()

        code = _generate_link_code()
        with _pending_links_lock:
            _pending_links[code] = (time.time(), new_uuid)

        self._send_json({"uuid": new_uuid, "link_code": code})

    def _handle_player_link(self) -> None:
        body = _read_json_body(self)
        if body is None:
            self._send_json({"error": "invalid or missing JSON body"}, status=400)
            return

        code = body.get("link_code")
        if not isinstance(code, str):
            self._send_json({"error": "missing link_code"}, status=400)
            return

        resolved_uuid = _resolve_link_code(code)
        if resolved_uuid is None:
            self._send_json({"error": "unknown or expired link_code"}, status=404)
            return

        with _players_lock:
            player = _players.get(resolved_uuid)
        if player is None:
            # registered uuid vanished from the registry between register
            # and link (should not happen outside test isolation bugs,
            # but fail closed rather than crash) - same 404 shape as an
            # unknown code, no extra information leaked either way.
            self._send_json({"error": "unknown or expired link_code"}, status=404)
            return

        self._send_json({"uuid": resolved_uuid, **player})
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `python tests/test_player_register.py`
Expected: `12/12 passed`

- [ ] **Step 7: Run the full existing suite to confirm no regression**

Run: `python tests/test_player_route.py && python tests/test_player_route_cache.py`
Expected: both `5/5 passed` and `8/8 passed`.

- [ ] **Step 8: Add `collector/players.json` to `.gitignore`**

Check current `.gitignore` for a `collector/` section:

Run: `grep -n "collector/" .gitignore`

Add this line to `.gitignore` (in the collector section if one exists, otherwise at the end):

```
collector/players.json
```

- [ ] **Step 9: Commit**

```bash
git add collector/collector.py tests/test_player_register.py .gitignore
git commit -m "feat(mesh): add player registration and link-code endpoints

POST /player-register {nick, country, city} -> {uuid, link_code}
persists to collector/players.json (flat file, no DB - see spec).
POST /player-link {link_code} resolves the single-use 6-digit code
back to the registered uuid, for the app to import (Task 5)."
```

---

## Task 5: App imports linked uuid (ClientIdentity + BackendClient + UI)

**Files:**
- Modify: `player-ping-app/ClientIdentity.cs`, `player-ping-app/BackendClient.cs`, `player-ping-app/MainWindow.axaml`, `player-ping-app/MainWindow.axaml.cs`

**Interfaces:**
- Consumes: existing `BackendClient` HTTP pattern (see `PostRouteAsync`)
- Produces: `ClientIdentity.SetUuid(string uuid) -> void`, `BackendClient.LinkAsync(string baseUrl, string linkCode) -> Task<LinkResult?>` where `LinkResult` is a new record `{Uuid, Nick, Country, City}`.

- [ ] **Step 1: Add `SetUuid` to `ClientIdentity`**

Read the current file first:

Run: `cat player-ping-app/ClientIdentity.cs`

Add this method to the `ClientIdentity` static class, after `GetOrCreateUuid`:

```csharp
    /// <summary>
    /// Overwrites the locally persisted uuid - called after a successful
    /// /player-link, so the app adopts the site-issued uuid (the stable
    /// identity chosen at registration) instead of the site ever having
    /// to adopt the app's. Best-effort persistence, same as
    /// GetOrCreateUuid: a write failure doesn't crash the app, it just
    /// means the import won't survive a restart.
    /// </summary>
    public static void SetUuid(string uuid)
    {
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(StatePath)!);
            File.WriteAllText(StatePath, uuid);
        }
        catch (IOException)
        {
            // best-effort; caller already has the uuid in memory for this run
        }
    }
```

- [ ] **Step 2: Add `LinkResult` record and `LinkAsync` to `BackendClient`**

Read the current file first (already read earlier in this plan's research — reproduced here for the exact insertion point):

In `player-ping-app/BackendClient.cs`, add this record after `RouteResult`:

```csharp
internal sealed record LinkResult(
    [property: JsonPropertyName("uuid")] string Uuid,
    [property: JsonPropertyName("nick")] string Nick,
    [property: JsonPropertyName("country")] string Country,
    [property: JsonPropertyName("city")] string City);
```

Add this method to the `BackendClient` class, after `PostRouteAsync`:

```csharp
    public async Task<LinkResult?> LinkAsync(string baseUrl, string linkCode)
    {
        var payload = new { link_code = linkCode };
        try
        {
            var json = JsonSerializer.Serialize(payload);
            var content = new StringContent(json, Encoding.UTF8, "application/json");
            var response = await Http.PostAsync($"{baseUrl}/player-link", content);
            if (!response.IsSuccessStatusCode)
            {
                return null;
            }
            return await response.Content.ReadFromJsonAsync<LinkResult>();
        }
        catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException or NotSupportedException)
        {
            return null;
        }
    }
```

- [ ] **Step 3: Add the link UI to `MainWindow.axaml`**

In `MainWindow.axaml`, find the header `StackPanel` (Grid.Row="0"). Add a link row right after the subtitle `TextBlock`:

```xml
    <StackPanel Grid.Row="0" Margin="0,0,0,12">
      <TextBlock Text="qwfwd  •  player ping" FontSize="18" FontWeight="Bold" Foreground="#EBEBF0" />
      <TextBlock Text="escaneia seu ping real, escolha um servidor e veja a rota calculada até ele"
                 Foreground="#9696A5" Margin="0,4,0,0" />
      <StackPanel Orientation="Horizontal" Margin="0,8,0,0" Spacing="8">
        <TextBox Name="LinkCodeBox" Watermark="código do site (6 dígitos)" Width="200" />
        <Button Name="LinkButton" Content="Vincular" Click="OnLinkClicked" />
        <TextBlock Name="LinkStatusLabel" Foreground="#9696A5" VerticalAlignment="Center" />
      </StackPanel>
    </StackPanel>
```

- [ ] **Step 4: Wire `OnLinkClicked` in `MainWindow.axaml.cs`**

Add this method to the `MainWindow` class, after `OnScanClicked`:

```csharp
    private async void OnLinkClicked(object? sender, RoutedEventArgs e)
    {
        var code = LinkCodeBox.Text?.Trim();
        if (string.IsNullOrEmpty(code))
        {
            LinkStatusLabel.Text = "digite o código do site";
            return;
        }

        LinkButton.IsEnabled = false;
        LinkStatusLabel.Text = "vinculando...";

        var result = await _backend.LinkAsync(BackendBaseUrl, code);
        if (result is null)
        {
            LinkStatusLabel.Text = "código inválido ou expirado";
            LinkButton.IsEnabled = true;
            return;
        }

        ClientIdentity.SetUuid(result.Uuid);
        LinkStatusLabel.Text = $"vinculado como {result.Nick} ({result.City}, {result.Country})";
        LinkButton.IsEnabled = true;
    }
```

Note: `_uuid` (the field read by `OnScanClicked`/`ServerList_SelectionChanged` for the `POST /player-route` calls) is set once in the constructor from `ClientIdentity.GetOrCreateUuid()` and stays a `readonly` field for the rest of this task — after a successful link, the *persisted* uuid changes for the next app launch, but this task does not require the *current* run to immediately start using the new uuid for in-flight scans (out of scope: hot-swapping `_uuid` mid-session). Document this in a comment rather than leaving it implicit:

In `MainWindow.axaml.cs`, find:

```csharp
    private readonly string _uuid;
```

Change to:

```csharp
    // Set once from ClientIdentity at construction. A successful link
    // (OnLinkClicked) overwrites the FILE on disk via ClientIdentity.SetUuid
    // for the *next* launch - it does not hot-swap this field mid-session,
    // since nothing in this run has yet used the old uuid for anything the
    // player would notice diverge (no scan has necessarily happened yet).
    private readonly string _uuid;
```

- [ ] **Step 5: Build to verify no compile errors**

Run: `cd player-ping-app && dotnet build`
Expected: `Compilação com êxito` / `Build succeeded`, 0 errors.

- [ ] **Step 6: Manual verification**

This UI change has no C#/XAML test framework in this repo (documented existing decision, see README). Verify manually:
1. Start a local collector: `python collector/collector.py` (or point `BackendBaseUrl` at one — see README's Configuração section).
2. `curl -X POST http://127.0.0.1:8730/player-register -d '{"nick":"test","country":"Brazil","city":"Fortaleza"}' -H "Content-Type: application/json"` — note the returned `link_code`.
3. Run the app (`dotnet run`), paste that code into the new "código do site" field, click "Vincular".
4. Confirm the status label shows "vinculado como test (Fortaleza, Brazil)".
5. Restart the app, confirm the UUID shown in the route panel (`RouteResultBox`'s "UUID local: ...") now matches the uuid returned by `/player-register` in step 2, not a freshly generated one.

- [ ] **Step 7: Commit**

```bash
git add player-ping-app/ClientIdentity.cs player-ping-app/BackendClient.cs player-ping-app/MainWindow.axaml player-ping-app/MainWindow.axaml.cs
git commit -m "feat(player-ping-app): import site-issued uuid via link code

New 'Vincular' field pastes the 6-digit code from the site's
registration flow, calls POST /player-link, and overwrites the
app's persisted uuid (ClientIdentity.SetUuid) with the one the site
already has - the site's uuid is the stable identity, per spec."
```

---

## Task 6: Site registration form + cached-route lookup

**Files:**
- Modify: `.gh-pages-worktree/index.html`

**Interfaces:**
- Consumes: `COLLECTOR_BASE` (existing constant), `chooseNearbyDestination` (existing function, modified — see exact current code below, read from the real file at lines 1160-1209)
- Produces: `registerPlayer(nick, country, city) -> Promise<{uuid, link_code} | null>`, `fetchCachedLegRtt(addr) -> Promise<number | null>` (returns the PLAYER-TO-PROXY leg RTT in ms, or null — matches `measureViaLocalApp`'s contract exactly, NOT the full end-to-end `/player-route` total, since `chooseNearbyDestination` already adds the proxy-to-destination leg itself via `/routes-to`)

**Important — read before implementing:** `chooseNearbyDestination` (current code, `.gh-pages-worktree/index.html:1160-1209`) computes two legs separately and sums them client-side: `playerLegMs` (you → candidate proxy, from `measureViaLocalApp`/`measureProxyRttViaStun`/`estimatePlayerLegMs`) plus `route.total_ping_ms` (candidate proxy → destination, from `/routes-to`, already mesh-wide and multi-hop). The collector's `/player-route-cached` (Task 3) returns a DIFFERENT number: the full end-to-end total (you → ... → destination) via `dijkstra_with_extra_edges`, which already includes both legs. **Do not plug `/player-route-cached`'s `total_ping_ms` into `playerLegMs`** — that would double-count the proxy-to-destination leg (once from the cached total, once again from `route.total_ping_ms` added by the existing code). This task uses the cache only for the player-leg RTT to a specific candidate, the same role `measureViaLocalApp` already fills — it does NOT ask `/player-route-cached` for a route "to the destination", it reuses the cached SAMPLES (the raw per-candidate pings) as a per-candidate lookup. Since Task 3's endpoint takes a single `to` and returns a route (not a raw sample list), the correct call here is one `/player-route-cached?uuid=...&to=<candidate.addr>` per candidate, using its `total_ping_ms` ONLY when the returned `hops` is `0` (meaning the "route" is just the direct player→candidate sample with no further hops) — hops > 0 means the cache route already went past this candidate to some other node and its total does not isolate the leg. Filter it in JS accordingly.

- [ ] **Step 1: Add `fetchCachedLegRtt` next to `measureViaLocalApp`**

In `.gh-pages-worktree/index.html`, add this function right after `measureViaLocalApp` (which ends right before `async function measureProxyRttViaStun(addr, timeoutMs = 2200) {` at line 993):

```javascript
// Reads the player's own uuid (issued by /player-register) from
// localStorage and asks the collector for the player's own RTT to this
// specific candidate proxy, recomputed from samples the app already
// POSTed to /player-route - works even if the app isn't open right now,
// unlike measureViaLocalApp. Only trusts a hops===0 result (a direct
// player->candidate sample, no further relay) - see Task 6 header note
// on why a multi-hop cached route can't be reused as a single leg RTT.
// Returns the RTT in ms on success, null on any failure (unregistered
// player, expired cache, no direct sample for this candidate, network
// error) - same null-on-failure contract as measureViaLocalApp so
// callers chain it the same way.
async function fetchCachedLegRtt(addr, timeoutMs = 800) {
  const playerUuid = localStorage.getItem('qwfwd_player_uuid');
  if (!playerUuid) return null;
  try {
    const resp = await fetch(
      `${COLLECTOR_BASE}/player-route-cached?uuid=${encodeURIComponent(playerUuid)}&to=${encodeURIComponent(addr)}`,
      { cache: 'no-store', signal: AbortSignal.timeout(timeoutMs) }
    );
    if (!resp.ok) return null;
    const body = await resp.json();
    if (body.hops !== 0 || typeof body.total_ping_ms !== 'number') return null;
    return body.total_ping_ms;
  } catch {
    return null;
  }
}
```

- [ ] **Step 2: Wire it into `chooseNearbyDestination`'s fallback chain**

Find the exact existing block (`.gh-pages-worktree/index.html:1183-1195`):

```javascript
  try {
    const measuredRtts = new Map(); // addr -> { rtt, source: 'app' | 'stun' }
    await Promise.all(candidates.slice(0, 12).map(async candidate => {
      const viaApp = await measureViaLocalApp(candidate.addr);
      if (Number.isFinite(viaApp)) {
        measuredRtts.set(candidate.addr, { rtt: viaApp, source: 'app' });
        return;
      }
      const viaStun = await measureProxyRttViaStun(candidate.addr);
      if (Number.isFinite(viaStun)) {
        measuredRtts.set(candidate.addr, { rtt: viaStun, source: 'stun' });
      }
    }));
```

Replace with:

```javascript
  try {
    const measuredRtts = new Map(); // addr -> { rtt, source: 'cached' | 'app' | 'stun' }
    await Promise.all(candidates.slice(0, 12).map(async candidate => {
      const viaCache = await fetchCachedLegRtt(candidate.addr);
      if (Number.isFinite(viaCache)) {
        measuredRtts.set(candidate.addr, { rtt: viaCache, source: 'cached' });
        return;
      }
      const viaApp = await measureViaLocalApp(candidate.addr);
      if (Number.isFinite(viaApp)) {
        measuredRtts.set(candidate.addr, { rtt: viaApp, source: 'app' });
        return;
      }
      const viaStun = await measureProxyRttViaStun(candidate.addr);
      if (Number.isFinite(viaStun)) {
        measuredRtts.set(candidate.addr, { rtt: viaStun, source: 'stun' });
      }
    }));
```

`playerLegSource`/`playerLegMeasured` (`.gh-pages-worktree/index.html:1204-1205`) already derive from `measured.source` generically (`measured ? measured.source : 'estimate'`), so no change is needed there — `'cached'` flows through automatically as a new possible value alongside the existing `'app'`/`'stun'`/`'estimate'`.

- [ ] **Step 3: Add a display label for the new source**

Find the label mapping (`.gh-pages-worktree/index.html:1235-1237`):

```javascript
      const legSourceLabel = route.playerLegSource === 'app' ? tr('appMeasured')
        : route.playerLegSource === 'stun' ? tr('stunMeasured')
```

Read the next line (the `estimate` fallback) to confirm the ternary's final `else`:

Run: `sed -n '1235,1238p' .gh-pages-worktree/index.html`

Add a `'cached'` branch before the `'app'` check:

```javascript
      const legSourceLabel = route.playerLegSource === 'cached' ? tr('cachedMeasured')
        : route.playerLegSource === 'app' ? tr('appMeasured')
        : route.playerLegSource === 'stun' ? tr('stunMeasured')
```

- [ ] **Step 4: Add the `cachedMeasured` translation key**

Run: `grep -n "appMeasured" .gh-pages-worktree/index.html` to find the existing translation table(s) defining `appMeasured` (likely one entry per supported language). Add a `cachedMeasured` key next to each `appMeasured` entry found, using a label consistent with that language's existing `appMeasured` string style (e.g. if `appMeasured: 'medido via app'` exists for Portuguese, add `cachedMeasured: 'medido via cache'` in the same object).

- [ ] **Step 5: Add the registration form**

Find a sensible insertion point — the grep from Step 1 will show where player-facing controls already live. Add a small form (plain HTML, matching the site's existing inline-style conventions — check a nearby existing form/button element for the class/style pattern before adding this):

```html
<div id="registerPanel" style="display:none;">
  <input id="registerNick" placeholder="seu nick" maxlength="24" />
  <input id="registerCountry" placeholder="país" maxlength="64" />
  <input id="registerCity" placeholder="cidade" maxlength="64" />
  <button onclick="handleRegisterClick()">registrar</button>
  <div id="registerResult"></div>
</div>
```

(Exact styling/placement deferred to match the site's existing CSS classes — read the surrounding markup at the insertion point before finalizing; the functional pieces below are what matter for this task's test.)

- [ ] **Step 6: Add `registerPlayer` and the click handler**

Add near `fetchCachedRoute`:

```javascript
async function registerPlayer(nick, country, city) {
  try {
    const resp = await fetch(`${COLLECTOR_BASE}/player-register`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ nick, country, city }),
    });
    if (!resp.ok) return null;
    return await resp.json(); // {uuid, link_code}
  } catch {
    return null;
  }
}

async function handleRegisterClick() {
  const nick = document.getElementById('registerNick').value.trim();
  const country = document.getElementById('registerCountry').value.trim();
  const city = document.getElementById('registerCity').value.trim();
  const resultEl = document.getElementById('registerResult');

  if (!nick || !country || !city) {
    resultEl.textContent = 'preencha nick, país e cidade';
    return;
  }

  resultEl.textContent = 'registrando...';
  const result = await registerPlayer(nick, country, city);
  if (!result) {
    resultEl.textContent = 'falha ao registrar - tente novamente';
    return;
  }

  localStorage.setItem('qwfwd_player_uuid', result.uuid);
  resultEl.textContent = `registrado! cole este código no app: ${result.link_code}`;
}
```

- [ ] **Step 7: Manual verification**

No JS test framework in this repo (existing documented decision). Verify manually:
1. Start a local collector.
2. Serve `.gh-pages-worktree/index.html` locally (or open it directly), open browser devtools console.
3. Fill the registration form, click "registrar", confirm the code appears and `localStorage.getItem('qwfwd_player_uuid')` returns a valid uuid.
4. With the collector having a cached direct sample for that uuid against a specific candidate (e.g. via a manual `curl -X POST .../player-route?to=<candidate>` with a single-sample body first, so the cached route to that candidate is `hops===0` — or an app linked to it and scanned, see Task 5's manual test), click a nearby destination and confirm the network tab shows `player-route-cached` requests, with at least one candidate's leg RTT coming from the cache (label shows the new `cachedMeasured` text) instead of falling to `measureViaLocalApp`.
5. Clear `localStorage` and repeat step 4, confirming it falls through to the pre-existing local-bridge/STUN/estimate behavior without any visible error.

- [ ] **Step 8: Commit**

```bash
git add .gh-pages-worktree/index.html
git commit -m "feat(mesh): site registration form + cached-route lookup

Registration form (nick/country/city) calls POST /player-register,
stores the returned uuid in localStorage and shows the link_code for
the player to paste into the app. chooseNearbyDestination tries
GET /player-route-cached first, before the existing local-bridge/
STUN/estimate chain - falls through unchanged when unregistered or
cache miss."
```

---

## Task 7: Update player-ping-app README with the link flow

**Files:**
- Modify: `player-ping-app/README.md`

- [ ] **Step 1: Add a "Vincular ao site" section**

Add this section to `player-ping-app/README.md`, after the existing "Testar junto com o site (bridge local)" section:

```markdown
## Vincular ao site (identidade estável)

Além da ponte local (acima, exige o app aberto no momento do clique), dá
pra vincular o app a um registro feito no site (nick, país, cidade) —
assim o coletor guarda seus últimos pings por alguns minutos
(`/player-route-cached`) e o site consegue calcular sua rota mesmo
depois de você fechar o app.

1. No site, preencha o formulário de registro (nick/país/cidade) — ele
   mostra um código de 6 dígitos.
2. No app, cole esse código no campo "código do site" (no topo da
   janela) e clique "Vincular".
3. O app confirma o nick e passa a usar o mesmo UUID do registro do
   site a partir da próxima vez que abrir — o vínculo é permanente
   (arquivo local `client-id.txt` sobrescrito), não precisa repetir a
   cada sessão.

O código expira em 15 minutos e só pode ser usado uma vez — se expirar,
gere um novo no site.
```

- [ ] **Step 2: Commit**

```bash
git add player-ping-app/README.md
git commit -m "docs(player-ping-app): document the site link flow

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Final Task: Whole-branch verification

- [ ] **Step 1: Run every collector test**

Run: `python tests/test_player_route.py && python tests/test_player_route_cache.py && python tests/test_player_register.py && python tests/test_mesh_e2e.py`
Expected: all pass (mesh e2e test is the pre-existing one referenced in project memory — confirm it still passes, since Task 1's refactor touches the same file it exercises).

- [ ] **Step 2: Build the app**

Run: `cd player-ping-app && dotnet build`
Expected: `Compilação com êxito`, 0 errors, 0 warnings.

- [ ] **Step 3: Publish for both platforms as a smoke check**

Run: `cd player-ping-app && dotnet publish -r linux-x64 --self-contained false -c Release && dotnet publish -r win-x64 --self-contained false -c Release`
Expected: both succeed.

- [ ] **Step 4: Full manual walkthrough**

Repeat Task 5 Step 6 and Task 6 Step 7 end to end in one sitting: register on the site, link the code in the app, run a scan, confirm the site's map shows the `cachedMeasured` label for a candidate the app already pinged directly (a `hops===0` cached entry), close the app, reload the site, and confirm the cached leg RTT is still usable until the 120-second TTL elapses, then confirm it falls through cleanly to STUN/estimate after that.
