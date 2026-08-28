from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from wheel_agent.config import AgentConfig
from wheel_agent.evalkit import CheckResult, EvalReport, TaskOutcome, eval_agent_config
from wheel_agent.evals.polyglot import CATALOGS, PYTHON_EXERCISES, REPO_URL
from wheel_agent.loop import run_agent
from wheel_agent.model import ModelClient
from wheel_agent.types import RunResult

DISABLED = re.compile(r'\n[ \t]*@Disabled\("Remove to run test"\)[ \t]*')
JAVA_HOME = Path.home() / ".sdkman" / "candidates" / "java" / "current"


def default_cache() -> Path:
    return Path.home() / ".wheel" / "eval" / "polyglot-benchmark"


def default_work_root(lang: str = "python") -> Path:
    suffix = "" if lang == "python" else f"-{lang}"
    return Path.home() / ".wheel" / "eval" / f"polyglot{suffix}-runs"


def ensure_repo(cache: str | Path | None = None) -> Path:
    dest = Path(cache) if cache else default_cache()
    marker = dest / "python" / "exercises" / "practice"
    if marker.is_dir():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest)
    proc = subprocess.run(
        ["git", "clone", "--depth", "1", REPO_URL, str(dest)],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout or "git clone polyglot-benchmark failed")
    if not marker.is_dir():
        raise RuntimeError(f"polyglot clone missing python exercises: {dest}")
    return dest


def exercise_src(repo: Path, name: str, lang: str = "python") -> Path:
    path = repo / lang / "exercises" / "practice" / name
    if not path.is_dir():
        raise FileNotFoundError(f"unknown polyglot {lang} exercise: {name}")
    return path


def read_instructions(src: Path) -> str:
    docs = src / ".docs"
    chunks: list[str] = []
    for filename in ("instructions.md", "instructions.append.md", "introduction.md"):
        path = docs / filename
        if path.is_file():
            chunks.append(path.read_text(encoding="utf-8"))
    return "\n\n".join(chunks).strip()


def prepare_exercise(src: Path, dest: Path, *, lang: str = "python") -> None:
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns(".meta", ".git"))
    if lang == "java":
        enable_java_tests(dest)


def enable_java_tests(root: Path) -> int:
    changed = 0
    for path in root.rglob("*.java"):
        rel = path.as_posix()
        if "/src/test/" not in f"/{rel}/":
            continue
        text = path.read_text(encoding="utf-8")
        updated = DISABLED.sub("\n", text)
        if updated != text:
            path.write_text(updated, encoding="utf-8")
            changed += 1
    return changed


def protected_tests_fingerprint(root: Path, lang: str = "python") -> str:
    parts: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if lang == "java":
            keep = "/src/test/" in f"/{rel}/" or rel == "build.gradle"
        else:
            keep = "/tests/" in f"/{rel}/" or rel.startswith("tests/") or path.name.endswith("_test.py")
        if not keep:
            continue
        # An empty tests/__init__.py is harmless package boilerplate the agent
        # may create; a non-empty one could monkeypatch at import time while
        # still reporting protected_tests_unchanged, so only empty ones are
        # exempt from the fingerprint.
        if path.name == "__init__.py" and not path.read_bytes().strip():
            continue
        parts.append(rel + ":" + hashlib.sha256(path.read_bytes()).hexdigest())
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def task_prompt(name: str, instructions: str, lang: str = "python") -> str:
    if lang == "java":
        return (
            "Implement the Java Exercism practice exercise in this workspace so all "
            "unit tests pass. Do not modify src/test or build.gradle. "
            "Keep the existing public types and method names. "
            "Run tests with ./gradlew test --rerun-tasks. When tests pass, stop.\n\n"
            f"Exercise: {name}\n\n"
            f"{instructions}"
        )
    return (
        "Implement the Python Exercism practice exercise in this workspace so the "
        "included unit tests pass. Do not modify test files. Do not add extra packages. "
        "Keep the public function names. When tests pass, stop.\n\n"
        f"Exercise: {name}\n\n"
        f"{instructions}"
    )


def java_env() -> dict[str, str]:
    env = os.environ.copy()
    home = JAVA_HOME if JAVA_HOME.is_dir() else Path.home() / ".sdkman" / "candidates" / "java" / "17.0.9-tem"
    if home.is_dir():
        home = home.resolve()
        env["JAVA_HOME"] = str(home)
        env["PATH"] = str(home / "bin") + os.pathsep + env.get("PATH", "")
    return env


def run_python_tests(dest: Path, timeout: int = 60) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", str(dest), "-p", "*_test.py", "-q"],
            cwd=dest,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        # A hung test run fails this task; it must not take down the whole eval.
        return False, f"test run timed out after {timeout}s"
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return proc.returncode == 0, out[-2000:]


