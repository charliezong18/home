"""用量看板 P1/P2：usage_data rows + collect_usage 聚合。全部用临时 fixture，不碰真实数据。"""
import datetime as dt
import json
import os
import shutil
import sqlite3
import tempfile
import unittest

import build_home
import collect_usage
import usage_data

SECRET = "机密提示词-绝不能出现-PROMPT-XYZ"
TZ = usage_data.TZ


def ts(day, hour):
    return dt.datetime.fromisoformat(f"{day}T{hour:02d}:00:00").replace(tzinfo=TZ).timestamp()


class Fixture:
    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="usage-test-")
        self.dispatch = os.path.join(self.root, "dispatch")
        os.makedirs(os.path.join(self.dispatch, "runs"))
        self.db = os.path.join(self.root, "state.db")
        self.jobs = os.path.join(self.root, "jobs.json")
        with open(self.jobs, "w", encoding="utf-8") as f:
            json.dump({"jobs": [{"id": "e8319478c051", "name": "巡检 heartbeat（独立会话）", "prompt": SECRET},
                                {"id": "other", "name": "夜间养卡", "prompt": SECRET}]}, f, ensure_ascii=False)
        ledger = {"tasks": {
            "t-a": {"task_id": "t-a", "template": "build", "model": "opus", "question": SECRET,
                    "created_at": "2026-11-23T18:00:00+00:00", "finished_at": "2026-11-23T19:00:00+00:00"},
            "menxia-b": {"task_id": "menxia-b", "template": "lookup", "model": "sonnet", "question": SECRET,
                         "created_at": "2026-11-23T20:00:00+00:00", "finished_at": "2026-11-23T21:00:00+00:00"},
            "t-heavy": {"task_id": "t-heavy", "template": "build", "model": "opus", "question": SECRET,
                        "created_at": "2026-11-23T22:00:00+00:00", "finished_at": "2026-11-23T23:00:00+00:00"},
            "t-old": {"task_id": "t-old", "template": "build", "question": SECRET,
                      "finished_at": "2026-11-22T20:00:00+00:00"},
        }}
        self.write("ledger.json", ledger)
        mu = lambda m, cost, inp=1000, cc=0: {m: {"inputTokens": inp, "outputTokens": 500, "cacheReadInputTokens": 2000,
                                                 "cacheCreationInputTokens": cc, "costUSD": cost}}
        self.run("t-a", {"type": "result", "subtype": "success", "total_cost_usd": 3.0, "num_turns": 20, "result": SECRET,
                         "permission_denials": [{"tool_name": "Bash", "tool_input": {"command": SECRET}}],
                         "modelUsage": {**mu("claude-opus-5-5", 2.5), **mu("claude-haiku-5-5", 0.5)}}, exit_code="0")
        self.run("menxia-b", {"type": "result", "subtype": "error_max_turns", "total_cost_usd": 1.0, "num_turns": 30,
                              "result": SECRET, "permission_denials": [], "modelUsage": mu("claude-sonnet-5-5", 1.0)},
                 exit_code="1", billing="api")
        self.run("t-heavy", {"type": "result", "subtype": "success", "total_cost_usd": 4.0, "num_turns": 2, "result": SECRET,
                             "permission_denials": [], "modelUsage": mu("claude-opus-5-5", 4.0, cc=300_000)})
        self.run("t-old", {"type": "result", "subtype": "success", "total_cost_usd": 9.0, "num_turns": 5, "result": SECRET,
                           "permission_denials": [], "modelUsage": mu("claude-opus-5-5", 9.0)})
        with open(os.path.join(self.dispatch, "runs", "t-partial.json"), "w") as f:
            f.write("")                                   # 还在写的空文件：跳过
        self.make_db()

    def write(self, name, obj):
        with open(os.path.join(self.dispatch, name), "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)

    def run(self, tid, obj, exit_code=None, billing=None):
        self.write(os.path.join("runs", tid + ".json"), obj)
        with open(os.path.join(self.dispatch, "runs", tid + ".prompt.txt"), "w", encoding="utf-8") as f:
            f.write(SECRET)
        if exit_code is not None:
            with open(os.path.join(self.dispatch, "runs", tid + ".exit"), "w") as f:
                f.write(exit_code + "\n")
        if billing:
            with open(os.path.join(self.dispatch, "runs", tid + ".billing"), "w") as f:
                f.write(billing + "\n")

    def make_db(self):
        con = sqlite3.connect(self.db)
        con.executescript("""
          CREATE TABLE sessions (id TEXT, source TEXT, model TEXT, title TEXT, started_at REAL, input_tokens INT, output_tokens INT,
            cache_read_tokens INT, cache_write_tokens INT, estimated_cost_usd REAL, api_call_count INT, message_count INT, system_prompt TEXT);
          CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, timestamp REAL);
        """)
        d = "2026-11-23"
        rows = [
            ("hb1", "cron", "claude-opus-5-5", "巡检 heartbeat（独立会话） · Nov 23 01:00", ts(d, 1), 1000, 100, 50000, 2000, 0.6, 3, 4),
            ("hb2", "cron", "claude-opus-5-5", "巡检 heartbeat（独立会话） · Nov 23 01:30", ts(d, 2), 1000, 100, 50000, 2000, 0.7, 3, 4),
            ("cr1", "cron", "claude-opus-5-5", "夜间养卡 · Nov 23 03:00", ts(d, 3), 1000, 100, 0, 0, 1.2, 5, 6),
            ("nag", "cron", None, None, ts(d, 4), 0, 0, 0, 0, 0, 0, 0),            # no_agent 脚本：不计
            ("loc", "local", "claude-opus-5-5", SECRET, ts(d, 5), 10, 10, 0, 0, 0.3, 1, 2),
            ("tg", "telegram", "claude-opus-5-5", SECRET, ts("2026-11-01", 9), 100000, 1000, 0, 0, 100.0, 50, 50),
        ]
        con.executemany("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'" + SECRET + "')", rows)
        msgs = [("hb1", "user", SECRET, ts(d, 1)), ("hb1", "assistant", "[SILENT]", ts(d, 1)),
                ("hb2", "user", SECRET, ts(d, 2)), ("hb2", "assistant", "发现 1 个 error：" + SECRET, ts(d, 2)),
                ("tg", "user", SECRET, ts(d, 9))]
        # telegram：共 4 条 assistant，1 条在 11-23 → 摊 1/4
        msgs += [("tg", "assistant", SECRET, ts("2026-11-01", 9 + i)) for i in range(3)] + [("tg", "assistant", SECRET, ts(d, 10))]
        con.executemany("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)", msgs)
        con.commit()
        con.close()

    def rows(self, since="2026-11-23", until="2026-11-23"):
        return usage_data.load_rows(since, until, dispatch=self.dispatch, db_path=self.db, jobs_path=self.jobs)

    def close(self):
        shutil.rmtree(self.root, ignore_errors=True)


class UsageDataTest(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        # since 过滤按 mtime 跳旧文件：fixture 文件 mtime 是现在，所以把 since 设成 11-23 也会读到
        self.rows = self.fx.rows()

    def tearDown(self):
        self.fx.close()

    def by(self, source):
        return [r for r in self.rows if r["source"] == source]

    def test_sources_and_counts(self):
        w = self.by("worker")
        self.assertEqual(sorted({r["task_id"] for r in w}), ["menxia-b", "t-a", "t-heavy"])   # t-old 是 11-22，t-partial 跳过
        self.assertAlmostEqual(sum(r["cost_usd"] for r in w), 8.0)
        self.assertEqual(len(self.by("hermes_cron_heartbeat")), 2)
        self.assertEqual(len(self.by("hermes_cron_other")), 1)          # no_agent 会话不计
        self.assertEqual(len(self.by("hermes_local")), 1)
        main = self.by("hermes_main")
        self.assertEqual(len(main), 1)
        self.assertAlmostEqual(main[0]["cost_usd"], 25.0)               # 100 × 1/4

    def test_worker_multi_model_counts_turns_once(self):
        a = [r for r in self.by("worker") if r["task_id"] == "t-a"]
        self.assertEqual({r["model"] for r in a}, {"opus-5-5", "haiku-5-5"})
        self.assertEqual(sum(r["turns"] for r in a), 20)
        self.assertEqual(sum(r["denials"] for r in a), 1)
        self.assertEqual(a[0]["model"], "opus-5-5")                      # 成本大的在前，turns 记在它身上

    def test_heartbeat_idle_detected_in_sql(self):
        hb = sorted(self.by("hermes_cron_heartbeat"), key=lambda r: r["cost_usd"])
        self.assertEqual([r["idle"] for r in hb], [True, False])
        self.assertEqual(hb[0]["task_id"], "cron:e8319478c051")

    def test_day_is_los_angeles(self):
        # 2026-11-23T19:00Z = 11-23 11:00 PST
        self.assertEqual(usage_data.la_day("2026-11-24T07:59:32+00:00"), "2026-11-23")
        self.assertEqual(usage_data.la_day("2026-11-24T08:00:01+00:00"), "2026-11-24")
        self.assertIsNone(usage_data.la_day("not a date"))

    def test_norm_model(self):
        self.assertEqual(usage_data.norm_model("claude-opus-5-5[1m]"), "opus-5-5")
        self.assertEqual(usage_data.norm_model(None), "unknown")

    def test_rows_have_no_text(self):
        blob = json.dumps(self.rows, ensure_ascii=False)
        self.assertNotIn(SECRET, blob)
        self.assertNotIn("巡检", blob)
        self.assertNotIn("SILENT", blob)
        allowed = {"day", "source", "model", "task_id", "template", "cost_usd", "tokens", "turns", "denials", "subtype",
                   "exit_code", "billing", "cache_create", "idle", "session_id"}
        for r in self.rows:
            self.assertLessEqual(set(r), allowed)

    def test_missing_sources_are_empty(self):
        self.assertEqual(usage_data.hermes_rows(os.path.join(self.fx.root, "nope.db")), [])
        self.assertEqual(usage_data.worker_rows(os.path.join(self.fx.root, "nope")), [])


class CollectUsageTest(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        rows = self.fx.rows(since="2026-11-17", until="2026-11-23")
        now = dt.datetime(2026, 11, 23, 23, 0, tzinfo=TZ)
        self.u = collect_usage.build(rows, now.date(), now)
        self.d = self.u["days"][0]

    def tearDown(self):
        self.fx.close()

    def test_schema_shape(self):
        self.assertEqual(len(self.u["days"]), 30)
        self.assertEqual(self.d["date"], "2026-11-23")
        self.assertEqual(self.u["quota"]["source"], "none")
        self.assertIsNone(self.u["quota"]["seven_day_pct"])
        self.assertEqual(set(self.d["by_source"]), set(collect_usage.SOURCES))
        self.assertEqual(self.d["by_source"]["worker"]["runs"], 3)
        self.assertEqual(self.d["by_source"]["hermes_cron_heartbeat"]["sessions"], 2)
        self.assertEqual(self.d["total"]["sessions"], 4)                 # 2 hb + 1 other cron + 1 local
        self.assertEqual(self.u["week"]["iso"], "2026-W48")

    def test_top_tasks_and_prefix(self):
        self.assertEqual([t["task_id"] for t in self.d["top_tasks"]], ["t-heavy", "t-a", "menxia-b"])
        self.assertEqual(self.d["top_tasks"][1]["cost_usd"], 3.0)
        self.assertEqual(self.d["by_task_prefix"], {"t-": 7.0, "menxia": 1.0})
        self.assertEqual(self.d["billing_api_runs"], 1)

    def test_failures(self):
        f = self.d["failures"]
        self.assertEqual((f["error_max_turns"], f["permission_denials"], f["exit_nonzero"]), (1, 1, 1))
        self.assertIn({"task_id": "menxia-b", "kind": "exit_nonzero", "code": 1}, f["ids"])

    def test_waste_rules(self):
        w = {x["rule"]: x for x in self.d["waste"]}
        self.assertEqual(set(w), {"R-CRON-IDLE", "R-RESUME-HEAVY", "R-MAXTURNS", "R-DENY"})
        self.assertEqual((w["R-CRON-IDLE"]["count"], w["R-CRON-IDLE"]["cost_usd"]), (1, 0.6))
        self.assertEqual(w["R-RESUME-HEAVY"]["ids"], ["t-heavy"])
        self.assertEqual(w["R-MAXTURNS"]["ids"], ["menxia-b"])
        self.assertEqual(w["R-DENY"]["count"], 1)

    def test_empty_day_zeroes(self):
        d = self.u["days"][5]
        self.assertEqual(d["total"]["cost_usd"], 0)
        self.assertEqual(d["waste"], [])

    def test_output_has_no_prompt_text(self):
        blob = json.dumps(self.u, ensure_ascii=False)
        self.assertNotIn(SECRET, blob)
        self.assertNotIn("巡检", blob)
        self.assertNotIn("夜间养卡", blob)

    def test_build_home_usage_normalized_ignores_generated(self):
        a = dict(self.u, generated="x")
        b = dict(self.u, generated="y")
        self.assertEqual(build_home.usage_normalized(a), build_home.usage_normalized(b))
        self.assertIsNone(build_home.usage_normalized(None))


if __name__ == "__main__":
    unittest.main()
