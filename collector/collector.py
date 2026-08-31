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
  1. Descobre servidores via masters QW públicos (protocolo nativo).
  2. Para cada um que responde na porta 30000 (convenção, não garantia):
     tenta meshstatus primeiro (dados ricos: ping+jitter+loss, várias
     arestas de uma vez). Se não responder, tenta pingstatus (legado,
     só ping direto, sem jitter/loss).
  3. Monta o grafo dirigido (arestas com origem explícita, nunca assume
     simetria — RTT pode divergir por direção).
  4. Expõe /route?from=X&to=Y calculando Dijkstra sob demanda, e
     /snapshot com o grafo bruto para debug/mapa.

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
class GraphState:
    # adjacency: (ip,port) -> list[Edge]. Directed - an edge measured by
    # node A about node B says nothing about B's measurement of A.
    edges: dict[tuple[str, int], list[Edge]] = field(default_factory=dict)
    mesh_capable: set[tuple[str, int]] = field(default_factory=set)
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
            return  # mesh-capable node already gave us richer data, no need for pingstatus too

        # fallback: legacy pingstatus, ping-only, no jitter/loss
        entries = probe_pingstatus(sock, addr)
        for ip, port, ping in entries:
            graph.add_edge(addr, Edge(ip, port, float(ping), source="pingstatus"))
    finally:
        sock.close()


def collect_once() -> None:
    servers = discover_servers()
    candidates = [(ip, port) for ip, port in servers if port == PROXY_PORT_HINT]
    print(f"[collector] discovered {len(servers)} servers, {len(candidates)} proxy candidates (port {PROXY_PORT_HINT})")

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [pool.submit(probe_one, addr) for addr in candidates]
        for f in as_completed(futures):
            f.result()  # propagate exceptions instead of swallowing them silently

    graph.last_collected_at = time.time()
    total_edges = sum(len(v) for v in graph.edges.values())
    print(f"[collector] cycle done: {len(graph.edges)} nodes, {total_edges} edges, {len(graph.mesh_capable)} mesh-capable")


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
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)

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
            self._send_json(
                {
                    "from": from_str,
                    "to": to_str,
                    "total_ping_ms": total_ping,
                    "hops": len(path) - 1,
                    "path": [f"{ip}:{port}" for ip, port in path],
                }
            )
            return

        if parsed.path == "/health":
            self._send_json({"status": "ok", "last_collected_at": graph.last_collected_at})
            return

        self._send_json({"error": "not found"}, status=404)


def main() -> None:
    print("[collector] running initial collection cycle...")
    collect_once()

    recollect_thread = threading.Thread(target=recollect_loop, daemon=True)
    recollect_thread.start()

    server = ThreadingHTTPServer(("0.0.0.0", 8730), Handler)
    print("[collector] serving on :8730 (/route?from=ip:port&to=ip:port, /snapshot, /health)")
    server.serve_forever()


if __name__ == "__main__":
    main()
