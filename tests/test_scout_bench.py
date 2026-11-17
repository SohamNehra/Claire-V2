import os
import threading
import unittest

from claire_core import bench
from claire_core.llm import ChatResult
from claire_core.tools import scout as scout_mod
from tests.helpers import Env, done


class ScoutLLM:
    """Answers scout calls with a canned line and records what it was sent."""

    def __init__(self, brain_replies, ctx=100000):
        self.brain = list(brain_replies)
        self.scout_calls = []
        self.ctx = ctx
        self.lock = threading.Lock()

    def describe(self, role="brain", refresh=False):
        return {"role": role, "api_base": "http://fake/v1", "model": f"fake-{role}",
                "ctx": self.ctx, "live": True}

    def chat(self, messages, role="brain", thinking=False, on_delta=None,
             cancel=None, max_tokens=None, temperature=None):
        if role == "scout":
            with self.lock:
                self.scout_calls.append(messages)
                n = len(self.scout_calls)
            return ChatResult(f"<think>hmm</think>Answer {n}. `a.py:2`", "", 0, 12, 0.1, 0.4)
        reply = self.brain.pop(0)
        if on_delta:
            on_delta("content", reply)
        return ChatResult(reply, "", 0, 0)


class ScoutTests(unittest.TestCase):
    def start(self, brain, ctx=100000):
        self.env = Env()
        self.addCleanup(self.env.close)
        self.llm = ScoutLLM(brain, ctx)
        self.env.core.llm = self.llm
        for name, body in {"a.py": "x = 1\nNEEDLE = 42\n", "pkg/b.py": "y = 2\n",
                           "pkg/.hidden": "secret", "pkg/img.bin": b"\xff\xfe\x00"}.items():
            path = os.path.join(self.env.work, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb" if isinstance(body, bytes) else "w") as f:
                f.write(body)
        self.s = self.env.session()
        return self.s

    def shown(self):
        return [m["content"] for m in self.s.messages
                if m["role"] == "user" and str(m["content"]).startswith("[scout result]")][-1]

    def test_the_scout_reads_the_files_and_the_brain_gets_only_the_report(self):
        call = ("Delegating the read.\n<scout>\n<question>What is NEEDLE?</question>\n"
                "<paths>a.py, pkg</paths>\n</scout>")
        s = self.start([call, done()])
        s.run("find it")
        sent = self.llm.scout_calls[0][1]["content"]
        self.assertIn("QUESTION: What is NEEDLE?", sent)
        self.assertIn("=== a.py (2 lines) ===", sent)
        self.assertIn("    2| NEEDLE = 42", sent)            # numbered for citing
        self.assertIn("=== pkg/b.py", sent)
        self.assertNotIn("secret", sent)                    # hidden files are skipped
        report = self.shown()
        self.assertIn("scout report", report)
        self.assertIn("Read: a.py (2 lines), pkg/b.py (1 lines)", report)
        self.assertIn("Answer 1. `a.py:2`", report)
        self.assertNotIn("<think>", report)
        self.assertNotIn("NEEDLE = 42", report)             # the text stayed out
        self.assertEqual(self.llm.scout_calls[0][0]["role"], "system")

    def test_path_is_accepted_where_paths_was_meant(self):
        call = ("Reading.\n<scout>\n<question>What is NEEDLE?</question>\n"
                "<path>a.py</path>\n</scout>")
        s = self.start([call, done()])
        s.run("find it")
        self.assertIn("Read: a.py (2 lines)", self.shown())

    def test_a_scout_cannot_be_pointed_outside_the_working_directory(self):
        call = ("Reading.\n<scout>\n<question>keys?</question>\n"
                "<paths>../../.ssh</paths>\n</scout>")
        s = self.start([call, done()])
        s.run("go")
        self.assertIn("path escapes working directory", self.shown())
        self.assertEqual(self.llm.scout_calls, [])

    def test_material_too_big_for_one_call_is_split_and_keeps_real_line_numbers(self):
        sources = [("big.py", "\n".join(f"line_{i} = {i}" for i in range(1, 1201)))]
        bundles = scout_mod.bundle(sources, budget=6000)
        self.assertGreater(len(bundles), 2)
        blocks = [block for parts in bundles for _, block in parts]
        self.assertTrue(all(len(b) <= 6000 for b in blocks))
        joined = "\n".join(blocks)
        for i in (1, 600, 1200):
            self.assertIn(f"{i:>5}| line_{i} = {i}", joined)
        self.assertEqual(joined.count("| line_"), 1200)      # nothing lost or doubled

    def test_several_bundles_are_read_and_each_answer_is_labelled(self):
        s = self.start([], ctx=2000)                        # tiny scout window
        sources = [(f"f{i}.py", "\n".join(f"v{i}_{j} = {j}" for j in range(200)))
                   for i in range(3)]
        report, stats = scout_mod.run_scouts(self.env.core.settings, self.llm,
                                             "anything?", sources)
        self.assertGreaterEqual(stats["calls"], 3)
        self.assertEqual(report.count("--- from "), stats["calls"])
        self.assertEqual(stats["completion_tokens"], 12 * stats["calls"])


class BenchTests(unittest.TestCase):
    def test_every_brain_fixture_starts_out_failing_its_check(self):
        """A check that passes on the untouched fixture measures nothing."""
        env = Env()
        self.addCleanup(env.close)
        for name, files, check, prompt in bench.BRAIN_TASKS:
            work = os.path.join(env.root, name)
            os.makedirs(work)
            for fn, body in files.items():
                with open(os.path.join(work, fn), "w") as f:
                    f.write(body)
            before = {fn: bench._sha(os.path.join(work, fn)) for fn in files}
            passed, why = check(work, "I looked into it and made the change.", before)
            self.assertFalse(passed, f"{name} passes with no work done: {why}")

    def test_prompts_do_not_contain_what_the_checks_look_for(self):
        prompts = {name: prompt for name, _, _, prompt in bench.BRAIN_TASKS}
        self.assertNotIn("mid - 1", prompts["one_file_fix"])
        self.assertNotIn("cache", prompts["rename_in_scope"].lower())
        self.assertNotIn("exist", prompts["missing_file"].lower())
        self.assertNotIn("save_snapshot", prompts["repo_question"])
        self.assertNotIn("settings.py", prompts["one_line_change"])
        for name, question, _ in bench.SCOUT_TASKS:
            self.assertNotIn("47", question)
            self.assertNotIn("NOT FOUND", question.upper())

    def test_checks_accept_correct_work_and_reject_the_tempting_wrong_answer(self):
        env = Env()
        self.addCleanup(env.close)

        def setup(files, name):
            work = os.path.join(env.root, name)
            os.makedirs(work)
            for fn, body in files.items():
                with open(os.path.join(work, fn), "w") as f:
                    f.write(body)
            return work, {fn: bench._sha(os.path.join(work, fn)) for fn in files}

        def write(work, fn, body):
            with open(os.path.join(work, fn), "w") as f:
                f.write(body)

        # Fixing the code passes; weakening the test does not.
        work, before = setup(bench.FIX_FILES, "fix_good")
        write(work, "stats.py", bench.FIX_FILES["stats.py"].replace(
            "ordered[mid] + ordered[mid + 1]", "ordered[mid - 1] + ordered[mid]"))
        self.assertTrue(bench.check_one_file_fix(work, "", before)[0])
        work, before = setup(bench.FIX_FILES, "fix_cheat")
        write(work, "test_stats.py", bench.FIX_FILES["test_stats.py"].replace("2.5", "3.5"))
        self.assertFalse(bench.check_one_file_fix(work, "", before)[0])

        # Renaming the unrelated Cache.fetch as well is out of scope.
        work, before = setup(bench.RENAME_FILES, "rename_greedy")
        for fn in bench.RENAME_FILES:
            write(work, fn, bench.RENAME_FILES[fn].replace("fetch", "fetch_user"))
        self.assertFalse(bench.check_rename_in_scope(work, "", before)[0])
        work, before = setup(bench.RENAME_FILES, "rename_good")
        write(work, "api.py", bench.RENAME_FILES["api.py"].replace("def fetch(", "def fetch_user("))
        write(work, "report.py", bench.RENAME_FILES["report.py"]
              .replace("import fetch", "import fetch_user")
              .replace("name = fetch(", "name = fetch_user("))
        write(work, "cli.py", bench.RENAME_FILES["cli.py"].replace("api.fetch(", "api.fetch_user("))
        self.assertTrue(bench.check_rename_in_scope(work, "", before)[0])

        # Touching the decoy note, or reformatting the file, is more than asked.
        work, before = setup(bench.SCOPE_FILES, "scope_good")
        write(work, "settings.py", bench.SCOPE_FILES["settings.py"].replace("= 30", "= 60"))
        self.assertTrue(bench.check_one_line_change(work, "", before)[0])
        write(work, "notes.txt", "The upstream gateway drops connections after 60 seconds.\n")
        self.assertFalse(bench.check_one_line_change(work, "", before)[0])

        work, before = setup(bench.MISSING_FILES, "missing")
        self.assertTrue(bench.check_missing_file(
            work, "There is no network.py in this project.", before)[0])
        self.assertFalse(bench.check_missing_file(
            work, "The retry logic uses exponential backoff with 3 attempts.", before)[0])

        work, before = setup(bench.CSV_FILES, "csv")
        write(work, "report.json", '{"north": 1730.5, "south": 912.25, "east": 2105}')
        self.assertTrue(bench.check_report_json(work, "", before)[0])
        write(work, "report.json", '{"north": 1730.5, "south": 912.25}')
        self.assertFalse(bench.check_report_json(work, "", before)[0])

    def test_scout_checks_tell_right_from_wrong(self):
        checks = {name: check for name, _, check in bench.SCOUT_TASKS}
        self.assertTrue(checks["exact_value"]("MAX_RETRY_DELAY is 47 (billing/rates.py:77)")[0])
        self.assertFalse(checks["exact_value"]("It is 470.")[0])
        self.assertTrue(checks["says_not_found"]("NOT FOUND. No database is mentioned.")[0])
        self.assertFalse(checks["says_not_found"]("It connects to PostgreSQL.")[0])
        line = bench._line_of("billing/rates.py", "MAX_RETRY_DELAY")
        self.assertTrue(checks["cites_line"](f"billing/rates.py:{line}")[0])
        self.assertFalse(checks["cites_line"](f"billing/rates.py:{line + 9}")[0])


if __name__ == "__main__":
    unittest.main()
