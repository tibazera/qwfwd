#!/usr/bin/env python3
"""
Coletor externo da malha qwfwd.

Arquitetura (decidida em conjunto com revisão do Codex nesta sessão):
o cálculo de rota (Dijkstra) fica AQUI, não em cada qwfwd — cada proxy só
expõe dados brutos (meshstatus, e pingstatus como fallback legado). Isso
evita duplicar o grafo mundial e o cálculo em centenas de proxies, lida
naturalmente com a malha mista (nós patcheados vs os ~280 legados que só
falam pingstatus), e mantém o qwfwd "burro" e simples.

Fluxo:
  1. Descobre servidores via masters QW públicos (protocolo nativo) E via
     o servers.json mantido por terceiros em github.com/vikpe/qw-data
     (usado pelo próprio tools.quake.world/servers/) - essa segunda fonte
     também traz coordenadas geográficas reais (geo.coordinates) por
     servidor, o que resolve por completo a necessidade de geolocalização
     própria de IP para o mapa mundial.
  2. Para cada um que responde na porta 30000 (convenção, não garantia):
     tenta meshstatus primeiro (dados ricos: ping+jitter+loss, várias
     arestas de uma vez). Se não responder, tenta pingstatus (legado,
     só ping direto, sem jitter/loss).
  3. Monta o grafo dirigido (arestas com origem explícita, nunca assume
     simetria — RTT pode divergir por direção).
  4. Expõe /route?from=X&to=Y calculando Dijkstra sob demanda,
     /snapshot com o grafo bruto para debug/mapa, e /geo com as
     coordenadas conhecidas por endereço.

Isso é uma primeira versão funcional, não um serviço de produção 24/7
ainda — roda um ciclo de coleta, serve o resultado via HTTP enquanto
processos leves de recoleta acontecem em background.
"""
from __future__ import annotations

import heapq
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import protocol

MASTERS = [
    ("master.quakeworld.nu", 27000),
    ("qwmaster.fodquake.net", 27000),
    ("master.quakeservers.net", 27000),
]

# maintained by a third party (github.com/vikpe/qw-data), also the data
# source behind tools.quake.world/servers/ - includes geo.coordinates per
# server, which is otherwise unavailable from the master/pingstatus
# protocols themselves
QW_DATA_SERVERS_URL = "https://raw.githubusercontent.com/vikpe/qw-data/main/servers.json"
QW_DATA_TIMEOUT = 10.0

# our own 4 mesh-patched pilot instances (Lisbon/São Paulo/Miami/Fortaleza,
# isolated test ports 30501-30504, not production 30000) - always probed
# regardless of what masters/qw-data report this cycle, so they never
# silently drop off the map due to a transient master-query miss. These
# are the ones we actually want ranked and compared reliably.
PINNED_PROXIES = [
    ("103.63.29.40", 30501),   # Lisboa
    ("54.232.22.245", 30502),  # São Paulo
    ("140.235.125.12", 30503), # Miami
    ("201.23.3.62", 30504),    # Fortaleza
]

PROXY_PORT_HINT = 30000  # convention, not guaranteed - we still verify via protocol response
DISCOVERY_TIMEOUT = 5.0
PROBE_TIMEOUT = 1.0
MAX_WORKERS = 64
RECOLLECT_INTERVAL_SECONDS = 300
MESH_MAX_PAGES = 10


@dataclass
class Edge:
    to_ip: str
    to_port: int
    ping: float
    jitter: int | None = None
    loss_pct: int | None = None
    source: str = "unknown"  # "meshstatus" | "pingstatus"


@dataclass
class GeoInfo:
    country_code: str
    country: str
    region: str
    city: str
    lat: float
    lon: float
    hostname: str
    is_proxy: bool


