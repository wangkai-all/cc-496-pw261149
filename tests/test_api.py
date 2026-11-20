"""HTTP API smoke test: boots the real server against a temp database."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

import churn.server as server
from churn.demo import build_demo
from churn.store import connect, init_db


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="churn-api-")
        cls.db_path = os.path.join(cls.tmpdir, "api.sqlite")
        conn = connect(cls.db_path)
        init_db(conn)
        with contextlib.redirect_stdout(io.StringIO()):
            build_demo(conn)
        conn.close()
        server._conn = None
        server.DB_PATH = cls.db_path
        cls.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        server._conn = None
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    # -- helpers ----------------------------------------------------------
    def request(self, method: str, path: str, body: dict | None = None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
            headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(req) as res:
                raw = res.read()
                ctype = res.headers.get("Content-Type", "")
                return res.status, (json.loads(raw) if "json" in ctype else raw), ctype
        except urllib.error.HTTPError as err:
            raw = err.read()
            return err.code, json.loads(raw) if raw else None, err.headers.get("Content-Type", "")

    def get(self, path: str):
        return self.request("GET", path)

    def post(self, path: str, body: dict | None = None):
        return self.request("POST", path, body or {})

    # -- static -----------------------------------------------------------
    def test_index_and_assets_are_served(self):
        status, raw, ctype = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(b'<main id="app"', raw)
        self.assertIn("text/html", ctype)
        status, raw, ctype = self.get("/js/main.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", ctype)
        status, raw, _ = self.get("/css/app.css")
        self.assertEqual(status, 200)
        self.assertIn(b"--series-1", raw)

    def test_path_traversal_is_blocked(self):
        status, payload, _ = self.get("/../churn/seed.py")
        self.assertIn(status, (403, 404))
        if status == 404:
            self.assertIn("error", payload)
        status, _, _ = self.get("/%2e%2e/churn/server.py")
        self.assertEqual(status, 403)

    # -- reads ------------------------------------------------------------
    def test_every_read_endpoint_returns_200(self):
        paths = [
            "/api/bootstrap", "/api/portfolio", "/api/portfolio?tier=critical",
            "/api/risk-history", "/api/validation", "/api/versions",
            "/api/runs", "/api/jobs", "/api/customer/1", "/api/customer/6",
            "/api/warnings?status=open", "/api/warnings?status=closed",
            "/api/warnings?status=all", "/api/warnings/1",
            "/api/portfolio?as_of=2026-10-07T00:00:00Z",
            "/api/customer/1?as_of=2026-10-07T00:00:00Z",
        ]
        for path in paths:
            with self.subTest(path=path):
                status, payload, _ = self.get(path)
                self.assertEqual(status, 200, payload)

    def test_bootstrap_shape(self):
        _, data, _ = self.get("/api/bootstrap")
        self.assertEqual(len(data["features"]), 5)
        self.assertEqual([s["version"] for s in data["feature_sets"]], [1, 2])
        self.assertTrue(any(o["trigger"] == "initial" for o in data["as_of_options"]))
        self.assertIsNotNone(data["latest_as_of"])

    def test_portfolio_filters(self):
        _, all_items, _ = self.get("/api/portfolio")
        _, critical, _ = self.get("/api/portfolio?tier=critical")
        self.assertTrue(all(i["tier"] == "critical" for i in critical["items"]))
        self.assertEqual(critical["total"], all_items["counts"]["critical"])
        _, search, _ = self.get("/api/portfolio?q=%E5%8C%97%E8%BE%B0")  # 北辰
        self.assertTrue(any("北辰" in i["name"] for i in search["items"]))
        self.assertGreaterEqual(all_items["late_entities"], 0)

    def test_historical_as_of_changes_the_picture(self):
        _, latest, _ = self.get("/api/portfolio")
        _, early, _ = self.get("/api/portfolio?as_of=2026-10-07T00:00:00Z")
        self.assertEqual(early["as_of"], "2026-10-07T00:00:00Z")
        self.assertNotEqual(early["counts"], latest["counts"])
        self.assertLessEqual(early["total"], latest["total"])

    def test_historical_alerts_are_projected_not_todays(self):
        cut = "2026-10-21T12:00:00Z"
        _, early, _ = self.get(f"/api/warnings?status=all&as_of={cut}")
        self.assertEqual(early["counts"]["closed"], 0)
        self.assertTrue(all(w["closed_at"] is None for w in early["items"]))
        self.assertTrue(all(e["at"] <= cut for w in early["items"] for e in w["events"]))
        _, port, _ = self.get(f"/api/portfolio?as_of={cut}")
        self.assertEqual(port["open_alerts"], early["counts"]["open"])
        _, late, _ = self.get("/api/warnings?status=all")
        self.assertGreater(late["counts"]["closed"], early["counts"]["closed"])

    def test_customer_detail_has_evidence_and_history(self):
        _, data, _ = self.get("/api/customer/1")
        self.assertEqual(len(data["explanation"]["evidence"]), 5)
        self.assertTrue(all(e.get("label") for e in data["explanation"]["evidence"]))
        self.assertGreaterEqual(len(data["history"]), 2)
        self.assertTrue(data["revisions"])
        self.assertTrue(data["labels"])
        self.assertTrue(data["warnings"])
        late = [r for r in data["revisions"] if r["is_late_for_as_of"]]
        self.assertTrue(all(r["available_at"] > data["as_of"] for r in late))

    def test_merged_customer_resolves_to_survivor(self):
        _, data, _ = self.get("/api/customer/11")
        self.assertEqual(data["entity"], 11)  # merge was undone in the demo
        _, early, _ = self.get("/api/customer/11?as_of=2026-10-22T00:00:00Z")
        self.assertEqual(early["entity"], 11)
        _, mid, _ = self.get("/api/customer/11?as_of=2026-11-05T00:00:00Z")
        self.assertEqual(mid["entity"], 6)

    def test_warnings_dedupe_and_lifecycle_are_visible(self):
        _, data, _ = self.get("/api/warnings?status=all")
        self.assertGreater(data["counts"]["open"], 0)
        self.assertGreater(data["counts"]["closed"], 0)
        self.assertGreater(data["counts"]["deduped_events"], 0)
        for w in data["items"]:
            self.assertTrue(w["events"])
            kinds = {e["kind"] for e in w["events"]}
            self.assertIn("opened", kinds)
            times = [e["at"] for e in w["events"]]
            self.assertEqual(times, sorted(times))

    def test_validation_policies_and_flips(self):
        _, data, _ = self.get("/api/validation")
        self.assertEqual(len(data["policies"]), 3)
        self.assertTrue(data["flips"])
        for f in data["flips"]:
            self.assertIn(f["from"], ("hit", "miss", "unknown"))
            self.assertIn(f["detail"], (
                "迟到标签把当时无法判定的告警补判为命中",
                "迟到标签把当时无法判定的告警补判为漏报",
                "回溯标签推翻了此前的命中",
                "回溯标签把此前的漏报改判为命中",
                "标签时间口径改变判定",
            ))
        self.assertTrue(any(l["retracted"] for l in data["labels"]))

    def test_unknown_customer_is_404(self):
        status, payload, _ = self.get("/api/customer/999")
        self.assertEqual(status, 404)
        self.assertIn("error", payload)

    # -- writes -----------------------------------------------------------
    def test_feedback_then_close_then_reopen(self):
        _, data, _ = self.get("/api/warnings?status=open")
        wid = data["items"][0]["id"]
        status, out, _ = self.post(f"/api/warnings/{wid}/feedback",
                                   {"verdict": "valid", "note": "冒烟测试", "user": "测试"})
        self.assertEqual(status, 200)
        self.assertIsNotNone(out["feedback_id"])
        status, out, _ = self.post(f"/api/warnings/{wid}/close",
                                   {"reason": "resolved", "user": "测试"})
        self.assertEqual(status, 200)
        status, out, _ = self.post(f"/api/warnings/{wid}/close",
                                   {"reason": "resolved", "user": "测试"})
        self.assertEqual(status, 400)  # already closed
        status, out, _ = self.post(f"/api/warnings/{wid}/reopen", {"user": "测试"})
        self.assertEqual(status, 200)
        _, detail, _ = self.get(f"/api/warnings/{wid}")
        self.assertIsNone(detail["closed_at"])
        self.assertIn("reopened", [e["kind"] for e in detail["events"]])

    def test_feature_revision_rescores_only_one_customer(self):
        _, before, _ = self.get("/api/portfolio")
        target = before["items"][-1]["customer_id"]
        status, out, _ = self.post("/api/features/revise", {
            "customer_id": target, "code": "logins_30d", "value": 99,
            "effective_at": "2026-11-19T00:00:00Z",
            "available_at": "2026-11-19T00:00:00Z",
            "note": "api smoke",
        })
        self.assertEqual(status, 200)
        self.assertEqual(out["run"]["scope"], "affected")
        self.assertEqual(out["run"]["scored"], 1)

    def test_bad_body_returns_400(self):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/features/revise", data=b"{not json",
            method="POST", headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code, 400)

    def test_label_replay_and_retraction_over_api(self):
        _, detail, _ = self.get("/api/customer/9")
        status, out, _ = self.post("/api/labels/replay", {
            "customer_id": 9, "value": "churned",
            "effective_at": "2026-11-10T00:00:00Z", "available_at": "2026-11-12T00:00:00Z",
            "note": "api smoke label",
        })
        self.assertEqual(status, 200)
        self.assertEqual(out["version"], 1)
        status, out, _ = self.post("/api/labels/replay", {
            "customer_id": 9, "value": "unknown",
            "effective_at": "2026-11-10T00:00:00Z", "available_at": "2026-11-13T00:00:00Z",
            "retract": True, "note": "api smoke retraction",
        })
        self.assertEqual(status, 200)
        self.assertTrue(out["retracted"])
        _, led, _ = self.get("/api/validation")
        mine = [l for l in led["labels"] if l["customer_id"] == 9]
        self.assertTrue(mine[0]["retracted"])
        self.assertFalse(mine[1]["retracted"])

    def test_merge_and_undo_over_api(self):
        status, out, _ = self.post("/api/customers/merge", {
            "from_id": 2, "to_id": 4,
            "effective_at": "2026-11-19T00:00:00Z", "available_at": "2026-11-19T00:00:00Z",
        })
        self.assertEqual(status, 200)
        _, detail, _ = self.get("/api/customer/2")
        self.assertEqual(detail["entity"], 4)
        merge_id = out["merge_id"]
        status, out, _ = self.post("/api/customers/undo-merge", {"merge_id": merge_id})
        self.assertEqual(status, 200)
        _, detail, _ = self.get("/api/customer/2")
        self.assertEqual(detail["entity"], 2)

    def test_feature_set_activation_and_version_compare(self):
        status, out, _ = self.post("/api/feature-sets/activate", {"version": 2})
        self.assertEqual(status, 200)
        _, data, _ = self.get("/api/versions")
        active = [s for s in data["sets"] if s["active"]]
        self.assertEqual(active[0]["version"], 2)
        self.post("/api/feature-sets/activate", {"version": 1})

    def test_reset_rebuilds_the_demo(self):
        status, out, _ = self.post("/api/demo/reset")
        self.assertEqual(status, 200)
        self.assertTrue(out["ok"])
        _, boot, _ = self.get("/api/bootstrap")
        self.assertEqual(len(boot["as_of_options"]), 12)
        _, val, _ = self.get("/api/validation")
        self.assertEqual(len(val["labels"]), 5)


if __name__ == "__main__":
    unittest.main()
