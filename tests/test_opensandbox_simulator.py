import tempfile
import unittest
from pathlib import Path

import sys

sys.path.append(str(Path(__file__).parent.parent))

from core.config_loader import ConfigLoader
from experiments.common import init_runtime
from stages.simulator.opensandbox_openclaw import OpenSandboxOpenClawSimulator


class _FakeRunCommandOpts:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _FakeWriteEntry:
    def __init__(self, path, data=None, mode=0o644, **_kwargs):
        self.path = path
        self.data = data
        self.mode = mode


class _FakeMessage:
    def __init__(self, text):
        self.text = text


class _FakeLogs:
    def __init__(self, stdout="", stderr=""):
        self.stdout = [_FakeMessage(stdout)] if stdout else []
        self.stderr = [_FakeMessage(stderr)] if stderr else []


class _FakeExecution:
    def __init__(self, stdout="", stderr="", exit_code=0):
        self.logs = _FakeLogs(stdout=stdout, stderr=stderr)
        self.exit_code = exit_code
        self.error = None


class _FakeCommands:
    def __init__(self):
        self.calls = []

    def run(self, command, **kwargs):
        self.calls.append((command, kwargs))
        return _FakeExecution(stdout="ok\n", exit_code=0)


class _FakeFiles:
    def __init__(self):
        self.entries = []

    def write_files(self, entries):
        self.entries.extend(entries)


class _FakeSandbox:
    def __init__(self, sandbox_id="sbx-1"):
        self.id = sandbox_id
        self.commands = _FakeCommands()
        self.files = _FakeFiles()
        self.killed = False
        self.closed = False

    def kill(self):
        self.killed = True

    def close(self):
        self.closed = True


class _FakeSandboxSync:
    created_kwargs = None

    @classmethod
    def create(cls, **kwargs):
        cls.created_kwargs = kwargs
        return _FakeSandbox()


class _FakeConnectionConfig:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _FakeNetworkRule:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _FakeNetworkPolicy:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


def _fake_sdk():
    return {
        "SandboxSync": _FakeSandboxSync,
        "ConnectionConfigSync": _FakeConnectionConfig,
        "RunCommandOpts": _FakeRunCommandOpts,
        "WriteEntry": _FakeWriteEntry,
        "NetworkPolicy": _FakeNetworkPolicy,
        "NetworkRule": _FakeNetworkRule,
    }


