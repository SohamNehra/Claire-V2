"""Projects: chats and tasks filed in folders, each project with its own memory,
and one view of the work under way across all of them."""

import json
import os
import sqlite3
import subprocess
import time
import unittest
from unittest.mock import patch

from claire_core.hub import Hub
from claire_core.projects import scratch_base
from claire_core.store import Store
from tests.helpers import Env, done
from tests.test_controller import decision
from tests.test_engine import call
from tests.test_service import ServiceCase


def folder(env, *parts, git=False):
    path = os.path.join(env.root, *parts)
    os.makedirs(path, exist_ok=True)
    if git:
        os.makedirs(os.path.join(path, ".git"), exist_ok=True)
    return os.path.realpath(path)


class ProjectMemoryTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)
        self.core = self.env.core
        self.hub = Hub(self.core, start_brain=False)
        self.addCleanup(self.hub.stop)

    def task_in(self, path, title="Work", parent=None):
        return self.hub.spawn_task(title, "Do the work.", path, parent=parent)

    def test_a_task_in_a_folder_makes_that_folder_a_project_with_its_own_memory(self):
        repo = folder(self.env, "shop", git=True)
        row = self.task_in(repo, "Fix checkout")
        project = self.core.store.project(row["project"])
        self.assertEqual((project["name"], project["path"]), ("shop", repo))
        brief = os.path.join(self.core.vault.path, "projects", project["slug"], "PROJECT.md")
        self.assertTrue(os.path.isfile(brief))
        # A second task deeper in the same repository joins the same project.
        inner = folder(self.env, "shop", "api")
        self.assertEqual(self.task_in(inner)["project"], project["id"])
        self.assertEqual(len(self.core.store.projects()), 1)

    def test_scratch_work_belongs_with_the_chat_that_asked_for_it(self):
        group = self.core.projects.create("Research")
        chat = self.hub.new_chat(project=group["id"])
        scratch = os.path.join(scratch_base(), "claire-test-" + str(time.time_ns()))
        os.makedirs(scratch)
        self.addCleanup(lambda: os.path.isdir(scratch) and os.rmdir(scratch))
        self.assertEqual(self.task_in(scratch, parent=chat["id"])["project"], group["id"])
        self.assertEqual(self.task_in(scratch, parent="home")["project"], "")
        self.assertEqual(len(self.core.store.projects()), 1)

    def test_os_locations_never_become_projects_on_their_own(self):
        self.assertIsNone(self.core.projects.for_task(os.path.expanduser("~/Desktop")))

    def test_each_project_sees_only_its_own_memory(self):
        a, b = folder(self.env, "alpha"), folder(self.env, "beta")
        self.core.vault.remember("project", "Alpha deploys with fly.", workdir=a)
        self.core.vault.remember("project", "Beta uses postgres.", workdir=b)
        self.assertIn("fly", self.core.vault.load_memory(a))
        self.assertNotIn("postgres", self.core.vault.load_memory(a))
        self.assertTrue(self.core.vault.project_note(a).endswith(os.path.join("alpha", "PROJECT.md")))

    def test_a_task_record_is_kept_in_full_by_its_project_and_summarised_globally(self):
        repo = folder(self.env, "shop", git=True)
        row = self.task_in(repo, "Fix checkout")
        project = self.core.store.project(row["project"])
        self.hub._record_work(row["id"], "Fix checkout", repo, "done", "Fixed the bug. " + "x" * 2000)
        own = self.core.vault.project_work(project["slug"]).entries()[0][1]
        self.assertIn("x" * 2000, own)
        shared = self.core.vault.work.context()
        self.assertIn("Fix checkout", shared)
        self.assertIn("Project: shop", shared)
        self.assertNotIn("x" * 1000, shared)

    def test_worker_prompt_carries_its_project_memory_and_not_another(self):
        self.core.settings.controller_enabled = True
        a, b = folder(self.env, "alpha"), folder(self.env, "beta")
        self.core.vault.remember("project", "Alpha deploys with fly.", workdir=a)
        self.core.vault.remember("project", "Beta uses postgres.", workdir=b)
        row = self.task_in(a, "Ship alpha")
        prompt = self.hub._session(row["id"]).system_prompt()
        self.assertIn("# Project: alpha", prompt)
        self.assertIn("Alpha deploys with fly.", prompt)
        self.assertNotIn("postgres", prompt)

    def test_project_chat_is_given_its_project_and_main_chat_the_whole_board(self):
        self.core.settings.controller_enabled = True
        repo = folder(self.env, "shop", git=True)
        project = self.core.projects.create("Shop", repo)
        self.core.vault.write_project_brief(project["slug"], "# Shop\n\nA store front in Flask.\n")
        chat = self.hub.new_chat(project=project["id"])
        task = self.task_in(repo, "Fix checkout")
        session = self.hub._session(chat["id"])
        self.assertIn("A store front in Flask.", session.system_prompt())
        self.assertIn('belongs to the project "Shop"', session.system_prompt())
        from claire_core import controller
        self.assertIn("Fix checkout", controller.state(session))
        general = self.hub.new_chat()
        main = controller.state(self.hub._session(general["id"]))
        self.assertIn('"project": "Shop"', main)          # active work names its project
        self.assertIn("· Shop · Fix checkout (queued) → projects/", main)
        prompt = self.hub._session(general["id"]).system_prompt()
        self.assertIn("- Shop (", prompt)                  # every chat knows the projects that exist
        self.assertIn("→ projects/" + project["slug"] + "/PROJECT.md", prompt)
        self.assertNotIn("A store front in Flask.\n", prompt.split("Shared memory index")[0])
        self.assertEqual(task["project"], project["id"])

    def test_spawn_from_a_project_chat_works_in_the_project_folder(self):
        self.core.settings.controller_enabled = True
        repo = folder(self.env, "shop", git=True)
        project = self.core.projects.create("Shop", repo)
        chat = self.hub.new_chat(project=project["id"])
        self.env.llm.replies = [decision("spawn_task", title="Add tests", task="Add tests for checkout."),
                                decision("done", summary="Started.")]
        self.hub._session(chat["id"]).run("add tests")
        task = self.core.store.threads(kind="task")[0]
        self.assertEqual((task["workdir"], task["project"], task["parent"]), (repo, project["id"], chat["id"]))

    def test_recall_puts_the_threads_own_project_first(self):
        a, b = folder(self.env, "alpha"), folder(self.env, "beta")
        self.core.vault.remember("project", "Deploy notes: alpha uses fly.", workdir=a)
        self.core.vault.remember("project", "Deploy notes: beta uses render.", workdir=b)
        slug = self.core.vault.project_folder(b)
        first = self.core.vault.recall("deploy notes", prefer=slug).split("### ")[1]
        self.assertTrue(first.startswith(f"projects/{slug}/"), first[:80])

    def test_old_single_file_project_notes_and_work_move_into_the_project(self):
        repo = folder(self.env, "legacy")
        flat = os.path.join(self.core.vault.path, "projects", "legacy.md")
        os.makedirs(os.path.dirname(flat), exist_ok=True)
        with open(flat, "w") as f:
            f.write(f"---\ntags: [claire, project]\npath: {repo}\n---\n# legacy\n\nUses make.\n")
        self.core.vault.work.record("legacy", "Refactored the parser.", workdir=repo, entry_id="old-1")
        project = self.core.projects.for_task(repo)
        self.assertFalse(os.path.exists(flat))
        self.assertIn("Uses make.", self.core.vault.project_brief(project["slug"]))
        self.assertIn("Refactored the parser.", self.core.vault.project_work(project["slug"]).context())
        self.core.vault.ensure_project(project["slug"], "legacy", repo)     # adopting twice adds nothing
        self.assertEqual(len(self.core.vault.project_work(project["slug"]).entries()), 1)

    def test_tasks_from_before_projects_are_filed_when_the_service_starts(self):
        repo = folder(self.env, "older")
        tid = self.core.store.create_thread("task", "Old work", repo)
        before = self.core.store.thread(tid)["updated"]
        Hub(self.core, start_brain=False)
        row = self.core.store.thread(tid)
        self.assertEqual(self.core.store.project(row["project"])["path"], repo)
        self.assertEqual(row["updated"], before)          # filing is not activity

    def test_an_existing_database_gains_the_project_column(self):
        path = os.path.join(self.env.root, "old.db")
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, kind TEXT, title TEXT, workdir TEXT, "
                   "parent TEXT, status TEXT, summary TEXT, created REAL, updated REAL)")
        db.execute("INSERT INTO threads VALUES ('t1','task','x','/x',NULL,'done','',1,1)")
        db.commit()
        db.close()
        store = Store(path)
        self.addCleanup(store.close)
        self.assertEqual(store.thread("t1")["project"], "")


