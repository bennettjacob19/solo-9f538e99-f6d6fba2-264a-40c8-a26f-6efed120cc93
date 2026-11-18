"""Background connectivity probes against published ``host:port`` addresses.

A probe issues ``GET /healthz`` over plain HTTP with a hard two-second total
deadline (connect + request + response headers).  Results fall into one of:

* ``ok_200``          – the server answered with HTTP 200
* ``bad_status``      – the server answered with any other status code
* ``connect_failed``  – DNS/connection/HTTP protocol failure
* ``timeout``         – the 2s deadline elapsed before a response
* ``unprobeable``     – the stored address is not a probeable ``host:port``

The module deliberately depends only on the standard library and never on the
:mod:`svcdir.store` package, so it can be unit-tested with a fake runner and
used from a bounded thread pool without import cycles.
"""

from __future__ import annotations

import contextlib
import http.client
import ipaddress
import logging
import re
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

PROBE_TIMEOUT = 2.0
PROBE_PATH = "/healthz"
PROBE_USER_AGENT = "svcdir-probe/1.0"

logger = logging.getLogger("svcdir.probing")

_HOSTNAME_RE = re.compile(r"(?i)^[a-z0-9](?:[a-z0-9._-]{0,253}[a-z0-9])?$")

# Terminal statuses a worker (or the store) may persist.
TERMINAL_STATUSES = frozenset(
    {"ok_200", "bad_status", "connect_failed", "timeout", "unprobeable", "interrupted"}
)


class Unprobeable(ValueError):
    """Raised when an address cannot be turned into a probe target."""


def parse_probe_target(address: Any) -> tuple[str, int]:
    """Split a directory address into ``(host, port)`` or raise.

    Only plain ``host:port`` (and bracketed IPv6 ``[::1]:8080``) is
    probeable: schemes, paths, query strings, bare hosts without a port and
    non-numeric ports are rejected so the operator sees an explicit
    "address not probeable" result instead of a guessed request.
    """
    if not isinstance(address, str) or not address:
        raise Unprobeable("地址不可探测：目标地址必须是 host:port 形式")
    if any(ch in address for ch in "/?#") or "://" in address:
        raise Unprobeable("地址不可探测：仅支持 host:port，不能带协议、路径或查询串")
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in address):
        raise Unprobeable("地址不可探测：地址不能包含空白或控制字符")

    if address.startswith("["):
        bracket_end = address.find("]")
        if bracket_end < 0 or bracket_end + 1 >= len(address) or address[bracket_end + 1] != ":":
            raise Unprobeable("地址不可探测：IPv6 地址需写成 [地址]:端口")
        host = address[1:bracket_end]
        port_text = address[bracket_end + 2 :]
        try:
            ip = ipaddress.ip_address(host)
        except ValueError as exc:
            raise Unprobeable("地址不可探测：方括号内不是合法的 IP 地址") from exc
        if ip.version != 6:
            raise Unprobeable("地址不可探测：方括号形式仅用于 IPv6 地址")
    else:
        if address.count(":") != 1:
            raise Unprobeable("地址不可探测：需要明确的 host:port（IPv6 请用 [地址]:端口）")
        host, port_text = address.rsplit(":", 1)
        if not host:
            raise Unprobeable("地址不可探测：主机名不能为空")
        if not _HOSTNAME_RE.fullmatch(host):
            raise Unprobeable("地址不可探测：主机名只能包含字母、数字、点、连字符或下划线")
        try:
            ipaddress.IPv4Address(host)
        except ValueError:
            # A DNS hostname is fine; the regex above already shaped it.
            pass

    if not port_text.isdigit() or not (1 <= int(port_text) <= 65535):
        raise Unprobeable("地址不可探测：端口必须是 1-65535 的数字")
    return host, int(port_text)


def probe_address(address: str) -> dict:
    """Perform one probe.  Never raises: failures become result records."""
    try:
        host, port = parse_probe_target(address)
    except Unprobeable as exc:
        return {"status": "unprobeable", "status_code": None, "detail": str(exc)}

    deadline = time.monotonic() + PROBE_TIMEOUT
    sock: socket.socket | None = None
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise socket.timeout
        # Bound the whole attempt: the socket is created with the remaining
        # budget and re-armed before every subsequent blocking operation.
        sock = socket.create_connection((host, port), timeout=remaining)
        conn = http.client.HTTPConnection(
            host, port, timeout=max(0.01, deadline - time.monotonic())
        )
        conn.sock = sock
        sock.settimeout(max(0.01, deadline - time.monotonic()))
        conn.request(
            "GET",
            PROBE_PATH,
            headers={"User-Agent": PROBE_USER_AGENT, "Connection": "close"},
        )
        response = conn.getresponse()
        code = int(response.status)
        return {
            "status": "ok_200" if code == 200 else "bad_status",
            "status_code": code,
            "detail": f"GET http://{host}:{port}{PROBE_PATH} -> HTTP {code}",
        }
    except (socket.timeout, TimeoutError):
        return {
            "status": "timeout",
            "status_code": None,
            "detail": f"超过 {PROBE_TIMEOUT:.0f} 秒仍未完成连接或收到响应",
        }
    except (OSError, http.client.HTTPException) as exc:
        return {
            "status": "connect_failed",
            "status_code": None,
            "detail": f"{type(exc).__name__}: {exc}"[:300],
        }
    finally:
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.close()


class ProbeManager:
    """Owns the worker pool and persists results through the store.

    Each call to :meth:`start` inserts its own probe row, so a slow older
    task can never overwrite a newer task's result: workers update their own
    row by id and the read side only shows the highest id per service.
    """

    def __init__(
        self,
        store: Any,
        *,
        runner: Callable[[str], dict] = probe_address,
        max_workers: int = 8,
    ) -> None:
        self._store = store
        self._runner = runner
        self._pool = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="svcdir-probe"
        )

    def start(self, service: str) -> dict:
        record = self._store.create_probe(service)
        if record["status"] == "running":
            self._pool.submit(self._run, record["id"], record["address"])
        return record

    def _run(self, probe_id: int, address: str) -> None:
        try:
            result = self._runner(address)
        except Exception as exc:  # a buggy runner must not kill the worker
            logger.exception("probe %s failed unexpectedly", probe_id)
            result = {
                "status": "connect_failed",
                "status_code": None,
                "detail": f"核验器异常: {type(exc).__name__}: {exc}"[:300],
            }
        try:
            self._store.finish_probe(
                probe_id,
                status=str(result.get("status", "connect_failed")),
                status_code=result.get("status_code"),
                detail=str(result.get("detail", "")),
            )
        except Exception:
            logger.exception("could not persist result of probe %s", probe_id)

    def shutdown(self) -> None:
        # Pending probes stay "running" in the database; the next process
        # startup marks them interrupted.
        self._pool.shutdown(wait=False, cancel_futures=True)
