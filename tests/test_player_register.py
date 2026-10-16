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
