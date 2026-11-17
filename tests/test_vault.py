import os
import unittest

from tests.helpers import Env


class VaultTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.vault = self.env.core.vault

    def tearDown(self):
        self.env.close()

    def test_identity_is_seeded_once_and_then_belongs_to_the_vault(self):
        self.assertIn("You are Claire", self.vault.identity())
        self.assertNotIn("tags:", self.vault.identity())
        path = os.path.join(self.vault.path, "identity", "claire.md")
        with open(path, "w") as f:
            f.write("# Claire\n\nEdited by sir.\n")
        self.vault.init()
        self.assertIn("Edited by sir.", self.vault.identity())

    def test_remember_then_recall_by_related_word(self):
        self.vault.remember("learned", "llama-server honours enable_thinking per request.",
                            note="llama-server thinking", workdir=self.env.work)
        self.vault.remember("learned", "The uploader retries three times.",
                            note="uploader", workdir=self.env.work)
        hit = self.vault.recall("llama-cpp")
        self.assertIn("llama-server-thinking.md", hit)
        self.assertNotIn("uploader", hit)
        self.assertIn("No notes matching", self.vault.recall("kubernetes"))

    def test_recall_sees_edits_and_deletions_made_by_hand(self):
        self.vault.remember("learned", "Deploys go out on Fridays.", note="deploys",
                            workdir=self.env.work)
        self.assertIn("Fridays", self.vault.recall("deploys"))
        path = os.path.join(self.vault.path, "learned", "deploys.md")
        with open(path, "w") as f:
            f.write("# deploys\n\nDeploys go out on Tuesdays.\n")
        os.utime(path, (1, 1))
        self.assertIn("Tuesdays", self.vault.recall("deploys"))
        os.remove(path)
        self.assertIn("No notes matching", self.vault.recall("deploys"))

    def test_the_same_fact_is_not_saved_twice(self):
        first = self.vault.remember("project", "Tests run with pytest.", workdir=self.env.work)
        again = self.vault.remember("project", "Tests run with pytest.", workdir=self.env.work)
        self.assertTrue(first.startswith("Created"))
        self.assertTrue(again.startswith("Already in memory"))

    def test_two_projects_with_the_same_folder_name_get_separate_notes(self):
        a = os.path.join(self.env.root, "a", "app")
        b = os.path.join(self.env.root, "b", "app")
        os.makedirs(a), os.makedirs(b)
        self.vault.remember("project", "A uses postgres.", workdir=a)
        self.vault.remember("project", "B uses sqlite.", workdir=b)
        self.assertNotEqual(self.vault.project_note(a), self.vault.project_note(b))
        self.assertIn("postgres", self.vault.load_memory(a))
        self.assertNotIn("sqlite", self.vault.load_memory(a))
        self.assertIn("sqlite", self.vault.load_memory(b))

    def test_notes_about_dad_are_kept_by_the_home_thread_only(self):
        refused = self.vault.remember("sir", "Likes short answers.", workdir=self.env.work)
        self.assertTrue(refused.startswith("ERROR"))
        self.vault.remember("sir", "Likes short answers.", home=True)
        self.assertIn("Likes short answers.", self.vault.identity())

    def test_inbox_notes_stay_open_until_marked_done(self):
        path = self.vault.inbox_note("tool-web_search", "`web_search` needs repair", "It broke.")
        self.assertEqual(self.vault.inbox_open(),
                         [("tool-web-search", "`web_search` needs repair")])
        self.assertIn("tool-web-search", self.vault.load_home_memory())
        with open(path) as f:
            text = f.read()
        with open(path, "w") as f:
            f.write(text.replace("status: open", "status: done"))
        self.assertEqual(self.vault.inbox_open(), [])
        self.vault.inbox_note("tool-web_search", "`web_search` needs repair", "Broke again.")
        self.assertEqual(len(self.vault.inbox_open()), 1)   # a new fault reopens it


if __name__ == "__main__":
    unittest.main()
