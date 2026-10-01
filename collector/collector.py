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
  2. Verifica todos os endereços descobertos, independentemente da porta:
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
import ipaddress
import json
import os
import secrets
import socket
import threading
import time
import urllib.error
import urllib.request
import uuid as uuid_lib
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

# free, no-key geo-IP lookup for a *player's* request IP (not a game
# server - those already have real geo via qw-data). Used only to sort
# /player-targets so a player's own country/continent comes first; never
# authoritative, always falls back to the unsorted list on any failure.
PLAYER_GEOIP_URL = "http://ip-api.com/json/{ip}?fields=status,countryCode,continent"
PLAYER_GEOIP_TIMEOUT = 2.0

# POST /player-route sample cap: covers the full mesh (183 known hosts as
# of writing) with headroom to grow, while still bounding the worst-case
# cost of the per-request dijkstra_with_extra_edges call against an
# unauthenticated public endpoint - a raised but still finite ceiling, not
# "no limit" (see body-size cap below for the hard stop on that axis).
PLAYER_ROUTE_MAX_SAMPLES = 500

# TTL for the per-uuid cache written by POST /player-route and read by
# GET /player-route-cached - ping changes fast, so stale data past this
# window is worse than falling through to the site's other fallbacks
# (local bridge / STUN / geographic estimate).
PLAYER_SAMPLES_TTL_SECONDS = 120

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
MESH_PAGE_DELAY_SECONDS = 1.05  # qwfwd permits one mesh reply/source/second
ROUTE_MAX_EDGE_AGE_SECONDS = 900
ROUTE_MAX_LOSS_PCT = 25
ROUTE_JITTER_WEIGHT = 0.50
ROUTE_LOSS_WEIGHT_MS = 2.0
ROUTE_RELAY_PENALTY_MS = 3.0


@dataclass
class Edge:
    to_ip: str
    to_port: int
    ping: float
    jitter: int | None = None
    loss_pct: int | None = None
    source: str = "unknown"  # "meshstatus" | "pingstatus"
    age_seconds: int = 0


