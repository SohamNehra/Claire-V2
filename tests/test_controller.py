import json
import os
import unittest
from unittest.mock import patch

from claire_core import controller
from claire_core.hub import Hub
from claire_core.llm import ModelError
from tests.helpers import Env, symlink_or_skip
from claire_core.tools.local import locations


def decision(tool, **args):
    return json.dumps({"tool": tool, "arguments": args})


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)
        self.env.core.settings.controller_enabled = True
        self.hub = Hub(self.env.core, start_brain=False)
        publish = self.hub.publish
        def collect(event):
            self.env.events.append(event)
            publish(event)
        self.hub.publish = collect
        self.addCleanup(self.hub.stop)

    def home(self, replies=()):
        self.env.llm.replies = list(replies)
        return self.hub._session("home")

    def test_greeting_uses_only_talk_and_does_not_route_skills(self):
        self.env.core.settings.skill_router = True
        self.env.llm.replies = [decision("done", summary="Hey sir.")]
        with patch("claire_core.hub.skillroute.choose", side_effect=AssertionError("extra routing call")):
            self.hub._run_turn("home", "hi", 1)
        self.assertEqual([c["role"] for c in self.env.llm.calls], ["talk"])
        self.assertEqual(self.hub._session("home").profile, "controller")

    def test_literal_folder_creation_and_verification(self):
        path = os.path.join(self.env.work, "Café Δ")
        s = self.home([decision("local_files", action="create_directory", path=path),
                       decision("done", summary="Created the directory.")])
        self.assertEqual(s.run("Create the directory " + path)["status"], "done")
        self.assertTrue(os.path.isdir(path))
        self.assertEqual(os.listdir(path), [])
        self.assertNotIn("spawn_task", [e.get("name") for e in self.env.results()])
        self.assertEqual(self.env.asked, [])

    def test_model_title_casing_is_restored_to_the_users_literal_name(self):
        proposed = os.path.join(self.env.work, "Claire Testing")
        s = self.home([decision("local_files", action="create_directory", path=proposed),
                       decision("done", summary="Created claire testing.")])
        s.run("Create a folder named claire testing in " + self.env.work)
        self.assertIn("claire testing", os.listdir(self.env.work))
        self.assertNotIn("Claire Testing", os.listdir(self.env.work))

    def test_an_invented_path_is_refused_without_side_effects(self):
        path = os.path.join(self.env.work, "unasked")
        s = self.home([decision("local_files", action="create_directory", path=path),
                       decision("done", summary="Which name and location?")])
        s.run("make it there")
        self.assertFalse(os.path.exists(path))
        self.assertTrue(any(e.get("failed") for e in self.env.results()))

    def test_lowercase_desktop_resolves_without_renaming_system_landmark(self):
        if not os.path.isdir(locations().get('desktop', '')):
            self.skipTest('This account has no Desktop directory; redirected folder fixtures are covered by test_host')
        for phrase in ("which folders are on desktop?", "umm my pc desktop"):
            s = self.home([decision("local_files", action="list_directory", path="~/Desktop", kind="directories"),
                           decision("done", summary="Here are the folders.")])
            s.run(phrase)
            result = [m["content"] for m in s.messages if isinstance(m["content"], str)
                      and m["content"].startswith("[local_files result]")][-1]
            self.assertIn("Verified: listed " + locations()["desktop"], result)
            self.assertNotIn("REFUSED", result)

    def test_readonly_navigation_can_discover_root_without_user_spelling_it(self):
        s = self.home([decision("local_files", action="list_directory", path="/"),
                       decision("done", summary="That was the wrong location.")])
        s.run("Find the system folders")
        self.assertFalse(self.env.results()[-1]["failed"])

    def test_invalid_path_is_an_argument_error_not_a_permission_refusal(self):
        s = self.home([decision("local_files", action="list_directory", path="not a valid path"),
                       decision("done", summary="The path argument was invalid.")])
        answer = s.run("Find my Desktop")["answer"]
        self.assertIn('ERROR: invalid path', answer)
        self.assertNotIn('REFUSED:', answer)
        self.assertIn('including / and Desktop', s.registry.get('local_files').doc)

    def test_root_listing_is_root_and_home_alias_is_supported(self):
        s = self.home([decision("local_files", action="list_directory", path="root"),
                       decision("done", summary="Here are the root entries.")])
        s.run("tell me all paths at root")
        self.assertFalse(self.env.results()[-1]["failed"])
        receipt = next(m["content"] for m in s.messages if m["content"].startswith("[local_files result]"))
        self.assertIn("Verified: listed " + locations()["root"] + " (", receipt)
        self.assertIn("[directory]", receipt)
        self.env.llm.replies = [decision("local_files", action="list_directory", path="~"),
                                decision("done", summary="Here are the home entries.")]
        s.run("Now list my home directory")
        self.assertFalse(self.env.results()[-1]["failed"])

    def test_os_location_discovery_needs_no_user_supplied_absolute_path(self):
        s = self.home([decision("local_files", action="locations"), decision("done", summary="Located it.")])
        s.run("Where is my desktop?")
        self.assertFalse(self.env.results()[-1]["failed"])
        # OS locations never change, so they are standing facts in the system
        # prompt (read once and reused), not in the block rewritten every call.
        view = controller.messages(s)
        self.assertIn('"root": ' + json.dumps(locations()["root"]), view[0]["content"])
        self.assertNotIn('OS locations:', view[-1]["content"])

    def test_listing_filters_folders_and_establishes_navigation(self):
        folder = os.path.join(self.env.work, "child")
        os.mkdir(folder)
        with open(os.path.join(self.env.work, "file.txt"), "w") as f:
            f.write("hello")
        s = self.home([decision("local_files", action="list_directory", path=self.env.work, kind="directories"),
                       decision("done", summary="child is a folder.")])
        s.run("Which folders are in " + self.env.work)
        receipt = next(m["content"] for m in s.messages if m["content"].startswith("[local_files result]"))
        self.assertIn("[directory] " + folder, receipt)
        self.assertNotIn("file.txt", receipt)
        self.env.llm.replies = [decision("local_files", action="list_directory", path=folder),
                                decision("done", summary="It is empty.")]
        s.run("What is inside that folder?")
        self.assertFalse(self.env.results()[-1]["failed"])

    @unittest.skipUnless(os.path.isdir("/Applications"), "macOS Applications directory")
    def test_requested_system_directory_listing_is_read_only(self):
        s = self.home([decision("local_files", action="list_directory", path="/Applications"),
                       decision("done", summary="Listed Applications.")])
        s.run("List /Applications")
        self.assertFalse(self.env.results()[-1]["failed"])

    def test_failed_tool_does_not_authorize_a_pending_or_success_claim(self):
        path = os.path.join(self.env.work, "missing")
        s = self.home([decision("local_files", action="list_directory", path=path),
                       decision("done", summary="The results are still pending. Please wait."),
                       decision("done", summary="Done, listed everything.")])
        answer = s.run("List " + path)["answer"]
        self.assertIn("could not complete", answer)
        self.assertIn("ERROR:", answer)
        self.assertNotIn("pending", answer)
        self.assertEqual(json.loads(s.messages[-1]["content"])["arguments"]["summary"], answer)

    def test_wait_phrase_alone_is_not_treated_as_a_claim_of_running_work(self):
        s = self.home([decision("done", summary="Please wait.")])
        self.assertEqual(s.run('Say "Please wait."')["answer"], "Please wait.")

    def test_runtime_requests_exclude_internal_user_role_feedback(self):
        s = self.home([decision("done", summary="Understood.")])
        self.hub._run_turn("home", "List folders on Desktop", 1)
        s.messages.append({"role":"user", "content":"Your output reached the model token limit before completion."})
        requests = controller.actual_requests(s)
        self.assertEqual(len(requests), 1)
        self.assertIn('List folders on Desktop', requests[0])
        self.assertNotIn('token limit', controller.state(s))

    def test_empty_controller_response_retries_in_json_protocol(self):
        s = self.home(["", decision("done", summary="Hello.")])
        s.run("hi")
        correction = '\n'.join(m['content'] for m in self.env.llm.calls[-1]['messages'][-3:])
        self.assertIn('JSON decision', correction)
        self.assertNotIn('XML', correction)

    def test_results_followup_cannot_invent_pending_work(self):
        s = self.home([decision("done", summary="The results are still pending."),
                       decision("done", summary="I am currently trying to list the contents. Please wait.")])
        answer = s.run("um okay, where are the results?")["answer"]
        self.assertEqual(answer, "No operation is running for this request. Nothing has been started.")

    def test_completed_local_read_is_not_reported_as_pending(self):
        s = self.home([decision("local_files", action="list_directory", path=self.env.work),
                       decision("done", summary="The results are pending. Please wait."),
                       decision("done", summary="I am currently listing the directory.")])
        answer = s.run("List " + self.env.work)["answer"]
        self.assertTrue(answer.startswith("Verified: listed "))
        self.assertNotIn("pending", answer)

    def test_read_only_directory_navigation_keeps_credential_guards(self):
        path = os.path.join(self.env.core.settings.state_dir, "private")
        os.mkdir(path)
        s = self.home([decision("local_files", action="list_directory", path=path),
                       decision("done", summary="This location is protected.")])
        s.run("List " + path)
        self.assertTrue(self.env.results()[-1]["failed"])
        self.assertIn("private state", s.messages[-2]["content"])

    def test_execution_check_can_recover_by_actually_running_the_missing_action(self):
        s = self.home([decision("done", summary="Let me list that now."),
                       decision("local_files", action="list_directory", path=self.env.work),
                       decision("done", summary="The directory is empty.")])
        s.run("List " + self.env.work)
        self.assertEqual(len(self.env.results()), 1)
        self.assertFalse(self.env.results()[-1]["failed"])

    def test_requested_path_can_be_followed_by_sentence_punctuation(self):
        path = os.path.join(self.env.work, 'hello.txt')
        with open(path,'w') as f:
            f.write('hello')
        s = self.home([decision('local_files', action='read_text', path=path),
                       decision('done', summary='hello')])
        s.run('Read ' + path + '. Tell me what it says.')
        self.assertFalse(self.env.results()[-1]['failed'])

    def test_omitted_workdir_uses_the_users_existing_project(self):
        s = self.home([decision('spawn_task', title='Fix parser', task='Fix the parser and run its tests.'),
                       decision('done', summary='The task is underway.')])
        s.run('Fix the CSV parser in ' + self.env.work + '. Preserve its API.')
        row = self.env.core.store.threads(kind='task')[0]
        self.assertEqual(row['workdir'], os.path.realpath(self.env.work))

    def test_omitted_workdir_handles_spaces_and_refuses_ambiguous_projects(self):
        from claire_core.tools.tasks import requested_workdir
        project = os.path.join(self.env.work, 'Project With Spaces')
        os.mkdir(project)
        s = self.home()
        s._active_request = 'Fix the parser in "' + project + '".'
        self.assertEqual(requested_workdir(s), (os.path.realpath(project), None))
        other = os.path.join(self.env.root, 'other')
        os.mkdir(other)
        s._active_request = 'Fix ' + self.env.work + ' and ' + other
        self.assertIn('several existing', requested_workdir(s)[1])

    def test_unsupported_directory_denial_uses_specialist_with_same_guards(self):
        s = self.home([decision('done', summary="I cannot list the root directory for security reasons."),
                       decision('local_files', action='list_directory', path='/'),
                       decision('done', summary='Listed the root directory.')])
        s.run('List the root directory')
        self.assertEqual([c['role'] for c in self.env.llm.calls], ['talk','brain','brain'])
        self.assertEqual(s.role, 'talk')
        self.assertFalse(self.env.results()[-1]['failed'])

    def test_bad_controller_target_recovers_in_original_project(self):
        wrong = 'invalid-relative-target'
        self.home([decision('local_files', action='read_text', path=wrong),
                   decision('done', summary='I could not read that target.')])
        self.hub._run_turn('home', 'Fix the parser in ' + self.env.work + '.', 1)
        tasks = self.env.core.store.threads(kind='task')
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]['workdir'], os.path.realpath(self.env.work))

    def test_natural_reference_to_listed_file_is_readable(self):
        path = os.path.join(self.env.work, 'FABLE5_CAPABILITIES.md')
        with open(path, 'w') as f:
            f.write('A reference document about tool usage.')
        s = self.home([decision('local_files', action='list_directory', path=self.env.work),
                       decision('done', summary='FABLE5_CAPABILITIES.md is there.')])
        s.run('List files in ' + self.env.work)
        self.env.llm.replies = [decision('local_files', action='read_text', path=path),
                                decision('done', summary='A reference document about tool usage.')]
        s.run('read fable5 capabilities file and tell me what it is')
        self.assertFalse(self.env.results()[-1]['failed'])

    def test_read_text_pages_unicode_and_preserves_current_file_context(self):
        path = os.path.join(self.env.work, 'Report.md')
        body = 'α' * 7900 + 'MIDDLE_MARKER' + 'β' * 8500 + 'FINAL_MARKER'
        with open(path, 'w') as f:
            f.write(body)
        s = self.home([decision('local_files', action='read_text', path=path),
                       decision('local_files', action='read_text', path=path, offset='8000'),
                       decision('local_files', action='read_text', path=path, offset='16000'),
                       decision('done', summary='Read the full document.')])
        s.run('Read the full report, including the end.')
        self.assertFalse(any(e['failed'] for e in self.env.results()))
        sent = self.env.llm.calls[-1]['messages']
        pages = [m['content'].partition('\n')[2] for m in sent
                 if m['content'].startswith('[local_files result]')]
        text = ''.join(page.partition('\n')[2] for page in pages)
        self.assertEqual(text, body)
        self.assertEqual(s._local_read_chars, len(body))

    def test_read_text_continues_the_same_file_when_only_the_offset_is_given(self):
        path = os.path.join(self.env.work, 'Report.md')
        body = 'a' * 8000 + 'b' * 500
        with open(path, 'w') as f:
            f.write(body)
        s = self.home([decision('local_files', action='read_text', path=path),
                       decision('local_files', action='read_text', offset='8000'),
                       decision('done', summary='Read it.')])
        s.run('Read the full report.')
        self.assertFalse(any(e['failed'] for e in self.env.results()))
        self.assertEqual(s._local_read_chars, len(body))
        # Without a file read in this turn there is nothing to continue.
        s = self.home([decision('local_files', action='read_text', offset='8000'),
                       decision('done', summary='Which file?')])
        s.run('Carry on reading.')
        self.assertTrue(self.env.results()[-1]['failed'])

    def test_read_discovery_still_refuses_private_state_and_symlinks(self):
        protected = os.path.join(self.env.core.settings.state_dir, 'private.txt')
        with open(protected, 'w') as f:
            f.write('not readable')
        link = os.path.join(self.env.work, 'report.md')
        symlink_or_skip(protected, link)
        s = self.home([decision('local_files', action='read_text', path=link),
                       decision('done', summary='That path is protected.')])
        answer = s.run('Read that report')["answer"]
        self.assertIn('REFUSED:', answer)
        self.assertNotIn('not readable', answer)

    def test_read_budget_delegates_with_known_file_and_correct_parent(self):
        path = os.path.join(self.env.work, 'LongReport.md')
        with open(path,'w') as f:
            f.write('x' * 30000)
        self.home([decision('local_files', action='read_text', path=path, limit='12000'),
                   decision('local_files', action='read_text', path=path, offset='12000', limit='12000'),
                   decision('local_files', action='read_text', path=path, offset='24000'),
                   decision('done', summary='The document needs a larger reading budget.')])
        self.hub._run_turn('home','Read the full long report',1)
        tasks = self.env.core.store.threads(kind='task')
        self.assertEqual(len(tasks),1)
        self.assertEqual(tasks[0]['workdir'], os.path.realpath(self.env.work))
        brief = list(self.hub._queues['task'].queue)[0][1]
        self.assertIn(path, brief)

    def test_local_read_schema_constrains_offsets_and_action_names(self):
        names = ['local_files', 'done']
        self.assertEqual(controller.parse(decision('local_files',action='read_text',path='/tmp/report',offset='let me read the next page'),names), [])
        self.assertEqual(controller.parse(decision('local_files',action='read',path='/tmp/report'),names), [])
        good = decision('local_files',action='read_text',path='/tmp/report',offset='8000')
        self.assertTrue(controller.parse(good,names))
        schema = controller.response_format(names)['json_schema']['schema']['oneOf'][0]
        self.assertIn('pattern', schema['properties']['arguments']['properties']['offset'])

    def test_read_discovery_protects_case_aliases_on_case_insensitive_volumes(self):
        state = self.env.core.settings.state_dir
        path = os.path.join(state, 'secret.txt')
        with open(path,'w') as f:
            f.write('PRIVATE_CONTENT_SENTINEL')
        alias = os.path.join(os.path.dirname(state), os.path.basename(state).upper(), 'secret.txt')
        if not os.path.isfile(alias):
            self.skipTest('case-sensitive filesystem')
        s = self.home([decision('local_files',action='read_text',path=alias),
                       decision('done',summary='That location is protected.')])
        answer = s.run('Read that file')["answer"]
        self.assertIn('REFUSED:', answer)
        self.assertNotIn('PRIVATE_CONTENT_SENTINEL', answer)

    def test_existing_file_is_never_overwritten(self):
        path = os.path.join(self.env.work, "keep.txt")
        with open(path, "w") as f:
            f.write("original")
        s = self.home([decision("local_files", action="write_text", path=path, content="replacement"),
                       decision("done", summary="The file already exists; preserved it.")])
        s.run("Create " + path + " containing replacement")
        with open(path) as f:
            self.assertEqual(f.read(), "original")

    def test_simple_file_tool_cannot_write_trusted_skills(self):
        path = os.path.join(self.env.core.settings.vault, "skills", "new-skill.md")
        s = self.home([decision("local_files", action="write_text", path=path, content="new instructions"),
                       decision("done", summary="Use the skill editor for that.")])
        s.run("Create " + path + " containing new instructions")
        self.assertFalse(os.path.exists(path))

    def test_protected_paths_and_symlinks_remain_protected(self):
        target = os.path.join(self.env.core.settings.state_dir, "protected")
        link = os.path.join(self.env.work, "link")
        symlink_or_skip(self.env.core.settings.state_dir, link)
        path = os.path.join(link, "protected")
        s = self.home([decision("local_files", action="create_directory", path=path),
                       decision("done", summary="That location is protected.")])
        s.run("Create " + path)
        self.assertFalse(os.path.exists(target))

    def test_listing_a_safe_directory_with_private_state_siblings(self):
        s = self.home([decision("local_files", action="list_directory", path=self.env.work),
                       decision("done", summary="The directory is empty.")])
        s.run("List " + self.env.work)
        self.assertFalse(any(e.get("failed") for e in self.env.results()))

    def test_followup_uses_recent_verified_location(self):
        path = os.path.join(self.env.work, "notes")
        s = self.home([decision("local_files", action="create_directory", path=path),
                       decision("done", summary="Created it.")])
        s.run("Create " + path)
        child = os.path.join(path, "hello.txt")
        self.env.llm.replies = [decision("local_files", action="write_text", path=child, content="hello"),
                                decision("done", summary="Created hello.txt in that folder.")]
        s.run("Now create hello.txt in that folder, containing hello")
        with open(child) as f:
            self.assertEqual(f.read(), "hello")

    def test_request_window_is_bounded_and_full_history_is_retained(self):
        s = self.home([decision("done", summary="Understood.")])
        old = [{"role": "user", "content": f"old-{i} " + "x" * 2500} for i in range(60)]
        s.messages.extend(old)
        s.run("Remember the new request exactly.")
        sent = self.env.llm.calls[-1]["messages"]
        self.assertLess(sum(len(str(m["content"])) for m in sent), 20000)
        self.assertTrue(any(m["content"] == "Remember the new request exactly." for m in sent))
        retained = self.env.core.store.load_messages("home")
        self.assertTrue(any(m["content"] == old[0]["content"] for m in retained))
        self.assertFalse(any(e["type"] == "compacted" for e in self.env.events))

    def test_request_window_start_moves_in_steps_so_the_prompt_prefix_is_reused(self):
        s = self.home()
        views = []
        for turn in range(30):
            s.messages.append({"role": "user", "content": f"question {turn}"})
            s.messages.append({"role": "assistant", "content": f"answer {turn}"})
            view = controller.messages(s)[1:-1]            # without system and runtime facts
            # The newest twelve messages are always there, as before.
            self.assertEqual([m["content"] for m in view[-12:]],
                             [m["content"] for m in s.messages[1:][-12:]])
            views.append(view)
        # A start that moved every turn would never reuse the previous view.
        reused = sum(after[:len(before)] == before for before, after in zip(views, views[1:]))
        self.assertGreaterEqual(reused, 24)
        self.assertLessEqual(max(len(v) for v in views), 12 + controller.WINDOW_STEP)

    def test_request_window_step_never_adds_more_than_the_budget_again(self):
        s = self.home()
        s.messages.extend({"role": "user", "content": f"old-{i} " + "x" * 2500} for i in range(60))
        history = controller.messages(s)[1:-1]
        self.assertLessEqual(sum(len(m["content"]) for m in history),
                             2 * s.settings.controller_history_chars)

    def test_conversation_view_is_primed_without_its_runtime_facts(self):
        primed = []
        self.env.llm.prime = lambda messages, role="brain", cancel=None: primed.append(messages) or 0.0
        s = self.home([decision("done", summary="Hello.")])
        s.run("hi")
        sent = self.env.llm.calls[-1]["messages"]
        self.assertEqual(primed, [sent[:-1]])
        self.assertIn("Runtime facts", sent[-1]["content"])
        self.assertFalse(any("Runtime facts" in str(m["content"]) for m in primed[0]))

    def test_preload_warms_the_conversation_system_prompt_with_two_branches(self):
        primed = []
        self.env.llm.prime = lambda messages, role="brain", cancel=None: primed.append((role, messages)) or 0.0
        self.hub.store.thread("home") or self.env.core.store.create_thread("home", "Home", self.env.work)
        with patch("claire_core.hub.boot.ensure_brain", return_value=None):
            self.hub._preload()
        self.assertEqual(len(primed), 2)
        system = self.hub._session("home").system_prompt()
        self.assertTrue(all(m[0] == {"role": "system", "content": system} for _, m in primed))
        self.assertNotEqual(primed[0][1][1]["content"], primed[1][1][1]["content"])
        self.assertEqual({role for role, _ in primed}, {self.hub._session("home").role})

    def test_worker_gets_only_work_and_procedures(self):
        tid = self.env.core.store.create_thread("task", "Fix it", self.env.work, skills=["coding"])
        with patch.object(self.env.core.vault, "identity", side_effect=AssertionError("worker persona")), \
             patch.object(self.env.core.vault, "load_memory", side_effect=AssertionError("global memory")):
            worker = self.hub._session(tid)
        self.assertEqual(worker.role, "brain")
        self.assertEqual(worker.profile, "worker")
        self.assertIn("# Skill: coding", worker.system_prompt())
        self.assertNotIn("This is your home thread", worker.system_prompt())
        self.assertNotIn("spawn_task", worker.tools)

    def test_worker_errors_do_not_toggle_reasoning_and_invalidate_prompt_cache(self):
        from tests.test_engine import call
        from tests.helpers import done
        for thinking in (False, True):
            self.env.llm.replies = [call("read_file", path="missing.txt"), done("Missing file.")]
            self.env.llm.calls = []
            s = self.env.session(profile="worker", thinking=thinking)
            s.run("Find this file.")
            self.assertEqual([c["thinking"] for c in self.env.llm.calls], [thinking, thinking])

    def test_large_worker_reads_are_windowed_and_explicit_ranges_still_work(self):
        path = os.path.join(self.env.work, "large.py")
        with open(path, "w") as f:
            f.write("\n".join(f"line_{i}" for i in range(1, 301)))
        s = self.env.session(profile="worker")
        text, failed, _ = s._execute("read_file", {"path": "large.py"})
        self.assertFalse(failed)
        self.assertIn("lines 1-120 of 300", text)
        self.assertNotIn("line_121", text)
        text, failed, _ = s._execute("read_file", {"path": "large.py", "start": "121", "end": "300"})
        self.assertFalse(failed)
        self.assertIn("line_300", text)

    def test_technical_failure_escalates_once_with_original_request(self):
        self.env.llm.replies = [ModelError("talk failed")]
        with patch.object(self.hub, "resolve_workdir", return_value=(self.env.work, None)):
            self.hub._run_turn("home", "Find the cause of the parser crash", 1)
        tasks = self.env.core.store.threads(kind="task")
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["parent"], "home")
        self.assertIn("Find the cause of the parser crash", self.env.core.vault.work.context())

    def test_a_failed_attempt_that_is_handed_on_shows_only_the_handoff(self):
        bad = decision("local_files", action="list_directory", kind="all")      # no path: fails
        self.env.llm.replies = [bad, bad]
        with patch.object(self.hub, "resolve_workdir", return_value=(self.env.work, None)), \
             patch.object(self.hub, "post"):
            self.hub._run_turn("home", "fix the parser in my project", 1)
        answers = [e["text"] for e in self.env.events if e["type"] == "answer"]
        self.assertEqual(len(answers), 1, answers)
        self.assertIn("handed this to the coding worker", answers[0])
        self.assertEqual(len(self.env.core.store.threads(kind="task")), 1)

    def test_a_failed_attempt_that_is_not_handed_on_still_says_what_went_wrong(self):
        bad = decision("local_files", action="list_directory", kind="all")
        self.env.llm.replies = [bad, bad]
        with patch.object(self.hub, "resolve_workdir", return_value=(None, "no folder")):
            self.hub._run_turn("home", "list that folder", 1)
        answers = [e["text"] for e in self.env.events if e["type"] == "answer"]
        self.assertEqual(len(answers), 1, answers)
        self.assertIn("could not complete", answers[0])

    def test_refusal_is_not_escalated_to_bypass_it(self):
        s = self.home()
        start = len(s.messages)
        s.messages.append({"role": "user", "content": "[local_files result]\nREFUSED: protected path"})
        outcome = self.hub._escalate_controller(s, "change it", {"status": "stopped", "answer": ""}, start)
        self.assertEqual(outcome["status"], "done")
        self.assertEqual(self.env.core.store.threads(kind="task"), [])
        self.assertTrue(any(e["type"] == "answer" and "protected path" in e["text"] for e in self.env.events))

    def test_successful_dispatch_is_not_duplicated_after_model_failure(self):
        self.env.llm.replies = [decision("spawn_task", title="Fix parser", task="Fix parser with a regression test.",
                                         workdir=self.env.work, skills="coding"), ModelError("lost controller")]
        self.hub._run_turn("home", "fix the parser", 1)
        self.assertEqual(len(self.env.core.store.threads(kind="task")), 1)

    def test_invalid_json_cannot_be_accepted_as_a_claimed_success(self):
        s = self.home(["Created it."] * 3)
        self.assertEqual(s.run("do it")["status"], "stopped")

    def test_missing_talk_server_falls_back_to_compact_brain_request(self):
        self.hub._start_brain = True
        self.env.llm.replies = [decision("done", summary="Hello.")]
        with patch("claire_core.hub.boot.ensure_role", return_value="talk unavailable"), \
             patch("claire_core.hub.boot.ensure_brain", return_value=None):
            self.hub._run_turn("home", "hello", 1)
        self.assertEqual(self.env.llm.calls[0]["role"], "brain")
        self.assertEqual(self.hub._session("home").profile, "controller")

    def test_explicit_deep_mode_uses_brain_then_switches_back_to_talk(self):
        s = self.home([decision("done", summary="Thoughtful answer.")])
        s.thinking = True
        self.hub._run_turn("home", "Consider this carefully.", 1)
        self.assertEqual(self.env.llm.calls[-1]["role"], "brain")
        self.assertTrue(self.env.llm.calls[-1]["thinking"])
        s.thinking = False
        self.env.llm.replies = [decision("done", summary="Hello.")]
        self.hub._run_turn("home", "hi", 2)
        self.assertEqual(self.env.llm.calls[-1]["role"], "talk")
        self.assertFalse(self.env.llm.calls[-1]["thinking"])

    def test_reset_clears_old_request_and_local_location_context(self):
        s = self.home([decision("done", summary="Understood.")])
        s.run("A request to clear later")
        s._local_locations = [self.env.work]
        s.reset()
        sent = controller.messages(s)
        self.assertNotIn("A request to clear later", str(sent))
        self.assertEqual(s._local_locations, [])