# manual geo for our pinned test proxies (real coordinates of the actual
# datacenters, not the isolated test port itself) - qw-data has no record
# of ports 305xx since they're not on the public master network
PINNED_PROXY_GEO = {
    ("103.63.29.40", 30501): GeoInfo("PT", "Portugal", "Europe", "Lisbon", 38.7223, -9.1393, "qwfwd-mesh-test (Lisboa)", True),
    ("54.232.22.245", 30502): GeoInfo("BR", "Brazil", "South America", "São Paulo", -23.5505, -46.6333, "qwfwd-mesh-test (São Paulo)", True),
    ("140.235.125.12", 30503): GeoInfo("US", "United States", "North America", "Miami", 25.7617, -80.1918, "qwfwd-mesh-test (Miami)", True),
    ("201.23.3.62", 30504): GeoInfo("BR", "Brazil", "South America", "Fortaleza", -3.7319, -38.5267, "qwfwd-mesh-test (Fortaleza)", True),
}


@dataclass
class GraphState:
    # adjacency: (ip,port) -> list[Edge]. Directed - an edge measured by
    # node A about node B says nothing about B's measurement of A.
    edges: dict[tuple[str, int], list[Edge]] = field(default_factory=dict)
    mesh_capable: set[tuple[str, int]] = field(default_factory=set)
    geo: dict[tuple[str, int], GeoInfo] = field(default_factory=dict)
    last_collected_at: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def add_edge(self, from_addr: tuple[str, int], edge: Edge) -> None:
        if from_addr == (edge.to_ip, edge.to_port):
            return  # self-loop, never routable (mirrors the server-side filter in qwfwd itself)
        with self.lock:
            lst = self.edges.setdefault(from_addr, [])
            for i, existing in enumerate(lst):
                if (existing.to_ip, existing.to_port) == (edge.to_ip, edge.to_port):
                    lst[i] = edge  # keep freshest measurement for this specific edge
                    return
            lst.append(edge)

    def snapshot(self) -> dict:
        with self.lock:
            return {
                f"{ip}:{port}": [
                    {
                        "to": f"{e.to_ip}:{e.to_port}",
                        "ping": e.ping,
                        "jitter": e.jitter,
                        "loss_pct": e.loss_pct,
                        "source": e.source,
                    }
                    for e in edges
                ]
                for (ip, port), edges in self.edges.items()
            }


graph = GraphState()


def discover_servers() -> list[tuple[str, int]]:
    """Queries all known masters, deduplicates the combined server list."""
    seen: set[tuple[str, int]] = set()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(DISCOVERY_TIMEOUT)
    for host, port in MASTERS:
        try:
            addr = (socket.gethostbyname(host), port)
        except socket.gaierror:
            continue
        try:
            sock.sendto(protocol.MASTER_QUERY, addr)
            data, _ = sock.recvfrom(65535)
            for entry in protocol.parse_master_reply(data):
                seen.add(entry)
        except (socket.timeout, OSError):
            continue
    sock.close()
    return sorted(seen)


def fetch_qw_data_servers() -> list[tuple[tuple[str, int], GeoInfo]]:
    """Fetches the third-party servers.json (vikpe/qw-data, also used by
    tools.quake.world/servers/). Used as (a) a second discovery source and
    (b) the ONLY source of real geographic coordinates - the QW protocol
    itself has no notion of geolocation. Best-effort: network failure here
    must never break the primary master-based discovery path."""
    results: list[tuple[tuple[str, int], GeoInfo]] = []
    try:
        req = urllib.request.Request(QW_DATA_SERVERS_URL, headers={"User-Agent": "qwfwd-mesh-collector"})
        with urllib.request.urlopen(req, timeout=QW_DATA_TIMEOUT) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
        print(f"[collector] qw-data fetch failed (non-fatal, continuing with master-only discovery): {e}")
        return results

    for entry in data:
        address = entry.get("address", "")
        if ":" not in address:
            continue
        ip, _, port_str = address.rpartition(":")
        try:
            port = int(port_str)
        except ValueError:
            continue

        geo = entry.get("geo") or {}
        coords = geo.get("coordinates")
        if not coords or len(coords) != 2:
            continue  # no point keeping a geo entry with no coordinates

        version = str(entry.get("version", ""))
        settings = entry.get("settings", {}) or {}
        hostname = str(settings.get("hostname", ""))
        is_proxy = "qwfwd" in version.lower() or port == PROXY_PORT_HINT

        results.append(
            (
                (ip, port),
                GeoInfo(
                    country_code=str(geo.get("cc", "")),
                    country=str(geo.get("country", "")),
                    region=str(geo.get("region", "")),
                    city=str(geo.get("city", "")),
                    lat=float(coords[0]),
                    lon=float(coords[1]),
                    hostname=hostname,
                    is_proxy=is_proxy,
                ),
            )
        )

    return results


