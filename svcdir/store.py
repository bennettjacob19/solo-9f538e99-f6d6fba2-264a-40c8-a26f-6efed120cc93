"""Consistent, versioned service-discovery directory backed by SQLite.

Design summary
-------------
* The directory is never edited in place.  Every accepted change creates a new
  immutable *version* that contains the complete directory, and publishing a
  version is a single ``UPDATE meta ... current_version`` statement.
* A reader pins one version inside one SQLite read transaction (WAL snapshot
  isolation), so it can never observe a mixture of the directory before and
  after a restore.  Old versions and their rows are never deleted.
* A named backup pins the current version id at a single commit point, so it is
  always a complete directory even while writes keep arriving.
* A restore is: mark a lease -> validate the backup -> atomically create a new
  version copied from the backup and switch ``current_version``.  The switch is
  one transaction: after a crash/restart the directory is either the original
  version or the complete restored version, never a partial one.
* While a restore holds a live lease, writes are rejected with
  ``RestoreInProgress``.  An expired lease (crashed restorer) is reclaimed, and
  a restorer that lost its lease aborts instead of overwriting newer state.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .errors import (
    BackupNameTaken,
    BackupValidationError,
    ConflictError,
    DatabaseBusy,
    DatabaseError,
    DirectoryError,
    InputError,
    NotFoundError,
    RestoreInProgress,
)

SCHEMA_VERSION = 1
BACKUP_FORMAT = "svcdir-backup-v1"
MAX_SERVICES = 5000
MAX_ADDRESS_LENGTH = 512
MAX_BACKUP_NAME_LENGTH = 64
MAX_NOTE_LENGTH = 200

SERVICE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")

_RESTORE_KEYS = (
    "restore_state",
    "restore_backup",
    "restore_owner",
    "restore_started_at",
    "restore_lease_until",
)


# ---------------------------------------------------------------------------
# Input validation


def _text(raw: Any, field: str) -> str:
    if not isinstance(raw, str):
        raise InputError(f"{field}必须是字符串")
    return raw.strip()


def validate_service_name(raw: Any) -> str:
    name = _text(raw, "服务名")
    if not SERVICE_NAME_RE.fullmatch(name):
        raise InputError(
            "服务名不合法：需以字母或数字开头，仅可包含字母、数字、'_'、'.'、':'、'-'，"
            "长度 1-128"
        )
    return name


def validate_address(raw: Any) -> str:
    address = _text(raw, "目标地址")
    if not address:
        raise InputError("目标地址不能为空")
    if len(address) > MAX_ADDRESS_LENGTH:
        raise InputError(f"目标地址过长（最多 {MAX_ADDRESS_LENGTH} 个字符）")
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in address):
        raise InputError("目标地址不能包含空白或控制字符")
    return address


def validate_backup_name(raw: Any) -> str:
    name = _text(raw, "备份名称")
    if not name:
        raise InputError("备份名称不能为空")
    if len(name) > MAX_BACKUP_NAME_LENGTH:
        raise InputError(f"备份名称过长（最多 {MAX_BACKUP_NAME_LENGTH} 个字符）")
    if "/" in name or "\\" in name or name in (".", ".."):
        raise InputError("备份名称不能包含路径分隔符")
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in name):
        raise InputError("备份名称不能包含空白或控制字符")
    return name


def _clean_note(raw: Any) -> str:
    if not isinstance(raw, str):
        return ""
    return raw.strip()[:MAX_NOTE_LENGTH]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _compute_digest(backup_name: str, version_id: int, entries: tuple[tuple[str, str], ...]) -> str:
    payload = json.dumps(
        {
            "format": BACKUP_FORMAT,
            "backup": backup_name,
            "version": version_id,
            "entries": [{"service": s, "address": a} for s, a in entries],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _translate_db_error(exc: sqlite3.Error) -> DirectoryError:
    message = str(exc).lower()
    if "locked" in message or "busy" in message:
        return DatabaseBusy()
    return DatabaseError()


@dataclass(frozen=True)
class BackupValidation:
    name: str
    version_id: int
    entry_count: int
    digest: str
    created_at: str


class _PinnedView:
    """Read-only accessor bound to one already-started read transaction."""

    def __init__(self, conn: sqlite3.Connection, store: "DirectoryStore") -> None:
        self._conn = conn
        self._store = store
        self.version = store._current_version(conn)
        self.restore = store._restore_summary(conn)

    @property
    def restore_in_progress(self) -> bool:
        return self.restore.get("state") == "restoring"

    def resolve(self, name: str | None = None) -> dict:
        service = validate_service_name(name) if name is not None else None
        if service is None:
            rows = self._store._entries(self._conn, self.version)
            return {
                "version": self.version,
                "count": len(rows),
                "services": dict(rows),
                "restore_in_progress": self.restore_in_progress,
                "restore": self.restore,
            }
        row = self._conn.execute(
            "SELECT address FROM entries WHERE version_id=? AND service=?",
            (self.version, service),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"未知服务: {service}")
        return {
            "version": self.version,
            "name": service,
            "address": str(row["address"]),
            "restore_in_progress": self.restore_in_progress,
            "restore": self.restore,
        }


# ---------------------------------------------------------------------------
# Store


class DirectoryStore:
    def __init__(
        self,
        db_path: str | Path,
        *,
        timeout: float = 15.0,
        restore_lease_seconds: float = 30.0,
        hooks: dict[str, Callable[[dict], None]] | None = None,
    ) -> None:
        self.db_path = str(db_path)
        self._timeout = max(1.0, float(timeout))
        self._lease_seconds = max(0.1, float(restore_lease_seconds))
        self._hooks = dict(hooks or {})
        self._init_lock = threading.RLock()
        self._initialized = False

    # -- infrastructure ---------------------------------------------------

    def _ensure_ready(self) -> None:
        if self._initialized:
            return
        with self._init_lock:
            if not self._initialized:
                self.initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path, timeout=self._timeout, isolation_level=None
        )
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={int(self._timeout * 1000)}")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    @contextlib.contextmanager
    def _txn(self, write: bool):
        conn = self._connect()
        try:
            try:
                conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            except sqlite3.Error as exc:
                raise _translate_db_error(exc) from exc
            try:
                yield conn
            except BaseException as exc:
                with contextlib.suppress(sqlite3.Error):
                    conn.execute("ROLLBACK")
                if isinstance(exc, sqlite3.Error):
                    raise _translate_db_error(exc) from exc
                raise
            else:
                try:
                    conn.execute("COMMIT")
                except sqlite3.Error as exc:
                    with contextlib.suppress(sqlite3.Error):
                        conn.execute("ROLLBACK")
                    raise _translate_db_error(exc) from exc
        finally:
            conn.close()

    def initialize(self) -> None:
        path = Path(self.db_path)
        if path.parent and str(path.parent) not in ("", "."):
            path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.Error as exc:
                raise _translate_db_error(exc) from exc
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.Error as exc:
                raise _translate_db_error(exc) from exc
            try:
                self._create_or_check_schema(conn)
                conn.execute("COMMIT")
            except BaseException as exc:
                with contextlib.suppress(sqlite3.Error):
                    conn.execute("ROLLBACK")
                if isinstance(exc, sqlite3.Error):
                    raise _translate_db_error(exc) from exc
                raise
        finally:
            conn.close()
        with self._init_lock:
            self._initialized = True
        # A previous process may have died mid-restore.  Its lease is either
        # still live (another worker owns it) or expired and safe to reclaim.
        self.recover_stale_restore()

    def _create_or_check_schema(self, conn: sqlite3.Connection) -> None:
        has_meta = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
        ).fetchone()
        if has_meta:
            row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if row is None or str(row["value"]) != str(SCHEMA_VERSION):
                raise RuntimeError(
                    f"数据库 schema 版本不兼容：期望 {SCHEMA_VERSION}，实际 "
                    f"{None if row is None else row['value']}"
                )
            return
        conn.execute(
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute(
            """
            CREATE TABLE versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                kind TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE entries (
                version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE RESTRICT,
                service TEXT NOT NULL,
                address TEXT NOT NULL,
                PRIMARY KEY (version_id, service)
            )
            """
        )
        conn.execute(
            "CREATE INDEX idx_entries_service ON entries(service)"
        )
        conn.execute(
            """
            CREATE TABLE backups (
                name TEXT PRIMARY KEY,
                version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE RESTRICT,
                created_at TEXT NOT NULL,
                digest TEXT NOT NULL,
                entry_count INTEGER NOT NULL
            )
            """
        )
        now = _utc_now_iso()
        cur = conn.execute(
            "INSERT INTO versions(created_at, kind, note) VALUES (?, 'initial', '初始空目录')",
            (now,),
        )
        initial_version = int(cur.lastrowid)
        self._meta_set(conn, "schema_version", str(SCHEMA_VERSION))
        self._meta_set(conn, "current_version", str(initial_version))
        self._meta_set(conn, "restore_state", "idle")

    # -- meta helpers -----------------------------------------------------

    @staticmethod
    def _meta_get(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return default if row is None else str(row["value"])

    @staticmethod
    def _meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    @staticmethod
    def _meta_del(conn: sqlite3.Connection, key: str) -> None:
        conn.execute("DELETE FROM meta WHERE key=?", (key,))

    def _current_version(self, conn: sqlite3.Connection) -> int:
        raw = self._meta_get(conn, "current_version")
        try:
            return int(raw)
        except (TypeError, ValueError):
            raise ConflictError("目录状态损坏：current_version 无效", code="corrupt_state")

    def _set_current(self, conn: sqlite3.Connection, version_id: int) -> None:
        self._meta_set(conn, "current_version", str(version_id))

    def _restore_state(self, conn: sqlite3.Connection) -> str:
        return self._meta_get(conn, "restore_state", "idle") or "idle"

    def _lease_expired(self, conn: sqlite3.Connection) -> bool:
        raw = self._meta_get(conn, "restore_lease_until")
        if raw is None:
            return True
        try:
            return float(raw) <= time.time()
        except ValueError:
            return True

    def _lock_path(self) -> str:
        return self.db_path + ".restore.lock"

    def _restorer_alive(self) -> bool | None:
        """Whether the restore lock is held, or ``None`` if unknowable.

        The OS drops the lock when the holding process dies, so a restarted
        container can reclaim an abandoned restore immediately instead of
        waiting out the lease.  ``None`` means this filesystem does not support
        probing (e.g. some network filesystems); callers then fall back to the
        lease.
        """
        try:
            fd = os.open(self._lock_path(), os.O_CREAT | os.O_RDWR, 0o600)
        except OSError:
            return None
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                    return True
                return None
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        finally:
            os.close(fd)

    def _restore_is_stale(self, conn: sqlite3.Connection) -> bool:
        alive = self._restorer_alive()
        if alive is False:
            return True
        if alive is True:
            # A live lock holder always wins over an expired lease: a paused
            # process must not lose its restore to a sibling.
            return False
        return self._lease_expired(conn)

    def _acquire_restore_lock(self, attempts: int = 5) -> int | None:
        """Hold an exclusive OS lock for this restore; None if unsupported."""
        for attempt in range(attempts):
            try:
                fd = os.open(self._lock_path(), os.O_CREAT | os.O_RDWR, 0o600)
            except OSError:
                return None
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                os.close(fd)
                if exc.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                    # A concurrent liveness probe holds the lock for a
                    # microsecond; retry briefly before declaring a conflict.
                    time.sleep(0.01 * (attempt + 1))
                    continue
                return None
            return fd
        raise RestoreInProgress()

    @staticmethod
    def _release_restore_lock(fd: int | None) -> None:
        if fd is None:
            return
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(fd)

    def _clear_restore(self, conn: sqlite3.Connection) -> None:
        for key in _RESTORE_KEYS:
            self._meta_del(conn, key)
        self._meta_set(conn, "restore_state", "idle")

    def _restore_summary(self, conn: sqlite3.Connection) -> dict:
        state = self._restore_state(conn)
        if state != "restoring":
            return {"state": "idle"}
        if self._restore_is_stale(conn):
            # Do not mutate inside a read transaction: just report it as
            # effectively idle.  The next writer/wake-up reclaims the lease.
            return {"state": "idle", "stale_lease": True}
        return {
            "state": "restoring",
            "backup": self._meta_get(conn, "restore_backup"),
            "started_at": self._meta_get(conn, "restore_started_at"),
            "lease_expires_at": self._meta_get(conn, "restore_lease_until"),
        }

    def _require_writable(self, conn: sqlite3.Connection) -> None:
        state = self._restore_state(conn)
        if state == "restoring":
            # A live restore holds the OS lock.  Clearing a dead restorer's
            # marker here is safe: holding the write transaction implies the
            # restorer's switch transaction either aborted or already committed
            # and cleared the marker.
            alive = self._restorer_alive()
            if alive is True or (alive is None and not self._lease_expired(conn)):
                raise RestoreInProgress()
            self._clear_restore(conn)
        elif state != "idle":
            raise ConflictError(f"目录处于未知状态: {state}", code="corrupt_state")

    # -- version helpers --------------------------------------------------

    def _create_version(
        self, conn: sqlite3.Connection, source_version: int, kind: str, note: str
    ) -> int:
        cur = conn.execute(
            "INSERT INTO versions(created_at, kind, note) VALUES (?, ?, ?)",
            (_utc_now_iso(), kind, note),
        )
        new_version = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO entries(version_id, service, address) "
            "SELECT ?, service, address FROM entries WHERE version_id=?",
            (new_version, source_version),
        )
        return new_version

    def _entries(self, conn: sqlite3.Connection, version_id: int) -> tuple[tuple[str, str], ...]:
        rows = conn.execute(
            "SELECT service, address FROM entries WHERE version_id=? ORDER BY service",
            (version_id,),
        ).fetchall()
        return tuple((str(r["service"]), str(r["address"])) for r in rows)

    def _fire(self, name: str, payload: dict) -> None:
        hook = self._hooks.get(name)
        if hook is not None:
            hook(payload)

    # -- public reads -----------------------------------------------------

    def resolve(self, name: str | None = None) -> dict:
        self._ensure_ready()
        with self.read_view() as view:
            return view.resolve(name)

    def list_backups(self) -> list[dict]:
        self._ensure_ready()
        with self._txn(write=False) as conn:
            rows = conn.execute(
                "SELECT name, version_id, created_at, digest, entry_count "
                "FROM backups ORDER BY created_at DESC, name"
            ).fetchall()
        return [
            {
                "name": str(r["name"]),
                "version": int(r["version_id"]),
                "created_at": str(r["created_at"]),
                "digest": str(r["digest"]),
                "entry_count": int(r["entry_count"]),
            }
            for r in rows
        ]

    def snapshot(self) -> dict:
        """Coherent view used by the HTML page and ``/api/state``."""
        self._ensure_ready()
        with self._txn(write=False) as conn:
            version = self._current_version(conn)
            rows = self._entries(conn, version)
            restore = self._restore_summary(conn)
            backups = [
                {
                    "name": str(r["name"]),
                    "version": int(r["version_id"]),
                    "created_at": str(r["created_at"]),
                    "digest": str(r["digest"]),
                    "entry_count": int(r["entry_count"]),
                }
                for r in conn.execute(
                    "SELECT name, version_id, created_at, digest, entry_count "
                    "FROM backups ORDER BY created_at DESC, name"
                ).fetchall()
            ]
        return {
            "version": version,
            "count": len(rows),
            "services": [
                {"service": s, "address": a} for s, a in rows
            ],
            "restore": restore,
            "backups": backups,
        }

    def ping(self) -> None:
        self._ensure_ready()
        with self._txn(write=False) as conn:
            conn.execute("SELECT 1")

    @contextlib.contextmanager
    def read_view(self):
        """Pin one version for the lifetime of the context.

        A query request that started before a restore committed keeps seeing
        exactly one complete version, even if it re-reads rows after the switch.
        """
        self._ensure_ready()
        with self._txn(write=False) as conn:
            yield _PinnedView(conn, self)

    # -- writes -----------------------------------------------------------

    def upsert(self, service: Any, address: Any, note: Any = "") -> dict:
        svc = validate_service_name(service)
        addr = validate_address(address)
        clean_note = _clean_note(note)
        self._ensure_ready()
        with self._txn(write=True) as conn:
            self._require_writable(conn)
            previous = self._current_version(conn)
            exists = conn.execute(
                "SELECT 1 FROM entries WHERE version_id=? AND service=?",
                (previous, svc),
            ).fetchone()
            if not exists and conn.execute(
                "SELECT COUNT(*) AS n FROM entries WHERE version_id=?", (previous,)
            ).fetchone()["n"] >= MAX_SERVICES:
                raise InputError(f"服务数量超出上限 {MAX_SERVICES}")
            new_version = self._create_version(
                conn, previous, "update", clean_note or f"设置 {svc}"
            )
            conn.execute(
                "INSERT INTO entries(version_id, service, address) VALUES (?, ?, ?) "
                "ON CONFLICT(version_id, service) DO UPDATE SET address=excluded.address",
                (new_version, svc, addr),
            )
            self._set_current(conn, new_version)
        return {
            "version": new_version,
            "previous_version": previous,
            "service": svc,
            "address": addr,
        }

    def delete(self, service: Any) -> dict:
        svc = validate_service_name(service)
        self._ensure_ready()
        with self._txn(write=True) as conn:
            self._require_writable(conn)
            previous = self._current_version(conn)
            new_version = self._create_version(
                conn, previous, "update", f"删除 {svc}"
            )
            cur = conn.execute(
                "DELETE FROM entries WHERE version_id=? AND service=?",
                (new_version, svc),
            )
            if cur.rowcount == 0:
                raise NotFoundError(f"未知服务: {svc}")
            self._set_current(conn, new_version)
        return {
            "version": new_version,
            "previous_version": previous,
            "service": svc,
            "deleted": True,
        }

    def create_backup(self, name: Any) -> dict:
        backup_name = validate_backup_name(name)
        self._ensure_ready()
        with self._txn(write=True) as conn:
            if conn.execute("SELECT 1 FROM backups WHERE name=?", (backup_name,)).fetchone():
                raise BackupNameTaken(f"备份名称已存在: {backup_name}")
            version = self._current_version(conn)
            entries = self._entries(conn, version)
            if len(entries) > MAX_SERVICES:
                raise InputError(f"服务数量超出上限 {MAX_SERVICES}")
            digest = _compute_digest(backup_name, version, entries)
            created_at = _utc_now_iso()
            conn.execute(
                "INSERT INTO backups(name, version_id, created_at, digest, entry_count) "
                "VALUES (?, ?, ?, ?, ?)",
                (backup_name, version, created_at, digest, len(entries)),
            )
        self._fire("after_backup_created", {"name": backup_name, "version": version})
        return {
            "name": backup_name,
            "version": version,
            "created_at": created_at,
            "digest": digest,
            "entry_count": len(entries),
        }

    # -- backup validation ------------------------------------------------

    def validate_backup(self, name: Any) -> dict:
        backup_name = validate_backup_name(name)
        self._ensure_ready()
        with self._txn(write=False) as conn:
            result = self._validate_backup(conn, backup_name)
        return {
            "name": result.name,
            "version": result.version_id,
            "entry_count": result.entry_count,
            "digest": result.digest,
            "created_at": result.created_at,
            "valid": True,
        }

    def _validate_backup(self, conn: sqlite3.Connection, backup_name: str) -> BackupValidation:
        row = conn.execute(
            "SELECT name, version_id, created_at, digest, entry_count "
            "FROM backups WHERE name=?",
            (backup_name,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"备份不存在: {backup_name}")
        version_id = int(row["version_id"])
        problems: list[str] = []
        if conn.execute("SELECT 1 FROM versions WHERE id=?", (version_id,)).fetchone() is None:
            raise BackupValidationError(
                f"备份 {backup_name} 校验失败",
                details=["备份指向的目录版本不存在"],
            )
        entries = self._entries(conn, version_id)
        recorded_count = int(row["entry_count"])
        if len(entries) > MAX_SERVICES:
            problems.append(f"条目数量 {len(entries)} 超过上限 {MAX_SERVICES}")
        if len(entries) != recorded_count:
            problems.append(
                f"条目数量不一致：备份记录 {recorded_count}，实际 {len(entries)}"
            )
        if len({s for s, _ in entries}) != len(entries):
            problems.append("存在重复的服务名")
        seen: set[str] = set()
        for service, address in entries:
            if service in seen:
                continue
            seen.add(service)
            try:
                validate_service_name(service)
            except DirectoryError as exc:
                problems.append(f"服务名 {service!r} 无效：{exc.message}")
            try:
                validate_address(address)
            except DirectoryError as exc:
                problems.append(f"服务 {service!r} 的地址无效：{exc.message}")
            if len(problems) >= 8:
                break
        stored_digest = str(row["digest"])
        if not DIGEST_RE.fullmatch(stored_digest):
            problems.append("备份摘要格式无效")
        else:
            recomputed = _compute_digest(backup_name, version_id, entries)
            if not hmac.compare_digest(stored_digest, recomputed):
                problems.append("内容摘要不匹配（备份可能被篡改或损坏）")
        if problems:
            raise BackupValidationError(
                f"备份 {backup_name} 校验失败，未修改当前目录",
                details=problems[:8],
            )
        return BackupValidation(
            name=str(row["name"]),
            version_id=version_id,
            entry_count=len(entries),
            digest=stored_digest,
            created_at=str(row["created_at"]),
        )

    # -- restore ----------------------------------------------------------

    def recover_stale_restore(self) -> bool:
        """Clear an abandoned restore marker whose owner is gone."""
        self._ensure_ready()
        with self._txn(write=True) as conn:
            if self._restore_state(conn) == "restoring" and self._restore_is_stale(conn):
                self._clear_restore(conn)
                return True
        return False

    def _start_lease_keeper(self, owner: str, stop_event: threading.Event) -> threading.Thread:
        interval = max(0.1, self._lease_seconds / 3.0)

        def renew() -> None:
            while not stop_event.wait(interval):
                try:
                    with self._txn(write=True) as conn:
                        if (
                            self._restore_state(conn) != "restoring"
                            or self._meta_get(conn, "restore_owner") != owner
                        ):
                            return
                        self._meta_set(
                            conn,
                            "restore_lease_until",
                            f"{time.time() + self._lease_seconds:.6f}",
                        )
                except Exception:  # lease renewal is best-effort; switch rechecks
                    continue

        thread = threading.Thread(
            target=renew, name="svcdir-restore-lease", daemon=True
        )
        thread.start()
        return thread

    def restore_backup(self, name: Any) -> dict:
        backup_name = validate_backup_name(name)
        self._ensure_ready()
        owner = secrets.token_hex(16)
        now = time.time()
        stop_event = threading.Event()
        keeper: threading.Thread | None = None
        lock_fd = self._acquire_restore_lock()
        try:
            # Phase 1: publish the restore intent.  Acquiring the write lock
            # here means every already-accepted write has committed
            # ("settled") first; writes arriving afterwards see a live lease
            # and are rejected.
            with self._txn(write=True) as conn:
                # Holding the exclusive lock proves no other process is
                # restoring, so any marker we find is abandoned regardless of
                # how much lease time it still claims.
                if self._restore_state(conn) == "restoring" and (
                    lock_fd is not None or self._restore_is_stale(conn)
                ):
                    self._clear_restore(conn)
                if self._restore_state(conn) != "idle":
                    raise RestoreInProgress()
                if conn.execute("SELECT 1 FROM backups WHERE name=?", (backup_name,)).fetchone() is None:
                    raise NotFoundError(f"备份不存在: {backup_name}")
                self._meta_set(conn, "restore_state", "restoring")
                self._meta_set(conn, "restore_backup", backup_name)
                self._meta_set(conn, "restore_owner", owner)
                self._meta_set(conn, "restore_started_at", _utc_now_iso())
                self._meta_set(
                    conn, "restore_lease_until", f"{now + self._lease_seconds:.6f}"
                )
        except BaseException:
            self._release_restore_lock(lock_fd)
            raise

        try:
            self._fire("after_restore_marked", {"name": backup_name, "owner": owner})
            keeper = self._start_lease_keeper(owner, stop_event)

            # Phase 2: validate the backup before touching the live directory.
            with self._txn(write=False) as conn:
                validated = self._validate_backup(conn, backup_name)

            self._fire(
                "before_restore_switch",
                {"name": backup_name, "version": validated.version_id},
            )

            # Phase 3: one atomic commit creates the complete new version and
            # flips current_version.  A crash leaves the original version.
            with self._txn(write=True) as conn:
                if (
                    self._restore_state(conn) != "restoring"
                    or self._meta_get(conn, "restore_owner") != owner
                ):
                    raise ConflictError(
                        "恢复租约已失效，已中止切换，当前目录未修改",
                        code="restore_lease_lost",
                    )
                validated = self._validate_backup(conn, backup_name)
                previous = self._current_version(conn)
                new_version = self._create_version(
                    conn, validated.version_id, "restore", f"恢复备份 {backup_name}"
                )
                self._set_current(conn, new_version)
                self._clear_restore(conn)

            result = {
                "backup": backup_name,
                "from_version": previous,
                "new_version": new_version,
                "restored_version": validated.version_id,
                "entry_count": validated.entry_count,
                "digest": validated.digest,
            }
            self._fire("after_restore_commit", result)
            return result
        except BaseException:
            # Best-effort cleanup for ordinary failures.  A real process death
            # skips this; the expired lease is reclaimed on restart.
            try:
                with self._txn(write=True) as conn:
                    if (
                        self._restore_state(conn) == "restoring"
                        and self._meta_get(conn, "restore_owner") == owner
                    ):
                        self._clear_restore(conn)
            except Exception:
                pass
            raise
        finally:
            stop_event.set()
            if keeper is not None:
                keeper.join(timeout=2.0)
            self._release_restore_lock(lock_fd)
