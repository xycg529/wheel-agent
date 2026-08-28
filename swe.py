from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from wheel_agent.config import AgentConfig
from wheel_agent.evalkit import CheckResult, EvalReport, TaskOutcome, eval_agent_config
from wheel_agent.evals.swe_lite import DATASET, INSTANCE_IDS, INSTANCE_IDS_TINY
from wheel_agent.loop import run_agent
from wheel_agent.model import ModelClient
from wheel_agent.replay import replay_run
from wheel_agent.types import RunResult

RUN_ID = "wheel"


def load_lite_rows(ids: tuple[str, ...] | list[str] = INSTANCE_IDS) -> dict[str, dict[str, Any]]:
    try:
        from datasets import load_dataset
    except ImportError as exc:
        raise RuntimeError("SWE-bench live eval needs the datasets package: pip install 'wheel-agent[eval]'") from exc
    ds = load_dataset(DATASET, split="test")
    wanted = set(ids)
    rows = {}
    for row in ds:
        iid = row["instance_id"]
        if iid in wanted:
            rows[iid] = dict(row)
        if len(rows) == len(wanted):
            break
    missing = [iid for iid in ids if iid not in rows]
    if missing:
        raise RuntimeError(f"missing SWE-bench Lite instances: {missing[:5]}")
    return rows


