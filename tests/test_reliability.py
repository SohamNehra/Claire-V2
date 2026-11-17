"""Regressions at the skill, context and integration boundaries."""

import importlib.util
import io
import json
import os
import unittest
from unittest.mock import patch

from claire_core import skillroute
from claire_core.connectors import builder
from claire_core.llm import ChatResult, LLM, ModelError
from tests.helpers import Env, done
from tests import test_integrations as integrations
from tests.test_skillroute import skill


class SkillReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)

    def test_upload_and_body_edit_preserve_procedure_metadata(self):
        store = self.env.core.skills
        uploaded = store.save_upload("procedure.md", "---\nname: procedure\n"
                                    "description: Build reports.\nwhen: a report is needed\n"
                                    "requires: coding, testing\nkinds: home, agent\nsteps: 65\n"
                                    "always: true\n---\n# Write reports\n")
        for item in (uploaded, store.save("procedure", "Reports.", "New body.")):
            self.assertEqual(item["requires"], ["coding", "testing"])
            self.assertEqual(item["kinds"], ["home", "agent"])
            self.assertEqual(item["steps"], 65)
            self.assertEqual(item["when"], "a report is needed")
            self.assertTrue(item["always"])

    def test_windows_uploads_keep_their_frontmatter(self):
        item = self.env.core.skills.save_upload("procedure.md", "---\r\nname: procedure\r\n"
                                                "description: Test reports.\r\nsteps: 60\r\n"
                                                "requires: coding\r\n---\r\n# Reports\r\n")
        self.assertEqual(item["steps"], 60)
        self.assertEqual(item["requires"], ["coding"])

    def test_dependencies_load_at_construction_and_cycles_do_not_recurse(self):
        skill(self.env, "alpha", "Alpha procedure.", requires="bravo")
        skill(self.env, "bravo", "Bravo procedure.", requires="alpha")
        session = self.env.session(skills=["alpha"])
        self.assertEqual(set(session.skills), {"alpha", "bravo"})
        session = self.env.session()
        session.load_skill("alpha")
        self.assertEqual(set(session.skills), {"alpha", "bravo"})
        self.assertEqual(len(session.skills), 2)

    def test_dependency_chain_is_not_silently_cut_off(self):
        names = ["chain-" + str(i) for i in range(7)]
        for i, name in enumerate(names):
            skill(self.env, name, "A dependent procedure.",
                  requires=names[i+1] if i+1 < len(names) else "")
        self.assertEqual(skillroute.with_requirements(self.env.core.skills, names[:1]), names[::-1])

    def test_router_outage_leaves_discoverable_procedures(self):
        self.env.llm.replies = [ModelError("router is down")]
        self.assertEqual(skillroute.choose(self.env.core, "acceptance-test the feature"), [])
        skill(self.env, "acceptance", "Verify the user journey and record observed outcomes.")
        self.assertIn("acceptance: Verify the user journey", self.env.session().system_prompt())


class ContextReliabilityTests(unittest.TestCase):
    def test_compaction_preserves_exact_request_even_when_summary_fails(self):
        for reply in ("Earlier work was summarized without the goal.", ModelError("clerk failed")):
            env = Env([reply])
            self.addCleanup(env.close)
            session = env.session()
            request = "Build a report. Preserve Unicode: café. Do not send any emails."
            session._active_request = request
            session.messages += [{"role": "user", "content": request}]
            session.messages += [{"role": "user", "content": "recent evidence " * 50}] * 12
            before = len(str(session.messages))
            self.assertTrue(session.compact())
            self.assertLess(len(str(session.messages)), before)
            self.assertTrue(any(request in m["content"] for m in session.messages))


class ModelMetadataTests(unittest.TestCase):
    def test_qwen38_fast_mode_disables_reasoning_but_other_models_keep_their_mapping(self):
        for model, thinking, role, expected in (("qwen/qwen3.8-27b", False, "brain", "none"),
                                                ("qwen/qwen3.8-27b", True, "brain", "medium"),
                                                ("qwen/qwen3.8-27b", False, "clerk", "none"),
                                                ("other-model", False, "brain", "low")):
            env = Env(model=model, brain_server="lmstudio")
            self.addCleanup(env.close)
            captured = []
            def request(req, timeout):
                captured.append(json.loads(req.data))
                return io.BytesIO(b'data: [DONE]\n\n')
            with patch("claire_core.llm.urllib.request.urlopen", request):
                LLM(env.core.settings).chat([{"role": "user", "content": "go"}], thinking=thinking, role=role)
            self.assertEqual(captured[0]["reasoning_effort"], expected)

    def test_explicit_lmstudio_talk_role_switches_hybrid_qwen_reasoning_off(self):
        for model in ("qwen/qwen3.6-35b-a3b", "qwen_qwen3.5-4b", "qwen3.8-27b-uncensored"):
            env = Env(model="brain", brain_server="lmstudio")
            self.addCleanup(env.close)
            settings = env.core.settings
            settings.roles['talk'] = {'api_base': settings.api_base, 'model': model}
            captured = []
            def request(req, timeout):
                captured.append(json.loads(req.data))
                return io.BytesIO(b'data: [DONE]\n\n')
            with patch("claire_core.llm.urllib.request.urlopen", request):
                LLM(settings).chat([{"role":"user", "content":"hi"}], role="talk", thinking=False)
            self.assertEqual(captured[0]['reasoning_effort'], 'none')
            settings.roles['talk']['api_base'] = 'http://other-server:8081/v1'
            captured.clear()
            with patch("claire_core.llm.urllib.request.urlopen", request):
                LLM(settings).chat([{"role":"user", "content":"hi"}], role="talk", thinking=False)
            self.assertNotIn('reasoning_effort', captured[0])

    def test_stream_preserves_the_token_limit_finish_reason(self):
        env = Env(model="fixture-model")
        self.addCleanup(env.close)
        stream = io.BytesIO(b'data: {"choices":[{"delta":{"content":"partial"},"finish_reason":null}]}\n\n'
                            b'data: {"choices":[{"delta":{},"finish_reason":"length"}]}\n\n'
                            b'data: [DONE]\n\n')
        with patch("claire_core.llm.urllib.request.urlopen", return_value=stream):
            result = LLM(env.core.settings).chat([{"role": "user", "content": "go"}])
        self.assertEqual(result.finish_reason, "length")
        self.assertEqual(result.content, "partial")

    def test_truncated_file_writes_are_not_executed(self):
        env = Env()
        self.addCleanup(env.close)
        session = env.session()
        replies = [ChatResult('<write_file><path>partial.py</path><content>print(', '', 1, 10,
                              finish_reason="length"), ChatResult(done("Blocked; no file written."), '', 1, 10)]
        with patch.object(env.llm, "chat", side_effect=replies):
            self.assertEqual(session.run("Write a valid program.")["status"], "done")
        self.assertFalse(os.path.exists(os.path.join(env.work, "partial.py")))

    def test_lmstudio_uses_native_metadata_without_querying_llama_props(self):
        env = Env(brain_server="lmstudio", model="fixture-model", ctx_limit=0)
        self.addCleanup(env.close)
        calls = []
        def get(url, timeout=5):
            calls.append(url)
            if url.endswith("/v1/models") and not url.endswith("/api/v1/models"):
                return {"data": [{"id": "fixture-model"}]}
            if url.endswith("/api/v1/models"):
                return {"models": [{"key": "fixture-model", "loaded_instances": [
                    {"config": {"context_length": 32768, "parallel": 2}}]}]}
            self.fail("unsupported endpoint: " + url)
        with patch("claire_core.llm._get_json", get):
            result = LLM(env.core.settings).describe()
        self.assertEqual(result["ctx"], 32768)
        self.assertEqual(result["slots"], 2)
        self.assertEqual(len(calls), 2)


class KitReliabilityTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("kit_under_test", builder.KIT)
        self.kit = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.kit)
        self.kit._MANIFEST = {"hosts": ["api.example.com"], "credentials": []}

    def test_allowed_http_errors_and_empty_bodies_match_real_transport(self):
        kit = self.kit
        with kit.testing.fixtures({"POST https://api.example.com/items": kit.testing.Reply(404, {})}) as sent:
            reply = kit.http("POST", "https://api.example.com/items", json_body={}, allow=(404,))
            self.assertEqual(reply.status, 404)
            self.assertEqual(sent[0].body, {})
            with self.assertRaises(kit.Fail):
                kit.http("POST", "https://api.example.com/items", json_body={})

    def test_nested_fixtures_restore_outer_offline_transport(self):
        kit = self.kit
        url = "https://api.example.com/items"
        with kit.testing.fixtures({"GET " + url: {"source": "outer"}}) as outer:
            with kit.testing.fixtures({"GET " + url: {"source": "inner"}}):
                self.assertEqual(kit.http("GET", url).json()["source"], "inner")
            self.assertIsNotNone(kit._FIXTURES)  # must not attempt a live network request
            self.assertEqual(kit.http("GET", url).json()["source"], "outer")
            self.assertEqual(len(outer), 1)


class IntegrationReliabilityTests(unittest.TestCase):
    setUp = integrations.BuildTests.setUp
    write = integrations.BuildTests.write

    def test_an_empty_test_cannot_pass_an_integration(self):
        self.write(test='print("all tests passed")\n')
        with self.assertRaisesRegex(builder.BuildError, "never successfully called"):
            builder.check(self.core, self.folder)

    def test_manifest_parameters_must_match_python_function(self):
        self.write(manifest=dict(integrations.MANIFEST, actions=[dict(integrations.MANIFEST["actions"][0],
                                                       params={"query": "search text"}),
                                                   integrations.MANIFEST["actions"][1]]))
        with self.assertRaisesRegex(builder.BuildError, "does not load"):
            builder.check(self.core, self.folder)

    def test_credential_checks_must_be_valid_and_present(self):
        for check in (None, {"action": "list_items", "args": {"unknown": "x"}}):
            with self.assertRaises(builder.BuildError):
                builder.validate(dict(integrations.MANIFEST, check=check))

    def test_submit_exposes_credential_free_connector_in_same_turn(self):
        manifest = dict(integrations.MANIFEST, credentials=[])
        self.write(manifest=manifest,
                   server=integrations.SERVER.format(port=self.port).replace('secret("NOTES_KEY")', '"k"'))
        session = self.env.session()
        self.assertNotIn("use_connector", session.tools)
        view = {"id": "notes-api", "status": "connected", "credentials": []}
        with patch.object(self.core.connectors, "upsert_built", return_value=view), \
             patch.object(self.core, "capabilities", return_value={"sandbox", "connectors"}), \
             patch.object(self.core.connectors, "prompt_section", return_value="Notes API is connected"):
            result, failed, fault = session._execute("build_integration", {"action": "submit", "name": "notes_api"})
        self.assertIsNone(fault, result)
        self.assertFalse(failed, result)
        self.assertIn("Installed and connected", result)
        self.assertIn("use_connector", session.tools)
        self.assertIn("Notes API is connected", session.messages[0]["content"])

    def test_auto_loading_skill_does_not_mask_build_errors(self):
        session = self.env.session()
        result, failed, fault = session._execute("build_integration", {"action": "check", "name": "missing"})
        self.assertIsNone(fault)
        self.assertTrue(failed, result)
        self.assertTrue(result.startswith("ERROR:"), result)


if __name__ == "__main__":
    unittest.main()
