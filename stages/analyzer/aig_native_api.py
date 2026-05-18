import json
import mimetypes
import os
import re
import shutil
import tempfile
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib import error as urllib_error
from urllib import request as urllib_request

from core.base import AnalyzerBase
from core.config_loader import ConfigLoader
from core.llm_router import LLMRouter
from core.schemas import AnalysisResult, SurfaceResult
from core.surface_protocol import (
    analysis_public_payload,
    build_skill_hash,
    ensure_surface_ids,
    native_cache_path,
)
from stages.analyzer.taxonomy import AIG_CANONICAL_TAXONOMY, infer_taxonomy_category, normalize_taxonomy_label


class AIGNativeAPIAnalyzer(AnalyzerBase):
    _DONE_STATUSES = {"done", "complete", "completed"}
    _FAILED_STATUSES = {"error", "failed", "terminated"}
    _CACHE_VERSION = 2

    @staticmethod
    def _severity_weight(level: str) -> int:
        normalized = str(level or "").strip().lower()
        mapping = {
            "critical": 4,
            "严重": 4,
            "high": 3,
            "高危": 3,
            "medium": 2,
            "中危": 2,
            "low": 1,
            "低危": 1,
        }
        return mapping.get(normalized, 0)

    @staticmethod
    def _normalize_base_url(base_url: str) -> str:
        text = str(base_url or "").strip()
        if not text:
            return "http://localhost:18088"
        return text.rstrip("/")

    @staticmethod
    def _bool_cfg(value: Any, default: bool = False) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        lowered = str(value).strip().lower()
        if lowered in {"1", "true", "yes", "y", "on"}:
            return True
        if lowered in {"0", "false", "no", "n", "off"}:
            return False
        return default

    @staticmethod
    def _extract_model_credentials(profile_name: str) -> Dict[str, str]:
        profile = ConfigLoader().get_model_profile(profile_name)
        if not profile:
            raise ValueError(f"Model profile '{profile_name}' not found for native AIG scan.")

        model = str(profile.get("model") or "").strip()
        base_url = str(profile.get("base_url") or "").strip()
        base_url_env = str(profile.get("base_url_env") or "").strip()
        if not base_url and base_url_env:
            base_url = str(os.environ.get(base_url_env) or "").strip()
        api_key = str(profile.get("api_key") or "").strip()
        api_key_env = str(profile.get("api_key_env") or "").strip()
        if not api_key and api_key_env:
            api_key = str(os.environ.get(api_key_env) or "").strip()

        if not model or not api_key:
            raise ValueError(
                "Native AIG `mcp_scan` requires an explicit model name and API key. "
                f"Profile '{profile_name}' is incomplete."
            )

        return {"model": model, "token": api_key, "base_url": base_url}

    @staticmethod
    def _json_headers(extra_headers: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
        headers = {"Accept": "application/json"}
        if isinstance(extra_headers, dict):
            for key, value in extra_headers.items():
                headers[str(key)] = str(value)
        return headers

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        payload: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, Any]] = None,
        timeout: float = 60.0,
    ) -> Dict[str, Any]:
        request_headers = self._json_headers(headers)
        data = None
        if payload is not None:
            request_headers["Content-Type"] = "application/json"
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        req = urllib_request.Request(url=url, data=data, method=method.upper(), headers=request_headers)
        try:
            with urllib_request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib_error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"AIG API HTTP {exc.code} at {url}: {body}") from exc
        except urllib_error.URLError as exc:
            raise RuntimeError(f"AIG API request failed for {url}: {exc}") from exc

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"AIG API returned invalid JSON from {url}: {raw[:500]}") from exc
        if not isinstance(parsed, dict):
            raise RuntimeError(f"AIG API returned unexpected payload type from {url}.")
        return parsed

    def _upload_archive(
        self,
        *,
        archive_path: Path,
        base_url: str,
        headers: Optional[Dict[str, Any]] = None,
        timeout: float = 120.0,
    ) -> str:
        boundary = f"----SkillAttackAIG{uuid.uuid4().hex}"
        filename = archive_path.name
        content_type = mimetypes.guess_type(filename)[0] or "application/zip"
        file_bytes = archive_path.read_bytes()

        body = b"".join(
            [
                f"--{boundary}\r\n".encode("utf-8"),
                (
                    f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                    f"Content-Type: {content_type}\r\n\r\n"
                ).encode("utf-8"),
                file_bytes,
                f"\r\n--{boundary}--\r\n".encode("utf-8"),
            ]
        )
        request_headers = self._json_headers(headers)
        request_headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        request_headers["Content-Length"] = str(len(body))

        url = f"{base_url}/api/v1/app/taskapi/upload"
        req = urllib_request.Request(url=url, data=body, method="POST", headers=request_headers)
        try:
            with urllib_request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib_error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"AIG upload failed with HTTP {exc.code}: {body_text}") from exc
        except urllib_error.URLError as exc:
            raise RuntimeError(f"AIG upload failed for {url}: {exc}") from exc

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"AIG upload returned invalid JSON: {raw[:500]}") from exc

        file_url = str((((parsed or {}).get("data") or {}).get("fileUrl") or "")).strip()
        if int(parsed.get("status", 1)) != 0 or not file_url:
            raise RuntimeError(f"AIG upload did not return a usable fileUrl: {parsed}")
        return file_url

    def _create_scan_task(
        self,
        *,
        base_url: str,
        model_payload: Dict[str, str],
        file_url: str,
        prompt: str,
        headers: Optional[Dict[str, Any]] = None,
        timeout: float = 60.0,
    ) -> str:
        payload = {
            "type": "mcp_scan",
            "content": {
                "prompt": prompt,
                "model": {
                    "model": model_payload["model"],
                    "token": model_payload["token"],
                    "base_url": model_payload.get("base_url") or "",
                },
                "thread": int(self.config.get("thread", 4) or 4),
                "language": str(self.config.get("language", "zh") or "zh"),
                "attachments": file_url,
            },
        }
        response = self._request_json(
            "POST",
            f"{base_url}/api/v1/app/taskapi/tasks",
            payload=payload,
            headers=headers,
            timeout=timeout,
        )
        session_id = str((((response or {}).get("data") or {}).get("session_id") or "")).strip()
        if int(response.get("status", 1)) != 0 or not session_id:
            raise RuntimeError(f"AIG task creation failed: {response}")
        return session_id

    def _poll_task_result(
        self,
        *,
        base_url: str,
        session_id: str,
        headers: Optional[Dict[str, Any]] = None,
        timeout_seconds: float = 900.0,
        poll_interval_seconds: float = 5.0,
    ) -> Dict[str, Any]:
        deadline = time.time() + max(timeout_seconds, 1.0)
        last_status_payload: Dict[str, Any] = {}

        while time.time() < deadline:
            status_payload = self._request_json(
                "GET",
                f"{base_url}/api/v1/app/taskapi/status/{session_id}",
                headers=headers,
                timeout=min(max(poll_interval_seconds, 1.0), 30.0),
            )
            last_status_payload = status_payload
            status_value = str((((status_payload or {}).get("data") or {}).get("status") or "")).strip().lower()
            if status_value in self._DONE_STATUSES:
                result_payload = self._request_json(
                    "GET",
                    f"{base_url}/api/v1/app/taskapi/result/{session_id}",
                    headers=headers,
                    timeout=60.0,
                )
                if int(result_payload.get("status", 1)) != 0:
                    raise RuntimeError(f"AIG task completed but result fetch failed: {result_payload}")
                return {"status_payload": status_payload, "result_payload": result_payload}
            fallback_native_result = self._native_result_from_status_log(status_payload)
            if fallback_native_result is not None:
                return {
                    "status_payload": status_payload,
                    "result_payload": {
                        "status": 0,
                        "message": "status-log-fallback",
                        "data": {"result": fallback_native_result},
                    },
                }
            if status_value in self._FAILED_STATUSES:
                raise RuntimeError(f"AIG task {session_id} failed: {status_payload}")
            time.sleep(max(poll_interval_seconds, 0.5))

        raise TimeoutError(f"AIG task {session_id} did not finish before timeout. Last status: {last_status_payload}")

    @staticmethod
    def _extract_native_result(result_payload: Dict[str, Any]) -> Dict[str, Any]:
        data = (result_payload or {}).get("data")
        if not isinstance(data, dict):
            return {}
        event = data.get("event")
        if isinstance(event, dict) and isinstance(event.get("result"), dict):
            return dict(event["result"])
        if isinstance(data.get("result"), dict):
            return dict(data["result"])
        return dict(data)

    @staticmethod
    def _parse_json_payload(text: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(text, str):
            return None
        raw = text.strip()
        if not raw:
            return None
        if raw.startswith("```"):
            raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        try:
            parsed = json.loads(raw)
        except Exception:
            start = raw.find("{")
            end = raw.rfind("}")
            if start == -1 or end == -1 or end <= start:
                return None
            try:
                parsed = json.loads(raw[start : end + 1])
            except Exception:
                return None
        return parsed if isinstance(parsed, dict) else None

    @staticmethod
    def _status_log_text(status_payload: Dict[str, Any]) -> str:
        return str((((status_payload or {}).get("data") or {}).get("log") or "")).strip()

    @classmethod
    def _extract_status_log_report(cls, status_payload: Dict[str, Any]) -> str:
        log_text = cls._status_log_text(status_payload)
        if not log_text:
            return ""

        matches = re.findall(r"```(?:markdown)?\s*(# .*?)```", log_text, flags=re.DOTALL)
        if matches:
            return str(matches[-1]).strip()
        return ""

    @classmethod
    def _native_result_from_status_log(cls, status_payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        log_text = cls._status_log_text(status_payload)
        if not log_text:
            return None

        for block in reversed(re.findall(r"```(?:json)?\s*(.*?)```", log_text, flags=re.DOTALL)):
            parsed = cls._parse_json_payload(block)
            if isinstance(parsed, dict) and isinstance(parsed.get("results"), list):
                return parsed

        lowered = log_text.lower()
        no_findings_markers = (
            "无需生成漏洞条目",
            "无须生成漏洞条目",
            "不符合 skill 项目漏洞报告标准",
            "不符合skill项目漏洞报告标准",
            "无需生成任何漏洞条目",
            "无需生成任何漏洞",
            "未发现已确认高危漏洞",
            "无需要报告",
            "无需要生成漏洞",
            "no vulnerabilities need to be reported",
            "no vulnerability entries need to be generated",
        )
        if any(marker in lowered for marker in no_findings_markers):
            report = cls._extract_status_log_report(status_payload)
            return {
                "readme": report,
                "results": [],
            }
        return None

    @staticmethod
    def _native_findings(native_result: Dict[str, Any]) -> List[Dict[str, str]]:
        findings = native_result.get("results")
        if not isinstance(findings, list):
            return []

        cleaned: List[Dict[str, str]] = []
        for item in findings:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            description = str(item.get("description") or item.get("desc") or "").strip()
            risk_type = str(item.get("risk_type") or "").strip()
            level = str(item.get("level") or "").strip()
            suggestion = str(item.get("suggestion") or "").strip()
            if not any([title, description, risk_type, level, suggestion]):
                continue
            cleaned.append(
                {
                    "title": title,
                    "description": description,
                    "risk_type": risk_type,
                    "level": level,
                    "suggestion": suggestion,
                }
            )
        return cleaned

    @classmethod
    def _build_readme(cls, skillname: str, native_result: Dict[str, Any], findings: List[Dict[str, str]]) -> str:
        readme = str(native_result.get("readme") or "").strip()
        if readme:
            return readme
        if not findings:
            return f"# Security Audit Report: {skillname}\n\nNo findings were returned by the native analyzer."

        ranked = sorted(findings, key=lambda item: cls._severity_weight(item.get("level", "")), reverse=True)
        top = ranked[0]
        return (
            f"# Security Audit Report: {skillname}\n\n"
            f"- Finding count: {len(findings)}\n"
            f"- Highest severity: {str(top.get('level') or 'Unknown').strip()}\n"
            f"- Primary risk: {str(top.get('risk_type') or 'Unknown').strip()}\n"
            f"- Primary finding: {str(top.get('title') or 'Top Finding').strip()}\n"
        )

    def _load_remap_prompt(self) -> str:
        prompt_path = Path(str(self.config.get("risk_type_remap_prompt_path") or "prompts/analyzer_risk_type_remap.txt"))
        if prompt_path.exists():
            return prompt_path.read_text(encoding="utf-8")
        return (
            "Remap each finding risk_type into exactly one canonical label from the provided list. "
            "Return JSON only with key `results`, preserving order."
        )

    def _classify_findings(
        self,
        *,
        skillname: str,
        skill_content: str,
        findings: List[Dict[str, str]],
    ) -> Tuple[List[SurfaceResult], List[Dict[str, Any]]]:
        if not findings:
            return [], []

        normalized_labels: List[Optional[str]] = [None] * len(findings)
        results_payload: List[Any] = []
        classifier_profile = str(self.config.get("risk_type_remap_model_profile") or "").strip()
        if classifier_profile:
            messages = [
                {"role": "system", "content": self._load_remap_prompt()},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "skillname": skillname,
                            "allowed_risk_types": list(AIG_CANONICAL_TAXONOMY),
                            "findings": [
                                {
                                    "title": item.get("title", ""),
                                    "description": item.get("description", ""),
                                    "risk_type": item.get("risk_type", ""),
                                    "level": item.get("level", ""),
                                }
                                for item in findings
                            ],
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                },
            ]
            try:
                response = LLMRouter().chat_completion(
                    classifier_profile,
                    messages,
                    response_format={"type": "json_object"},
                )
                payload = self._parse_json_payload(response)
            except Exception:
                payload = None

            results_payload = list((payload or {}).get("results") or [])
            if len(results_payload) == len(findings):
                for index, item in enumerate(results_payload):
                    if not isinstance(item, dict):
                        continue
                    normalized_labels[index] = normalize_taxonomy_label(str(item.get("risk_type") or ""))

        normalized_texts: List[Optional[Dict[str, str]]] = [None] * len(findings)
        if len(results_payload) == len(findings):
            for index, item in enumerate(results_payload):
                if not isinstance(item, dict):
                    continue
                en_title = str(item.get("title") or "").strip()
                en_desc = str(item.get("description") or "").strip()
                if en_title or en_desc:
                    normalized_texts[index] = {"title": en_title, "description": en_desc}

        remap_records: List[Dict[str, Any]] = []
        remapped_results: List[SurfaceResult] = []
        for index, finding in enumerate(findings, start=1):
            original_risk = str(finding.get("risk_type") or "").strip()
            remapped = normalized_labels[index - 1]
            used_fallback = not bool(remapped)
            if not remapped:
                remapped = infer_taxonomy_category(
                    raw_label=original_risk,
                    attack_surface=[str(finding.get("title") or "")],
                    trigger_patterns=[str(finding.get("description") or "")],
                    skill_content=skill_content,
                )
            en = normalized_texts[index - 1]
            final_title = (en["title"] if en and en["title"] else str(finding.get("title") or "")).strip()
            final_desc = (en["description"] if en and en["description"] else str(finding.get("description") or "")).strip()
            remap_records.append(
                {
                    "index": index,
                    "title": final_title,
                    "original_risk_type": original_risk,
                    "final_risk_type": remapped,
                    "used_fallback": used_fallback,
                }
            )
            remapped_results.append(
                SurfaceResult(
                    id="",
                    title=final_title,
                    description=final_desc,
                    risk_type=str(remapped or "Data Exfiltration"),
                    level=str(finding.get("level") or "").strip(),
                )
            )
        return ensure_surface_ids(remapped_results), remap_records

    @staticmethod
    def _zip_directory(source_dir: Path, output_zip: Path) -> None:
        with zipfile.ZipFile(output_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(source_dir.rglob("*")):
                if path.is_file():
                    zf.write(path, arcname=str(path.relative_to(source_dir)))

    @staticmethod
    def _zip_skill_content(skill_content: str, output_zip: Path) -> None:
        with zipfile.ZipFile(output_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("SKILL.md", str(skill_content or ""))

    def _build_archive_for_scan(self, skill_content: str, context: Dict[str, Any]) -> Path:
        tmpdir = Path(tempfile.mkdtemp(prefix="skillattack-aig-native-"))
        archive_path = tmpdir / "skill_source.zip"
        skill_path = Path(str((context or {}).get("skill_path") or "")).expanduser()
        if skill_path.is_dir():
            self._zip_directory(skill_path, archive_path)
        else:
            self._zip_skill_content(skill_content, archive_path)
        return archive_path

    def _build_skill_hash(self, skill_content: str, context: Dict[str, Any]) -> str:
        skill_path = Path(str((context or {}).get("skill_path") or "")).expanduser()
        return build_skill_hash(skill_path if skill_path.is_dir() else None, skill_content)

    def _cache_root(self) -> Path:
        configured = str(self.config.get("analysis_cache_root") or "").strip()
        if configured:
            return Path(configured).expanduser()
        app_cfg = ConfigLoader().app
        app_root = str((((app_cfg or {}).get("input") or {}).get("skills_analysis_result_root") or "")).strip()
        if app_root:
            return Path(app_root).expanduser()
        return Path("result/aig_cache")

    def _load_cached_analysis(self, *, skillname: str, skillhash: str) -> Optional[AnalysisResult]:
        cache_path = native_cache_path(self._cache_root(), skillname, skillhash)
        if not cache_path.exists():
            return None
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            return None
        analyze_payload = payload.get("analyze_result")
        if not isinstance(analyze_payload, dict):
            return None
        try:
            return AnalysisResult(**analyze_payload)
        except Exception:
            return None

    def _save_cached_analysis(
        self,
        *,
        skillname: str,
        skillhash: str,
        session_id: str,
        status_payload: Dict[str, Any],
        native_result: Dict[str, Any],
        native_findings: List[Dict[str, str]],
        remap_records: List[Dict[str, Any]],
        analysis: AnalysisResult,
    ) -> None:
        cache_root = self._cache_root()
        cache_root.mkdir(parents=True, exist_ok=True)
        cache_path = native_cache_path(cache_root, skillname, skillhash)
        payload = {
            "cache_version": self._CACHE_VERSION,
            "skillname": skillname,
            "skillhash": skillhash,
            "saved_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "session": {
                "session_id": session_id,
                "api_base_url": self._normalize_base_url(self.config.get("api_base_url", "http://localhost:18088")),
            },
            "status": dict(status_payload or {}),
            "native_result": dict(native_result or {}),
            "native_findings": list(native_findings or []),
            "risk_type_remap": list(remap_records or []),
            "analyze_result": analysis_public_payload(analysis),
        }
        cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def analyze(self, skill_content: str, context: Optional[Dict[str, Any]] = None) -> AnalysisResult:
        context = dict(context or {})
        skillname = str(context.get("skill_id") or "unknown").strip() or "unknown"
        skillhash = self._build_skill_hash(skill_content, context)

        if self._bool_cfg(self.config.get("use_analysis_cache"), True) and not self._bool_cfg(
            self.config.get("force_rescan"),
            False,
        ):
            cached = self._load_cached_analysis(skillname=skillname, skillhash=skillhash)
            if cached is not None:
                return cached

        base_url = self._normalize_base_url(self.config.get("api_base_url", "http://localhost:18088"))
        model_profile = str(self.config.get("model_profile") or "").strip()
        if not model_profile:
            raise ValueError("AIG native analyzer requires `model_profile` in stage config.")

        headers = dict(self.config.get("api_headers") or {})
        username = str(self.config.get("username") or "").strip()
        if username:
            headers.setdefault("username", username)

        model_payload = self._extract_model_credentials(model_profile)
        prompt = str(
            self.config.get("prompt")
            or "请使用 A.I.G 原生 mcp_scan 对该 Skill 源码进行完整安全扫描，重点关注 Agent Skill 风险。"
        ).strip()
        archive_path = self._build_archive_for_scan(skill_content, context)

        try:
            file_url = self._upload_archive(
                archive_path=archive_path,
                base_url=base_url,
                headers=headers,
                timeout=float(self.config.get("upload_timeout_seconds", 120) or 120),
            )
            session_id = self._create_scan_task(
                base_url=base_url,
                model_payload=model_payload,
                file_url=file_url,
                prompt=prompt,
                headers=headers,
                timeout=float(self.config.get("request_timeout_seconds", 60) or 60),
            )
            payloads = self._poll_task_result(
                base_url=base_url,
                session_id=session_id,
                headers=headers,
                timeout_seconds=float(self.config.get("poll_timeout_seconds", 900) or 900),
                poll_interval_seconds=float(self.config.get("poll_interval_seconds", 5) or 5),
            )
            native_result = self._extract_native_result(payloads.get("result_payload") or {})
            native_findings = self._native_findings(native_result)
            remapped_results, remap_records = self._classify_findings(
                skillname=skillname,
                skill_content=skill_content,
                findings=native_findings,
            )
            analysis = AnalysisResult(
                skillname=skillname,
                skillhash=skillhash,
                readme=self._build_readme(skillname, native_result, native_findings),
                results=remapped_results,
            )
            self._save_cached_analysis(
                skillname=skillname,
                skillhash=skillhash,
                session_id=session_id,
                status_payload=payloads.get("status_payload") or {},
                native_result=native_result,
                native_findings=native_findings,
                remap_records=remap_records,
                analysis=analysis,
            )
            return analysis
        finally:
            shutil.rmtree(str(archive_path.parent), ignore_errors=True)
