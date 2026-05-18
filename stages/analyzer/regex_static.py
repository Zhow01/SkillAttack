from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Pattern, Tuple

import yaml

from core.base import AnalyzerBase
from core.schemas import AnalysisResult, SurfaceResult
from core.surface_protocol import build_skill_hash, ensure_surface_ids
from stages.analyzer.taxonomy import AIG_CANONICAL_TAXONOMY, normalize_taxonomy_label


_SEVERITY_ORDER: Dict[str, int] = {
    "critical": 4,
    "high": 3,
    "medium": 2,
    "low": 1,
    "info": 0,
}


@dataclass(frozen=True)
class RegexRule:
    id: str
    title: str
    risk_type: str
    level: str
    description: str
    confidence: float
    patterns: Tuple[str, ...]
    exclude_patterns: Tuple[str, ...] = ()
    compiled_patterns: Tuple[Pattern[str], ...] = field(default_factory=tuple)
    compiled_excludes: Tuple[Pattern[str], ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class RegexFinding:
    rule: RegexRule
    file: str
    line: int
    snippet: str
    pattern: str


class RegexSkillAnalyzer(AnalyzerBase):
    """Deterministic regex analyzer mapped to the eight SkillAttack risk types."""

    DEFAULT_RULES_PATH = Path("configs/regex_analyzer_rules.yaml")
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
        self.rules_path = Path(
            str(
                self.config.get("rules_path")
                or self.config.get("regex_rules_path")
                or self.config.get("rules_file")
                or self.DEFAULT_RULES_PATH
            )
        ).expanduser()
        self.max_file_bytes = int(self.config.get("max_file_bytes", 1_000_000) or 1_000_000)
        self.max_evidence_per_surface = int(self.config.get("max_evidence_per_surface", 6) or 6)
        self.max_surfaces = int(self.config.get("max_surfaces", 32) or 32)
        self.min_confidence = float(self.config.get("min_confidence", 0.0) or 0.0)
        self.scan_extensions = {
            str(item).lower() if str(item).startswith(".") else f".{str(item).lower()}"
            for item in self.config.get("scan_extensions", self.DEFAULT_EXTENSIONS)
        }
        self.scan_filenames = {str(item) for item in self.config.get("scan_filenames", self.DEFAULT_FILENAMES)}
        self.exclude_dirs = {str(item) for item in self.config.get("exclude_dirs", self.DEFAULT_EXCLUDE_DIRS)}
        self.rules = self._load_rules(self.rules_path)

    @staticmethod
    def _compile(pattern: str) -> Pattern[str]:
        return re.compile(str(pattern), flags=re.IGNORECASE)

    @classmethod
    def _rule_from_config(cls, payload: Dict[str, Any], source: Path) -> RegexRule:
        rule_id = str(payload.get("id") or "").strip()
        if not rule_id:
            raise ValueError(f"Regex rule in {source} is missing `id`.")

        raw_risk_type = str(payload.get("risk_type") or "").strip()
        risk_type = normalize_taxonomy_label(raw_risk_type)
        if not risk_type:
            allowed = ", ".join(AIG_CANONICAL_TAXONOMY)
            raise ValueError(f"Regex rule {rule_id} uses unsupported risk_type {raw_risk_type!r}. Allowed: {allowed}")

        patterns = tuple(str(item) for item in list(payload.get("patterns") or []) if str(item).strip())
        if not patterns:
            raise ValueError(f"Regex rule {rule_id} in {source} has no patterns.")

        exclude_patterns = tuple(
            str(item) for item in list(payload.get("exclude_patterns") or []) if str(item).strip()
        )
        compiled_patterns = tuple(cls._compile(pattern) for pattern in patterns)
        compiled_excludes = tuple(cls._compile(pattern) for pattern in exclude_patterns)
        return RegexRule(
            id=rule_id,
            title=str(payload.get("title") or payload.get("name") or rule_id).strip(),
            risk_type=risk_type,
            level=str(payload.get("level") or payload.get("severity") or "Medium").strip(),
            description=str(payload.get("description") or "").strip(),
            confidence=float(payload.get("confidence", 0.8) or 0.8),
            patterns=patterns,
            exclude_patterns=exclude_patterns,
            compiled_patterns=compiled_patterns,
            compiled_excludes=compiled_excludes,
        )

    @classmethod
    def _iter_rule_payloads(cls, raw: Any) -> Iterable[Dict[str, Any]]:
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict):
                    yield item
            return
        if not isinstance(raw, dict):
            return
        if isinstance(raw.get("rules"), list):
            for item in raw["rules"]:
                if isinstance(item, dict):
                    yield item
            return
        for value in raw.values():
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        yield item

    @classmethod
    def _load_rules(cls, rules_path: Path) -> List[RegexRule]:
        if not rules_path.exists():
            raise FileNotFoundError(f"Regex analyzer rules file does not exist: {rules_path}")
        with rules_path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        rules = [
            cls._rule_from_config(payload, rules_path)
            for payload in cls._iter_rule_payloads(raw)
            if bool(payload.get("enabled", True))
        ]
        if not rules:
            raise ValueError(f"Regex analyzer rules file has no enabled rules: {rules_path}")
        return rules

    def _build_skill_hash(self, skill_content: str, context: Dict[str, Any]) -> str:
        raw_skill_path = str((context or {}).get("skill_path") or "").strip()
        skill_path = Path(raw_skill_path).expanduser() if raw_skill_path else None
        return build_skill_hash(skill_path if skill_path and skill_path.is_dir() else None, skill_content)

    def _iter_skill_files(self, skill_content: str, context: Dict[str, Any]) -> Iterable[Tuple[str, str]]:
        raw_skill_path = str((context or {}).get("skill_path") or "").strip()
        skill_path = Path(raw_skill_path).expanduser() if raw_skill_path else None
        if skill_path and skill_path.is_file():
            yield skill_path.name, self._read_file(skill_path)
            return
        if skill_path and skill_path.is_dir():
            root = skill_path
            for path in sorted(root.rglob("*")):
                if not path.is_file():
                    continue
                if any(part in self.exclude_dirs for part in path.relative_to(root).parts[:-1]):
                    continue
                if not self._should_scan_path(path):
                    continue
                yield str(path.relative_to(root)).replace("\\", "/"), self._read_file(path)
            return
        yield "SKILL.md", str(skill_content or "")

    def _should_scan_path(self, path: Path) -> bool:
        if path.name in self.scan_filenames:
            return True
        return path.suffix.lower() in self.scan_extensions

    def _read_file(self, path: Path) -> str:
        if path.stat().st_size > self.max_file_bytes:
            return ""
        return path.read_text(encoding="utf-8", errors="ignore")

    @staticmethod
    def _clean_snippet(line: str, max_len: int = 220) -> str:
        snippet = re.sub(r"\s+", " ", str(line or "")).strip()
        if len(snippet) > max_len:
            return snippet[: max_len - 3].rstrip() + "..."
        return snippet

    def scan_findings(self, skill_content: str, context: Optional[Dict[str, Any]] = None) -> List[RegexFinding]:
        findings: List[RegexFinding] = []
        for rel_path, content in self._iter_skill_files(skill_content, dict(context or {})):
            if not content:
                continue
            for line_no, line in enumerate(content.splitlines(), start=1):
                snippet = self._clean_snippet(line)
                if not snippet:
                    continue
                for rule in self.rules:
                    if rule.confidence < self.min_confidence:
                        continue
                    if any(pattern.search(line) for pattern in rule.compiled_excludes):
                        continue
                    for index, pattern in enumerate(rule.compiled_patterns):
                        if pattern.search(line):
                            findings.append(
                                RegexFinding(
                                    rule=rule,
                                    file=rel_path,
                                    line=line_no,
                                    snippet=snippet,
                                    pattern=rule.patterns[index],
                                )
                            )
                            break
        return findings

    @staticmethod
    def _severity_weight(level: str) -> int:
        return _SEVERITY_ORDER.get(str(level or "").strip().lower(), 0)

    def _group_findings(self, findings: Iterable[RegexFinding]) -> List[Tuple[RegexRule, List[RegexFinding]]]:
        groups: Dict[str, List[RegexFinding]] = {}
        rules_by_id: Dict[str, RegexRule] = {}
        for finding in findings:
            groups.setdefault(finding.rule.id, []).append(finding)
            rules_by_id[finding.rule.id] = finding.rule

        grouped = [(rules_by_id[rule_id], items) for rule_id, items in groups.items()]
        return sorted(
            grouped,
            key=lambda pair: (
                -self._severity_weight(pair[0].level),
                AIG_CANONICAL_TAXONOMY.index(pair[0].risk_type),
                pair[0].id,
            ),
        )

    def _surface_description(self, rule: RegexRule, items: List[RegexFinding]) -> str:
        evidence = items[: self.max_evidence_per_surface]
        lines = [
            f"Rule: {rule.id}",
            f"Risk type: {rule.risk_type}",
            f"Rule confidence: {rule.confidence:.2f}",
            f"Description: {rule.description}",
            "",
            "Matched evidence:",
        ]
        for item in evidence:
            lines.append(f"- {item.file}:{item.line}: {item.snippet}")
        remaining = len(items) - len(evidence)
        if remaining > 0:
            lines.append(f"- ... {remaining} additional matches omitted")
        return "\n".join(lines).strip()

    def _surfaces_from_findings(self, findings: List[RegexFinding]) -> List[SurfaceResult]:
        surfaces: List[SurfaceResult] = []
        for rule, items in self._group_findings(findings)[: self.max_surfaces]:
            suffix = f" ({len(items)} matches)" if len(items) != 1 else ""
            surfaces.append(
                SurfaceResult(
                    id="",
                    title=f"{rule.title}{suffix}",
                    description=self._surface_description(rule, items),
                    risk_type=rule.risk_type,
                    level=rule.level,
                )
            )
        return ensure_surface_ids(surfaces)

    def _build_readme(self, skillname: str, findings: List[RegexFinding], surfaces: List[SurfaceResult]) -> str:
        if not findings:
            return f"# Regex Security Audit Report: {skillname}\n\nNo regex findings matched the configured rules."

        by_risk: Dict[str, int] = {risk: 0 for risk in AIG_CANONICAL_TAXONOMY}
        by_level: Dict[str, int] = {}
        for finding in findings:
            by_risk[finding.rule.risk_type] = by_risk.get(finding.rule.risk_type, 0) + 1
            by_level[finding.rule.level] = by_level.get(finding.rule.level, 0) + 1

        lines = [
            f"# Regex Security Audit Report: {skillname}",
            "",
            f"- Total regex findings: {len(findings)}",
            f"- Attack surfaces emitted: {len(surfaces)}",
            f"- Rules file: {self.rules_path}",
            "",
            "## Findings by Risk Type",
        ]
        for risk in AIG_CANONICAL_TAXONOMY:
            count = by_risk.get(risk, 0)
            if count:
                lines.append(f"- {risk}: {count}")
        lines.extend(["", "## Findings by Level"])
        for level, count in sorted(by_level.items(), key=lambda item: (-self._severity_weight(item[0]), item[0])):
            lines.append(f"- {level}: {count}")
        return "\n".join(lines)

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
        findings = self.scan_findings(skill_content, context)
        surfaces = self._surfaces_from_findings(findings)
        return AnalysisResult(
            skillname=skillname,
            skillhash=skillhash,
            readme=self._build_readme(skillname, findings, surfaces),
            results=surfaces,
        )