def probe_meshstatus(sock: socket.socket, addr: tuple[str, int]) -> list[protocol.MeshPeerBlock] | None:
    """Full meshstatus fetch with pagination. Returns None if the node
    never answers meshstatus at all (not mesh-capable / unreachable);
    returns [] (possibly) if it answers but has nothing to report yet."""
    all_blocks: list[protocol.MeshPeerBlock] = []
    next_index = 0
    for _ in range(MESH_MAX_PAGES):
        reply = protocol.udp_request(sock, addr, protocol.build_meshstatus_query(next_index))
        if reply is None:
            return all_blocks if all_blocks else None
        blocks, next_idx = protocol.parse_meshstatus_reply(reply)
        if next_idx == -2:
            return None  # not a valid meshstatus reply at all
        all_blocks.extend(blocks)
        if next_idx == -1:
            break
        next_index = next_idx
    return all_blocks


def probe_pingstatus(sock: socket.socket, addr: tuple[str, int]) -> list[tuple[str, int, int]]:
    reply = protocol.udp_request(sock, addr, protocol.build_pingstatus_query())
    if reply is None:
        return []
    return protocol.parse_pingstatus_reply(reply)


def probe_one(addr: tuple[str, int]) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(PROBE_TIMEOUT)
    try:
        mesh_blocks = probe_meshstatus(sock, addr)
        if mesh_blocks is not None:
            graph.mesh_capable.add(addr)
            for block in mesh_blocks:
                peer_addr = (block.peer_ip, block.peer_port)
                for hop in block.hops:
                    graph.add_edge(
                        peer_addr,
                        Edge(hop.ip, hop.port, float(hop.ping), hop.jitter, hop.loss_pct, source="meshstatus"),
                    )
            if mesh_blocks:
                return  # mesh-capable node gave us richer data, no need for pingstatus too
            # mesh-capable but its peer cache is still empty (daemon just
            # started, hasn't accumulated mesh peers yet) - fall through to
            # pingstatus so the node still gets edges in the meantime.

        # fallback: legacy pingstatus, ping-only, no jitter/loss
        entries = probe_pingstatus(sock, addr)
        for ip, port, ping in entries:
            graph.add_edge(addr, Edge(ip, port, float(ping), source="pingstatus"))
    finally:
        sock.close()


def collect_once() -> None:
    servers = discover_servers()

    qw_data_entries = fetch_qw_data_servers()
    with graph.lock:
        for addr, geo_info in qw_data_entries:
            graph.geo[addr] = geo_info
        # our own pinned test proxies use isolated ports (305xx) qw-data
        # has never heard of - give them known-real coordinates directly so
        # they still show up on the map even though no third-party source
        # lists them.
        for addr, geo_info in PINNED_PROXY_GEO.items():
            graph.geo.setdefault(addr, geo_info)

    # combine both discovery sources: master-reported servers (authoritative
    # for "is it alive right now") plus qw-data proxy entries (may include
    # proxies momentarily missed by a master query, or proxies that opted
    # out of master registration but still answer the protocol directly)
    qw_data_proxy_addrs = {addr for addr, info in qw_data_entries if info.is_proxy}
    candidates = {(ip, port) for ip, port in servers if port == PROXY_PORT_HINT}
    candidates |= qw_data_proxy_addrs
    candidates |= set(PINNED_PROXIES)

    print(
        f"[collector] discovered {len(servers)} servers via masters, "
        f"{len(qw_data_entries)} entries via qw-data ({len(qw_data_proxy_addrs)} proxies), "
        f"{len(PINNED_PROXIES)} pinned test proxies, "
        f"{len(candidates)} total proxy candidates to probe"
    )

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [pool.submit(probe_one, addr) for addr in candidates]
        for f in as_completed(futures):
            f.result()  # propagate exceptions instead of swallowing them silently

    graph.last_collected_at = time.time()
    total_edges = sum(len(v) for v in graph.edges.values())
    print(
        f"[collector] cycle done: {len(graph.edges)} nodes, {total_edges} edges, "
        f"{len(graph.mesh_capable)} mesh-capable, {len(graph.geo)} with known coordinates"
    )