def checkout_repo(row: dict[str, Any], dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://github.com/{row['repo']}.git"
    clone = subprocess.run(
        ["git", "clone", "--filter=blob:none", url, str(dest)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if clone.returncode != 0:
        raise RuntimeError(clone.stderr or clone.stdout or "git clone failed")
    checkout = subprocess.run(
        ["git", "checkout", "--force", str(row["base_commit"])],
        cwd=dest,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if checkout.returncode != 0:
        raise RuntimeError(checkout.stderr or "git checkout failed")


def git_diff(repo: Path) -> str:
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, text=True)
    proc = subprocess.run(
        [
            "git",
            "diff",
            "--cached",
            "--",
            ".",
            ":(exclude).wheel",
            ":(exclude).wheel_runs",
            ":(exclude).wheel/**",
            ":(exclude).wheel_runs/**",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        proc = subprocess.run(["git", "diff"], cwd=repo, capture_output=True, text=True)
    return proc.stdout or ""


def protected_paths_changed(repo: Path) -> list[str]:
    proc = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    paths = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    return [
        path for path in paths
        if path.startswith("tests/") or "/tests/" in f"/{path}/" or Path(path).name.startswith("test_")
    ]


def issue_prompt(row: dict[str, Any]) -> str:
    return (
        "Fix the GitHub issue below in this repository. Keep the change minimal. "
        "Do not modify tests unless the issue requires it. When the fix is in, stop.\n\n"
        f"Repository: {row.get('repo')}\nInstance: {row.get('instance_id')}\n\n"
        f"{row.get('problem_statement') or ''}"
    )


def prediction(instance_id: str, patch: str, model: str) -> dict[str, str]:
    return {
        "instance_id": instance_id,
        "model_name_or_path": model,
        "model_patch": patch,
    }


def evaluate_swe(
    config: AgentConfig,
    *,
    model: ModelClient,
    work_root: str | Path,
    replay: bool = True,
    instance_ids: tuple[str, ...] | list[str] | None = None,
) -> EvalReport:
    ids = tuple(instance_ids or INSTANCE_IDS_TINY)
    root = Path(work_root)
    root.mkdir(parents=True, exist_ok=True)
    if shutil.which("docker") is None:
        outcomes = [
            TaskOutcome(
                task_id=iid,
                resolved=False,
                checks=[CheckResult("official_evaluator", False, "docker unavailable")],
                run=None,
                status="unavailable",
            )
            for iid in ids
        ]
        return EvalReport(
            suite=f"swe-lite-{len(ids)}",
            outcomes=outcomes,
            status="unavailable",
            error="Docker is required for the official SWE-bench evaluator",
        )
    try:
        rows = load_lite_rows(ids)
    except Exception as exc:
        outcomes = [TaskOutcome(iid, False, [], None, status="error") for iid in ids]
        return EvalReport(suite=f"swe-lite-{len(ids)}", outcomes=outcomes, status="error", error=str(exc))
    eval_config = eval_agent_config(config, config.runs_dir, 50)
    outcomes: list[TaskOutcome] = []
    preds: list[dict[str, str]] = []
    for iid in ids:
        row = rows[iid]
        live = root / iid / "live"
        result: RunResult | None = None
        patch = ""
        protected: list[str] = []
        try:
            checkout_repo(row, live)
            result = run_agent(
                issue_prompt(row),
                live,
                eval_config,
                model,
                extra_meta={"suite": f"swe-lite-{len(ids)}", "task_id": iid},
            )
            patch = git_diff(live)
            protected = protected_paths_changed(live)
        except Exception as exc:
            outcomes.append(
                TaskOutcome(
                    task_id=iid,
                    resolved=False,
                    checks=[
                        CheckResult("protected_tests_unchanged", False, ", ".join(protected[:5]))
                    ] if protected else [],
                    run=result,
                )
            )
            preds.append(prediction(iid, f"# error: {exc}\n", eval_config.provider.model))
            continue
        preds.append(prediction(iid, patch, eval_config.provider.model))
        replay_match = None
        replay_status = None
        if replay and result and result.run_id:
            replay_ws = root / iid / "replay"
            checkout_repo(row, replay_ws)
            replay_result = replay_run(eval_config.runs_dir, result.run_id, replay_ws, interactive=False)[1]
            replay_status = replay_result.replay_status or "error"
            replay_match = replay_status == "exact"
        outcomes.append(
            TaskOutcome(
                task_id=iid,
                resolved=False,
                checks=[
                    CheckResult("protected_tests_unchanged", False, ", ".join(protected[:5]))
                ] if protected else [],
                run=result,
                replay_match=replay_match,
                replay_status=replay_status,
            )
        )

    pred_path = root / "predictions.jsonl"
    pred_path.write_text("".join(json.dumps(p) + "\n" for p in preds), encoding="utf-8")
    eval_status, resolved, eval_error = _run_official_eval(
        pred_path, eval_config.provider.model, ids, root
    )
    for outcome in outcomes:
        if eval_status != "complete":
            outcome.status = eval_status
            outcome.checks.append(CheckResult("official_evaluator", False, eval_error))
        elif outcome.status == "complete" and not any(not check.passed for check in outcome.checks):
            outcome.resolved = outcome.task_id in resolved
    return EvalReport(suite=f"swe-lite-{len(ids)}", outcomes=outcomes, status=eval_status, error=eval_error)


def _run_official_eval(
    pred_path: Path, model_name: str, ids: tuple[str, ...], work_root: Path
) -> tuple[str, set[str], str]:
    if shutil.which("docker") is None:
        return "unavailable", set(), "Docker is required for the official evaluator"
    report_dir = work_root / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        "-m",
        "swebench.harness.run_evaluation",
        "--dataset_name",
        DATASET,
        "--predictions_path",
        str(pred_path),
        "--max_workers",
        "1",
        "--run_id",
        RUN_ID,
        "--report_dir",
        str(report_dir),
        "--instance_ids",
        *ids,
    ]
    try:
        proc = subprocess.run(cmd, cwd=work_root, capture_output=True, text=True, timeout=60 * 60 * 6)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "error", set(), f"official evaluator failed to start: {exc}"
    # The harness names the summary <model>.<run_id>.json inside --report_dir;
    # the old '**/wheel*.json' glob from the caller's cwd never matched it.
    resolved: set[str] = set()
    parsed = False
    candidates = list(report_dir.glob(f"{model_name}.{RUN_ID}.json")) or list(
        report_dir.glob(f"*.{RUN_ID}.json")
    )
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            found, recognized = _resolved_from_report(data, ids)
            resolved.update(found)
            parsed = parsed or recognized
    # Fallback for partial runs: per-instance reports under
    # logs/run_evaluation/<run_id>/<model>/<instance_id>/report.json.
    if not parsed:
        log_root = work_root / "logs" / "run_evaluation" / RUN_ID
        if log_root.is_dir():
            for rep in log_root.glob("*/*/report.json"):
                iid = rep.parent.name
                if iid not in ids:
                    continue
                try:
                    data = json.loads(rep.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if isinstance(data, dict) and data.get("resolved"):
                    resolved.add(iid)
                    parsed = True
    if proc.returncode != 0:
        return "error", resolved, f"official evaluator exited with code {proc.returncode}"
    if not parsed:
        return "error", resolved, "official evaluator report was missing or unrecognized"
    return "complete", resolved, ""


def _resolved_from_report(data: dict[str, Any], ids: tuple[str, ...]) -> tuple[set[str], bool]:
    out: set[str] = set()
    recognized = False
    resolved = data.get("resolved_ids") or data.get("resolved")
    if isinstance(resolved, list):
        recognized = True
        out.update(str(x) for x in resolved)
    results = data.get("results") or data.get("submitted_instances")
    if isinstance(results, dict):
        recognized = recognized or any(str(iid) in results for iid in ids)
        for iid, row in results.items():
            if isinstance(row, dict) and row.get("resolved"):
                out.add(str(iid))
    return out, recognized