class TestOpenSandboxSimulator(unittest.TestCase):
    def test_init_runtime_can_select_opensandbox_simulator(self):
        loader = ConfigLoader()
        original_simulator_cfg = dict(loader._config["stages"].get("simulator", {}) or {})
        try:
            loader._config["stages"]["simulator"] = {
                "implementation": "opensandbox",
                "target_agent_model_profile": "simulator_model",
                "opensandbox": {"domain": "http://localhost:8080"},
            }

            *_, simulator, _judge, _feedback = init_runtime()

            self.assertIsInstance(simulator, OpenSandboxOpenClawSimulator)
            self.assertTrue(simulator.config["isolate_per_run"])
        finally:
            loader._config["stages"]["simulator"] = original_simulator_cfg

    def test_docker_exec_is_mapped_to_opensandbox_command_with_env(self):
        simulator = OpenSandboxOpenClawSimulator({})
        sandbox = _FakeSandbox()
        simulator._opensandbox_sdk = _fake_sdk()
        simulator._sandboxes_by_name["demo-sbx"] = sandbox
        diagnostics = {}

        result = simulator._run_cmd(
            [
                "docker",
                "exec",
                "-e",
                "OPENCLAW_API_KEY=secret-value",
                "demo-sbx",
                "sh",
                "-lc",
                "printf ok",
            ],
            timeout=7,
            check=True,
            diagnostics_key="exec_probe",
            diagnostics=diagnostics,
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "ok")
        command, kwargs = sandbox.commands.calls[0]
        self.assertEqual(command, "printf ok")
        self.assertEqual(kwargs["opts"].kwargs["envs"], {"OPENCLAW_API_KEY": "secret-value"})
        self.assertIn("OPENCLAW_API_KEY=<redacted>", diagnostics["exec_probe"]["cmd"])

    def test_messages_to_text_separates_opensandbox_stdout_chunks(self):
        chunks = [
            _FakeMessage("[agent/embedded] embedded run done: durationMs=10"),
            _FakeMessage('{"payloads":[{"text":"ok"}],"meta":{"stopReason":"stop"}}'),
        ]

        text = OpenSandboxOpenClawSimulator._messages_to_text(chunks)

        self.assertIn("\n{", text)
        prefix, payload = OpenSandboxOpenClawSimulator._extract_json_suffix(text)
        self.assertIn("embedded run done", prefix)
        self.assertEqual(payload["payloads"][0]["text"], "ok")

    def test_local_model_base_url_is_preserved_for_opensandbox_host_network(self):
        loader = ConfigLoader()
        original_models = dict(loader._config.get("models", {}) or {})
        try:
            loader._config["models"] = {
                "profiles": {
                    "local_sim": {
                        "base_url": "http://localhost:8000/v1",
                        "model": "local-model",
                        "api_key": "local-key",
                        "openclaw_provider_name": "openai",
                    }
                }
            }
            simulator = OpenSandboxOpenClawSimulator({"target_agent_model_profile": "local_sim"})

            model_cfg = simulator._resolve_target_model()

            self.assertEqual(model_cfg["container_base_url"], "http://localhost:8000/v1")
        finally:
            loader._config["models"] = original_models

    def test_docker_cp_uploads_directory_to_opensandbox_files(self):
        simulator = OpenSandboxOpenClawSimulator({})
        sandbox = _FakeSandbox()
        simulator._opensandbox_sdk = _fake_sdk()
        simulator._sandboxes_by_name["demo-sbx"] = sandbox

        with tempfile.TemporaryDirectory(prefix="opensandbox-upload-") as tmpdir:
            source = Path(tmpdir) / "seed"
            nested = source / "nested"
            nested.mkdir(parents=True)
            (source / "hello.txt").write_text("hello", encoding="utf-8")
            (nested / "data.bin").write_bytes(b"\x00\x01")

            result = simulator._run_cmd(
                ["docker", "cp", f"{source}/.", "demo-sbx:/root/.openclaw/workspace"],
                timeout=30,
                check=True,
            )

        self.assertEqual(result.returncode, 0)
        paths = sorted(entry.path for entry in sandbox.files.entries)
        self.assertEqual(
            paths,
            [
                "/root/.openclaw/workspace/hello.txt",
                "/root/.openclaw/workspace/nested/data.bin",
            ],
        )
        self.assertTrue(any(call[0].startswith("mkdir -p") for call in sandbox.commands.calls))

    def test_file_mode_uses_opensandbox_decimal_permission_format(self):
        simulator = OpenSandboxOpenClawSimulator({})
        with tempfile.TemporaryDirectory(prefix="opensandbox-mode-") as tmpdir:
            path = Path(tmpdir) / "secret.txt"
            path.write_text("secret", encoding="utf-8")
            path.chmod(0o600)

            self.assertEqual(simulator._file_mode(path), 600)

    def test_cleanup_kills_and_closes_sandbox(self):
        simulator = OpenSandboxOpenClawSimulator({})
        sandbox = _FakeSandbox("sbx-cleanup")
        simulator._sandboxes_by_name["demo-sbx"] = sandbox
        simulator._sandbox_ids_by_name["demo-sbx"] = "sbx-cleanup"
        diagnostics = {}

        simulator._cleanup_isolated_container("demo-sbx", diagnostics)

        self.assertTrue(sandbox.killed)
        self.assertTrue(sandbox.closed)
        self.assertEqual(diagnostics["opensandbox_cleanup"]["sandbox_id"], "sbx-cleanup")


if __name__ == "__main__":
    unittest.main()