def recollect_loop() -> None:
    while True:
        try:
            collect_once()
        except Exception as e:
            print(f"[collector] collection cycle failed: {e}")
        time.sleep(RECOLLECT_INTERVAL_SECONDS)


def dijkstra(start: tuple[str, int], end: tuple[str, int]) -> tuple[float, list[tuple[str, int]]] | None:
    with graph.lock:
        # snapshot the adjacency under lock, then run Dijkstra lock-free
        adjacency = {k: list(v) for k, v in graph.edges.items()}

    dist: dict[tuple[str, int], float] = {start: 0.0}
    prev: dict[tuple[str, int], tuple[str, int]] = {}
    visited: set[tuple[str, int]] = set()
    pq: list[tuple[float, tuple[str, int]]] = [(0.0, start)]

    while pq:
        d, node = heapq.heappop(pq)
        if node in visited:
            continue
        visited.add(node)
        if node == end:
            break
        for edge in adjacency.get(node, []):
            neighbor = (edge.to_ip, edge.to_port)
            if neighbor in visited:
                continue
            nd = d + edge.ping
            if neighbor not in dist or nd < dist[neighbor]:
                dist[neighbor] = nd
                prev[neighbor] = node
                heapq.heappush(pq, (nd, neighbor))

    if end not in dist:
        return None

    path = [end]
    seen = {end}
    while path[-1] != start:
        nxt = prev.get(path[-1])
        if nxt is None or nxt in seen:
            return None  # disconnected or cycle guard
        seen.add(nxt)
        path.append(nxt)
    path.reverse()
    return dist[end], path