def run_java_tests(dest: Path, timeout: int = 300) -> tuple[bool, str]:
    gradlew = dest / "gradlew"
    if not gradlew.is_file():
        return False, "missing ./gradlew"
    env = java_env()
    java_bin = Path(env.get("JAVA_HOME", "")) / "bin" / "java"
    if not java_bin.is_file() and not shutil.which("java", path=env.get("PATH")):
        return False, "java not found (install a JDK or SDKMAN 17)"
    try:
        proc = subprocess.run(
            [str(gradlew), "test", "--rerun-tasks"],
            cwd=dest,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        # Gradle can hang on its daemon; fail this task, keep the eval going.
        return False, f"test run timed out after {timeout}s"
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return proc.returncode == 0, out[-2000:]


def run_tests(dest: Path, lang: str) -> tuple[bool, str]:
    if lang == "java":
        return run_java_tests(dest)
    return run_python_tests(dest)


def select_ids(
    limit: int = 10,
    ids: list[str] | None = None,
    *,
    catalog: tuple[str, ...] = PYTHON_EXERCISES,
    skip: list[str] | None = None,
) -> tuple[str, ...]:
    skip_set = set(skip or ())
    if ids:
        unknown = [name for name in ids if name not in catalog]
        if unknown:
            raise ValueError(f"unknown exercises: {unknown[:5]}")
        return tuple(name for name in ids if name not in skip_set)
    names = catalog if limit <= 0 else catalog[:limit]
    return tuple(name for name in names if name not in skip_set)


def evaluate_polyglot(
    config: AgentConfig,
    *,
    model: ModelClient,
    work_root: str | Path | None = None,
    cache: str | Path | None = None,
    limit: int = 10,
    ids: list[str] | None = None,
    skip: list[str] | None = None,
    replay: bool = False,
    lang: str = "python",
) -> EvalReport:
    lang = lang.strip().lower()
    catalog = CATALOGS.get(lang)
    suite = f"aider-polyglot-{lang}"
    if catalog is None:
        return EvalReport(suite=suite, outcomes=[], status="error", error=f"unknown lang {lang!r}")
    try:
        repo = ensure_repo(cache)
        names = select_ids(limit, ids, catalog=catalog, skip=skip)
    except Exception as exc:
        requested = tuple(ids) if ids else (catalog[:limit] if limit > 0 else catalog)
        requested = tuple(n for n in requested if n not in set(skip or ()))
        outcomes = [TaskOutcome(name, False, [], None, status="error") for name in requested]
        return EvalReport(suite=suite, outcomes=outcomes, status="error", error=str(exc))
    root = Path(work_root) if work_root else default_work_root(lang)
    root.mkdir(parents=True, exist_ok=True)
    eval_config = eval_agent_config(config, config.runs_dir, 20)
    progress = root / "progress.jsonl"
    outcomes: list[TaskOutcome] = []
    for name in names:
        src = exercise_src(repo, name, lang)
        live = root / name / "live"
        result: RunResult | None = None
        ok = False
        detail = ""
        tests_unchanged = True
        try:
            prepare_exercise(src, live, lang=lang)
            prompt = task_prompt(name, read_instructions(src), lang)
            protected_before = protected_tests_fingerprint(live, lang)
            result = run_agent(
                prompt,
                live,
                eval_config,
                model,
                extra_meta={"suite": suite, "task_id": name, "lang": lang},
            )
            ok, detail = run_tests(live, lang)
            tests_unchanged = protected_tests_fingerprint(live, lang) == protected_before
            if not tests_unchanged:
                ok = False
                detail = (detail + "\n" if detail else "") + "protected test files changed"
            outcome = TaskOutcome(
                task_id=name,
                resolved=ok,
                checks=[
                    CheckResult("tests", ok, detail or "ok"),
                    CheckResult("protected_tests_unchanged", tests_unchanged, "ok" if tests_unchanged else "tests changed"),
                ],
                run=result,
            )
        except Exception as exc:
            # One broken exercise (workspace prep, provider crash, hung test)
            # must not discard the outcomes collected so far.
            outcome = TaskOutcome(
                task_id=name,
                resolved=False,
                checks=[CheckResult("runner", False, str(exc))],
                run=result,
                status="error",
            )
        outcomes.append(outcome)
        with progress.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "task_id": name,
                        "resolved": ok,
                        "tests_unchanged": tests_unchanged,
                        "stop_reason": result.stop_reason if result else "",
                        "turns": result.turns if result else 0,
                        "run_id": result.run_id if result else "",
                        "status": outcome.status,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        print(
            f"[{'PASS' if ok else 'FAIL'}] {name} "
            f"turns={result.turns if result else 0} {result.stop_reason if result else outcome.status}",
            flush=True,
        )
        if replay and outcome.status == "complete" and result is not None and result.run_id:
            from wheel_agent.replay import replay_run

            replay_ws = root / name / "replay"
            prepare_exercise(src, replay_ws, lang=lang)
            replay_result = replay_run(eval_config.runs_dir, result.run_id, replay_ws, interactive=False)[1]
            replay_ok, _ = run_tests(replay_ws, lang)
            outcomes[-1].replay_status = replay_result.replay_status or "error"
            outcomes[-1].replay_match = outcomes[-1].replay_status == "exact"
    return EvalReport(suite=suite, outcomes=outcomes)
