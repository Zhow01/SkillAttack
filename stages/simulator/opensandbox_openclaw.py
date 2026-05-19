from __future__ import annotations

import os
import shlex
import stat
import subprocess
import hashlib
import io
import uuid
from contextlib import redirect_stderr
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from stages.simulator.openclaw import OpenClawSimulator


class OpenSandboxOpenClawSimulator(OpenClawSimulator):
    """
    Run the existing OpenClaw simulator workload through OpenSandbox.

    The parent OpenClaw simulator already contains the prompt construction,
    OpenClaw output parsing, trace extraction, and failure classification. This
    subclass keeps that behavioral layer intact and swaps the container API from
    direct Docker commands to OpenSandbox lifecycle/command/files APIs.
    """

    def __init__(self, config: Dict[str, Any]):
        merged = dict(config or {})
        merged["isolate_per_run"] = True
        super().__init__(merged)
        self._opensandbox_sdk: Optional[Dict[str, Any]] = None
        self._sandboxes_by_name: Dict[str, Any] = {}
        self._sandbox_ids_by_name: Dict[str, str] = {}

    def _opensandbox_cfg(self) -> Dict[str, Any]:
        cfg = dict(self.config.get("opensandbox", {}) or {})
        for key in (
            "opensandbox_domain",
            "opensandbox_protocol",
            "opensandbox_api_key_env",
            "opensandbox_api_key",
            "opensandbox_image",
        ):
            if key in self.config and key not in cfg:
                cfg[key.replace("opensandbox_", "")] = self.config[key]
        return cfg

    def _load_opensandbox_sdk(self) -> Dict[str, Any]:
        if self._opensandbox_sdk is not None:
            return self._opensandbox_sdk
        try:
            from opensandbox import SandboxSync
            from opensandbox.config import ConnectionConfigSync
            from opensandbox.models.execd import RunCommandOpts
            from opensandbox.models.sandboxes import NetworkPolicy, NetworkRule
            try:
                from opensandbox.models.filesystem import WriteEntry
            except Exception:
                from opensandbox.models import WriteEntry
        except Exception as exc:
            raise RuntimeError(
                "OpenSandbox SDK is not installed or not supported by this Python runtime. "
                "Use Python >= 3.10, install with `pip install opensandbox`, and start "
                "`opensandbox-server` before using simulator.implementation=opensandbox."
            ) from exc

        self._opensandbox_sdk = {
            "SandboxSync": SandboxSync,
            "ConnectionConfigSync": ConnectionConfigSync,
            "RunCommandOpts": RunCommandOpts,
            "WriteEntry": WriteEntry,
            "NetworkPolicy": NetworkPolicy,
            "NetworkRule": NetworkRule,
        }
        return self._opensandbox_sdk

    def _opensandbox_domain(self) -> str:
        cfg = self._opensandbox_cfg()
        return str(
            cfg.get("domain")
            or cfg.get("server")
            or os.environ.get("OPEN_SANDBOX_DOMAIN")
            or os.environ.get("OPENSANDBOX_DOMAIN")
            or "http://localhost:8080"
        ).strip()

    def _opensandbox_server_url(self) -> str:
        domain = self._opensandbox_domain()
        cfg = self._opensandbox_cfg()
        protocol = str(cfg.get("protocol") or "http").strip() or "http"
        if "://" not in domain:
            domain = f"{protocol}://{domain}"
        return domain.rstrip("/")

    def _create_connection_config(self) -> Any:
        sdk = self._load_opensandbox_sdk()
        cfg = self._opensandbox_cfg()
        domain = self._opensandbox_domain()
        protocol = str(cfg.get("protocol") or "").strip()
        api_key_env = str(cfg.get("api_key_env") or "OPEN_SANDBOX_API_KEY").strip()
        api_key = str(cfg.get("api_key") or os.environ.get(api_key_env, "") or "").strip()

        kwargs: Dict[str, Any] = {"domain": domain}
        if api_key:
            kwargs["api_key"] = api_key
        if protocol and "://" not in domain:
            kwargs["protocol"] = protocol
        if "use_server_proxy" in cfg:
            kwargs["use_server_proxy"] = self._bool_cfg(cfg.get("use_server_proxy"), False)
        if "debug" in cfg:
            kwargs["debug"] = self._bool_cfg(cfg.get("debug"), False)

        request_timeout = self._int_cfg(cfg.get("request_timeout_seconds"), 30)
        if request_timeout:
            kwargs["request_timeout"] = timedelta(seconds=request_timeout)

        return sdk["ConnectionConfigSync"](**kwargs)

    def _build_network_policy(self) -> Any:
        cfg = self._opensandbox_cfg()
        policy_cfg = dict(cfg.get("network_policy", {}) or {})
        if not self._bool_cfg(policy_cfg.get("enabled"), False):
            return None

        sdk = self._load_opensandbox_sdk()
        default_action = str(
            policy_cfg.get("defaultAction")
            or policy_cfg.get("default_action")
            or "deny"
        ).strip()
        rules = []
        for item in list(policy_cfg.get("egress") or []):
            if not isinstance(item, dict):
                continue
            action = str(item.get("action") or "").strip()
            target = str(item.get("target") or "").strip()
            if action and target:
                rules.append(sdk["NetworkRule"](action=action, target=target))
        return sdk["NetworkPolicy"](defaultAction=default_action, egress=rules)

    def _resolve_target_model(self) -> Dict[str, str]:
        model_cfg = dict(super()._resolve_target_model())
        cfg = self._opensandbox_cfg()
        explicit_base_url = str(cfg.get("container_base_url") or "").strip()
        if explicit_base_url:
            model_cfg["container_base_url"] = explicit_base_url
            return model_cfg

        strategy = str(cfg.get("local_base_url_strategy") or "host").strip().lower()
        if strategy in {"host", "preserve", "none"} and self._is_local_base_url(model_cfg.get("base_url", "")):
            model_cfg["container_base_url"] = model_cfg["base_url"]
        return model_cfg

    def preflight(self) -> Dict[str, Any]:
        diagnostics: Dict[str, Any] = {
            "backend": "opensandbox",
            "opensandbox_server_url": self._opensandbox_server_url(),
        }

        try:
            self._load_opensandbox_sdk()
        except Exception as exc:
            return {
                "ok": False,
                "code": "opensandbox_sdk_missing",
                "message": str(exc),
                "diagnostics": diagnostics,
            }

        health = self._probe_http_endpoint(f"{self._opensandbox_server_url()}/health")
        diagnostics["opensandbox_health_probe"] = health
        if not health.get("ok"):
            return {
                "ok": False,
                "code": "opensandbox_server_unreachable",
                "message": str(health.get("message") or "OpenSandbox server is unreachable."),
                "diagnostics": diagnostics,
            }

        try:
            model_cfg = self._resolve_target_model()
        except Exception as exc:
            return {
                "ok": False,
                "code": self._classify_exception_code(exc),
                "message": str(exc),
                "diagnostics": diagnostics,
            }

        diagnostics["target_model_profile"] = model_cfg["profile_name"]
        diagnostics["target_model_name"] = model_cfg["model_name"]
        diagnostics["target_model_base_url"] = model_cfg["base_url"]
        diagnostics["openclaw_provider_name"] = model_cfg["provider_name"]
        diagnostics["openclaw_container_base_url"] = model_cfg["container_base_url"]

        endpoint_probe = self._probe_http_endpoint(model_cfg["base_url"])
        diagnostics["model_endpoint_probe"] = endpoint_probe
        if not endpoint_probe.get("ok"):
            return {
                "ok": False,
                "code": "model_endpoint_unreachable",
                "message": str(endpoint_probe.get("message") or "Model endpoint is unreachable."),
                "diagnostics": diagnostics,
            }

        return {
            "ok": True,
            "code": "ok",
            "message": "OpenSandbox OpenClaw preflight passed.",
            "diagnostics": diagnostics,
        }

    def _entrypoint_cfg(self, value: Any) -> Optional[List[str]]:
        if value in (None, ""):
            return ["tail", "-f", "/dev/null"]
        if isinstance(value, list):
            return [str(part) for part in value]
        if isinstance(value, str):
            return shlex.split(value)
        return ["tail", "-f", "/dev/null"]

    def _start_isolated_container(
        self,
        base_container_name: str,
        run_id: str,
        diagnostics: Dict[str, Any],
    ) -> Tuple[str, str]:
        sdk = self._load_opensandbox_sdk()
        cfg = self._opensandbox_cfg()
        image = str(cfg.get("image") or cfg.get("openclaw_image") or "skillrt-openclaw-clean:latest").strip()
        if not image:
            raise RuntimeError("OpenSandbox image is empty.")

        run_hash = hashlib.sha1(str(run_id).encode("utf-8")).hexdigest()[:10]
        rand_suffix = uuid.uuid4().hex[:6]
        temp_name = f"{base_container_name}-osb-{run_hash}-{rand_suffix}"
        if len(temp_name) > 63:
            temp_name = temp_name[:63]

        timeout_seconds = self._int_cfg(
            cfg.get("timeout_seconds") or cfg.get("sandbox_timeout_seconds") or self.config.get("timeout"),
            3600,
        )
        ready_timeout_seconds = self._int_cfg(cfg.get("ready_timeout_seconds"), 60)
        resource = dict(cfg.get("resource", {}) or {"cpu": "2", "memory": "4Gi"})
        env = {str(k): str(v) for k, v in dict(cfg.get("env", {}) or {}).items()}
        metadata = {str(k): str(v) for k, v in dict(cfg.get("metadata", {}) or {}).items()}
        metadata.setdefault("project", "SkillAttack")
        metadata.setdefault("backend", "opensandbox")
        metadata.setdefault("run_id", str(run_id))

        create_kwargs: Dict[str, Any] = {
            "image": image,
            "timeout": timedelta(seconds=timeout_seconds),
            "ready_timeout": timedelta(seconds=ready_timeout_seconds),
            "entrypoint": self._entrypoint_cfg(cfg.get("entrypoint")),
            "env": env,
            "metadata": metadata,
            "resource": resource,
            "connection_config": self._create_connection_config(),
        }
        network_policy = self._build_network_policy()
        if network_policy is not None:
            create_kwargs["network_policy"] = network_policy

        diagnostics["opensandbox_create"] = {
            "image": image,
            "timeout_seconds": timeout_seconds,
            "ready_timeout_seconds": ready_timeout_seconds,
            "resource": resource,
            "entrypoint": create_kwargs["entrypoint"],
            "network_policy_enabled": network_policy is not None,
            "server_url": self._opensandbox_server_url(),
        }

        sandbox = None
        try:
            try:
                sandbox = sdk["SandboxSync"].create(**create_kwargs)
            except TypeError:
                image_value = create_kwargs.pop("image")
                sandbox = sdk["SandboxSync"].create(image_value, **create_kwargs)

            self._sandboxes_by_name[temp_name] = sandbox
            self._sandbox_ids_by_name[temp_name] = str(getattr(sandbox, "id", "") or temp_name)

            self._run_cmd(
                ["docker", "exec", temp_name, "sh", "-lc", "rm -rf /root/.openclaw/workspace && mkdir -p /root/.openclaw/workspace"],
                timeout=20,
                check=True,
                diagnostics_key="opensandbox_workspace_reset",
                diagnostics=diagnostics,
            )

            workspace_seed = str(self.config.get("workspace_seed_dir") or "data/skillinject/task_files").strip()
            if workspace_seed and os.path.isdir(workspace_seed):
                self._run_cmd(
                    ["docker", "cp", f"{workspace_seed}/.", f"{temp_name}:/root/.openclaw/workspace"],
                    timeout=120,
                    check=True,
                    diagnostics_key="opensandbox_workspace_seed_cp",
                    diagnostics=diagnostics,
                )
            else:
                diagnostics["isolated_workspace_seed_missing"] = workspace_seed

            skills_seed = str(self.config.get("skills_seed_dir") or "sandbox/openclaw/data/skills").strip()
            if skills_seed and os.path.isdir(skills_seed):
                self._run_cmd(
                    ["docker", "exec", temp_name, "sh", "-lc", "rm -rf /root/.openclaw/skills && mkdir -p /root/.openclaw/skills"],
                    timeout=20,
                    check=True,
                    diagnostics_key="opensandbox_skills_reset",
                    diagnostics=diagnostics,
                )
                self._run_cmd(
                    ["docker", "cp", f"{skills_seed}/.", f"{temp_name}:/root/.openclaw/skills"],
                    timeout=120,
                    check=True,
                    diagnostics_key="opensandbox_skills_seed_cp",
                    diagnostics=diagnostics,
                )
            else:
                diagnostics["isolated_skills_seed_missing"] = skills_seed

            self._mount_target_skill_into_container(temp_name, diagnostics)
            self._run_cmd(
                ["docker", "exec", temp_name, "sh", "-lc", "rm -f /root/.openclaw/agents/main/sessions/*.jsonl /root/.openclaw/agents/main/sessions/*.lock || true"],
                timeout=20,
                check=False,
                diagnostics_key="opensandbox_session_cleanup",
                diagnostics=diagnostics,
            )
        except Exception:
            if sandbox is not None:
                self._kill_opensandbox(temp_name, diagnostics, "opensandbox_create_cleanup")
            raise

        diagnostics["isolated_container_image"] = image
        diagnostics["isolated_container_name"] = temp_name
        diagnostics["opensandbox_sandbox_id"] = self._sandbox_ids_by_name.get(temp_name, "")
        return temp_name, image

    def _kill_opensandbox(self, container_name: str, diagnostics: Optional[Dict[str, Any]], diagnostics_key: str) -> None:
        sandbox = self._sandboxes_by_name.pop(container_name, None)
        sandbox_id = self._sandbox_ids_by_name.pop(container_name, "")
        result = {"sandbox_id": sandbox_id, "killed": False, "closed": False}
        if sandbox is None:
            if diagnostics is not None:
                diagnostics[diagnostics_key] = result
            return
        try:
            kill = getattr(sandbox, "kill", None)
            if callable(kill):
                try:
                    with redirect_stderr(io.StringIO()):
                        kill()
                    result["killed"] = True
                except Exception as exc:
                    result["kill_error"] = self._redact_text(str(exc))
                    if sandbox_id:
                        self._force_remove_opensandbox_docker_container(
                            f"sandbox-{sandbox_id}",
                            diagnostics,
                            f"{diagnostics_key}_docker_fallback",
                        )
        finally:
            close = getattr(sandbox, "close", None)
            if callable(close):
                try:
                    close()
                    result["closed"] = True
                except Exception as exc:
                    result["close_error"] = self._redact_text(str(exc))
            if diagnostics is not None:
                diagnostics[diagnostics_key] = result

    def _cleanup_isolated_container(self, container_name: str, diagnostics: Dict[str, Any]) -> None:
        self._kill_opensandbox(container_name, diagnostics, "opensandbox_cleanup")

    def _force_remove_opensandbox_docker_container(
        self,
        docker_container_name: str,
        diagnostics: Optional[Dict[str, Any]],
        diagnostics_key: str,
    ) -> None:
        name = str(docker_container_name or "").strip()
        if not name:
            return
        record: Dict[str, Any] = {"container": name, "host_pid": None, "removed": False}
        inspect = super()._run_cmd(
            ["docker", "inspect", "-f", "{{.State.Pid}}", name],
            timeout=10,
            check=False,
            diagnostics_key=f"{diagnostics_key}_inspect_pid",
            diagnostics=diagnostics,
        )
        if inspect.returncode != 0:
            record["inspect_error"] = self._redact_text((inspect.stderr or inspect.stdout or "").strip())
            if diagnostics is not None:
                diagnostics[diagnostics_key] = record
            return
        try:
            pid = int(str(inspect.stdout or "").strip())
        except Exception:
            pid = 0
        record["host_pid"] = pid
        if pid > 1:
            super()._run_cmd(
                ["kill", "-9", str(pid)],
                timeout=10,
                check=False,
                diagnostics_key=f"{diagnostics_key}_kill_pid",
                diagnostics=diagnostics,
            )
            super()._run_cmd(
                ["docker", "wait", name],
                timeout=5,
                check=False,
                diagnostics_key=f"{diagnostics_key}_wait",
                diagnostics=diagnostics,
            )
        rm = super()._run_cmd(
            ["docker", "rm", "-f", name],
            timeout=30,
            check=False,
            diagnostics_key=f"{diagnostics_key}_rm",
            diagnostics=diagnostics,
        )
        record["removed"] = rm.returncode == 0
        if rm.returncode != 0:
            record["rm_error"] = self._redact_text((rm.stderr or rm.stdout or "").strip())
        if diagnostics is not None:
            diagnostics[diagnostics_key] = record

    def _is_container_running(self, container_name: str) -> bool:
        return container_name in self._sandboxes_by_name

    def _parse_docker_exec(self, cmd: List[Any]) -> Tuple[str, List[str], Dict[str, str]]:
        env: Dict[str, str] = {}
        idx = 2
        parts = [str(part) for part in cmd]
        while idx < len(parts):
            part = parts[idx]
            if part in {"-e", "--env"} and idx + 1 < len(parts):
                key, value = self._split_env_assignment(parts[idx + 1])
                if key:
                    env[key] = value
                idx += 2
                continue
            if part.startswith("--env="):
                key, value = self._split_env_assignment(part.split("=", 1)[1])
                if key:
                    env[key] = value
                idx += 1
                continue
            if part.startswith("-e") and "=" in part[2:]:
                key, value = self._split_env_assignment(part[2:])
                if key:
                    env[key] = value
                idx += 1
                continue
            if part.startswith("-"):
                idx += 1
                continue
            break
        if idx >= len(parts):
            raise RuntimeError("Invalid docker exec command: missing container name.")
        container_name = parts[idx]
        return container_name, parts[idx + 1 :], env

    @staticmethod
    def _split_env_assignment(value: str) -> Tuple[str, str]:
        if "=" not in str(value):
            return "", ""
        key, raw = str(value).split("=", 1)
        return key, raw

    @staticmethod
    def _command_parts_to_shell(command_parts: List[str]) -> str:
        if not command_parts:
            return ""
        if len(command_parts) >= 3 and command_parts[0] in {"sh", "bash"} and command_parts[1] in {"-c", "-lc"}:
            return str(command_parts[2])
        return " ".join(shlex.quote(str(part)) for part in command_parts)

    def _run_opensandbox_command(
        self,
        container_name: str,
        command: str,
        env: Dict[str, str],
        timeout: int,
    ) -> subprocess.CompletedProcess:
        sandbox = self._sandboxes_by_name.get(container_name)
        if sandbox is None:
            return subprocess.CompletedProcess(
                args=["opensandbox", container_name, command],
                returncode=1,
                stdout="",
                stderr=f"OpenSandbox instance '{container_name}' is not active.",
            )

        sdk = self._load_opensandbox_sdk()
        opts = None
        try:
            opts = sdk["RunCommandOpts"](
                timeout=timedelta(seconds=int(timeout)) if timeout else None,
                envs=env or None,
            )
        except Exception:
            try:
                opts = sdk["RunCommandOpts"](
                    timeout=timedelta(seconds=int(timeout)) if timeout else None,
                    env=env or None,
                )
            except Exception:
                opts = None

        try:
            if opts is not None:
                execution = sandbox.commands.run(command, opts=opts)
            else:
                execution = sandbox.commands.run(self._prefix_env(command, env))
        except TypeError:
            execution = sandbox.commands.run(self._prefix_env(command, env))
        except Exception as exc:
            return subprocess.CompletedProcess(
                args=["opensandbox", container_name, command],
                returncode=1,
                stdout="",
                stderr=str(exc),
            )

        return self._execution_to_completed_process(execution, ["opensandbox", container_name, command])

    @staticmethod
    def _prefix_env(command: str, env: Dict[str, str]) -> str:
        if not env:
            return command
        assignments = " ".join(f"{key}={shlex.quote(value)}" for key, value in env.items())
        return f"env {assignments} sh -lc {shlex.quote(command)}"

    @staticmethod
    def _messages_to_text(messages: Any) -> str:
        chunks: List[str] = []
        for item in list(messages or []):
            if isinstance(item, dict):
                text = item.get("text")
            else:
                text = getattr(item, "text", None)
            if text is not None:
                chunks.append(str(text))
        return "\n".join(chunk.rstrip("\n") for chunk in chunks)

    def _execution_to_completed_process(self, execution: Any, args: List[str]) -> subprocess.CompletedProcess:
        logs = getattr(execution, "logs", None)
        stdout = self._messages_to_text(getattr(logs, "stdout", []))
        stderr = self._messages_to_text(getattr(logs, "stderr", []))
        if not stdout:
            text = getattr(execution, "text", None)
            if isinstance(text, str):
                stdout = text
        error_obj = getattr(execution, "error", None)
        if error_obj is not None:
            error_name = str(getattr(error_obj, "name", "") or "")
            error_value = str(getattr(error_obj, "value", "") or "")
            error_text = ": ".join([part for part in [error_name, error_value] if part])
            if error_text:
                stderr = f"{stderr}\n{error_text}".strip()
        exit_code = getattr(execution, "exit_code", None)
        if exit_code is None:
            exit_code = 1 if error_obj is not None else 0
        try:
            returncode = int(exit_code)
        except Exception:
            returncode = 1 if error_obj is not None else 0
        return subprocess.CompletedProcess(args=args, returncode=returncode, stdout=stdout, stderr=stderr)

    def _copy_to_opensandbox(self, src: str, dest: str, timeout: int) -> subprocess.CompletedProcess:
        if ":" not in dest:
            return subprocess.CompletedProcess(args=["opensandbox-cp", src, dest], returncode=1, stdout="", stderr="destination must be container:path")
        container_name, dest_path = dest.split(":", 1)
        sandbox = self._sandboxes_by_name.get(container_name)
        if sandbox is None:
            return subprocess.CompletedProcess(args=["opensandbox-cp", src, dest], returncode=1, stdout="", stderr=f"OpenSandbox instance '{container_name}' is not active.")

        source_text = str(src)
        copy_contents = source_text.endswith("/.") or source_text.endswith("\\.")
        source_path = Path(source_text[:-2] if copy_contents else source_text).expanduser()
        try:
            if source_path.is_dir():
                upload_root = dest_path if copy_contents else self._join_remote(dest_path, source_path.name)
                self._upload_dir(sandbox, source_path, upload_root, timeout)
            elif source_path.is_file():
                self._upload_file(sandbox, source_path, dest_path)
            else:
                return subprocess.CompletedProcess(args=["opensandbox-cp", src, dest], returncode=1, stdout="", stderr=f"source path not found: {source_path}")
        except Exception as exc:
            return subprocess.CompletedProcess(args=["opensandbox-cp", src, dest], returncode=1, stdout="", stderr=str(exc))
        return subprocess.CompletedProcess(args=["opensandbox-cp", src, dest], returncode=0, stdout="", stderr="")

    def _upload_dir(self, sandbox: Any, source_dir: Path, dest_dir: str, timeout: int) -> None:
        dest_root = str(dest_dir).rstrip("/") or "/"
        dirs = [dest_root]
        for path in source_dir.rglob("*"):
            if path.is_dir():
                dirs.append(self._join_remote(dest_root, path.relative_to(source_dir)))
        for chunk_start in range(0, len(dirs), 100):
            chunk = dirs[chunk_start : chunk_start + 100]
            command = "mkdir -p " + " ".join(shlex.quote(path) for path in chunk)
            sandbox.commands.run(command)

        entries = []
        for path in source_dir.rglob("*"):
            if not path.is_file():
                continue
            remote_path = self._join_remote(dest_root, path.relative_to(source_dir))
            entries.append(self._write_entry(remote_path, path.read_bytes(), self._file_mode(path)))
            if len(entries) >= 64:
                sandbox.files.write_files(entries)
                entries = []
        if entries:
            sandbox.files.write_files(entries)

    def _upload_file(self, sandbox: Any, source_path: Path, dest_path: str) -> None:
        parent = str(Path(dest_path).parent)
        if parent and parent != ".":
            sandbox.commands.run(f"mkdir -p {shlex.quote(parent)}")
        sandbox.files.write_files([
            self._write_entry(str(dest_path), source_path.read_bytes(), self._file_mode(source_path))
        ])

    def _write_entry(self, path: str, data: bytes, mode: int) -> Any:
        sdk = self._load_opensandbox_sdk()
        return sdk["WriteEntry"](path=path, data=data, mode=mode)

    @staticmethod
    def _file_mode(path: Path) -> int:
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except Exception:
            mode = 0o644
        return int(format(mode, "o"))

    @staticmethod
    def _join_remote(root: str, relative: Path) -> str:
        rel = relative.as_posix().lstrip("/")
        return f"{root.rstrip('/')}/{rel}" if rel else root.rstrip("/")

    def _record_and_maybe_raise(
        self,
        result: subprocess.CompletedProcess,
        cmd: List[Any],
        timeout: int,
        check: bool,
        diagnostics_key: str,
        diagnostics: Optional[Dict[str, Any]],
    ) -> subprocess.CompletedProcess:
        if diagnostics is not None and diagnostics_key:
            diagnostics[diagnostics_key] = {
                "cmd": self._redact_cmd(cmd),
                "returncode": result.returncode,
                "stdout": self._redact_text((result.stdout or "")[:1000]),
                "stderr": self._redact_text((result.stderr or "")[:1000]),
                "timed_out": bool(result.returncode == 124),
                "timeout_sec": int(timeout),
                "backend": "opensandbox",
            }
        if check and result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip() or f"exit_code={result.returncode}"
            safe_cmd = " ".join(self._redact_cmd(cmd))
            safe_detail = self._redact_text(detail)
            raise RuntimeError(f"Command failed: {safe_cmd} :: {safe_detail}")
        return result

    def _run_cmd(
        self,
        cmd: list,
        timeout: int = 30,
        check: bool = False,
        diagnostics_key: str = "",
        diagnostics: Dict[str, Any] = None,
    ) -> subprocess.CompletedProcess:
        parts = [str(part) for part in list(cmd or [])]
        result: Optional[subprocess.CompletedProcess] = None
        try:
            if parts[:3] == ["docker", "inspect", "-f"] and len(parts) >= 5:
                container_name = parts[-1]
                template = parts[3]
                if container_name in self._sandboxes_by_name:
                    if template == "{{.State.Running}}":
                        result = subprocess.CompletedProcess(args=cmd, returncode=0, stdout="true\n", stderr="")
                    elif template == "{{.State.Pid}}":
                        result = subprocess.CompletedProcess(args=cmd, returncode=0, stdout="0\n", stderr="")
            elif parts[:2] == ["docker", "exec"]:
                container_name, command_parts, env = self._parse_docker_exec(parts)
                if container_name in self._sandboxes_by_name:
                    command = self._command_parts_to_shell(command_parts)
                    result = self._run_opensandbox_command(container_name, command, env, timeout)
            elif parts[:2] == ["docker", "cp"] and len(parts) >= 4:
                if ":" in parts[3] and parts[3].split(":", 1)[0] in self._sandboxes_by_name:
                    result = self._copy_to_opensandbox(parts[2], parts[3], timeout)
            elif parts[:2] == ["docker", "rm"] and parts[-1] in self._sandboxes_by_name:
                kill_key = f"{diagnostics_key}_kill" if diagnostics_key else "opensandbox_rm"
                self._kill_opensandbox(parts[-1], diagnostics, kill_key)
                result = subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
            elif parts[:2] == ["docker", "wait"] and len(parts) >= 3 and parts[2] in self._sandboxes_by_name:
                result = subprocess.CompletedProcess(args=cmd, returncode=0, stdout="0\n", stderr="")
        except Exception as exc:
            result = subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr=str(exc))

        if result is None:
            return super()._run_cmd(cmd, timeout=timeout, check=check, diagnostics_key=diagnostics_key, diagnostics=diagnostics)
        return self._record_and_maybe_raise(result, cmd, timeout, check, diagnostics_key, diagnostics)
