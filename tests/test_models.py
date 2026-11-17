"""Adding models from the app or `claire2 models`, and choosing which one
Claire runs. Everything here uses temporary folders and tiny fake models."""

import os
import struct
import unittest
from unittest.mock import patch

from claire_core import boot, models
from claire_core.client import Client
from claire_core.settings import Settings
from tests.helpers import Env, managed_server_spec
from tests.test_service import ServiceCase


def fake_gguf(path, arch="llama", name="Tiny Model", context=8192):
    def s(text):
        raw = text.encode()
        return struct.pack("<Q", len(raw)) + raw
    pairs = [("general.architecture", 8, s(arch)), ("general.name", 8, s(name)),
             ("tokenizer.ggml.tokens", 9, struct.pack("<IQ", 8, 3) + s("a") + s("b") + s("c")),
             ("tokenizer.ggml.scores", 9, struct.pack("<IQ", 6, 3) + struct.pack("<3f", 0, 0, 0)),
             (f"{arch}.context_length", 4, struct.pack("<I", context))]
    with open(path, "wb") as f:
        f.write(b"GGUF" + struct.pack("<IQQ", 3, 0, len(pairs)))
        for key, kind, payload in pairs:
            f.write(s(key) + struct.pack("<I", kind) + payload)
        f.write(b"\0" * 64)
    return os.path.realpath(path)


class ModelRegistryTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)
        self.settings = self.env.core.settings
        self.model = fake_gguf(os.path.join(self.env.root, "tiny.gguf"))

    def chosen(self, **overrides):
        """Settings as a fresh process would read them, with the app's choice on."""
        with patch.dict(os.environ, {"CLAIRE_MODELS": "on"}):
            for key in ("CLAIRE_WORKER_PROFILE", "CLAIRE_API", "CLAIRE_MODEL"):
                os.environ.pop(key, None)
            return Settings(state_dir=self.settings.state_dir, **overrides)

    def test_a_gguf_file_describes_itself(self):
        info = models.gguf_info(self.model)
        self.assertEqual((info["architecture"], info["name"], info["context_length"]), ("llama", "Tiny Model", 8192))
        not_model = os.path.join(self.env.root, "notes.txt")
        open(not_model, "w").write("hello")
        with self.assertRaises(models.ModelError):
            models.gguf_info(not_model)

    def test_added_model_gets_a_port_a_sensible_context_and_is_chosen_for_the_next_start(self):
        made = models.add(self.settings, "llama", file_path=self.model)
        self.assertEqual((made["name"], made["context"], made["port"]), ("tiny-model", 8192, models.FIRST_PORT))
        second = models.add(self.settings, "llama", file_path=fake_gguf(os.path.join(self.env.root, "b.gguf"), name="Tiny Model"),
                            context=4096)
        self.assertEqual((second["name"], second["port"]), ("tiny-model-2", models.FIRST_PORT + 1))
        models.choose(self.settings, "tiny-model")
        s = self.chosen()
        self.assertEqual((s.brain_server, s.api_base, s.ctx_limit, s.model_choice),
                         ("llama", f"http://127.0.0.1:{models.FIRST_PORT}/v1", 8192, "tiny-model"))
        spec = managed_server_spec(s, "brain")
        self.assertEqual((spec["gguf"], spec["port"], spec["context"]), (self.model, models.FIRST_PORT, 8192))
        command = boot.server_command(spec, s.config)
        self.assertEqual(command[command.index("-m") + 1], self.model)
        self.assertEqual(models.current(s), "tiny-model")
        self.assertTrue([m for m in models.catalogue(s)["models"] if m["name"] == "tiny-model"][0]["selected"])

    def test_lm_studio_models_and_configured_profiles_can_be_chosen(self):
        models.add(self.settings, "lmstudio", model="qwen/qwen3-8b", context=16384)
        models.choose(self.settings, "qwen3-8b")
        s = self.chosen()
        self.assertEqual((s.brain_server, s.model, s.ctx_limit), ("lmstudio", "qwen/qwen3-8b", 16384))
        self.assertEqual(managed_server_spec(s, "brain")["model"], "qwen/qwen3-8b")
        if s.worker_profiles:
            profile = sorted(s.worker_profiles)[-1]
            models.choose(self.settings, profile)
            self.assertEqual(self.chosen().worker_profile, profile)

    def test_a_flag_or_environment_still_wins_and_removing_goes_back_to_config(self):
        models.add(self.settings, "llama", file_path=self.model)
        models.choose(self.settings, "tiny-model")
        self.assertEqual(self.chosen(brain_server="llama", api_base="http://127.0.0.1:9/v1").model_choice, "")
        models.remove(self.settings, "tiny-model")
        self.assertEqual(models.load(self.settings)["active"], "")
        self.assertEqual(self.chosen().model_choice, "")
        with self.assertRaises(models.ModelError):
            models.remove(self.settings, "config")

    def test_mistakes_are_refused_with_a_reason(self):
        projector = fake_gguf(os.path.join(self.env.root, "mmproj-model-f16.gguf"))
        for kwargs in ({"file_path": os.path.join(self.env.root, "missing.gguf")},
                       {"file_path": projector},
                       {"file_path": self.model, "context": 999999},
                       {"file_path": self.model, "context": 100}):
            with self.assertRaises(models.ModelError, msg=str(kwargs)):
                models.add(self.settings, "llama", **kwargs)
        models.add(self.settings, "llama", file_path=self.model)
        with self.assertRaises(models.ModelError):
            models.add(self.settings, "llama", file_path=self.model)      # already added
        with self.assertRaises(models.ModelError):
            models.add(self.settings, "ollama", model="x")
        with self.assertRaises(models.ModelError):
            models.choose(self.settings, "nope")

    def test_the_cli_passes_on_only_a_profile_named_on_the_command_line(self):
        self.assertFalse(Settings(state_dir=self.settings.state_dir).worker_profile_explicit)
        profiles = Settings().worker_profiles
        if profiles:
            self.assertTrue(Settings(worker_profile=sorted(profiles)[0]).worker_profile_explicit)


