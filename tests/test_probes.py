"""Tests for the per-service connectivity probe feature."""

from __future__ import annotations

import http.server
import socket
import socketserver
import threading
import time

import pytest

from svcdir import DirectoryStore, create_app
from svcdir.errors import ConflictError, NotFoundError
from svcdir.probing import PROBE_TIMEOUT, ProbeManager, parse_probe_target, probe_address
from svcdir.probing import Unprobeable


# ---------------------------------------------------------------------------
# Local HTTP servers / sockets used as probe targets


class _HealthzHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - http.server API
        if self.path != "/healthz":
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(int(self.server.healthz_status))
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, *args):  # silence test output
        pass


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    healthz_status = 200


@pytest.fixture()
def http_server():
    server = _ThreadingHTTPServer(("127.0.0.1", 0), _HealthzHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture()
def dead_port():
    """A port that accepts nothing: close immediately after reserving it."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class _BlockingServer:
    """Accepts a TCP connection but never replies, to exercise the 2s cap."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        self.sock.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            # Hold the connection open without sending anything.
            conn.settimeout(None)
            self._stop.wait(4.0)
            conn.close()

    def stop(self):
        self._stop.set()
        self.sock.close()


# ---------------------------------------------------------------------------
# Address parsing: what counts as "unprobeable"


@pytest.mark.parametrize(
    "address",
    [
        "10.0.0.1:8080",
        "localhost:80",
        "orders-api.internal:1",
        "127.0.0.1:65535",
        "[::1]:8080",
        "[2001:db8::1]:443",
    ],
)
def test_parse_accepts_host_port(address):
    host, port = parse_probe_target(address)
    assert port >= 1


@pytest.mark.parametrize(
    "address",
    [
        "10.0.0.1",            # no port
        "10.0.0.1:0",          # port out of range
        "10.0.0.1:65536",
        "10.0.0.1:abc",
        "http://10.0.0.1:8080",  # scheme not allowed
        "10.0.0.1:8080/healthz",
        "10.0.0.1:8080?q=1",
        "10.0.0.1 :8080",      # whitespace
        "[::1]:8080/extra",
        "[1.2.3.4]:80",        # brackets only for IPv6
        "[not-an-ip]:80",
        ":80",                 # empty host
        "",
        None,
        12345,
    ],
)
def test_parse_rejects_unprobeable(address):
    with pytest.raises(Unprobeable):
        parse_probe_target(address)


# ---------------------------------------------------------------------------
# probe_address result classification


def test_probe_200(http_server):
    address = f"127.0.0.1:{http_server.server_port}"
    result = probe_address(address)
    assert result["status"] == "ok_200"
    assert result["status_code"] == 200
    assert "HTTP 200" in result["detail"]


def test_probe_non_200(http_server):
    http_server.healthz_status = 503
    result = probe_address(f"127.0.0.1:{http_server.server_port}")
    assert result["status"] == "bad_status"
    assert result["status_code"] == 503


def test_probe_connect_failure(dead_port):
    result = probe_address(f"127.0.0.1:{dead_port}")
    assert result["status"] == "connect_failed"
    assert result["status_code"] is None
    assert result["detail"]


def test_probe_timeout_is_capped_at_two_seconds():
    server = _BlockingServer()
    try:
        start = time.monotonic()
        result = probe_address(f"127.0.0.1:{server.port}")
        elapsed = time.monotonic() - start
    finally:
        server.stop()
    assert result["status"] == "timeout"
    # Hard ceiling on the whole attempt, not a per-socket-op timeout.
    assert elapsed <= PROBE_TIMEOUT + 0.5
    assert elapsed >= PROBE_TIMEOUT - 0.3


def test_probe_unprobeable_address_is_a_result_not_an_error():
    result = probe_address("not-a-host-port")
    assert result["status"] == "unprobeable"
    assert "不可探测" in result["detail"]
    assert result["status_code"] is None


def test_probe_unprobeable_scheme_address():
    result = probe_address("http://10.0.0.1:80/healthz")
    assert result["status"] == "unprobeable"


# ---------------------------------------------------------------------------
# Store-level probe records


@pytest.fixture()
def store(tmp_path):
    return DirectoryStore(tmp_path / "dir.sqlite3", restore_lease_seconds=30)


def seed(store, mapping):
    for service, address in mapping.items():
        store.upsert(service, address)


def test_create_probe_pins_current_version_and_address(store):
    seed(store, {"orders": "10.0.0.1:8080"})
    probe = store.create_probe("orders")
    assert probe["status"] == "running"
    assert probe["address"] == "10.0.0.1:8080"
    assert probe["version"] == store.resolve()["version"]
    assert probe["fresh"] is True


def test_create_probe_unknown_service_is_404(store):
    with pytest.raises(NotFoundError):
        store.create_probe("ghost")


def test_finish_probe_persists_terminal_result(store):
    seed(store, {"orders": "10.0.0.1:8080"})
    probe = store.create_probe("orders")
    finished = store.finish_probe(probe["id"], status="ok_200", status_code=200,
                                  detail="HTTP 200")
    assert finished["status"] == "ok_200"
    assert finished["status_code"] == 200
    assert finished["finished_at"] is not None
    latest = store.list_latest_probes()
    assert len(latest) == 1
    assert latest[0]["id"] == probe["id"]
    assert latest[0]["fresh"] is True


def test_finish_probe_rejects_bad_status(store):
    seed(store, {"orders": "10.0.0.1:8080"})
    probe = store.create_probe("orders")
    with pytest.raises(Exception):
        store.finish_probe(probe["id"], status="bogus")


def test_finished_probe_cannot_be_updated_again(store):
    seed(store, {"orders": "10.0.0.1:8080"})
    probe = store.create_probe("orders")
    store.finish_probe(probe["id"], status="timeout")
    with pytest.raises(ConflictError):
        store.finish_probe(probe["id"], status="ok_200", status_code=200)


# ---------------------------------------------------------------------------
# Staleness: any directory change invalidates the old result


def test_probe_goes_stale_after_address_change(store):
    seed(store, {"orders": "10.0.0.1:8080"})
    probe = store.create_probe("orders")
    store.finish_probe(probe["id"], status="ok_200", status_code=200)
    assert store.list_latest_probes()[0]["fresh"] is True

    store.upsert("orders", "10.0.0.2:8080")
    latest = store.list_latest_probes()[0]
    assert latest["fresh"] is False
    assert latest["address"] == "10.0.0.1:8080"  # the probed address is preserved


def test_probe_goes_stale_even_when_address_is_republished_identical(store):
    seed(store, {"orders": "10.0.0.1:8080"})
    probe = store.create_probe("orders")
    store.finish_probe(probe["id"], status="ok_200", status_code=200)
    # Re-publish the very same string: still a new version, so stale.
    store.upsert("orders", "10.0.0.1:8080")
    assert store.list_latest_probes()[0]["fresh"] is False


def test_probe_goes_stale_after_delete_and_recreate(store):
    seed(store, {"orders": "10.0.0.1:8080"})
    probe = store.create_probe("orders")
    store.finish_probe(probe["id"], status="ok_200", status_code=200)
    store.delete("orders")
    store.upsert("orders", "10.0.0.1:8080")  # identical address, new identity
    latest = store.list_latest_probes()[0]
    assert latest["fresh"] is False
    assert latest["address"] == "10.0.0.1:8080"


def test_probe_goes_stale_after_restore(store):
    seed(store, {"orders": "10.0.0.1:8080"})
    store.create_backup("snap")
    probe = store.create_probe("orders")
    store.finish_probe(probe["id"], status="ok_200", status_code=200)
    store.upsert("orders", "10.0.0.9:9000")
    store.restore_backup("snap")
    # After the restore the address string matches again, but the version
    # moved, so the old probe must not claim to represent the current state.
    latest = store.list_latest_probes()[0]
    assert latest["fresh"] is False
    assert store.resolve("orders")["address"] == "10.0.0.1:8080"


def test_unrelated_service_change_does_not_share_staleness(store):
    seed(store, {"a": "10.0.0.1:80", "b": "10.0.0.2:80"})
    probe_a = store.create_probe("a")
    store.finish_probe(probe_a["id"], status="ok_200", status_code=200)
    store.upsert("b", "10.0.0.3:80")  # bumps global version
    # Versions are global, so a's record is conservatively expired too:
    # the UI may only show a result as current for the current version.
    assert store.list_latest_probes()[0]["fresh"] is False


# ---------------------------------------------------------------------------
# Latest task wins; older tasks never overwrite the newer one


def test_only_latest_probe_per_service_is_listed(store):
    seed(store, {"orders": "10.0.0.1:8080"})
    first = store.create_probe("orders")
    second = store.create_probe("orders")
    third = store.create_probe("orders")
    # Older task finishes *after* the newer one (out-of-order completion).
    store.finish_probe(third["id"], status="ok_200", status_code=200)
    store.finish_probe(first["id"], status="timeout")
    store.finish_probe(second["id"], status="connect_failed")

    latest = store.list_latest_probes()
    assert len(latest) == 1
    assert latest[0]["id"] == third["id"]
    assert latest[0]["status"] == "ok_200"
    # Individual rows are still retrievable by id.
    assert store.get_probe(first["id"])["status"] == "timeout"
    assert store.get_probe(999999) is None


def test_probe_history_is_pruned_per_service(store):
    seed(store, {"orders": "10.0.0.1:8080"})
    import sqlite3

    for _ in range(55):
        store.create_probe("orders")
    with sqlite3.connect(store.db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM probes WHERE service='orders'").fetchone()[0]
    assert count == 50


# ---------------------------------------------------------------------------
# Restart semantics


def test_running_probes_marked_interrupted_on_restart(tmp_path):
    db = tmp_path / "dir.sqlite3"
    first = DirectoryStore(db)
    first.upsert("orders", "10.0.0.1:8080")
    done_probe = first.create_probe("orders")
    first.finish_probe(done_probe["id"], status="ok_200", status_code=200)
    open_probe = first.create_probe("orders")  # never finishes; process dies

    restarted = DirectoryStore(db)
    restarted.initialize()
    probes = {p["id"]: p for p in restarted.list_latest_probes()}
    # The latest record is the interrupted one...
    latest = restarted.list_latest_probes()[0]
    assert latest["id"] == open_probe["id"]
    assert latest["status"] == "interrupted"
    assert latest["finished_at"] is not None
    # ...while the earlier completed record remains viewable by id.
    assert restarted.get_probe(done_probe["id"])["status"] == "ok_200"


def test_late_worker_write_after_restart_does_not_resurrect_interrupted(tmp_path):
    db = tmp_path / "dir.sqlite3"
    first = DirectoryStore(db)
    first.upsert("orders", "10.0.0.1:8080")
    probe = first.create_probe("orders")

    restarted = DirectoryStore(db)
    restarted.initialize()
    assert restarted.get_probe(probe["id"])["status"] == "interrupted"
    # A zombie worker from the old process must not overwrite the marker.
    with pytest.raises(ConflictError):
        restarted.finish_probe(probe["id"], status="ok_200", status_code=200)


# ---------------------------------------------------------------------------
# ProbeManager: background execution, latest-wins under real threads


class _Gate:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def make_runner(self, result):
        gate = self

        def runner(address):
            gate.entered.set()
            gate.release.wait(5)
            return result

        return runner


def test_manager_runs_in_background_and_persists(tmp_path, http_server):
    store = DirectoryStore(tmp_path / "m.sqlite3")
    address = f"127.0.0.1:{http_server.server_port}"
    store.upsert("orders", address)
    manager = ProbeManager(store)
    try:
        record = manager.start("orders")
        assert record["status"] == "running"
        deadline = time.time() + 5
        while time.time() < deadline:
            latest = store.list_latest_probes()[0]
            if latest["status"] != "running":
                break
            time.sleep(0.02)
        latest = store.list_latest_probes()[0]
        assert latest["status"] == "ok_200"
        assert latest["status_code"] == 200
        assert latest["address"] == address
    finally:
        manager.shutdown()


def test_manager_slow_older_task_never_overwrites_newer(tmp_path):
    store = DirectoryStore(tmp_path / "m.sqlite3")
    store.upsert("orders", "10.0.0.1:8080")
    gate = _Gate()
    manager = ProbeManager(store, runner=gate.make_runner(
        {"status": "timeout", "status_code": None, "detail": "old slow task"}
    ))
    try:
        old = manager.start("orders")
        assert gate.entered.wait(5)
        # A second probe supersedes the first; give it an instant runner.
        fast_manager = ProbeManager(
            store,
            runner=lambda addr: {
                "status": "ok_200", "status_code": 200, "detail": "new fast task"
            },
        )
        try:
            new = fast_manager.start("orders")
            # Wait for the new task to finish while the old one is blocked.
            deadline = time.time() + 5
            while time.time() < deadline:
                if store.get_probe(new["id"])["status"] != "running":
                    break
                time.sleep(0.02)
            assert store.get_probe(new["id"])["status"] == "ok_200"
            assert store.list_latest_probes()[0]["id"] == new["id"]

            # Now let the old task complete.
            gate.release.set()
            deadline = time.time() + 5
            while time.time() < deadline:
                if store.get_probe(old["id"])["status"] != "running":
                    break
                time.sleep(0.02)
            assert store.get_probe(old["id"])["status"] == "timeout"
            # The displayed (latest) result is still the newer task.
            displayed = store.list_latest_probes()[0]
            assert displayed["id"] == new["id"]
            assert displayed["status"] == "ok_200"
        finally:
            fast_manager.shutdown()
    finally:
        manager.shutdown()


def test_manager_runner_exception_becomes_connect_failed(tmp_path):
    store = DirectoryStore(tmp_path / "m.sqlite3")
    store.upsert("orders", "10.0.0.1:8080")

    def boom(address):
        raise RuntimeError("runner exploded")

    manager = ProbeManager(store, runner=boom)
    try:
        record = manager.start("orders")
        deadline = time.time() + 5
        while time.time() < deadline:
            if store.get_probe(record["id"])["status"] != "running":
                break
            time.sleep(0.02)
        assert store.get_probe(record["id"])["status"] == "connect_failed"
        assert "runner exploded" in store.get_probe(record["id"])["detail"]
    finally:
        manager.shutdown()


def test_probes_allowed_during_restore_and_do_not_block_operations(tmp_path):
    store = DirectoryStore(tmp_path / "m.sqlite3", restore_lease_seconds=30)
    store.upsert("orders", "10.0.0.1:8080")
    store.create_backup("snap")

    class _Hooks:
        def __init__(self):
            self.reached = threading.Event()
            self.release = threading.Event()

        def install(self, store):
            def waiter(payload):
                self.reached.set()
                self.release.wait(10)

            store._hooks["before_restore_switch"] = waiter
            return self

    gate = _Hooks().install(store)
    thread = threading.Thread(target=store.restore_backup, args=("snap",))
    thread.start()
    assert gate.reached.wait(10)
    try:
        # Probe registration still works mid-restore; reads do too.
        probe = store.create_probe("orders")
        assert probe["status"] == "running"
        assert store.resolve("orders")["address"] == "10.0.0.1:8080"
        store.finish_probe(probe["id"], status="ok_200", status_code=200)
    finally:
        gate.release.set()
        thread.join(10)
    # Restore bumps the version, so the just-finished probe is now expired.
    assert store.list_latest_probes()[0]["fresh"] is False


# ---------------------------------------------------------------------------
# HTTP integration


@pytest.fixture()
def app(tmp_path):
    store = DirectoryStore(tmp_path / "web.sqlite3")
    application = create_app(store)
    application.config.update(TESTING=True)
    return application


def test_http_start_probe_returns_202_and_background_result(app, http_server):
    client = app.test_client()
    address = f"127.0.0.1:{http_server.server_port}"
    assert client.post("/api/services", json={"service": "orders", "address": address}).status_code == 200

    resp = client.post("/api/services/orders/probes")
    assert resp.status_code == 202
    body = resp.get_json()
    assert body["ok"] is True
    assert body["probe"]["status"] == "running"
    assert body["probe"]["address"] == address

    deadline = time.time() + 5
    state = None
    while time.time() < deadline:
        state = client.get("/api/state").get_json()
        if state["probes"] and state["probes"][0]["status"] != "running":
            break
        time.sleep(0.05)
    probe = state["probes"][0]
    assert probe["status"] == "ok_200"
    assert probe["status_code"] == 200
    assert probe["fresh"] is True

    listing = client.get("/api/probes").get_json()["probes"]
    assert listing[0]["service"] == "orders"
    latest = client.get("/api/services/orders/probes/latest").get_json()["probe"]
    assert latest["id"] == probe["id"]


def test_http_probe_unknown_service_is_404(app):
    resp = app.test_client().post("/api/services/ghost/probes")
    assert resp.status_code == 404
    assert resp.get_json()["error"]["code"] == "not_found"


def test_http_latest_probe_missing_is_404(app):
    resp = app.test_client().get("/api/services/ghost/probes/latest")
    assert resp.status_code == 404


def test_http_unprobeable_address_reports_unprobeable(app):
    client = app.test_client()
    client.post("/api/services", json={"service": "weird", "address": "no-port-here"})
    resp = client.post("/api/services/weird/probes")
    assert resp.status_code == 202
    deadline = time.time() + 5
    status = None
    while time.time() < deadline:
        probes = client.get("/api/probes").get_json()["probes"]
        if probes and probes[0]["status"] != "running":
            status = probes[0]["status"]
            break
        time.sleep(0.02)
    assert status == "unprobeable"


def test_http_directory_operations_stay_available_while_probe_running(app):
    client = app.test_client()
    # Point at an address that never completes quickly: unprobeable is
    # instant, so use a closed port (fast connect failure) plus normal ops.
    client.post("/api/services", json={"service": "a", "address": "127.0.0.1:9"})
    client.post("/api/services/a/probes")
    # These must remain fully functional while the probe is in flight.
    assert client.post("/api/services",
                       json={"service": "b", "address": "127.0.0.1:10"}).status_code == 200
    assert client.post("/api/backups", json={"name": "snap"}).status_code == 201
    assert client.get("/resolve?name=a").status_code == 200
    assert client.post("/api/backups/snap/restore").status_code == 200
    assert client.get("/resolve").status_code == 200


def test_html_page_renders_probe_controls_and_keeps_csp(app):
    client = app.test_client()
    client.post("/api/services", json={"service": "orders", "address": "10.0.0.1:8080"})
    page = client.get("/")
    text = page.get_data(as_text=True)
    assert "probe-btn" in text
    assert 'data-result-for="orders"' in text
    assert "连通性核验" in text
    # No inline scripts: probe logic stays in the external app.js.
    assert "script-src 'self'" in page.headers["Content-Security-Policy"]
    assert "<script>" not in text


def test_probe_result_does_not_change_resolve(app, http_server):
    client = app.test_client()
    address = f"127.0.0.1:{http_server.server_port}"
    client.post("/api/services", json={"service": "orders", "address": address})
    client.post("/api/services/orders/probes")
    deadline = time.time() + 5
    while time.time() < deadline:
        probes = client.get("/api/probes").get_json()["probes"]
        if probes and probes[0]["status"] != "running":
            break
        time.sleep(0.05)
    resolved = client.get("/resolve?name=orders").get_json()
    assert resolved["address"] == address
    assert "probe" not in resolved
