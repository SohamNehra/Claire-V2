import os
import os
import unittest

from claire_core.skillset import SkillError, Skills
from tests.helpers import Env, done

import claire_core.skillset as _skillset
BUILT_IN = sorted(f[:-3] for f in os.listdir(_skillset.BUILTIN) if f.endswith(".md"))
from tests.test_service import ServiceCase

WRITING = "---\nname: writing\ndescription: Writing clear reports for sir.\n---\n# Reports\n\nLead with the outcome.\n"


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)
        self.skills = self.env.core.skills

    def test_the_coding_skill_ships_built_in(self):
        coding = self.skills.get("coding")
        self.assertEqual(coding["source"], "builtin")
        for heading in ["# Root cause", "# Scope", "# Tests and specifications are the authority",
                        "# Auditing"]:
            self.assertIn(heading, coding["text"])
        self.assertNotIn("---", coding["text"][:10])              # header is not part of the text

    def test_saving_listing_and_removing_one_of_yours(self):
        made = self.skills.save("writing", "Writing clear reports.", "Lead with the outcome.")
        self.assertEqual((made["source"], made["text"]), ("user", "Lead with the outcome."))
        self.assertEqual([s["name"] for s in self.skills.list()], sorted(BUILT_IN + ["writing"]))
        path = os.path.join(self.env.core.vault.path, "skills", "writing.md")
        self.assertTrue(os.path.exists(path))                     # a file sir can edit
        with open(path, "a") as f:
            f.write("\nNo filler.\n")
        self.assertIn("No filler.", self.skills.get("writing")["text"])
        self.assertTrue(self.skills.delete("writing"))
        self.assertIsNone(self.skills.get("writing"))

    def test_yours_replaces_a_built_in_and_removing_it_brings_the_built_in_back(self):
        self.skills.save("coding", "My way of coding.", "Tabs, not spaces.")
        self.assertEqual(self.skills.get("coding")["text"], "Tabs, not spaces.")
        self.assertTrue(self.skills.delete("coding"))
        self.assertIn("# Root cause", self.skills.get("coding")["text"])
        self.assertFalse(self.skills.delete("coding"))            # the built-in stays

    def test_an_uploaded_file_names_and_describes_itself(self):
        made = self.skills.save_upload("anything.md", WRITING)
        self.assertEqual((made["name"], made["description"]), ("writing", "Writing clear reports for sir."))
        plain = self.skills.save_upload("My Research Notes.md", "# How to research\n\nCheck two sources.")
        self.assertEqual((plain["name"], plain["description"]), ("my-research-notes", "How to research"))

    def test_bad_skills_are_refused(self):
        for name, desc, text in [("../evil", "d", "t"), ("Has Space", "d", "t"), ("", "d", "t"),
                                 ("ok", "", "t"), ("ok", "d", "  "), ("ok", "d", "x" * 40_001),
                                 ("a/b", "d", "t"), (".hidden", "d", "t")]:
            with self.assertRaises(SkillError, msg=name):
                self.skills.save(name, desc, text)
        self.assertEqual(self.skills.names(), BUILT_IN)            # nothing was written
        self.assertFalse(os.path.exists(os.path.join(self.env.root, "evil.md")))


class ThreadTests(unittest.TestCase):
    def start(self, replies, **kw):
        self.env = Env(replies)
        self.addCleanup(self.env.close)
        self.env.core.skills.save("writing", "Writing clear reports.", "Lead with the outcome.")
        return self.env.session(**kw)

    def test_a_thread_without_a_skill_sees_the_list_but_not_the_text(self):
        prompt = self.start([]).system_prompt()
        offered = prompt.split("Not loaded now: ", 1)[1].split(".", 1)[0]
        self.assertIn("`coding`", offered)
        self.assertIn("`writing`", offered)
        self.assertIn("coding: Working in a codebase", prompt)   # recover if routing is unavailable
        self.assertIn("writing: Writing clear reports.", prompt)
        self.assertNotIn("# Root cause", prompt)
        self.assertNotIn("Lead with the outcome.", prompt)
        self.assertIn("# Verify before you claim done", prompt)   # the core method is always there

    def test_a_thread_started_with_a_skill_has_its_text(self):
        s = self.start([], skills=["coding", "no-such-skill"])
        prompt = s.system_prompt()
        self.assertEqual(s.skills, ["coding"])
        self.assertIn("Loaded for this task: `coding`", prompt)
        self.assertIn("# Skill: coding\n\n# Finding things", prompt)
        self.assertIn("# Root cause", prompt)
        self.assertNotIn("`coding`", prompt.split("Not loaded now: ", 1)[1].split(".", 1)[0])  # no longer offered

    def test_claire_loads_a_skill_when_the_task_calls_for_it(self):
        load = "This is report writing.\n<use_skill>\n<name>writing</name>\n</use_skill>"
        s = self.start([load, load, "Loading.\n<use_skill>\n<name>poetry</name>\n</use_skill>", done()])
        s.run("write the weekly report")
        results = [m["content"] for m in s.messages if str(m["content"]).startswith("[use_skill result]")]
        self.assertIn("Loaded `writing`.", results[0])
        self.assertIn("already loaded", results[1])
        self.assertIn("no skill matches 'poetry'. Available: ", results[2])
        self.assertIn("`writing`", results[2])
        self.assertIn("# Skill: writing\n\nLead with the outcome.", s.messages[0]["content"])
        seen_by_model = self.env.llm.calls[1]["messages"][0]["content"]
        self.assertIn("Lead with the outcome.", seen_by_model)   # in force on the very next step
        self.assertEqual(self.env.core.store.thread(s.thread_id), None)   # (no row in this test)

    def test_a_skill_marked_always_is_in_every_task_and_not_in_home(self):
        from claire_core.hub import Hub
        s = self.start([])
        self.env.core.skills.save("house-style", "How sir wants reports.", "End with CHECKED.", always=True)
        self.assertEqual(self.env.session().skills, ["house-style"])
        self.assertEqual(self.env.session(skills=["coding"]).skills, ["coding", "house-style"])
        with open(os.path.join(self.env.core.vault.path, "skills", "house-style.md")) as saved:
            self.assertIn("always: true", saved.read())
        Hub(self.env.core, start_brain=False)
        self.assertEqual(self.env.session(kind="home").skills, [])

    def test_home_is_told_which_skills_it_can_hand_to_a_task(self):
        from claire_core.hub import Hub
        self.start([])
        Hub(self.env.core, start_brain=False)
        home = self.env.session(kind="home")
        prompt = home.system_prompt()
        self.assertIn("When you start a task, name in spawn_task every skill its work needs", prompt)
        self.assertIn("`coding`", prompt.split("Not loaded now: ", 1)[1].split(".", 1)[0])
        self.assertIn("use_skill", home.tools)               # the chat can load one too
        self.assertIn("<skills>coding</skills>", prompt)

    def test_there_is_no_tool_that_writes_a_skill(self):
        s = self.start([])
        self.assertEqual([t for t in s.tools if "skill" in t], ["use_skill"])
        with self.assertRaises(Exception):
            from claire_core.guard import safe_path
            safe_path(s.workdir, os.path.join(self.env.core.vault.path, "skills", "x.md"))