class StopTests(unittest.TestCase):
    def test_stop_leaves_the_model_loaded_unless_asked_to_unload_it(self):
        from claire_core import cli
        with patch("claire_core.cli.Client") as client, \
             patch("claire_core.cli.boot.unload_models", return_value={"unloaded": ["brain"], "errors": []}) as unload, \
             patch("builtins.print"):
            client.return_value.healthy.return_value = True
            cli.stop(["stop"])
            unload.assert_not_called()
            cli.stop(["stop", "--unload"])
            unload.assert_called_once()
        self.assertEqual(client.return_value.post.call_args.args[0], "/shutdown")


class ModelServiceTests(ServiceCase):
    def test_models_are_inspected_added_chosen_and_removed_from_the_app(self):
        self.boot()
        path = fake_gguf(os.path.join(self.env.root, "tiny.gguf"))
        info = self.client.post("/models/inspect", {"path": path})
        self.assertEqual((info["context_length"], info["suggested_context"]), (8192, 8192))
        made = self.client.post("/models", {"server": "llama", "path": path, "label": "My tiny"})
        self.assertEqual(made["model"]["name"], "my-tiny")
        view = self.client.post("/models/use", {"name": "my-tiny"})
        self.assertEqual(view["selected"], "my-tiny")
        self.assertNotEqual(self.client.get("/status")["model_choice"]["running"], "my-tiny")   # until a restart
        with self.assertRaises(Exception):
            self.client.post("/models", {"server": "llama", "path": "/etc/hosts"})
        self.client.delete("/models/my-tiny")
        self.assertNotIn("my-tiny", [m["name"] for m in self.client.get("/models")["models"]])

    def test_restart_waits_for_work_and_otherwise_unloads_and_hands_over(self):
        gate = []
        def hold(messages):
            import time
            while not gate:
                time.sleep(0.02)
            return "All set.\n<done>\n<summary>\nok\n</summary>\n</done>"
        self.boot(task=[hold])
        task = self.client.post("/threads", {"task": "work", "workdir": self.env.work})["thread"]
        self.wait_for(lambda e: e["type"] == "thread_status" and e["thread"] == task["id"] and e["status"] == "running")
        with self.assertRaises(Exception):
            self.client.post("/models/restart")                 # never mid-task
        gate.append(1)
        self.wait_for(lambda e: e["type"] == "turn_end" and e["thread"] == task["id"])
        with patch("claire_core.service.boot.unload_models", return_value={"unloaded": [], "errors": []}) as unload, \
             patch("claire_core.service.subprocess.Popen") as spawn, \
             patch.object(self.service, "shutdown_all") as stop:
            self.assertTrue(self.client.post("/models/restart")["restarting"])
            for _ in range(100):
                if stop.called:
                    break
                import time; time.sleep(0.02)
        unload.assert_called_once()
        self.assertIn("claire_core", spawn.call_args.args[0][2])
        self.assertNotIn("CLAIRE_WORKER_PROFILE", spawn.call_args.kwargs["env"])
        stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()
