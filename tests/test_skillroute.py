"""Skill routing: a small model picks the skills a request needs."""

import os
import unittest
from unittest.mock import patch

from claire_core import skillroute
from tests.helpers import Env, done


def skill(env, name, description, **meta):
    folder = os.path.join(env.core.settings.vault, "skills")
    os.makedirs(folder, exist_ok=True)
    extra = "".join(f"{k}: {v}\n" for k, v in meta.items())
    with open(os.path.join(folder, f"{name}.md"), "w") as f:
        f.write(f"---\nname: {name}\ndescription: {description}\n{extra}---\n# {name}\n\nSteps for {name}.\n")


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)
        skill(self.env, "building-integrations", "Writing a new integration.",
              requires="coding", when="sir wants to connect a service Claire has no integration for")
        skill(self.env, "integrations", "Working with connected services.", kinds="home, agent")
        skill(self.env, "writing", "Writing reports.")

    def test_the_small_model_picks_and_requirements_come_along(self):
        self.env.llm.replies = ['Sure: {"skills": ["building-integrations", "integrations"]}']
        picked = skillroute.choose(self.env.core, "connect my youtube", "agent")
        self.assertEqual(picked, ["coding", "building-integrations", "integrations"])
        call = self.env.llm.calls[0]
        self.assertEqual(call["role"], "clerk")
        prompt = call["messages"][0]["content"]
        self.assertIn("- building-integrations: Writing a new integration. Use when: sir wants to connect", prompt)
        self.assertIn("connect my youtube", prompt)

    def test_only_skills_for_this_kind_of_thread_and_not_already_loaded_are_offered(self):
        self.env.llm.replies = ['{"skills": ["integrations", "writing"]}']
        self.assertEqual(skillroute.choose(self.env.core, "is gmail connected?", "home"), ["integrations"])
        prompt = self.env.llm.calls[0]["messages"][0]["content"]
        self.assertNotIn("- writing:", prompt)                       # task-only skill
        self.env.llm.replies = ['{"skills": ["writing"]}']
        self.assertEqual(skillroute.choose(self.env.core, "write it up", "agent", exclude=["writing"]), [])

    def test_routing_never_breaks_a_turn(self):
        for reply in ["no idea", '{"skills": ["poetry"]}', '{"skills": "writing"}', RuntimeError("down")]:
            self.env.llm.replies = [reply]
            self.assertEqual(skillroute.choose(self.env.core, "do something", "agent"), [], reply)
        self.assertEqual(skillroute.choose(self.env.core, "   ", "agent"), [])     # nothing asked: no call

    def test_use_skill_with_a_description_loads_what_the_router_picks(self):
        self.env.core.settings.skill_router = True
        self.env.llm.replies = ["Need it.\n<use_skill>\n<name>adding a youtube connection</name>\n</use_skill>",
                                '{"skills": ["building-integrations"]}', done()]
        session = self.env.session()
        session.run("go")
        self.assertEqual(session.skills, ["coding", "building-integrations"])
        self.assertIn("# Skill: building-integrations", session.messages[0]["content"])


class TurnTests(unittest.TestCase):
    def test_each_turn_starts_with_the_skills_it_needs_loaded(self):
        from claire_core.hub import Hub
        env = Env()
        self.addCleanup(env.close)
        skill(env, "writing", "Writing reports.")
        env.core.settings.skill_router = True
        hub = Hub(env.core, start_brain=False)
        tid = env.core.store.create_thread("task", "Report", env.work)
        seen = []
        env.llm.replies = ['{"skills": ["writing"]}',
                           lambda messages: seen.append(messages[0]["content"]) or done()]
        with patch.object(hub, "publish", lambda e: env.events.append(e)):
            hub._run_turn(tid, "write the weekly report", 1)
        self.assertIn("# Skill: writing", seen[0])                  # loaded before the main model began
        self.assertEqual(env.core.store.thread(tid)["skills"], "writing")
        self.assertTrue(any(e["type"] == "notice" and e.get("text") == "Skills loaded: writing"
                            for e in env.events))


class BudgetTests(unittest.TestCase):
    def test_a_skill_can_give_its_work_more_steps_even_loaded_partway(self):
        env = Env(max_steps=3)
        self.addCleanup(env.close)
        skill(env, "long-work", "Long work.", steps="6")
        load = "Need it.\n<use_skill>\n<name>long-work</name>\n</use_skill>"
        busy = "Looking.\n<list_files>\n<path>.</path>\n</list_files>"
        env.llm.replies = [load] + [busy] * 4 + [done()]
        session = env.session()
        self.assertEqual(session.run("go")["status"], "done")       # 6 steps, though the cap is 3
        self.assertEqual(session.step_budget(), 6)
        env.llm.replies = [busy] * 10
        plain = env.session()
        self.assertEqual(plain.run("go")["status"], "stopped")
        self.assertEqual(len(env.llm.calls) - 6, 3)                  # the usual cap without it


if __name__ == "__main__":
    unittest.main()