class SharedMemoryTests(unittest.TestCase):
    """One index every chat and task reads: a line per memory, and where its
    detail is, so the detail is read only when it is needed."""

    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)
        self.core = self.env.core
        self.hub = Hub(self.core, start_brain=False)
        self.addCleanup(self.hub.stop)
        vault = self.core.vault
        vault.remember("sir", "Works on Claire in the evenings; prefers short answers.", home=True)
        vault.remember("learned", "This machine has no `python`; use `python3`.", note="python3 only")
        self.repo = folder(self.env, "shop", git=True)
        self.project = self.core.projects.create("Shop", self.repo)
        vault.write_project_brief(self.project["slug"], "# Shop\n\nA store front in Flask. " + "Detail. " * 400)
        self.hub.spawn_task("Fix checkout", "Fix the checkout bug.", self.repo)

    def test_the_index_has_a_line_per_memory_and_where_its_detail_is(self):
        index = self.core.projects.memory_index()
        self.assertIn("- Works on Claire in the evenings; prefers short answers.", index)
        self.assertIn("· Shop · Fix checkout (queued) → projects/shop/work/recent.md", index)
        self.assertIn("- Shop (", index)
        self.assertIn("A store front in Flask.", index)
        self.assertIn("→ projects/shop/PROJECT.md", index)
        self.assertIn("- python3-only: This machine has no `python`; use `python3`. → learned/python3-only.md", index)
        self.assertNotIn("Detail. Detail. Detail. Detail. Detail. Detail. Detail. Detail. Detail. Detail. Detail. Detail. Detail. Detail. Detail. Detail. Detail. Detail. Detail.", index)
        with open(os.path.join(self.core.vault.path, "MEMORY.md")) as f:
            self.assertIn("→ projects/shop/PROJECT.md", f.read())

    def test_scratch_folder_work_is_listed_as_no_project(self):
        scratch = os.path.join(os.path.expanduser("~"), "claire-work", "20261112-220925-build-it")
        self.core.vault.work.record("20261112-220925-build-it", "Task: Build it\nStatus: done\n\nBuilt.",
                                    workdir=scratch, entry_id="old-scratch")
        self.assertIn("· no project · Build it (done)", self.core.projects.memory_index())

    def test_recall_reads_a_note_by_its_path_and_never_returns_the_index(self):
        self.core.projects.memory_index()
        full = self.core.vault.recall("projects/shop/PROJECT.md")
        self.assertIn("Detail. " * 300, full)
        self.assertIn("No note at", self.core.vault.recall("learned/missing.md"))
        self.assertIn("No note at", self.core.vault.recall("../outside.md"))
        self.assertNotIn("### MEMORY.md", self.core.vault.recall("Shop"))

    def test_every_chat_and_task_carries_the_index(self):
        self.core.settings.controller_enabled = True
        chat = self.hub.new_chat()
        prompt = self.hub._session(chat["id"]).system_prompt()
        self.assertIn("Shared memory index", prompt)
        self.assertIn("→ learned/python3-only.md", prompt)
        task = self.core.store.threads(kind="task")[0]
        worker = self.hub._session(task["id"]).system_prompt()
        self.assertIn("Works on Claire in the evenings", worker)
        self.assertIn("→ learned/python3-only.md", worker)
        self.assertIn("Fix checkout (queued)", worker)