class ApiTests(ServiceCase):
    SPAWN = ("Code work, so the coding skill.\n<spawn_task>\n<title>Fix it</title>\n"
             "<skills>coding</skills>\n<task>\nFix the bug.\n</task>\n</spawn_task>")

    def test_the_skills_api(self):
        self.boot()
        self.assertEqual([s["name"] for s in self.client.get("/skills")["skills"]], BUILT_IN)
        made = self.client.post("/skills", {"filename": "writing.md", "text": WRITING})["skill"]
        self.assertEqual((made["name"], made["source"]), ("writing", "user"))
        self.assertEqual(self.client.get("/skills/writing")["skill"]["text"], "# Reports\n\nLead with the outcome.")
        edited = self.client.post("/skills", {"name": "writing", "description": "Reports.",
                                              "text": "Outcome first. No filler."})["skill"]
        self.assertEqual(edited["text"], "Outcome first. No filler.")
        for bad in [{"name": "../x", "description": "d", "text": "t"}, {"name": "ok"}]:
            with self.assertRaises(Exception):
                self.client.post("/skills", bad)
        auth = {"Authorization": f"Bearer {self.service.token}"}
        self.assertEqual(self.raw("/skills/writing", method="DELETE"), 403)
        self.assertEqual(self.raw("/skills/coding", auth, method="DELETE"), 400)    # built-in
        self.assertEqual(self.raw("/skills/writing", auth, method="DELETE"), 200)
        self.assertEqual(self.raw("/skills/writing", auth, method="DELETE"), 404)

    def test_uploaded_always_skill_is_applied_to_new_tasks(self):
        self.boot(task=[done("CHECKED")])
        raw = WRITING.replace("description:", "always: true\ndescription:")
        made = self.client.post("/skills", {"filename": "writing.md", "text": raw})["skill"]
        self.assertTrue(made["always"])
        task = self.client.post("/threads", {"task": "Write a report", "workdir": self.env.work})["thread"]
        self.assertIn("writing", task["skills"])
        prompt = self.hub._session(task["id"]).system_prompt()
        self.assertIn("Lead with the outcome.", prompt)
        self.assertNotIn("Lead with the outcome.", self.hub._session("home").system_prompt())

    def test_home_starts_a_task_with_the_skill_it_names(self):
        self.boot(home=[self.SPAWN, done("Started.")], task=[done("Fixed.")])
        self.llm.scripts["home"].append(done("It is fixed."))
        self.client.post("/threads/home/messages", {"text": "fix the bug in my project"})
        self.wait_for(lambda e: e["type"] == "answer" and e["text"] == "It is fixed.")
        task = self.client.get("/threads?kind=task")["threads"][0]
        self.assertEqual(task["skills"], "coding")
        self.assertIn("# Skill: coding", self.hub._session(task["id"]).messages[0]["content"])
        home_prompt = self.hub._session("home").messages[0]["content"]
        self.assertNotIn("# Root cause", home_prompt)             # home itself stays light

    def test_a_task_cannot_be_started_with_a_skill_that_does_not_exist(self):
        self.boot()
        with self.assertRaises(Exception):
            self.client.post("/threads", {"task": "x", "workdir": self.env.work, "skills": ["nope"]})
        row = self.client.post("/threads", {"task": "x", "workdir": self.env.work,
                                            "skills": ["coding"]})["thread"]
        self.assertEqual(row["skills"], "coding")

    def test_a_new_skill_reaches_an_open_conversation_without_a_restart(self):
        self.boot(home=[done("Hello."), done("Hello again.")])

        def turn(text):
            n = self.client.post("/threads/home/messages", {"text": text})["turn"]
            self.wait_for(lambda e: e["type"] == "turn_end" and e.get("turn") == n)
            return self.hub._session("home").messages[0]["content"]

        self.assertNotIn("`writing`", turn("hi"))
        self.client.post("/skills", {"filename": "writing.md", "text": WRITING})
        self.assertIn("`writing`", turn("hi again"))


if __name__ == "__main__":
    unittest.main()