class DecisionTests(unittest.TestCase):
    def test_unavailable_tools_and_malformed_arguments_are_rejected(self):
        for payload in (decision("run_command", command="echo x"), decision("done", unknown="x"),
                        '{"tool":"done","arguments":{"summary":42}}', "not JSON"):
            self.assertEqual(controller.parse(payload, ["done"]), [])
        self.assertEqual(controller.parse(decision("done", summary="Hello"), ["done"]),
                         [("done", {"summary": "Hello"})])


if __name__ == "__main__":
    unittest.main()


class BrowserCapabilityTests(unittest.TestCase):
    def setUp(self):
        ControllerTests.setUp(self)
        # Native I/O is mocked below; exercise Mac contracts on every host.
        from types import SimpleNamespace
        platform = patch('claire_core.connectors.mac.sys', SimpleNamespace(platform='darwin'))
        platform.start()
        self.addCleanup(platform.stop)
    home = ControllerTests.home
    def connect_mac(self):
        conn=self.env.core.connectors
        iid=conn.create('mac','Mac apps',{'apps':'TextEdit, Google Chrome'})['id']
        conn.store.update_integration(iid,status='connected',account_label='2 apps')
        return iid

    def test_controller_knows_the_actual_browser_and_its_allowed_app(self):
        iid=self.connect_mac()
        s=self.home()
        self.assertIn('use_connector',s.tools)
        self.assertIn(iid,s.messages[0]['content'])
        self.assertIn("Sir's own Google Chrome",s.messages[0]['content'])
        self.assertIn('text-only model',s.messages[0]['content'])
        state=controller.state(s)
        self.assertIn('"provider": "mac"',state)
        self.assertIn('Google Chrome',state)

    def test_false_denial_is_challenged_then_the_original_tab_request_runs(self):
        from claire_core.connectors.mac import Mac
        iid=self.connect_mac()
        args=lambda action,**payload:decision('use_connector',connector=iid,action=action,input=json.dumps(payload))
        s=self.home([decision('done',summary="I can't open or control your browser tabs directly."),
                     args('help'),args('key',app='Google Chrome',key='t',modifiers=['cmd']),
                     args('type',app='Google Chrome',text='https://google.com\n'),
                     args('key',app='Google Chrome',key='w',modifiers=['cmd']),
                     decision('done',summary='Opened Google in a new tab and closed that tab.')])
        s.approver=self.env.approver
        sent=[]
        def run(**request):
            sent.append(request)
            if request['op']=='snapshot':return {'title':'Google Chrome','full':False,'nodes':[]}
            return {'ok':True}
        with patch.object(Mac,'claim'),patch.object(Mac,'_run',side_effect=run):
            result=s.run('Open a new tab in this current browser, enter google.com and close that tab.')
        self.assertEqual(result['status'],'done')
        self.assertNotIn("can't",result['answer'])
        actions=[r for r in sent if r['op'] in ('key','type')]
        self.assertEqual([r['op'] for r in actions],['key','type','key'])
        self.assertEqual(actions[0]['key'],'t')
        self.assertEqual(actions[1]['text'],'https://google.com\n')
        self.assertEqual(actions[2]['key'],'w')
        self.assertEqual(len(self.env.asked),1)  # existing close-tab approval retained
        self.assertEqual(len(self.env.core.store.threads(kind='task')),0)

    def test_actual_access_errors_are_not_replaced_with_a_claim_of_success(self):
        iid=self.connect_mac()
        s=self.home()
        s._active_request='Open a tab in my current browser.'
        self.assertFalse(controller.unverified_browser_denial(s,"I can't control browser tabs.",
            [('use_connector',{'connector':iid,'action':'snapshot'},'ERROR: accessibility permission missing',True)]))

    def test_no_browser_and_wrong_browser_are_real_capability_limits(self):
        s=self.home()
        s._active_request='Open a tab in this current browser.'
        self.assertFalse(controller.unverified_browser_denial(s,"I can't control your browser tabs.",[]))
        self.connect_mac()
        s._active_request='Open a tab in Safari.'
        self.assertFalse(controller.unverified_browser_denial(s,"I can't access Safari.",[]))

    def test_dedicated_browser_does_not_grant_access_to_current_user_tabs(self):
        s=self.home()
        s._active_request='Open a tab in my current browser.'
        with patch.object(self.env.core.connectors,'connected',return_value=[{'provider':'browser','config':{}}]):
            self.assertFalse(controller.unverified_browser_denial(s,"I can't control your current browser tabs.",[]))
            s._active_request='Open a browser tab to Google.'
            self.assertTrue(controller.unverified_browser_denial(s,"I can't open browser tabs.",[]))

    def test_missing_screenshot_vision_does_not_mean_missing_browser_controls(self):
        self.connect_mac()
        s=self.home()
        s._active_request='Look at the picture in this browser.'
        self.assertFalse(controller.unverified_browser_denial(s,"I can't see images in this text model.",[]))

    def test_browser_help_preserves_tab_and_key_parameters_in_controller_budget(self):
        iid=self.connect_mac()
        text=self.env.core.connectors.help(iid,compact=True)
        self.assertLess(len(text),5000)
        for term in ('key(app, key*, modifiers)','type(app, text*)','tabs(app)','use_tab(app, tab*)','closes the current tab','asks approval'):
            self.assertIn(term,text)
        self.assertNotIn('<action>',text)
        self.assertIn('<action>',self.env.core.connectors.help(iid))

    def test_current_browser_route_names_the_mac_connector_and_first_help_call(self):
        iid=self.connect_mac()
        s=self.home()
        s._active_request='Open a new tab in this current browser and close it.'
        other={'id':'other-browser','provider':'browser','config':{}}
        rows=self.env.core.connectors.connected()+[other]
        with patch.object(self.env.core.connectors,'connected',return_value=rows):
            route=controller.browser_route(s)
        self.assertEqual((route['connector'],route['app']),(iid,'Google Chrome'))
        self.assertEqual(route['tool'],'computer')
        self.assertNotIn('first_decision',route)
        s._connector_help={iid}
        self.assertNotIn('first_decision',controller.browser_route(s))

    def test_unspecified_browser_work_prefers_the_separate_profile(self):
        self.connect_mac()
        s=self.home()
        s._active_request='Open a browser tab to a website.'
        rows=self.env.core.connectors.connected()+[{'id':'other-browser','provider':'browser','config':{}}]
        with patch.object(self.env.core.connectors,'connected',return_value=rows):
            route=controller.browser_route(s)
        self.assertEqual(route['connector'],'other-browser')
        self.assertEqual(route['scope'],'separate browser')

    def test_named_unconnected_browser_is_not_mapped_to_another_app(self):
        self.connect_mac()
        s=self.home()
        s._active_request='Open a tab in Safari.'
        self.assertFalse(controller.browser_route(s)['available'])

    def test_greetings_do_not_gain_browser_routing_instructions(self):
        self.connect_mac()
        s=self.home()
        s._active_request='Hi Claire.'
        self.assertIsNone(controller.browser_route(s))

    def test_failed_configured_mac_connection_is_a_recheck_target(self):
        iid=self.connect_mac()
        self.env.core.store.update_integration(iid,status='error',error='Previous timeout')
        s=self.home()
        s._active_request='Open a tab in this current browser, try again.'
        route=controller.browser_route(s)
        self.assertTrue(route['configured'])
        self.assertFalse(route['available'])
        self.assertEqual(route['connector'],iid)
        self.assertEqual(route['first_decision']['arguments'],{'action':'inspect','app':'Google Chrome'})
        self.assertTrue(controller.unverified_browser_check(s,"I checked the browser connection, but it is not currently available.",[]))

    def test_claimed_check_requires_a_tool_receipt(self):
        iid=self.connect_mac()
        s=self.home()
        s._active_request='Open google.com in my current browser.'
        summary="I checked the browser connection. You'll need to open it yourself."
        self.assertTrue(controller.unverified_browser_check(s,summary,[]))
        actual=[('use_connector',{'connector':iid,'action':'help'},'ERROR: macOS permission required',True)]
        self.assertFalse(controller.unverified_browser_check(s,summary,actual))

    def test_retry_rechecks_the_connection_and_reports_the_specific_permission(self):
        from claire_core.connectors import ConnectorError
        from claire_core.connectors.mac import Mac, PERMISSION
        iid=self.connect_mac()
        self.env.core.store.update_integration(iid,status='error',error='Previous timeout')
        s=self.home([decision('done',summary="I checked the browser connection, but it is not currently available for control."),
                     decision('use_connector',connector=iid,action='help'),
                     decision('done',summary='macOS control permission needs to be enabled.')])
        with patch.object(Mac,'test',side_effect=ConnectorError(PERMISSION)) as test:
            result=s.run('Open a tab in this current browser and go to google.com. Try again.')
        test.assert_called_once()
        self.assertIn('macOS has not allowed Claire',result['answer'])
        self.assertIn('Device Control and Data Access',result['answer'])
        self.assertTrue(self.env.results()[-1]['failed'])
        self.assertEqual(len(self.env.core.store.threads(kind='task')),0)

    def test_computer_completion_cannot_be_invented_without_receipts(self):
        self.connect_mac();s=self.home();s._active_request='Open a new tab in my current browser and close it.'
        self.assertIn('No verified computer action',controller.completion_issue(s,'Opened the tab and closed it.',[]))
        receipt=json.dumps({'ok':True,'performed':True,'verified':True})
        first=[('computer',{'action':'new_tab'},receipt,False)]
        self.assertIn('No verified close action',controller.completion_issue(s,'Opened and closed the new tab.',first))
        first.append(('computer',{'action':'close_tab'},receipt,False))
        self.assertIsNone(controller.completion_issue(s,'Opened and closed the new tab.',first))

    def test_computer_schema_restricts_actions_and_vision_quality(self):
        schema=controller.response_format(['computer'])['json_schema']['schema']['oneOf'][0]
        branches={x['properties']['action']['const']:x for x in schema['properties']['arguments']['oneOf']}
        self.assertIn('close_tab',branches)
        self.assertNotIn('run_shell',branches)
        self.assertEqual(branches['look']['properties']['quality']['enum'],['fast','deep'])
        self.assertTrue({'observation','x','y'}.issubset(branches['click_at']['required']))
        self.assertNotIn('target',branches['new_tab']['properties'])
        self.assertIn('pattern',branches['click_at']['properties']['x'])

    def test_filling_a_field_cannot_support_a_claim_that_save_was_clicked(self):
        self.connect_mac();s=self.home();s._active_request='Set the name and click Save in my current browser.'
        receipt=json.dumps({'ok':True,'performed':True,'verified':True})
        outcomes=[('computer',{'action':'set_text'},receipt,False)]
        self.assertIn('No verified clicked action',controller.completion_issue(s,"I've filled the field and clicked Save.",outcomes))
        outcomes.append(('computer',{'action':'click'},receipt,False))
        self.assertIsNone(controller.completion_issue(s,"I've filled the field and clicked Save.",outcomes))


class HandoffTitleTests(unittest.TestCase):
    def test_a_handed_on_request_is_titled_by_its_first_sentence(self):
        from claire_core.hub import task_title
        self.assertEqual(task_title("A customer says invoice INV-1002 is wrong. We billed them 1,095.64."),
                         "A customer says invoice INV-1002 is wrong")
        long = task_title("Read that report and tell me what the quarterly numbers mean for our hiring plan")
        self.assertTrue(long.endswith("…") and len(long) <= 61 and not long.startswith("Finish"))