class ProjectServiceTests(ServiceCase):
    def test_browse_returns_the_folder_picked_in_the_finder(self):
        self.boot()
        repo = folder(self.env, "shop")
        picked = subprocess.CompletedProcess([], 0, repo + "/\n", "")
        with patch("claire_core.service.sys.platform", "darwin"), \
             patch("claire_core.service.subprocess.run", return_value=picked) as run:
            self.assertEqual(self.client.post("/folders/pick")["path"], repo)
        self.assertEqual(run.call_args.args[0][0], "osascript")
        self.assertEqual(run.call_args.kwargs["timeout"], 300)
        cancelled = subprocess.CompletedProcess([], 1, "", "execution error: User canceled. (-128)")
        with patch("claire_core.service.sys.platform", "darwin"), \
             patch("claire_core.service.subprocess.run", return_value=cancelled):
            self.assertEqual(self.client.post("/folders/pick")["path"], "")
        home = subprocess.CompletedProcess([], 0, os.path.expanduser("~") + "/\n", "")
        with patch("claire_core.service.sys.platform", "darwin"), \
             patch("claire_core.service.subprocess.run", return_value=home):
            with self.assertRaises(Exception):
                self.client.post("/folders/pick")          # the home folder itself is refused
        with patch("claire_core.service.sys.platform", "linux"):
            with self.assertRaises(Exception):
                self.client.post("/folders/pick")

    def test_projects_are_created_listed_edited_and_removed_without_losing_chats(self):
        self.boot()
        repo = folder(self.env, "shop", git=True)
        made = self.client.post("/projects", {"path": repo})["project"]
        self.assertEqual((made["name"], made["path"]), ("shop", repo))
        group = self.client.post("/projects", {"name": "Reading list"})["project"]
        self.assertEqual(group["path"], "")
        names = [p["name"] for p in self.client.get("/projects")["projects"]]
        self.assertEqual(sorted(names), ["Reading list", "shop"])

        chat = self.client.post("/threads", {"kind": "chat", "project": made["id"]})["thread"]
        self.assertEqual(chat["project"], made["id"])
        self.client.post(f"/projects/{made['id']}/brief", {"text": "# Shop\n\nFlask app.\n"})
        self.client.post(f"/projects/{made['id']}/rename", {"name": "Shop"})
        detail = self.client.get(f"/projects/{made['id']}")
        self.assertIn("Flask app.", detail["project"]["brief"])
        self.assertEqual(detail["project"]["name"], "Shop")
        self.assertEqual([t["id"] for t in detail["threads"]], [chat["id"]])

        moved = self.client.post(f"/threads/{chat['id']}/project", {"project": group["id"]})["thread"]
        self.assertEqual(moved["project"], group["id"])
        self.client.delete(f"/projects/{group['id']}")
        self.assertEqual(self.client.get(f"/threads/{chat['id']}")["thread"]["project"], "")
        memory = os.path.join(self.env.core.vault.path, "projects", group["slug"], "PROJECT.md")
        self.assertTrue(os.path.isfile(memory), "removing a project leaves its memory for sir")

    def test_a_project_folder_must_be_a_safe_existing_folder(self):
        self.boot()
        for bad in ["/", "~", "/etc", self.env.core.settings.state_dir, "/nope/x"]:
            with self.assertRaises(Exception, msg=bad):
                self.client.post("/projects", {"path": bad})
        repo = folder(self.env, "shop")
        self.client.post("/projects", {"path": repo})
        with self.assertRaises(Exception):
            self.client.post("/projects", {"path": repo})      # one project per folder
        self.assertEqual(len(self.client.get("/projects")["projects"]), 1)

    def test_the_main_chat_and_tasks_cannot_be_moved(self):
        self.boot(task=[done("ok")])
        group = self.client.post("/projects", {"name": "Group"})["project"]
        with self.assertRaises(Exception):
            self.client.post("/threads/home/project", {"project": group["id"]})
        task = self.client.post("/threads", {"task": "say ok", "project": group["id"]})["thread"]
        self.assertEqual(task["project"], group["id"])
        with self.assertRaises(Exception):
            self.client.post(f"/threads/{task['id']}/project", {"project": ""})

    def test_a_projects_task_works_in_its_folder_and_shows_in_global_active_work(self):
        gate = []
        def hold(messages):
            while not gate:
                time.sleep(0.02)
            return done("finished")
        self.boot(task=[hold])
        repo = folder(self.env, "shop", git=True)
        made = self.client.post("/projects", {"path": repo})["project"]
        task = self.client.post("/threads", {"task": "do it", "project": made["id"]})["thread"]
        self.assertEqual(task["workdir"], repo)
        self.wait_for(lambda e: e["type"] == "thread_status" and e["thread"] == task["id"]
                      and e["status"] == "running")
        active = self.client.get("/status")["active_work"]
        self.assertEqual([(a["id"], a["project_name"]) for a in active], [(task["id"], "shop")])
        self.assertEqual(self.client.get("/projects")["projects"][0]["active"], 1)
        gate.append(1)
        self.wait_for(lambda e: e["type"] == "turn_end" and e["thread"] == task["id"])
        self.assertEqual(self.client.get("/status")["active_work"], [])


