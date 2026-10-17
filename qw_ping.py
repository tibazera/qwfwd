#!/usr/bin/env python3
"""
QuakeWorld Server Pinger
Busca todos os servidores do master server e mede:
  - Ping médio, mínimo e máximo
  - Packet loss (%)
  - Jitter (desvio padrão entre os pings)

Master: master.quakeservers.net:27000
"""

import socket
import struct
import time
import statistics
import concurrent.futures

MASTER_HOST  = "master.quakeservers.net"
MASTER_PORT  = 27000
PROXY_PORT   = 30000

PING_TIMEOUT = 1.5    # segundos aguardando cada pacote
MAX_WORKERS  = 100    # servidores pingados em paralelo
PING_COUNT   = 5      # pacotes enviados por servidor (para loss/jitter)
PING_DELAY   = 0.1    # intervalo entre pacotes (segundos)

# ── Protocolo QW ──────────────────────────────────────────────────────────────
MASTER_QUERY     = b"\xc3\x0a"
MASTER_QUERY_ALT = b"\x63\x0a"
OOB              = b"\xff\xff\xff\xff"
STATUS_QUERY     = OOB + b"status\n"


def query_master(host: str, port: int) -> list[tuple[str, int]]:
    """Consulta o master server e retorna lista de (ip, port)."""
    servers = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(5.0)
    try:
        addr = (socket.gethostbyname(host), port)
        for query in (MASTER_QUERY, MASTER_QUERY_ALT):
            sock.sendto(query, addr)
            try:
                data, _ = sock.recvfrom(65535)
                servers = _parse_master_response(data)
                if servers:
                    break
            except socket.timeout:
                continue
    finally:
        sock.close()
    return servers


def _parse_master_response(data: bytes) -> list[tuple[str, int]]:
    servers = []
    offset = 6 if len(data) >= 12 else 0
    while offset + 6 <= len(data):
        chunk    = data[offset:offset + 6]
        ip_bytes = chunk[:4]
        port     = struct.unpack(">H", chunk[4:6])[0]
        if ip_bytes == b"\x00\x00\x00\x00" and port == 0:
            break
        ip = ".".join(str(b) for b in ip_bytes)
        if port > 0:
            servers.append((ip, port))
        offset += 6
    return servers


def ping_server(ip: str, port: int) -> dict:
    """
    Envia PING_COUNT pacotes UDP e coleta:
      ping_avg, ping_min, ping_max, jitter, loss_pct
    """
    result = {
        "ip":       ip,
        "port":     port,
        "online":   False,
        "ping_avg": None,
        "ping_min": None,
        "ping_max": None,
        "jitter":   None,
        "loss_pct": 100.0,
        "info":     "",
    }

    samples = []
    info_parsed = False

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(PING_TIMEOUT)

    try:
        for i in range(PING_COUNT):
            if i > 0:
                time.sleep(PING_DELAY)
            try:
                t0 = time.perf_counter()
                sock.sendto(STATUS_QUERY, (ip, port))
                data, _ = sock.recvfrom(4096)
                elapsed = (time.perf_counter() - t0) * 1000  # ms
                samples.append(round(elapsed, 2))

                if not info_parsed:
                    try:
                        text = data[4:].decode("latin-1", errors="replace")
                        result["info"] = text.split("\n")[0].strip()
                        info_parsed = True
                    except Exception:
                        pass

            except (socket.timeout, OSError):
                pass  # pacote perdido
    finally:
        sock.close()

    if samples:
        received = len(samples)
        result.update({
            "online":   True,
            "ping_avg": round(statistics.mean(samples), 1),
            "ping_min": round(min(samples), 1),
            "ping_max": round(max(samples), 1),
            "jitter":   round(statistics.stdev(samples), 1) if received > 1 else 0.0,
            "loss_pct": round((1 - received / PING_COUNT) * 100, 1),
        })

    return result


def quality_tag(r: dict) -> str:
    """Retorna uma tag de qualidade baseada em ping + loss + jitter."""
    if r["loss_pct"] >= 60:
        return "!! CRITICO"
    if r["loss_pct"] >= 20 or r["jitter"] > 50:
        return "! INSTAVEL"
    if r["ping_avg"] > 200 or r["loss_pct"] >= 10:
        return "~ ALTO PING"
    return "OK"


def fmt_row(r: dict) -> str:
    return (
        f"  {r['ip']}:{r['port']:<5}  "
        f"avg={r['ping_avg']:>7.1f}ms  "
        f"min={r['ping_min']:>7.1f}ms  "
        f"max={r['ping_max']:>7.1f}ms  "
        f"jitter={r['jitter']:>5.1f}ms  "
        f"loss={r['loss_pct']:>5.1f}%  "
        f"{quality_tag(r)}"
    )


