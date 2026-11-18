"""Flask application: HTML directory editor plus the ``/resolve`` API."""

from __future__ import annotations

import os
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, url_for

from .errors import DirectoryError, InputError
from .store import DirectoryStore


def _store_from_env() -> DirectoryStore:
    db_path = os.environ.get("SVCDIR_DB", str(Path("data") / "directory.sqlite3"))
    lease = float(os.environ.get("SVCDIR_RESTORE_LEASE_SECONDS", "30"))
    return DirectoryStore(db_path, restore_lease_seconds=lease)


def create_app(store: DirectoryStore | None = None, *, config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(
        SVCDIR_TESTING=False,
        JSON_AS_ASCII=False,
    )
    if config:
        app.config.update(config)
    app.json.ensure_ascii = False
    app.json.sort_keys = False
    store = store or _store_from_env()
    app.extensions["svcdir"] = store
    store.initialize()

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; form-action 'self'; frame-ancestors 'none'; "
            "base-uri 'none'",
        )
        return response

    # -- error handling ---------------------------------------------------

    @app.errorhandler(DirectoryError)
    def handle_directory_error(exc: DirectoryError):
        if _wants_json():
            return jsonify({"ok": False, "error": exc.to_dict()}), exc.status_code
        return (
            render_template(
                "error.html",
                title="操作未完成",
                status=exc.status_code,
                error=exc.to_dict(),
            ),
            exc.status_code,
        )

    @app.errorhandler(404)
    def handle_404(exc):
        if _wants_json():
            return (
                jsonify({"ok": False, "error": {"code": "not_found", "message": "接口不存在"}}),
                404,
            )
        return render_template("error.html", title="未找到", status=404,
                               error={"code": "not_found", "message": "页面或接口不存在"}), 404

    @app.errorhandler(405)
    def handle_405(exc):
        if _wants_json():
            return (
                jsonify({"ok": False, "error": {"code": "method_not_allowed", "message": "方法不允许"}}),
                405,
            )
        return render_template("error.html", title="方法不允许", status=405,
                               error={"code": "method_not_allowed", "message": "请求方法不被允许"}), 405

    @app.errorhandler(500)
    def handle_500(exc):
        original = getattr(exc, "original_exception", None) or exc
        app.logger.error("unhandled error: %s", original, exc_info=original)
        if _wants_json():
            return (
                jsonify({"ok": False, "error": {"code": "internal_error", "message": "服务器内部错误"}}),
                500,
            )
        return render_template("error.html", title="服务器错误", status=500,
                               error={"code": "internal_error", "message": "服务器内部错误"}), 500

    def _wants_json() -> bool:
        if request.path.startswith("/api/") or request.path == "/resolve":
            return True
        return request.accept_mimetypes["application/json"] > request.accept_mimetypes["text/html"]

    # -- API --------------------------------------------------------------

    @app.get("/healthz")
    def healthz():
        store.ping()
        return jsonify({"ok": True})

    @app.get("/resolve")
    def resolve():
        name = request.args.get("name")
        with store.read_view() as view:
            result = view.resolve(name)
        body = {
            "ok": True,
            "version": result["version"],
            "restore_in_progress": result["restore_in_progress"],
        }
        if "address" in result:
            body["name"] = result["name"]
            body["address"] = result["address"]
        else:
            body["count"] = result["count"]
            body["services"] = result["services"]
        return jsonify(body)

    @app.get("/api/state")
    def api_state():
        return jsonify({"ok": True, **store.snapshot()})

    @app.get("/api/backups")
    def api_backups():
        return jsonify({"ok": True, "backups": store.list_backups()})

    @app.post("/api/services")
    def api_upsert():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            raise InputError("请求体必须是 JSON 对象")
        result = store.upsert(
            payload.get("service"), payload.get("address"), payload.get("note", "")
        )
        return jsonify({"ok": True, **result}), 200

    @app.delete("/api/services/<path:service>")
    def api_delete(service: str):
        return jsonify({"ok": True, **store.delete(service)})

    @app.post("/api/backups")
    def api_create_backup():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            raise InputError("请求体必须是 JSON 对象")
        result = store.create_backup(payload.get("name"))
        return jsonify({"ok": True, **result}), 201

    @app.get("/api/backups/<path:name>/validate")
    def api_validate_backup(name: str):
        return jsonify({"ok": True, **store.validate_backup(name)})

    @app.post("/api/backups/<path:name>/restore")
    def api_restore_backup(name: str):
        result = store.restore_backup(name)
        return jsonify({"ok": True, **result})

    # -- HTML -------------------------------------------------------------

    def _redirect_back(**kwargs):
        return redirect(url_for("index", **kwargs), code=303)

    @app.get("/")
    def index():
        snapshot = store.snapshot()
        return render_template("index.html", state=snapshot, message=request.args.get("message"),
                               error=request.args.get("error"))

    @app.post("/services")
    def form_upsert():
        try:
            result = store.upsert(
                request.form.get("service"), request.form.get("address"), request.form.get("note", "")
            )
        except DirectoryError as exc:
            return _redirect_back(error=exc.message)
        return _redirect_back(
            message=f"已保存 {result['service']} -> {result['address']}（目录版本 {result['version']}）"
        )

    @app.post("/services/<path:service>/delete")
    def form_delete(service: str):
        try:
            result = store.delete(service)
        except DirectoryError as exc:
            return _redirect_back(error=exc.message)
        return _redirect_back(message=f"已删除 {result['service']}（目录版本 {result['version']}）")

    @app.post("/backups")
    def form_create_backup():
        try:
            result = store.create_backup(request.form.get("name"))
        except DirectoryError as exc:
            return _redirect_back(error=exc.message)
        return _redirect_back(
            message=f"已创建备份 {result['name']}（{result['entry_count']} 条，版本 {result['version']}）"
        )

    @app.post("/backups/<path:name>/restore")
    def form_restore_backup(name: str):
        try:
            result = store.restore_backup(name)
        except DirectoryError as exc:
            return _redirect_back(error=exc.message)
        return _redirect_back(
            message=f"已从备份 {result['backup']} 恢复：{result['entry_count']} 条，"
                    f"新目录版本 {result['new_version']}"
        )

    return app
