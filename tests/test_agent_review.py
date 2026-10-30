"""在临时项目中验证审核运行器；所有模型命令均由本地程序模拟。"""

import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "agent-review.sh"
MOCK = ROOT / "tests" / "mock_cli.py"


class ReviewIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agent-review-tests-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        (self.project / "src").mkdir(parents=True)
        (self.project / "src" / "a.py").write_text("def answer():\n    return 42\n", encoding="utf-8")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name in ("opencode", "codex", "kimi", "cursor-agent", "agy", "devin"):
            (self.bin / name).symlink_to(MOCK)
        self.tmpdir = self.root / "runs"
        self.tmpdir.mkdir()
        self.config = self.root / "models.ini"
        self.config.write_text("""[a]
model = mock/a
backend = opencode
[b]
model = mock/b
backend = codex
[c]
model = mock/c
backend = kimi
[d]
model = mock/d
backend = cursor
[e]
model = mock/e
backend = agycli
[f]
model = mock/f
backend = devin
[summarizer]
model = a
""", encoding="utf-8")
        self.control = self.root / "control.json"
        self.log = self.root / "calls.jsonl"
        self.child_pid = self.root / "child.pid"
        self.set_rules({})
        self.env = os.environ.copy()
        self.env.update({"PATH": str(self.bin) + os.pathsep + self.env["PATH"], "TMPDIR": str(self.tmpdir),
                         "MOCK_LOG": str(self.log), "MOCK_CONTROL": str(self.control),
                         "MOCK_CHILD_PID": str(self.child_pid), "OPENCODE_SERVER_PASSWORD": "mock-not-a-secret"})
        self.env.pop("AGENT_REVIEW_CONFIG", None)
        self.env.pop("AGENT_REVIEW_MODEL", None)
        self.env.pop("AGENT_REVIEW_ROLE", None)

    def set_rules(self, rules):
        self.control.write_text(json.dumps({"rules": rules}, ensure_ascii=False), encoding="utf-8")

    def command(self, args, defaults=True):
        command = ["bash", str(ENTRY), "--config", str(self.config)]
        if defaults:
            command += ["--review-models", "a b", "--review-timeout", "5s", "--summary-timeout", "3s"]
        return command + list(args)

    def invoke(self, *args, defaults=True, timeout=30):
        return subprocess.run(self.command(args, defaults), cwd=self.project, env=self.env,
                              text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)

    def calls(self, kind=None):
        rows = []
        if self.log.exists():
            with self.log.open(encoding="utf-8") as stream:
                fcntl.flock(stream, fcntl.LOCK_SH)
                rows = [json.loads(line) for line in stream if line.strip()]
        return [row for row in rows if kind is None or row["request"]["kind"] == kind]

    def result(self, process, code=0, status=None):
        self.assertEqual(process.returncode, code, process.stdout)
        paths = re.findall(r"临时目录(?:保留于)?[：:]\s*(.+)", process.stdout)
        self.assertTrue(paths, process.stdout)
        run_dir = Path(paths[0].strip())
        data = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        if status is not None:
            self.assertEqual(data["status"], status, process.stdout)
        return run_dir, data

    def test_default_report_is_unique_and_uses_tmpdir(self):
        first, data1 = self.result(self.invoke("审核代码"), status="complete")
        second, data2 = self.result(self.invoke("审核代码"), status="complete")
        self.assertNotEqual(first, second)
        self.assertEqual(first.parent, self.tmpdir)
        self.assertEqual(Path(data1["report_path"]), first / "report.md")
        self.assertEqual(Path(data2["report_path"]), second / "report.md")
        self.assertTrue((first / "report.md").is_file())
        self.assertEqual(len(self.calls("review")), 4)
        self.assertEqual(len(self.calls("summary")), 2)
        self.assertEqual(data1["version"], 2)

    def test_legacy_positionals_and_relative_report(self):
        report = self.project / "reports" / "review.md"
        _, data = self.result(self.invoke("审核代码", "reports/review.md"), status="complete")
        self.assertEqual(Path(data["report_path"]), report)
        self.assertTrue(report.read_text(encoding="utf-8").strip())

    def test_list_models_and_help_do_not_start_models(self):
        listed = self.invoke("--list-models", defaults=False)
        self.assertEqual(listed.returncode, 0, listed.stdout)
        for backend in ("opencode", "codex", "kimi", "cursor", "agycli", "devin"):
            self.assertIn(backend, listed.stdout)
        help_result = self.invoke("--help", defaults=False)
        self.assertEqual(help_result.returncode, 0, help_result.stdout)
        for option in ("--review-timeout", "--summary-timeout", "--review-models", "--summarizer-model"):
            self.assertIn(option, help_result.stdout)
        for option in ("--checkpoint", "--resume", "--task-file"):
            self.assertNotIn(option, help_result.stdout)
        self.assertEqual(self.calls(), [])

    def test_invalid_and_missing_time_arguments_fail_before_launch(self):
        for invalid in ("0", "-1", "0s", "1.5h", "20x", "99999999999999999999999999999999999999h"):
            with self.subTest(invalid=invalid):
                result = self.invoke("--review-timeout", invalid, "审核代码", defaults=False)
                self.assertNotEqual(result.returncode, 0, result.stdout)
        for option in ("--review-timeout", "--summary-timeout"):
            with self.subTest(option=option):
                result = self.invoke(option, defaults=False)
                self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.calls(), [])

    def test_supported_time_units(self):
        for duration in ("3", "3s", "1m", "1h"):
            with self.subTest(duration=duration):
                self.result(self.invoke("--review-models", "a", "--review-timeout", duration,
                                        "--summary-timeout", duration, "审核代码", defaults=False), status="complete")

    def test_all_backends_receive_same_content(self):
        self.result(self.invoke("--review-models", "a b c d e f", "审核代码"), status="complete")
        rows = self.calls("review")
        self.assertEqual(len(rows), 6)
        self.assertEqual({row["request"]["model"] for row in rows}, set("abcdef"))
        prompts = {row["request"]["prompt"] for row in rows}
        self.assertEqual(len(prompts), 1)
        self.assertIn("审核代码", prompts.pop())
        self.assertTrue(all(row["cwd"] == str(self.project) for row in rows))
        argv = {row["request"]["model"]: row["argv"] for row in rows}
        self.assertIn("-o", argv["b"])
        self.assertIn("--prompt", argv["c"])
        self.assertIn("--workspace", argv["d"])
        self.assertIn("--print-timeout", argv["e"])
        self.assertIn("--respect-workspace-trust", argv["f"])
        self.assertIn("--permission-mode", argv["f"])
        self.assertEqual(argv["f"][argv["f"].index("--permission-mode") + 1], "dangerous")
        agy_limit = argv["e"][argv["e"].index("--print-timeout") + 1]
        match = re.fullmatch(r"(\d+)([smh]?)", agy_limit)
        self.assertIsNotNone(match, agy_limit)
        seconds = int(match[1]) * {"": 1, "s": 1, "m": 60, "h": 3600}[match[2]]
        self.assertGreater(seconds, 0)
        self.assertLessEqual(seconds, 5)
        opencode = next(row for row in rows if row["request"]["model"] == "a")
        self.assertIsNotNone(opencode["opencode_config"])
        self.assertFalse(opencode["server_password_set"])

    def test_prompt_asks_for_free_text_without_json_protocol(self):
        self.result(self.invoke("审核代码"), status="complete")
        review_prompt = self.calls("review")[0]["request"]["prompt"]
        self.assertNotIn("AGENT_REVIEW_JSON", review_prompt)
        self.assertNotIn("run_id", review_prompt)
        self.assertIn("不要输出 JSON", review_prompt)
        self.assertIn("严重程度", review_prompt)
        summary_prompt = self.calls("summary")[0]["request"]["prompt"]
        self.assertIn("汇总", summary_prompt)
        self.assertIn("模型 a", summary_prompt)
        self.assertNotIn("AGENT_REVIEW_JSON", summary_prompt)

    def test_every_backend_can_summarize(self):
        for model in "abcdef":
            with self.subTest(model=model):
                run, _ = self.result(self.invoke("--summarizer-model", model, "审核代码"), status="complete")
                self.assertIn("汇总结论", (run / "report.md").read_text(encoding="utf-8"))
        self.assertEqual({row["request"]["model"] for row in self.calls("summary")}, set("abcdef"))

    def test_empty_output_fails_without_report(self):
        self.set_rules({"review": {"mode": "empty"}})
        run, _ = self.result(self.invoke("--review-models", "a", "审核代码"), code=1, status="failed")
        self.assertEqual(len(self.calls("review")), 2)
        self.assertFalse((run / "report.md").exists())
        self.assertEqual(self.calls("summary"), [])

    def test_backend_failure_is_retried_once(self):
        self.set_rules({"review:a": [{"mode": "fail"}, {}]})
        _, data = self.result(self.invoke("--review-models", "a", "审核代码"), status="complete")
        attempts = data["models"]["m1"]["attempts"]
        self.assertEqual([item["ok"] for item in attempts], [False, True])
        self.assertEqual(attempts[0]["rc"], 7)
        self.assertEqual(len(self.calls("review")), 2)

    def test_one_model_failure_marks_partial(self):
        self.set_rules({"review:b": {"mode": "empty"}})
        run, data = self.result(self.invoke("审核代码"), status="partial")
        self.assertEqual(data["models"]["m2"]["status"], "failed")
        report = (run / "report.md").read_text(encoding="utf-8")
        self.assertNotIn("模型 b", report)
        self.assertIn("汇总结论", report)
        summary_prompt = self.calls("summary")[0]["request"]["prompt"]
        self.assertIn("模型 a", summary_prompt)
        self.assertNotIn("模型 b", summary_prompt)

    def test_summary_failure_does_not_replace_old_report(self):
        report = self.project / "report.md"
        for mode in ("fail", "empty", "write_then_fail"):
            with self.subTest(mode=mode):
                report.write_text("原有报告，不应替换\n", encoding="utf-8")
                self.set_rules({"summary": {"mode": mode}})
                before = len(self.calls("summary"))
                self.result(self.invoke("审核代码", str(report)), code=1, status="failed")
                self.assertEqual(report.read_text(encoding="utf-8"), "原有报告，不应替换\n")
                self.assertEqual(len(self.calls("summary")) - before, 3)

    def test_summary_timeout_does_not_publish(self):
        self.set_rules({"summary": {"mode": "sleep"}})
        run, _ = self.result(self.invoke("--summary-timeout", "1s", "审核代码"), code=1, status="failed")
        self.assertFalse((run / "report.md").exists())
        self.assertEqual(len(self.calls("review")), 2)
        self.assertEqual(len(self.calls("summary")), 3)

    def test_summary_retry_uses_new_candidate(self):
        self.set_rules({"summary": [{"mode": "empty"}, {}]})
        run, _ = self.result(self.invoke("审核代码"), status="complete")
        self.assertEqual(len(self.calls("summary")), 2)
        self.assertIn("汇总结论", (run / "report.md").read_text(encoding="utf-8"))

    def test_large_answers_use_prompt_file(self):
        self.set_rules({"review": {"body_size": 30000}})
        self.result(self.invoke("审核代码"), status="complete")
        summary_rows = self.calls("summary")
        self.assertTrue(summary_rows)
        self.assertTrue(all(row["prompt_file"] for row in summary_rows))
        for row in summary_rows:
            self.assertTrue(all(len(arg.encode()) < 48000 for arg in row["argv"]))

    def test_timeout_keeps_partial_answer(self):
        self.set_rules({"review:a": {"mode": "partial_then_sleep"}})
        _, data = self.result(self.invoke("--review-timeout", "2s", "审核代码"), status="partial")
        model = data["models"]["m1"]
        self.assertEqual(model["status"], "partial")
        self.assertTrue(model["answer"])
        self.assertTrue(Path(model["answer"]).is_file())
        summary_prompt = self.calls("summary")[0]["request"]["prompt"]
        self.assertIn("模型 a", summary_prompt)
        self.assertIn("可能中断", summary_prompt)

    def test_report_is_only_the_summary_body(self):
        run, _ = self.result(self.invoke("审核代码"), status="complete")
        report = (run / "report.md").read_text(encoding="utf-8")
        self.assertEqual(report.strip(), "## 汇总结论\n\n合并后的结论：空值处理需要补充校验，位置在 `src/a.py` 第 1-2 行。")
        for text in ("运行编号", "审核范围", "模型状态", "models/m", "answer.md", "空值导致请求失败"):
            self.assertNotIn(text, report)

    def test_report_replaces_final_symlink_without_overwriting_its_target(self):
        target = self.root / "unrelated-old-report.md"
        target.write_text("保留原有内容\n", encoding="utf-8")
        report = self.project / "review.md"
        report.symlink_to(target)
        _, data = self.result(self.invoke("审核代码", "review.md"), status="complete")
        self.assertEqual(Path(data["report_path"]), report)
        self.assertFalse(report.is_symlink())
        self.assertEqual(target.read_text(encoding="utf-8"), "保留原有内容\n")

    def test_sigint_stops_own_process_group_and_preserves_unrelated_process(self):
        self.set_rules({"review": {"mode": "descendant"}})
        unrelated = subprocess.Popen(["sleep", "90"])
        process = subprocess.Popen(self.command(["--review-models", "a", "审核代码"]), cwd=self.project,
                                   env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 8
            while not self.child_pid.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.025)
            self.assertTrue(self.child_pid.exists(), "模拟模型没有启动子进程")
            descendant = int(self.child_pid.read_text())
            process.send_signal(signal.SIGINT)
            output, _ = process.communicate(timeout=6)
            completed = subprocess.CompletedProcess(process.args, process.returncode, output)
            self.result(completed, code=130, status="interrupted")
            self.assertIsNone(unrelated.poll())
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                try:
                    os.kill(descendant, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.025)
            else:
                self.fail(f"本次审核的子进程 {descendant} 仍存在")
        finally:
            if process.poll() is None:
                process.terminate()
                process.communicate(timeout=8)
            unrelated.terminate()
            unrelated.wait(timeout=3)


if __name__ == "__main__":
    unittest.main()