@dataclass
class GeoInfo:
    country_code: str
    country: str
    region: str
    city: str
    lat: float | None
    lon: float | None
    hostname: str
    is_proxy: bool
    server_version: str = ""
    ktx_version: str = ""
    gamedir: str = ""
    sv_antilag: str = ""
    protocol_extensions: str = ""


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
    discovery: dict = field(default_factory=dict)
    confirmed_proxies: set[tuple[str, int]] = field(default_factory=set)
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

    def snapshot(self, top_n: int = 12) -> dict:
        # /snapshot feeds the map's edge-drawing pass, which only ever
        # renders the top-8 cheapest edges per node anyway - shipping every
        # raw edge (600+ candidates x hundreds of edges each) bloated this
        # to 4.4MB and made it unreliable to fetch on mobile/slow networks.
        # Cap server-side to the cheapest `top_n` per node. Full-fidelity
        # routing still goes through /top-routes and /route, which read
        # self.edges directly (not this method).
        with self.lock:
            return {
                f"{ip}:{port}": [
                    {
                        "to": f"{e.to_ip}:{e.to_port}",
                        "ping": e.ping,
                        "jitter": e.jitter,
                        "loss_pct": e.loss_pct,
                        "source": e.source,
                        "age_seconds": e.age_seconds,
                    }
                    for e in sorted(edges, key=lambda e: e.ping)[:top_n]
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
            coords = (None, None)

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
                    lat=float(coords[0]) if coords[0] is not None else None,
                    lon=float(coords[1]) if coords[1] is not None else None,
                    hostname=hostname,
                    is_proxy=is_proxy,
                    server_version=version,
                    ktx_version=str(settings.get("ktxver", "")),
                    gamedir=str(settings.get("*gamedir", "")),
                    sv_antilag=str(settings.get("sv_antilag", "")),
                    protocol_extensions=str(settings.get("*z_ext", "")),
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
        # meshstatus pages share qwfwd's anti-amplification rate limiter.
        # Asking for the next page immediately makes the daemon silently
        # drop it, leaving the graph with only the first peer block.
        time.sleep(MESH_PAGE_DELAY_SECONDS)
    return all_blocks


def probe_pingstatus(sock: socket.socket, addr: tuple[str, int]) -> list[tuple[str, int, int]] | None:
    reply = protocol.udp_request(sock, addr, protocol.build_pingstatus_query())
    if reply is None or not reply.startswith(protocol.OOB + b"n") or (len(reply) - 5) % 8:
        return None
    return protocol.parse_pingstatus_reply(reply)


def public_endpoint(addr: tuple[str, int]) -> bool:
    try:
        return ipaddress.IPv4Address(addr[0]).is_global and 0 < addr[1] <= 65535
    except (ValueError, TypeError):
        return False


def probe_one(addr: tuple[str, int], target_graph: GraphState) -> set[tuple[str, int]]:
    discovered = set()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(PROBE_TIMEOUT)
    try:
        # Always retain this qwfwd's own direct measurements. meshstatus is
        # the cache reported by its peers; it complements pingstatus and is
        # not a replacement for the node's own outbound edges.
        entries = probe_pingstatus(sock, addr)
        if entries is not None:
            with target_graph.lock:
                target_graph.edges.setdefault(addr, [])
                target_graph.confirmed_proxies.add(addr)
        for ip, port, ping in entries or []:
            discovered.add((ip, port))
            target_graph.add_edge(addr, Edge(ip, port, float(ping), source="pingstatus"))

        mesh_blocks = probe_meshstatus(sock, addr)
        if mesh_blocks is not None:
            with target_graph.lock:
                target_graph.mesh_capable.add(addr)
                target_graph.edges.setdefault(addr, [])
                target_graph.confirmed_proxies.add(addr)
            for block in mesh_blocks:
                peer_addr = (block.peer_ip, block.peer_port)
                if not public_endpoint(peer_addr):
                    continue
                discovered.add(peer_addr)
                for hop in block.hops:
                    target_graph.add_edge(
                        peer_addr,
                        Edge(hop.ip, hop.port, float(hop.ping), hop.jitter,
                             hop.loss_pct, source="meshstatus",
                             age_seconds=max(0, block.age)),
                    )
    finally:
        sock.close()
    return {peer for peer in discovered if public_endpoint(peer)}


def collect_once() -> None:
    servers = discover_servers()
    next_graph = GraphState()

    qw_data_entries = fetch_qw_data_servers()
    with next_graph.lock:
        for addr, geo_info in qw_data_entries:
            next_graph.geo[addr] = geo_info
        # A physical host keeps the same location on every qwfwd port.
        # Apply our operator-confirmed datacenter coordinates by IP while
        # preserving the production instance's own hostname and proxy flag.
        operator_geo_by_ip = {ip: info for (ip, _port), info in PINNED_PROXY_GEO.items()}
        for addr, current in list(next_graph.geo.items()):
            confirmed = operator_geo_by_ip.get(addr[0])
            if confirmed is not None:
                next_graph.geo[addr] = GeoInfo(
                    confirmed.country_code,
                    confirmed.country,
                    confirmed.region,
                    confirmed.city,
                    confirmed.lat,
                    confirmed.lon,
                    current.hostname or confirmed.hostname,
                    current.is_proxy,
                    current.server_version,
                    current.ktx_version,
                    current.gamedir,
                    current.sv_antilag,
                    current.protocol_extensions,
                )
        # our own pinned test proxies use isolated ports (305xx) qw-data
        # has never heard of - give them known-real coordinates directly so
        # they still show up on the map even though no third-party source
        # lists them.
        for addr, geo_info in PINNED_PROXY_GEO.items():
            # These are operator-confirmed datacenter locations and must
            # override third-party IP geolocation.  In particular,
            # 201.23.3.62 is physically in Fortaleza even though qw-data's
            # IP database currently reports Franca.
            next_graph.geo[addr] = geo_info

    # combine both discovery sources: master-reported servers (authoritative
    # for "is it alive right now") plus qw-data proxy entries (may include
    # proxies momentarily missed by a master query, or proxies that opted
    # out of master registration but still answer the protocol directly)
    qw_data_proxy_addrs = {addr for addr, info in qw_data_entries if info.is_proxy}
    candidates = set(servers) | {addr for addr, _ in qw_data_entries}
    candidates |= qw_data_proxy_addrs
    candidates |= set(PINNED_PROXIES)

    print(
        f"[collector] discovered {len(servers)} servers via masters, "
        f"{len(qw_data_entries)} entries via qw-data ({len(qw_data_proxy_addrs)} proxies), "
        f"{len(PINNED_PROXIES)} pinned test proxies, "
        f"{len(candidates)} total proxy candidates to probe"
    )

    candidates = {addr for addr in candidates if public_endpoint(addr)}
    visited = set()
    confirmed = set()
    # ponytail: cap external discovery at 4096 endpoints per cycle; expose truncation.
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        pending = candidates.copy()
        while pending and len(visited) < 4096:
            batch = sorted(pending)[:4096 - len(visited)]
            visited.update(batch)
            futures = {pool.submit(probe_one, addr, next_graph): addr for addr in batch}
            for future in as_completed(futures):
                candidates.update(future.result())
                addr = futures[future]
                if addr in next_graph.confirmed_proxies:
                    # Reported peer origins alone do not confirm a responding proxy.
                    # probe_one creates empty origins only for valid protocol replies.
                    confirmed.add(addr)
            pending = candidates - visited
    # Only independently confirmed proxies can be relay origins.
    next_graph.edges = {addr: edges for addr, edges in next_graph.edges.items() if addr in confirmed}
    for addr in confirmed:
        if addr not in next_graph.geo:
            next_graph.geo[addr] = GeoInfo('', '', '', '', None, None, f'{addr[0]}:{addr[1]}', True)
    next_graph.discovery = {
        'candidates': len(candidates), 'probed': len(visited),
        'confirmed': len(confirmed), 'not_confirmed': len(visited - confirmed),
        'remaining': len(candidates - visited), 'complete': candidates <= visited,
    }

    next_graph.last_collected_at = time.time()
    # Publish one complete collection atomically.  Readers never see a
    # half-rebuilt graph and edges which disappeared this cycle cannot live
    # forever as phantom routes.
    with graph.lock, next_graph.lock:
        graph.edges = next_graph.edges
        graph.mesh_capable = next_graph.mesh_capable
        graph.confirmed_proxies = next_graph.confirmed_proxies
        graph.discovery = next_graph.discovery
        graph.geo = next_graph.geo
        graph.last_collected_at = next_graph.last_collected_at
    total_edges = sum(len(v) for v in next_graph.edges.values())
    print(
        f"[collector] cycle done: {len(next_graph.edges)} nodes, {total_edges} edges, "
        f"{len(next_graph.mesh_capable)} mesh-capable, {len(next_graph.geo)} with known coordinates"
    )


def recollect_loop() -> None:
    while True:
        try:
            collect_once()
        except Exception as e:
            print(f"[collector] collection cycle failed: {e}")
        time.sleep(RECOLLECT_INTERVAL_SECONDS)


def dijkstra(start: tuple[str, int], end: tuple[str, int]) -> tuple[float, list[tuple[str, int]]] | None:
    return dijkstra_with_extra_edges(start, end)


def path_quality_cost(path: list[tuple[str, int]]) -> float:
    """Return the same ping+jitter+loss+relay score used by Dijkstra."""
    with graph.lock:
        adjacency = {k: list(v) for k, v in graph.edges.items()}
    total = 0.0
    end = path[-1]
    for source, target in zip(path, path[1:]):
        costs = []
        for edge in adjacency.get(source, []):
            if (edge.to_ip, edge.to_port) != target or edge.age_seconds > ROUTE_MAX_EDGE_AGE_SECONDS:
                continue
            if edge.loss_pct is not None and edge.loss_pct > ROUTE_MAX_LOSS_PCT:
                continue
            cost = edge.ping + ROUTE_JITTER_WEIGHT * float(edge.jitter or 0) + ROUTE_LOSS_WEIGHT_MS * float(edge.loss_pct or 0)
            costs.append(cost + (ROUTE_RELAY_PENALTY_MS if target != end else 0))
        if costs:
            total += min(costs)
    return total


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


def _lookup_player_country(player_ip: str) -> str | None:
    """Best-effort country-code lookup for a player's request IP, via a
    free no-key geo-IP service. Returns None on any failure (timeout,
    malformed response, service down) - callers must treat that as "no
    preference", never as an error."""
    try:
        url = PLAYER_GEOIP_URL.format(ip=player_ip)
        with urllib.request.urlopen(url, timeout=PLAYER_GEOIP_TIMEOUT) as resp:
            data = json.loads(resp.read())
        if data.get("status") != "success":
            return None
        return data.get("countryCode")
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
        return None


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
        end_geo = graph.geo.get(end)
    proxy_nodes = set(adjacency)
    local_game = end_geo is not None and not end_geo.is_proxy
    if extra_adjacency:
        for node, edges in extra_adjacency.items():
            adjacency.setdefault(node, []).extend(edges)

    dist = {start: 0.0}
    raw_ping = {start: 0.0}
    prev: dict[tuple[str, int], tuple[str, int]] = {}
    visited: set[tuple[str, int]] = set()
    pq = [(0.0, start)]
    end_node = None

    while pq:
        d, node = heapq.heappop(pq)
        if node in visited:
            continue
        visited.add(node)
        if node == end:
            end_node = node
            break
        if local_game and node in proxy_nodes and node[0] == end[0]:
            dist[end] = d
            raw_ping[end] = raw_ping[node]
            prev[end] = node
            end_node = end
            break
        for edge in adjacency.get(node, []):
            if edge.age_seconds > ROUTE_MAX_EDGE_AGE_SECONDS or (edge.loss_pct is not None and edge.loss_pct > ROUTE_MAX_LOSS_PCT):
                continue
            neighbor = (edge.to_ip, edge.to_port)
            jitter = float(edge.jitter or 0)
            loss = float(edge.loss_pct or 0)
            edge_cost = (edge.ping + ROUTE_JITTER_WEIGHT * jitter
                         + ROUTE_LOSS_WEIGHT_MS * loss)
            if neighbor != end:
                edge_cost += ROUTE_RELAY_PENALTY_MS
            nd = d + edge_cost
            if neighbor not in dist or nd < dist[neighbor]:
                dist[neighbor] = nd
                raw_ping[neighbor] = raw_ping[node] + edge.ping
                prev[neighbor] = node
                heapq.heappush(pq, (nd, neighbor))

    if end_node is None:
        return None

    path = [end_node]
    seen = {end_node}
    node = end_node
    while node != start:
        node = prev.get(node)
        if node is None or node in seen:
            return None
        seen.add(node)
        path.append(node)
    path.reverse()
    return raw_ping[end_node], path


def parse_addr_param(value: str) -> tuple[str, int] | None:
    if ":" not in value:
        return None
    ip, _, port_str = value.rpartition(":")
    try:
        return ip, int(port_str)
    except ValueError:
        return None


# caps total concurrent request handling and per-IP request rate: the
# collector is unauthenticated and CORS-open by design (see _send_json),
# so without this an unbounded ThreadingHTTPServer lets any caller spawn a
# thread per connection and repeatedly trigger expensive endpoints
# (/top-routes runs one Dijkstra search per known destination) - cheap
# thread/CPU exhaustion from a single unauthenticated client.
_MAX_CONCURRENT_REQUESTS = 32
_request_slots = threading.BoundedSemaphore(_MAX_CONCURRENT_REQUESTS)

_RATE_LIMIT_WINDOW = 1.0  # seconds
_RATE_LIMIT_PER_IP = 20   # requests/window/IP
_rate_lock = threading.Lock()
_rate_track: dict[str, tuple[float, int]] = {}  # ip -> (window_start, count)


def _rate_limited(ip: str) -> bool:
    now = time.time()
    with _rate_lock:
        window_start, count = _rate_track.get(ip, (now, 0))
        if now - window_start >= _RATE_LIMIT_WINDOW:
            window_start, count = now, 0
        count += 1
        _rate_track[ip] = (window_start, count)
        return count > _RATE_LIMIT_PER_IP


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


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # keep stdout to collection-cycle logs only

    def handle_one_request(self) -> None:
        if _rate_limited(self.client_address[0]):
            self.close_connection = True
            try:
                self.send_response(429)
                self.send_header("Content-Length", "0")
                self.end_headers()
            except Exception:
                pass
            return

        if not _request_slots.acquire(blocking=False):
            self.close_connection = True
            try:
                self.send_response(503)
                self.send_header("Content-Length", "0")
                self.end_headers()
            except Exception:
                pass
            return
        try:
            super().handle_one_request()
        finally:
            _request_slots.release()

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

        if parsed.path == "/routes-to":
            # Batch version of /route for a client picking an entry point:
            # given a destination and a list of candidate entry proxies
            # (typically ones the client already measured a local RTT to),
            # return one Dijkstra route per entry so the caller can add its
            # own client->entry cost and pick the cheapest total in one
            # request, instead of firing N sequential /route calls per
            # connection attempt.
            to_str = qs.get("to", [""])[0]
            entries_str = qs.get("entries", [""])[0]
            to_addr = parse_addr_param(to_str)
            if not to_addr or not entries_str:
                self._send_json(
                    {"error": "usage: /routes-to?to=ip:port&entries=ip1:port1,ip2:port2,..."},
                    status=400,
                )
                return

            entry_strs = [e for e in entries_str.split(",") if e]
            if len(entry_strs) > 50:
                self._send_json({"error": "too many entries, max 50"}, status=400)
                return

            results = []
            for entry_str in entry_strs:
                entry_addr = parse_addr_param(entry_str)
                if not entry_addr:
                    results.append({"entry": entry_str, "known": False})
                    continue
                r = dijkstra(entry_addr, to_addr)
                if r is None:
                    results.append({"entry": entry_str, "known": False})
                    continue
                total_ping, path = r
                with graph.lock:
                    path_geo = [_geo_to_dict(graph.geo.get(a)) for a in path]
                results.append(
                    {
                        "entry": entry_str,
                        "known": True,
                        "total_ping_ms": total_ping,
                        "quality_cost_ms": path_quality_cost(path),
                        "hops": len(path) - 1,
                        "path": [f"{ip}:{port}" for ip, port in path],
                        "path_geo": path_geo,
                    }
                )

            results.sort(key=lambda r: r["total_ping_ms"] if r["known"] else float("inf"))

            if qs.get("format", [""])[0] == "plain":
                # C clients (unezQuake) have no JSON parser in the build -
                # emit the one thing the caller actually needs to act on
                # (entry|total_ping_ms|hops|proxylist) as newline-separated
                # plain text, sorted best-first. proxylist is pre-formatted
                # in cl_proxyaddr's own "@"-joined syntax (intermediate
                # hops only, final destination excluded - same convention
                # as EX_browser_pathfind.c's SB_PingTree_GetProxyString) so
                # the client can Cvar_Set it directly with no parsing.
                lines = []
                for r in results:
                    if not r["known"]:
                        lines.append(f"{r['entry']}|-1|0|")
                        continue
                    proxylist = "@".join(r["path"][:-1])  # drop final hop (the destination itself)
                    lines.append(f"{r['entry']}|{r['total_ping_ms']:.0f}|{r['hops']}|{proxylist}")
                body = "\n".join(lines) + "\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-store")
                encoded = body.encode("utf-8")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
                return

            self._send_json({"to": to_str, "routes": results})
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
                # Only rank actual qwfwd proxies as destinations. pingstatus
                # replies list every host:port a proxy knows about (its own
                # game/QTV ports included), so raw edge targets mix in
                # non-proxy noise - graph.edges.keys() is proxy-only (we
                # only ever UDP-probe proxy candidates), so intersect
                # instead of also adding raw edge targets.
                all_nodes = set(graph.edges.keys())
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

        if parsed.path == "/player-targets":
            # Candidate list for a player's client-side ping app: one
            # entry per known HOST (deduplicated across its ports - a
            # 5-port game server previously ate 5 of the scan budget for
            # what a player's ping app treats as one place to try).
            #
            # Picking WHICH port to keep is not "lowest number wins" - on
            # a single host, the low round ports (28000, 30000, ...) are
            # infrastructure (qwfwd itself, or QTV, the spectator/demo
            # relay), never where a player actually connects to play; the
            # real game port is the "X501"-style one (27501, 28501, ...).
            # A prior version picked the lowest port and would silently
            # always offer QTV or the proxy port instead of the real
            # server on hosts that expose both - excluded here by
            # `is_proxy` (already tracked) and by "QTV" appearing in the
            # server's own version string (QTV always self-identifies,
            # unlike port-number conventions that can vary by host).
            with graph.lock:
                by_ip: dict[str, tuple[int, GeoInfo | None]] = {}
                for (ip, port), info in graph.geo.items():
                    if info is not None:
                        if info.is_proxy:
                            continue
                        if "qtv" in info.server_version.lower():
                            continue
                    if ip not in by_ip or port < by_ip[ip][0]:
                        by_ip[ip] = (port, info)
                targets = [
                    {"ip": ip, "port": port, "geo": _geo_to_dict(info)}
                    for ip, (port, info) in by_ip.items()
                ]

            # Best-effort: put the player's own country/continent first so
            # the scan (capped at 50 by the client) actually reaches nearby
            # servers instead of whatever happened to iterate first. Never
            # blocks or errors the response - on any geo-IP failure, the
            # list is returned in its original (unsorted) order.
            player_ip = qs.get("ip", [""])[0] or self.client_address[0]
            player_country = _lookup_player_country(player_ip)
            if player_country:
                def _priority(t: dict) -> int:
                    geo = t.get("geo") or {}
                    if geo.get("country_code") == player_country:
                        return 0
                    return 1
                targets.sort(key=_priority)

            targets = targets[:200]
            self._send_json({"targets": targets})
            return

        if parsed.path == "/snapshot":
            self._send_json(
                {
                    "last_collected_at": graph.last_collected_at,
                    "mesh_capable_count": len(graph.mesh_capable),
                    "discovery": graph.discovery,
                    "proxies": [f"{ip}:{port}" for ip, port in sorted(graph.confirmed_proxies)],
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

    def do_POST(self) -> None:
        if _rate_limited(self.client_address[0]):
            self._send_json({"error": "rate limited"}, status=429)
            return

        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)

        if parsed.path == "/player-register":
            self._handle_player_register()
            return

        if parsed.path == "/player-link":
            self._handle_player_link()
            return

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
        if not isinstance(samples, list) or not samples or len(samples) > PLAYER_ROUTE_MAX_SAMPLES:
            self._send_json(
                {"error": f"samples must be a non-empty list, max {PLAYER_ROUTE_MAX_SAMPLES} entries"},
                status=400,
            )
            return

        _store_player_samples(uuid, samples)
        result, error = _route_from_samples(uuid, samples, to_addr)
        if error == "no_samples":
            self._send_json({"error": "no valid samples (all rejected or targets unknown)"}, status=400)
            return
        if error == "no_route":
            self._send_json({"error": "no route found", "to": qs.get("to", [""])[0]}, status=404)
            return

        result["to"] = qs.get("to", [""])[0]
        self._send_json(result)

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
        "server_version": info.server_version,
        "ktx_version": info.ktx_version,
        "gamedir": info.gamedir,
        "sv_antilag": info.sv_antilag,
        "protocol_extensions": info.protocol_extensions,
    }


def main() -> None:
    global _players
    _players = _load_players()

    print("[collector] running initial collection cycle...")
    collect_once()

    recollect_thread = threading.Thread(target=recollect_loop, daemon=True)
    recollect_thread.start()

    server = ThreadingHTTPServer(("0.0.0.0", 8730), Handler)
    print(
        "[collector] serving on :8730 "
        "(/route, /routes-to, /top-routes, /estimate-route, /compare, /client-ping, /snapshot, /geo, /health, "
        "/player-targets, /player-route, /player-route-cached, /player-register, /player-link)"
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
