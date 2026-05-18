from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, Tuple

from core.config_loader import ConfigLoader
from core.run_guard import ensure_simulator_preflight
from stages.analyzer.aig_native_api import AIGNativeAPIAnalyzer
from stages.analyzer.prompt_api import PromptAPIAnalyzer
from stages.analyzer.regex_static import RegexSkillAnalyzer
from stages.attacker.upgraded import UpgradedAttacker
from stages.feedback.basic import BasicFeedback
from stages.judge.basic import BasicJudge
from stages.simulator.openclaw import OpenClawSimulator


ROOT = Path(__file__).resolve().parent.parent


def init_runtime(attacker_impl_override: Optional[str] = None) -> Tuple[Any, Any, Any, Any, Any]:
    loader = ConfigLoader()
    stages_cfg = loader.stages
    attacker_impl = str(attacker_impl_override or "").strip()
    if attacker_impl and attacker_impl not in {"upgraded", "attacker.upgraded"}:
        raise ValueError("Only the surface attacker is supported in the minimal pipeline.")

    analyzer_cfg = dict(stages_cfg.get("analyzer", {}) or {})
    analyzer_impl = str(analyzer_cfg.get("implementation") or analyzer_cfg.get("impl") or "aig_native_api").strip()
    if analyzer_impl in {"aig", "aig_native", "aig_native_api"}:
        analyzer = AIGNativeAPIAnalyzer(analyzer_cfg)
    elif analyzer_impl in {"prompt", "prompt_api", "llm_prompt", "api_prompt"}:
        prompt_cfg = dict(analyzer_cfg)
        prompt_cfg.update(dict(analyzer_cfg.get("prompt_api", {}) or {}))
        prompt_cfg.setdefault("prompt_path", analyzer_cfg.get("prompt_api_prompt_path", "prompts/analyzer_prompt_api.txt"))
        analyzer = PromptAPIAnalyzer(prompt_cfg)
    elif analyzer_impl in {"regex", "regex_static", "regex_analyzer"}:
        regex_cfg = dict(analyzer_cfg.get("regex", {}) or {})
        regex_cfg.setdefault("rules_path", analyzer_cfg.get("regex_rules_path", "configs/regex_analyzer_rules.yaml"))
        analyzer = RegexSkillAnalyzer(regex_cfg)
    else:
        raise ValueError(f"Unsupported analyzer implementation: {analyzer_impl}")
    attacker = UpgradedAttacker(dict(stages_cfg.get("attacker", {}) or {}))
    simulator = OpenClawSimulator(dict(stages_cfg.get("simulator", {}) or {}))
    judge = BasicJudge(dict(stages_cfg.get("judge", {}) or {}))
    feedback = BasicFeedback(dict(stages_cfg.get("feedback", {}) or {}))
    return analyzer, attacker, simulator, judge, feedback


def enforce_openclaw_isolate_per_run(simulator: Any) -> None:
    cls_name = simulator.__class__.__name__.lower()
    if "openclaw" not in cls_name:
        return
    cfg = getattr(simulator, "config", None)
    if isinstance(cfg, dict):
        cfg["isolate_per_run"] = True


def preflight_check_openclaw(simulator: Any, emit: Optional[Callable[[str], None]] = None) -> None:
    result = ensure_simulator_preflight(simulator)
    if emit is not None:
        emit(result.get("message", "ok"))


def resolve_run_root(mode: str) -> Path:
    loader = ConfigLoader()
    mode_cfg = loader.main_experiment if mode == "main" else loader.comparison_experiment
    common_cfg = loader.app
    project_cfg = mode_cfg.get("project", {}) or {}
    common_project_cfg = common_cfg.get("project", {}) or {}
    run_root = (
        project_cfg.get("run_root")
        or common_project_cfg.get("run_root")
        or "result/runs_organize/main"
    )
    return Path(run_root)


@contextmanager
def runtime_run_root(mode: str) -> Iterator[Path]:
    loader = ConfigLoader()
    previous = loader.get_runtime_run_root()
    run_root = resolve_run_root(mode)
    loader.set_runtime_run_root(run_root)
    try:
        yield run_root
    finally:
        loader.set_runtime_run_root(previous)
