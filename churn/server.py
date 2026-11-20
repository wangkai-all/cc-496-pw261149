"""Zero-dependency HTTP server: JSON API + static frontend.

Run:  python3 -m churn.server  (then open http://127.0.0.1:8765)
"""

from __future__ import annotations

import json
import mimetypes
import os
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import queries
from .engine import (
    add_feedback,
    campaign,
    close_warning,
    merge_customers,
    reopen_warning,
    replay_label,
    revise_feature,
    run_scoring,
    set_feature_set,
    undo_merge,
)
from .store import connect, utcnow

WEB_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
DB_PATH = os.environ.get("CHURN_DB", os.path.join(os.path.dirname(WEB_ROOT), "data", "churn.sqlite"))

_lock = threading.Lock()
_conn = None


def db():
    global _conn
    if _conn is None:
        _conn = connect(DB_PATH)
    return _conn


class ApiError(Exception):
    status = 400


class Handler(BaseHTTPRequestHandler):
    server_version = "churn-watch"
    protocol_version = "HTTP/1.1"

    # -- plumbing ---------------------------------------------------------
    def log_message(self, fmt, *args):  # quieter logs
        if os.environ.get("CHURN_HTTP_LOG"):
            super().log_message(fmt, *args)

    def _send(self, status: int, body: bytes, ctype: str = "application/json; charset=utf-8",
              extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, status: int = 200):
        self._send(status, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"))

    def _read_body_once(self):
        if hasattr(self, "_cached_body"):
            return
        n = int(self.headers.get("Content-Length") or 0)
        self._raw_body = self.rfile.read(n) if n else b""

    def _body(self) -> dict:
        raw = getattr(self, "_raw_body", b"")
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            raise ApiError("请求体不是合法 JSON")

    # -- routing ----------------------------------------------------------
    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def _route(self, method: str):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if method == "POST":
            self._read_body_once()
        qs = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        try:
            if path.startswith("/api/"):
                with _lock:
                    self._api(method, path[len("/api"):], qs)
            else:
                self._static(path)
        except ApiError as e:
            self._json({"error": str(e)}, getattr(e, "status", 400))
        except ValueError as e:
            # validation failures from the engine are client errors, not crashes
            self._json({"error": str(e)}, 400)
        except KeyError as e:
            self._json({"error": f"未找到：{e.args[0] if e.args else ''}"}, 404)
        except Exception as e:  # pragma: no cover - surfaced to the UI
            import traceback
            traceback.print_exc()
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def _api(self, method: str, path: str, qs: dict):
        conn = db()
        parts = [p for p in path.split("/") if p]
        as_of = qs.get("as_of") or None

        if method == "GET":
            if parts == ["bootstrap"]:
                return self._json(queries.bootstrap(conn))
            if parts == ["portfolio"]:
                return self._json(queries.portfolio(
                    conn, as_of=as_of, tier=qs.get("tier"), segment=qs.get("segment"),
                    region=qs.get("region"), q=qs.get("q")))
            if parts == ["risk-history"]:
                return self._json(queries.risk_history(conn, as_of))
            if parts == ["validation"]:
                return self._json(queries.validation(conn, as_of))
            if parts == ["versions"]:
                return self._json(queries.version_compare(conn, as_of))
            if parts == ["runs"]:
                return self._json(queries.runs_list(conn))
            if parts == ["jobs"]:
                return self._json(queries.jobs_list(conn))
            if parts and parts[0] == "customer" and len(parts) >= 2:
                return self._json(queries.customer_detail(conn, int(parts[1]), as_of))
            if parts and parts[0] == "warnings":
                if len(parts) == 1:
                    return self._json(queries.warnings_list(conn, qs.get("status", "open"), as_of,
                                                            int(qs["customer_id"]) if qs.get("customer_id") else None))
                return self._json(queries.warning_detail(conn, int(parts[1])))
            raise ApiError("未知接口")

        if method == "POST":
            body = self._body()
            if parts == ["demo", "reset"]:
                _reset()
                return self._json({"ok": True, "reset_at": utcnow()})
            if parts == ["scoring", "run"]:
                res = run_scoring(
                    conn,
                    as_of=body.get("as_of") or utcnow(),
                    trigger=body.get("trigger", "manual"),
                    notes=body.get("notes"),
                )
                return self._json(res)
            if parts == ["features", "revise"]:
                res = revise_feature(
                    conn,
                    customer_id=int(body["customer_id"]),
                    code=body["code"],
                    value=None if body.get("value") in (None, "") else float(body["value"]),
                    missing_reason=body.get("missing_reason") or None,
                    effective_at=body["effective_at"],
                    available_at=body.get("available_at") or None,
                    note=body.get("note"),
                    as_of=body.get("as_of") or None,
                )
                return self._json(res)
            if parts == ["customers", "merge"]:
                res = merge_customers(
                    conn,
                    from_id=int(body["from_id"]),
                    to_id=int(body["to_id"]),
                    effective_at=body.get("effective_at") or None,
                    available_at=body.get("available_at") or None,
                    reason=body.get("reason") or "人工合并",
                    as_of=body.get("as_of") or None,
                )
                return self._json(res)
            if parts == ["customers", "undo-merge"]:
                res = undo_merge(conn, merge_id=int(body["merge_id"]),
                                 as_of=body.get("as_of") or None)
                return self._json(res)
            if parts == ["labels", "replay"]:
                res = replay_label(
                    conn,
                    customer_id=int(body["customer_id"]),
                    value=body["value"],
                    effective_at=body["effective_at"],
                    available_at=body.get("available_at") or None,
                    backdated=body.get("backdated"),
                    retract=bool(body.get("retract")),
                    source=body.get("source") or "manual_replay",
                    note=body.get("note"),
                    as_of=body.get("as_of") or None,
                )
                return self._json(res)
            if parts == ["feature-sets", "activate"]:
                return self._json(set_feature_set(conn, version=int(body["version"])))
            if parts == ["campaign"]:
                res = campaign(
                    conn,
                    customer_ids=[int(x) for x in body["customer_ids"]],
                    as_of=body.get("as_of") or None,
                    trigger=body.get("trigger", "features"),
                    note=body.get("note", ""),
                    kind=body.get("kind", "rescore"),
                )
                return self._json(res)
            if parts and parts[0] == "warnings" and len(parts) == 3:
                wid = int(parts[1])
                if parts[2] == "feedback":
                    return self._json(add_feedback(
                        conn, warning_id=wid, verdict=body["verdict"],
                        note=body.get("note"), user=body.get("user") or "运营"))
                if parts[2] == "close":
                    return self._json(close_warning(
                        conn, warning_id=wid, reason=body["reason"],
                        user=body.get("user") or "运营", note=body.get("note")))
                if parts[2] == "reopen":
                    return self._json(reopen_warning(
                        conn, warning_id=wid, user=body.get("user") or "运营"))
            raise ApiError("未知接口")

        raise ApiError("不支持的方法")

    # -- static -----------------------------------------------------------
    def _static(self, path: str):
        if path in ("", "/"):
            path = "/index.html"
        rel = urllib.parse.unquote(path).lstrip("/")
        full = os.path.realpath(os.path.join(WEB_ROOT, rel))
        if not full.startswith(os.path.realpath(WEB_ROOT) + os.sep) and full != os.path.realpath(WEB_ROOT):
            self._json({"error": "禁止访问"}, 403)
            return
        if not os.path.isfile(full):
            self._json({"error": "文件不存在"}, 404)
            return
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        with open(full, "rb") as fh:
            self._send(200, fh.read(), ctype)


def _reset():
    global _conn
    if _conn is not None:
        _conn.close()
    from churn.demo import build_demo
    from churn.store import init_db

    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(DB_PATH + suffix):
            os.remove(DB_PATH + suffix)
    conn = connect(DB_PATH)
    init_db(conn)
    import contextlib, io
    with contextlib.redirect_stdout(io.StringIO()):
        build_demo(conn)
    _conn = conn


def main():
    global _conn
    from churn.store import init_db
    conn = connect(DB_PATH)
    init_db(conn)
    if conn.execute("SELECT COUNT(*) n FROM customer").fetchone()["n"] == 0:
        from churn.demo import build_demo
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()):
            build_demo(conn)
    _conn = conn
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8765"))
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"churn-watch  →  http://{host}:{port}   db={os.path.abspath(DB_PATH)}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