if __name__ == "__main__":
    unittest.main()


class NewFolderAndNoteTests(unittest.TestCase):
    """Found by live runs: a task asked for in a new folder failed before the
    folder was made; a note added to PROJECT.md merged into its last bullet."""

    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)
        self.env.core.settings.controller_enabled = True
        self.hub = Hub(self.env.core, start_brain=False)
        self.addCleanup(self.hub.stop)
        self.chat = self.hub.new_chat()["id"]

    def ask(self, text, workdir):
        self.env.llm.replies = [decision("spawn_task", title="Build it", task="Build the app.", workdir=workdir),
                                decision("done", summary="Done.")]
        session = self.hub._session(self.chat)
        session.sink = self.env.events.append
        session.run(text)
        return [e for e in self.env.events if e["type"] == "tool_result" and e["name"] == "spawn_task"][-1]

    def test_a_new_folder_named_in_the_request_is_created_for_its_task(self):
        target = os.path.join(self.env.root, "fresh-app")
        result = self.ask(f"Build me a small app in a new folder {target}", target)
        self.assertFalse(result["failed"])
        self.assertTrue(os.path.isdir(target))
        self.assertEqual(self.env.core.store.threads(kind="task")[0]["workdir"], os.path.realpath(target))

    def test_a_folder_the_request_does_not_name_is_not_created(self):
        target = os.path.join(self.env.root, "invented")
        result = self.ask("Build me a small app somewhere sensible", target)
        self.assertTrue(result["failed"])
        self.assertFalse(os.path.exists(target))
        self.assertEqual(self.env.core.store.threads(kind="task"), [])

    def test_a_protected_place_is_never_created(self):
        target = os.path.join(self.env.core.settings.state_dir, "sneaky")
        result = self.ask(f"Build it in a new folder {target}", target)
        self.assertTrue(result["failed"])
        self.assertFalse(os.path.exists(target))

    def test_a_note_after_a_list_starts_its_own_paragraph(self):
        repo = folder(self.env, "shop")
        project = self.env.core.projects.create("Shop", repo)
        self.env.core.vault.write_project_brief(project["slug"], "# Shop\n\n- Uses Flask.\n")
        self.env.core.vault.remember("project", "Amounts are in rupees.", folder=project["slug"], name="Shop")
        brief = self.env.core.vault.project_brief(project["slug"])
        self.assertIn("- Uses Flask.\n\nAmounts are in rupees.\n", brief)


class NewFolderPathTests(NewFolderAndNoteTests):
    def test_a_bare_name_is_never_created_relative_to_claires_own_folder(self):
        result = self.ask("Build it in a new folder called fresh-app", "fresh-app")
        self.assertTrue(result["failed"])
        self.assertFalse(os.path.exists(os.path.join(os.getcwd(), "fresh-app")))
