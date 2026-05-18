from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from core.base import AnalyzerBase
from core.config_loader import ConfigLoader
from core.llm_router import LLMRouter
from core.schemas import AnalysisResult, SurfaceResult
from core.surface_protocol import analysis_public_payload, build_skill_hash, ensure_surface_ids, slugify
from stages.analyzer.taxonomy import AIG_CANONICAL_TAXONOMY, infer_taxonomy_category, normalize_taxonomy_label


class PromptAPIAnalyzer(AnalyzerBase):
    """Prompt-only analyzer that asks a configured LLM to produce attack surfaces."""

    _CACHE_VERSION = 1
    DEFAULT_PROMPT_PATH = Path("prompts/analyzer_prompt_api.txt")
    DEFAULT_EXTENSIONS = {
        ".md",
        ".txt",
        ".py",
        ".js",
        ".ts",
        ".jsx",
        ".tsx",
        ".mjs",
        ".cjs",
        ".sh",
        ".bash",
        ".zsh",
        ".yml",
        ".yaml",
        ".json",
        ".toml",
        ".ini",
        ".cfg",
        ".env",
    }
    DEFAULT_FILENAMES = {
        "SKILL.md",
        "README.md",
        "Dockerfile",
        ".env",
        ".bashrc",
        ".zshrc",
    }
    DEFAULT_EXCLUDE_DIRS = {
        ".git",
        "__pycache__",
        "node_modules",
        ".venv",
        "venv",
        "dist",
        "build",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
    }

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.model_profile = str(self.config.get("model_profile") or "").strip()
        self.prompt_path = Path(str(self.config.get("prompt_path") or self.DEFAULT_PROMPT_PATH)).expanduser()
        self.max_input_chars = int(self.config.get("max_input_chars", 32_000) or 32_000)
        self.max_file_chars = int(self.config.get("max_file_chars", 8_000) or 8_000)
        self.max_files = int(self.config.get("max_files", 32) or 32)
        self.max_surfaces = int(self.config.get("max_surfaces", 16) or 16)
        self.use_analysis_cache = self._bool_cfg(self.config.get("use_analysis_cache"), True)
        self.force_rescan = self._bool_cfg(self.config.get("force_rescan"), False)
        self.scan_extensions = {
            str(item).lower() if str(item).startswith(".") else f".{str(item).lower()}"
            for item in self.config.get("scan_extensions", self.DEFAULT_EXTENSIONS)
        }
        self.scan_filenames = {str(item) for item in self.config.get("scan_filenames", self.DEFAULT_FILENAMES)}
        self.exclude_dirs = {str(item) for item in self.config.get("exclude_dirs", self.DEFAULT_EXCLUDE_DIRS)}

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
    def _parse_json_payload(text: Any) -> Optional[Dict[str, Any]]:
        raw = str(text or "").strip()
        if not raw:
            return None
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
            raw = re.sub(r"\s*```$", "", raw).strip()

        candidates = [raw]
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            candidates.append(raw[start : end + 1])

        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
            except Exception:
                continue
            if isinstance(parsed, dict):
                return parsed
        return None

    def _cache_root(self) -> Path:
        configured = str(self.config.get("analysis_cache_root") or "").strip()
        if configured:
            return Path(configured).expanduser()
        app_cfg = ConfigLoader().app
        app_root = str((((app_cfg or {}).get("input") or {}).get("skills_analysis_result_root") or "")).strip()
        if app_root:
            return Path(app_root).expanduser()
        return Path("result/aig_cache")

    def _cache_path(self, skillname: str, skillhash: str) -> Path:
        return self._cache_root() / f"{slugify(skillname)}_{skillhash}_prompt_api.json"

    def _load_cached_analysis(self, *, skillname: str, skillhash: str) -> Optional[AnalysisResult]:
        cache_path = self._cache_path(skillname, skillhash)
        if not cache_path.exists():
            return None
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            analyze_payload = payload.get("analyze_result")
            if not isinstance(analyze_payload, dict):
                return None
            return AnalysisResult(**analyze_payload)
        except Exception:
            return None

    def _save_cached_analysis(
        self,
        *,
        skillname: str,
        skillhash: str,
        prompt_payload: Dict[str, Any],
        raw_response: str,
        parsed_response: Dict[str, Any],
        analysis: AnalysisResult,
    ) -> None:
        cache_root = self._cache_root()
        cache_root.mkdir(parents=True, exist_ok=True)
        payload = {
            "cache_version": self._CACHE_VERSION,
            "skillname": skillname,
            "skillhash": skillhash,
            "saved_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "model_profile": self.model_profile,
            "prompt_payload": dict(prompt_payload or {}),
            "raw_response": str(raw_response or ""),
            "parsed_response": dict(parsed_response or {}),
            "analyze_result": analysis_public_payload(analysis),
        }
        self._cache_path(skillname, skillhash).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _build_skill_hash(self, skill_content: str, context: Dict[str, Any]) -> str:
        raw_skill_path = str((context or {}).get("skill_path") or "").strip()
        skill_path = Path(raw_skill_path).expanduser() if raw_skill_path else None
        return build_skill_hash(skill_path if skill_path and skill_path.is_dir() else None, skill_content)

    def _should_scan_path(self, path: Path) -> bool:
        if path.name in self.scan_filenames:
            return True
        return path.suffix.lower() in self.scan_extensions

    def _read_file(self, path: Path) -> str:
        text = path.read_text(encoding="utf-8", errors="ignore")
        if len(text) > self.max_file_chars:
            return text[: self.max_file_chars] + "\n...[truncated]..."
        return text

    @staticmethod
    def _with_line_numbers(content: str) -> str:
        lines = str(content or "").splitlines()
        return "\n".join(f"{index:04d}: {line}" for index, line in enumerate(lines, start=1))

    def _iter_skill_files(self, skill_content: str, context: Dict[str, Any]) -> Iterable[Tuple[str, str]]:
        raw_skill_path = str((context or {}).get("skill_path") or "").strip()
        skill_path = Path(raw_skill_path).expanduser() if raw_skill_path else None
        if skill_path and skill_path.is_file():
            yield skill_path.name, self._read_file(skill_path)
            return
        if skill_path and skill_path.is_dir():
            root = skill_path
            emitted = 0
            preferred = root / "SKILL.md"
            if preferred.exists() and preferred.is_file():
                yield "SKILL.md", self._read_file(preferred)
                emitted += 1
            for path in sorted(root.rglob("*")):
                if emitted >= self.max_files:
                    return
                if not path.is_file() or path == preferred:
                    continue
                if any(part in self.exclude_dirs for part in path.relative_to(root).parts[:-1]):
                    continue
                if not self._should_scan_path(path):
                    continue
                yield str(path.relative_to(root)).replace("\\", "/"), self._read_file(path)
                emitted += 1
            return
        yield "SKILL.md", str(skill_content or "")[: self.max_file_chars]

    def _collect_prompt_files(self, skill_content: str, context: Dict[str, Any]) -> List[Dict[str, str]]:
        files: List[Dict[str, str]] = []
        used_chars = 0
        for path, content in self._iter_skill_files(skill_content, context):
            remaining = self.max_input_chars - used_chars
            if remaining <= 0:
                break
            file_text = self._with_line_numbers(str(content or ""))
            if len(file_text) > remaining:
                file_text = file_text[:remaining] + "\n...[truncated by analyzer input budget]..."
            files.append({"path": path, "content": file_text})
            used_chars += len(file_text)
        return files

    def _load_system_prompt(self) -> str:
        if self.prompt_path.exists():
            return self.prompt_path.read_text(encoding="utf-8")
        return (
            "Analyze the provided Agent Skill files for security risks. Return JSON only with keys "
            "`readme` and `results`. Each result must include title, risk_type, level, and description."
        )

    def _prompt_payload(self, skillname: str, files: List[Dict[str, str]]) -> Dict[str, Any]:
        return {
            "skillname": skillname,
            "allowed_risk_types": list(AIG_CANONICAL_TAXONOMY),
            "files": files,
            "required_output_shape": {
                "readme": "markdown summary",
                "results": [
                    {
                        "title": "short finding title",
                        "risk_type": "one allowed risk type",
                        "level": "Critical|High|Medium|Low|Info",
                        "description": "evidence-grounded description with file path and line references where possible",
                    }
                ],
            },
        }

    def _clean_findings(self, parsed_response: Dict[str, Any], skill_content: str) -> List[SurfaceResult]:
        raw_results = parsed_response.get("results")
        if raw_results is None:
            raw_results = parsed_response.get("findings")
        if not isinstance(raw_results, list):
            return []

        surfaces: List[SurfaceResult] = []
        for index, item in enumerate(raw_results[: self.max_surfaces], start=1):
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or item.get("name") or f"Prompt finding {index}").strip()
            description = str(item.get("description") or item.get("desc") or item.get("evidence") or "").strip()
            raw_risk = str(item.get("risk_type") or item.get("risk") or item.get("category") or "").strip()
            risk_type = normalize_taxonomy_label(raw_risk)
            if not risk_type:
                risk_type = infer_taxonomy_category(
                    raw_label=raw_risk,
                    attack_surface=[title],
                    trigger_patterns=[description],
                    skill_content=skill_content,
                )
            level = str(item.get("level") or item.get("severity") or "Medium").strip() or "Medium"
            if not description:
                description = "Prompt-only analyzer returned this finding without detailed evidence."
            surfaces.append(
                SurfaceResult(
                    id="",
                    title=title,
                    description=description,
                    risk_type=str(risk_type or "Data Exfiltration"),
                    level=level,
                )
            )
        return ensure_surface_ids(surfaces)

    @staticmethod
    def _build_readme(skillname: str, parsed_response: Dict[str, Any], surfaces: List[SurfaceResult]) -> str:
        readme = str(parsed_response.get("readme") or "").strip()
        if readme:
            return readme
        if not surfaces:
            return f"# Prompt API Security Audit Report: {skillname}\n\nNo findings were returned by the prompt-only analyzer."
        return (
            f"# Prompt API Security Audit Report: {skillname}\n\n"
            f"- Finding count: {len(surfaces)}\n"
            f"- Primary risk: {surfaces[0].risk_type}\n"
            f"- Primary finding: {surfaces[0].title}\n"
        )

    def analyze(self, skill_content: str, context: Optional[Dict[str, Any]] = None) -> AnalysisResult:
        context = dict(context or {})
        raw_skill_path = str(context.get("skill_path") or "").strip()
        skill_path = Path(raw_skill_path).expanduser() if raw_skill_path else None
        skillname = str(context.get("skill_id") or "").strip()
        if not skillname and skill_path and skill_path.name:
            skillname = skill_path.name
        if not skillname:
            skillname = "unknown"

        skillhash = self._build_skill_hash(skill_content, context)
        if self.use_analysis_cache and not self.force_rescan:
            cached = self._load_cached_analysis(skillname=skillname, skillhash=skillhash)
            if cached is not None:
                return cached

        if not self.model_profile:
            raise ValueError("Prompt API analyzer requires `model_profile` in stage config.")

        files = self._collect_prompt_files(skill_content, context)
        prompt_payload = self._prompt_payload(skillname, files)
        messages = [
            {"role": "system", "content": self._load_system_prompt()},
            {"role": "user", "content": json.dumps(prompt_payload, ensure_ascii=False, indent=2)},
        ]
        raw_response = LLMRouter().chat_completion(
            self.model_profile,
            messages,
            response_format={"type": "json_object"},
        )
        parsed_response = self._parse_json_payload(raw_response)
        if not isinstance(parsed_response, dict):
            raise RuntimeError(f"Prompt API analyzer returned invalid JSON: {str(raw_response)[:500]}")

        merged_skill_content = "\n".join(str(file.get("content") or "") for file in files)
        surfaces = self._clean_findings(parsed_response, merged_skill_content or skill_content)
        analysis = AnalysisResult(
            skillname=skillname,
            skillhash=skillhash,
            readme=self._build_readme(skillname, parsed_response, surfaces),
            results=surfaces,
        )
        self._save_cached_analysis(
            skillname=skillname,
            skillhash=skillhash,
            prompt_payload=prompt_payload,
            raw_response=str(raw_response or ""),
            parsed_response=parsed_response,
            analysis=analysis,
        )
        return analysis