def parse_addr_param(value: str) -> tuple[str, int] | None:
    if ":" not in value:
        return None
    ip, _, port_str = value.rpartition(":")
    try:
        return ip, int(port_str)
    except ValueError:
        return None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # keep stdout to collection-cycle logs only

    def _send_json(self, obj: object, status: int = 200) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        # a browser page can be served from anywhere (the map artifact,
        # localhost during dev, etc) - this endpoint has no secret/mutating
        # behavior, so an open CORS policy is fine here
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        # CORS preflight, harmless to answer generically for any path
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)

        if parsed.path == "/client-ping":
            # A browser cannot open a raw UDP or TCP socket, so it cannot
            # measure its own RTT to a qwfwd proxy directly (the QW
            # protocol is UDP-only, and even a minimal TCP echo target
            # would need per-proxy HTTPS/WSS certs to be reachable from an
            # HTTPS page without mixed-content blocking - not "minimal" at
            # that point). This endpoint is deliberately the SMALLEST
            # possible response (no body work, no lookups) so that
            # round-trip time to it approximates network RTT to the
            # collector itself, not app latency. The browser is expected
            # to call this several times and use the minimum observed RTT
            # (median/min filters out one-off jitter from the JS event
            # loop, not the network).
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return

        if parsed.path == "/estimate-route":
            # ESTIMATE, not a measurement: total = (browser -> collector,
            # measured by the caller via /client-ping) + (collector's own
            # last-measured proxy -> ... -> destination path, real UDP
            # data). This is only as good as how close the collector's own
            # network position is to the visitor's - if the collector is
            # in São Paulo and the visitor is in Chile asking about a
            # route to Lisbon, summing via São Paulo can over- or
            # under-estimate substantially depending on where the real
            # backbone path goes. The response says so explicitly so a UI
            # never presents this as ground truth.
            client_to_collector_str = qs.get("client_to_collector_ms", [""])[0]
            entry_str = qs.get("entry", [""])[0]
            to_str = qs.get("to", [""])[0]

            try:
                client_to_collector_ms = float(client_to_collector_str)
            except ValueError:
                self._send_json(
                    {"error": "usage: /estimate-route?client_to_collector_ms=N&entry=ip:port&to=ip:port"},
                    status=400,
                )
                return
            if client_to_collector_ms < 0 or client_to_collector_ms > 5000:
                self._send_json({"error": "client_to_collector_ms out of plausible range"}, status=400)
                return

            entry_addr = parse_addr_param(entry_str)
            to_addr = parse_addr_param(to_str)
            if not entry_addr or not to_addr:
                self._send_json(
                    {"error": "usage: /estimate-route?client_to_collector_ms=N&entry=ip:port&to=ip:port"},
                    status=400,
                )
                return

            result = dijkstra(entry_addr, to_addr)
            if result is None:
                self._send_json(
                    {"error": "no known route from entry proxy to destination", "entry": entry_str, "to": to_str},
                    status=404,
                )
                return

            proxy_leg_ms, path = result
            with graph.lock:
                path_geo = [_geo_to_dict(graph.geo.get(addr)) for addr in path]

            self._send_json(
                {
                    "estimate": True,
                    "caveat": (
                        "client_to_collector_ms is your real measured RTT to the collector, "
                        "not to the entry proxy. The total assumes your path to the entry proxy "
                        "is similar in cost to your path to this collector, which is only accurate "
                        "if the collector is network-close to you. Treat this as an approximation."
                    ),
                    "client_to_collector_ms": client_to_collector_ms,
                    "entry_to_destination_ms": proxy_leg_ms,
                    "estimated_total_ms": client_to_collector_ms + proxy_leg_ms,
                    "entry": entry_str,
                    "to": to_str,
                    "hops": len(path) - 1,
                    "path": [f"{ip}:{port}" for ip, port in path],
                    "path_geo": path_geo,
                }
            )
            return

        if parsed.path == "/compare":
            # For the map UI: draw a straight "direct" line vs a "via mesh"
            # line for the same (from, to) pair, with real numbers for
            # both. "Direct" here is the real measured edge from->to (no
            # intermediate hop) if one exists in the graph - not a browser
            # measurement, an actual pingstatus/meshstatus sample between
            # those two nodes. "Via mesh" is the cheapest multi-hop path
            # (Dijkstra), which may legitimately equal the direct edge
            # (0 hops) when direct already is the best route - the UI
            # should make that obvious rather than pretend mesh always wins.
            from_str = qs.get("from", [""])[0]
            to_str = qs.get("to", [""])[0]
            from_addr = parse_addr_param(from_str)
            to_addr = parse_addr_param(to_str)
            if not from_addr or not to_addr:
                self._send_json({"error": "usage: /compare?from=ip:port&to=ip:port"}, status=400)
                return

            with graph.lock:
                direct_edges = graph.edges.get(from_addr, [])
                direct_edge = next((e for e in direct_edges if (e.to_ip, e.to_port) == to_addr), None)

            direct_ping = direct_edge.ping if direct_edge else None

            mesh_result = dijkstra(from_addr, to_addr)
            if mesh_result is None:
                mesh_ping, mesh_path, mesh_geo = None, None, None
            else:
                mesh_ping, path = mesh_result
                with graph.lock:
                    mesh_geo = [_geo_to_dict(graph.geo.get(addr)) for addr in path]
                mesh_path = [f"{ip}:{port}" for ip, port in path]

            with graph.lock:
                from_geo = _geo_to_dict(graph.geo.get(from_addr))
                to_geo = _geo_to_dict(graph.geo.get(to_addr))

            gain_ms = None
            if direct_ping is not None and mesh_ping is not None:
                gain_ms = direct_ping - mesh_ping

            self._send_json(
                {
                    "from": from_str,
                    "to": to_str,
                    "from_geo": from_geo,
                    "to_geo": to_geo,
                    "direct": {
                        "known": direct_edge is not None,
                        "ping_ms": direct_ping,
                    },
                    "via_mesh": {
                        "known": mesh_result is not None,
                        "ping_ms": mesh_ping,
                        "hops": (len(mesh_path) - 1) if mesh_path else None,
                        "path": mesh_path,
                        "path_geo": mesh_geo,
                    },
                    "gain_ms": gain_ms,  # positive = mesh route is faster than direct
                }
            )
            return

        if parsed.path == "/top-routes":
            # The core question the whole tool exists to answer: for THIS
            # proxy, which other proxies is it fastest to reach, and via
            # what path (direct or N-hop)? Runs Dijkstra from `from` to
            # every other known node, keeps the cheapest N results. This
            # is proxy<->proxy ranking - the "client->proxy->...->proxy"
            # leg is handled separately by /estimate-route.
            from_str = qs.get("from", [""])[0]
            from_addr = parse_addr_param(from_str)
            if not from_addr:
                self._send_json({"error": "usage: /top-routes?from=ip:port&limit=10"}, status=400)
                return
            try:
                limit = int(qs.get("limit", ["10"])[0])
            except ValueError:
                limit = 10
            limit = max(1, min(limit, 50))

            with graph.lock:
                all_nodes = set(graph.edges.keys())
                for edges in graph.edges.values():
                    for e in edges:
                        all_nodes.add((e.to_ip, e.to_port))
                all_nodes.discard(from_addr)

            results = []
            for to_addr in all_nodes:
                r = dijkstra(from_addr, to_addr)
                if r is None:
                    continue
                total_ping, path = r
                with graph.lock:
                    to_geo = _geo_to_dict(graph.geo.get(to_addr))
                    path_geo = [_geo_to_dict(graph.geo.get(a)) for a in path]
                results.append(
                    {
                        "to": f"{to_addr[0]}:{to_addr[1]}",
                        "to_geo": to_geo,
                        "total_ping_ms": total_ping,
                        "hops": len(path) - 1,
                        "path": [f"{ip}:{port}" for ip, port in path],
                        "path_geo": path_geo,
                    }
                )

            results.sort(key=lambda r: r["total_ping_ms"])

            with graph.lock:
                from_geo = _geo_to_dict(graph.geo.get(from_addr))

            self._send_json(
                {
                    "from": from_str,
                    "from_geo": from_geo,
                    "top_routes": results[:limit],
                    "total_known_destinations": len(results),
                }
            )
            return

        if parsed.path == "/snapshot":
            self._send_json(
                {
                    "last_collected_at": graph.last_collected_at,
                    "mesh_capable_count": len(graph.mesh_capable),
                    "edges": graph.snapshot(),
                }
            )
            return

        if parsed.path == "/route":
            from_str = qs.get("from", [""])[0]
            to_str = qs.get("to", [""])[0]
            from_addr = parse_addr_param(from_str)
            to_addr = parse_addr_param(to_str)
            if not from_addr or not to_addr:
                self._send_json({"error": "usage: /route?from=ip:port&to=ip:port"}, status=400)
                return

            result = dijkstra(from_addr, to_addr)
            if result is None:
                self._send_json({"error": "no route found", "from": from_str, "to": to_str}, status=404)
                return

            total_ping, path = result
            with graph.lock:
                path_geo = [_geo_to_dict(graph.geo.get(addr)) for addr in path]
            self._send_json(
                {
                    "from": from_str,
                    "to": to_str,
                    "total_ping_ms": total_ping,
                    "hops": len(path) - 1,
                    "path": [f"{ip}:{port}" for ip, port in path],
                    "path_geo": path_geo,  # same length/order as "path"; entries are null where unknown
                }
            )
            return

        if parsed.path == "/geo":
            with graph.lock:
                self._send_json(
                    {f"{ip}:{port}": _geo_to_dict(info) for (ip, port), info in graph.geo.items()}
                )
            return

        if parsed.path == "/health":
            self._send_json({"status": "ok", "last_collected_at": graph.last_collected_at})
            return

        self._send_json({"error": "not found"}, status=404)


def _geo_to_dict(info: GeoInfo | None) -> dict | None:
    if info is None:
        return None
    return {
        "country_code": info.country_code,
        "country": info.country,
        "region": info.region,
        "city": info.city,
        "lat": info.lat,
        "lon": info.lon,
        "hostname": info.hostname,
        "is_proxy": info.is_proxy,
    }


def main() -> None:
    print("[collector] running initial collection cycle...")
    collect_once()

    recollect_thread = threading.Thread(target=recollect_loop, daemon=True)
    recollect_thread.start()

    server = ThreadingHTTPServer(("0.0.0.0", 8730), Handler)
    print(
        "[collector] serving on :8730 "
        "(/route, /estimate-route, /compare, /client-ping, /snapshot, /geo, /health)"
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