def main():
    print(f"[*] Consultando master server {MASTER_HOST}:{MASTER_PORT}...")
    servers = query_master(MASTER_HOST, MASTER_PORT)

    if not servers:
        print("[!] Nenhum servidor recebido. Verifique conexão ou protocolo.")
        return

    print(f"[*] {len(servers)} servidores encontrados.")
    print(f"[*] Enviando {PING_COUNT} pacotes por servidor (timeout={PING_TIMEOUT}s)...\n")

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(ping_server, ip, port): (ip, port) for ip, port in servers}
        done = 0
        for future in concurrent.futures.as_completed(futures):
            done += 1
            r = future.result()
            results.append(r)
            if r["online"]:
                print(f"  [{done:>3}/{len(servers)}] {r['ip']:>15}:{r['port']:<5}  "
                      f"avg={r['ping_avg']:>6.1f}ms  loss={r['loss_pct']:>5.1f}%  "
                      f"jitter={r['jitter']:>5.1f}ms  {quality_tag(r)}")
            else:
                print(f"  [{done:>3}/{len(servers)}] {r['ip']:>15}:{r['port']:<5}  offline")

    # ── Separação proxies / servidores QW ─────────────────────────────────────
    proxies_online  = sorted([r for r in results if r["port"] == PROXY_PORT and r["online"]],     key=lambda x: x["ping_avg"])
    proxies_offline = sorted([r for r in results if r["port"] == PROXY_PORT and not r["online"]], key=lambda x: x["ip"])
    servers_online  = sorted([r for r in results if r["port"] != PROXY_PORT and r["online"]],     key=lambda x: x["ping_avg"])
    servers_offline = sorted([r for r in results if r["port"] != PROXY_PORT and not r["online"]], key=lambda x: (x["ip"], x["port"]))

    total_online  = len(proxies_online)  + len(servers_online)
    total_offline = len(proxies_offline) + len(servers_offline)

    # ── Resumo no terminal ────────────────────────────────────────────────────
    print(f"\n{'='*70}")
    print(f"  Total: {len(servers)}  |  Online: {total_online}  |  Offline: {total_offline}")
    print(f"  Proxies (:30000) : {len(proxies_online)} online / {len(proxies_offline)} offline")
    print(f"  Servidores QW    : {len(servers_online)} online / {len(servers_offline)} offline")

    if proxies_online:
        print(f"\n  Top 5 proxies (menor ping médio):")
        for r in proxies_online[:5]:
            print(f"    {r['ip']}:{r['port']}  avg={r['ping_avg']}ms  "
                  f"loss={r['loss_pct']}%  jitter={r['jitter']}ms  {quality_tag(r)}")

    print(f"{'='*70}")

    # ── Arquivo .txt ──────────────────────────────────────────────────────────
    output_file = "qw_results.txt"
    header_row  = (f"  {'IP:PORTA':<22}  {'AVG':>9}  {'MIN':>9}  {'MAX':>9}  "
                   f"{'JITTER':>9}  {'LOSS':>7}  STATUS\n")
    divider     = "  " + "-" * 85 + "\n"

    def write_section(f, title, rows, offline_rows=None):
        f.write(f"\n{title}\n")
        f.write("=" * 89 + "\n")
        if rows:
            f.write(header_row)
            f.write(divider)
            for r in rows:
                f.write(fmt_row(r) + "\n")
        else:
            f.write("  (nenhum online)\n")
        if offline_rows:
            f.write(f"\n  Offline / sem resposta ({len(offline_rows)}):\n")
            for r in offline_rows:
                f.write(f"  {r['ip']}:{r['port']}\n")

    with open(output_file, "w", encoding="utf-8") as f:
        f.write("QuakeWorld Server Ping Results\n")
        f.write(f"Master  : {MASTER_HOST}:{MASTER_PORT}\n")
        f.write(f"Data    : {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"Pacotes : {PING_COUNT} por servidor  |  Timeout: {PING_TIMEOUT}s\n")
        f.write(f"Total   : {len(servers)}  |  Online: {total_online}  |  Offline: {total_offline}\n")
        f.write(f"Proxies : {len(proxies_online)} online / {len(proxies_offline)} offline"
                f"    Servidores QW: {len(servers_online)} online / {len(servers_offline)} offline\n")

        write_section(f,
            f"[ PROXIES (porta {PROXY_PORT}) — ordenado por ping médio ]",
            proxies_online, proxies_offline)

        write_section(f,
            "[ SERVIDORES QW — ordenado por ping médio ]",
            servers_online, servers_offline)

    print(f"\n  Resultados salvos em: {output_file}\n")


if __name__ == "__main__":
    main()
