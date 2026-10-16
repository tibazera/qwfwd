"""
Protocolo binário QW/qwfwd usado pelo coletor: descoberta via master,
pingstatus (legado), meshprobe/meshstatus (fork feat/mesh-routing).

Todos os inteiros multi-byte no protocolo QW nativo são little-endian
(MSG_WriteShort/MSG_WriteLong em msg.c), exceto o endereço IP em si, que é
copiado como 4 bytes crus (não é um número a re-ordenar, é um octeto por
byte). A porta no protocolo de MASTER é big-endian (histórico, formato
próprio do master, não do QW). Cuidado: os dois formatos coexistem no mesmo
coletor porque falamos com masters E com qwfwd.
"""
from __future__ import annotations

import socket
import struct
from dataclasses import dataclass, field

OOB = b"\xff\xff\xff\xff"

# --- master discovery (big-endian port, legado, nunca muda) ---
MASTER_QUERY = b"c\n"


def parse_master_reply(data: bytes) -> list[tuple[str, int]]:
    """Master reply: 0xFF*4 + 'd' + '\\n' + N*(ip(4) + port(2, big-endian))."""
    if len(data) < 6:
        return []
    servers = []
    offset = 6
    while offset + 6 <= len(data):
        ip = ".".join(str(b) for b in data[offset : offset + 4])
        port = struct.unpack(">H", data[offset + 4 : offset + 6])[0]
        if port > 0:
            servers.append((ip, port))
        offset += 6
    return servers


# --- pingstatus (legado, qualquer qwfwd original responde) ---
PINGSTATUS_QUERY = OOB + b"pingstatus"


def parse_pingstatus_reply(data: bytes) -> list[tuple[str, int, int]]:
    """Reply: 0xFF*4 + 'n' + N*(ip(4) port(2,LE) ping(2,LE))."""
    if len(data) < 5 or data[4:5] != b"n":
        return []
    entries = []
    offset = 5
    while offset + 8 <= len(data):
        ip = ".".join(str(b) for b in data[offset : offset + 4])
        port = struct.unpack("<H", data[offset + 4 : offset + 6])[0]
        ping = struct.unpack("<h", data[offset + 6 : offset + 8])[0]
        if ping >= 0:
            entries.append((ip, port, ping))
        offset += 8
    return entries


# --- mesh protocol (fork feat/mesh-routing) ---
MESH_MAGIC = b"QM"
MESH_MSG_PINGSTATUS_REPLY = 1
MESH_MSG_MESHSTATUS_REPLY = 2


def build_meshprobe_query(nonce: int) -> bytes:
    return OOB + f"meshprobe {nonce}".encode()


def build_meshstatus_query(start_index: int = 0) -> bytes:
    return OOB + f"meshstatus {start_index}".encode()


def is_mesh_reply(data: bytes) -> bool:
    return len(data) >= 6 and data[4:6] == MESH_MAGIC


@dataclass
class Hop2Entry:
    ip: str
    port: int
    ping: int
    jitter: int
    loss_pct: int


@dataclass
class MeshPeerBlock:
    peer_ip: str
    peer_port: int
    age: int
    hops: list[Hop2Entry] = field(default_factory=list)


def parse_meshstatus_reply(data: bytes) -> tuple[list[MeshPeerBlock], int]:
    """Returns (peer_blocks, next_index). next_index == -1 means last page.
    next_index == -2 is a sentinel meaning "malformed/not a meshstatus reply"
    (never sent on the wire, used internally by callers to distinguish from
    a real -1)."""
    if len(data) < 15 or data[4:6] != MESH_MAGIC or data[6] != MESH_MSG_MESHSTATUS_REPLY:
        return [], -2

    next_index = struct.unpack("<i", data[11:15])[0]

    blocks = []
    offset = 15
    while offset + 10 <= len(data):
        peer_ip = ".".join(str(b) for b in data[offset : offset + 4])
        peer_port = struct.unpack("<H", data[offset + 4 : offset + 6])[0]
        age = struct.unpack("<h", data[offset + 6 : offset + 8])[0]
        count = struct.unpack("<h", data[offset + 8 : offset + 10])[0]
        offset += 10

        if count < 0 or count > 200:
            break  # corrupt/hostile, stop rather than trust it

        hops = []
        for _ in range(count):
            if offset + 12 > len(data):
                break  # truncated mid-entry, stop this block but keep what we parsed
            ip = ".".join(str(b) for b in data[offset : offset + 4])
            port = struct.unpack("<H", data[offset + 4 : offset + 6])[0]
            ping = struct.unpack("<h", data[offset + 6 : offset + 8])[0]
            jitter = struct.unpack("<h", data[offset + 8 : offset + 10])[0]
            loss_pct = struct.unpack("<h", data[offset + 10 : offset + 12])[0]
            if ping >= 0 and 0 <= loss_pct <= 100:
                hops.append(Hop2Entry(ip, port, ping, jitter, loss_pct))
            offset += 12

        blocks.append(MeshPeerBlock(peer_ip, peer_port, age, hops))

    return blocks, next_index


def build_pingstatus_query() -> bytes:
    return PINGSTATUS_QUERY


def udp_request(sock: socket.socket, addr: tuple[str, int], payload: bytes, bufsize: int = 8192) -> bytes | None:
    """Sends one datagram, returns the reply (or None on timeout/error).
    Caller owns the socket (and its timeout) so this can be reused across
    many sequential/parallel requests without re-creating sockets."""
    try:
        sock.sendto(payload, addr)
        data, from_addr = sock.recvfrom(bufsize)
        if from_addr[0] != addr[0]:
            return None  # reply from unexpected source, don't trust it
        return data
    except (socket.timeout, OSError):
        return None
