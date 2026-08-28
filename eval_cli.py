from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from wheel_agent.config import load_config
from wheel_agent.evals.polyglot import CATALOGS, JAVA_HAND_DONE
from wheel_agent.evals.swe_lite import INSTANCE_IDS_CLASSIC5
from wheel_agent.model import make_client
from wheel_agent.polyglot import default_work_root, evaluate_polyglot
from wheel_agent.swe import default_swe_work_root, evaluate_swe


def run_swe(args: argparse.Namespace) -> int:
    ids = [part.strip() for part in args.ids.split(",") if part.strip()] or list(INSTANCE_IDS_CLASSIC5)
    if args.list:
        for iid in ids:
            print(iid)
        print(f"# {len(ids)} SWE-bench Lite instances", file=sys.stderr)
        return 0
    if shutil.which("docker") is None:
        print(
            "# no Docker: running the agent side only; predictions.jsonl is written and "
            "scored later on a Docker machine (EVALUATION.md)",
            file=sys.stderr,
        )
    config = load_config(interactive=False)
    work_root = Path(args.work_root) if args.work_root else default_swe_work_root()
    work_root.mkdir(parents=True, exist_ok=True)
    config.runs_dir = (work_root / "wheel_runs").resolve()
    print(
        f"# suite=swe-lite model={config.provider.model} effort={config.effort} "
        f"instances={len(ids)} work_root={work_root}",
        file=sys.stderr,
    )
    model = make_client(config.provider, effort=config.effort)
    report = evaluate_swe(config, model=model, work_root=work_root, instance_ids=ids, replay=not args.no_replay)
    sys.stdout.write(report.format())
    (work_root / "report.txt").write_text(report.format(), encoding="utf-8")
    failed = sum(1 for item in report.outcomes if not item.resolved)
    if report.status == "agent_only":
        return 0  # agent side complete; official scoring pending on Docker
    if report.status != "complete":
        return 2
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Wheel eval suites. polyglot = Aider/Exercism, local tests, no Docker. "
        "swe = SWE-bench Lite subset via the official Docker harness.",
    )
    sub = parser.add_subparsers(dest="suite", required=True)
    poly = sub.add_parser("polyglot", help="Aider polyglot (Exercism Python unittest / Java Gradle)")
    poly.add_argument("--lang", default="python", choices=sorted(CATALOGS), help="python or java")
    poly.add_argument("--limit", type=int, default=10, help="first N exercises; 0 = all")
    poly.add_argument("--ids", default="", help="comma-separated exercise ids")
    poly.add_argument("--skip", default="", help="comma-separated exercise ids to skip")
    poly.add_argument(
        "--remaining",
        action="store_true",
        help="java: skip the five exercises already passed by hand",
    )
    poly.add_argument("--work-root", default="", help="default ~/.wheel/eval/polyglot[-lang]-runs")
    poly.add_argument("--cache", default="", help="polyglot-benchmark clone cache")
    poly.add_argument("--replay", action="store_true")
    poly.add_argument("--list", action="store_true", help="print exercise ids and exit")
    swe = sub.add_parser("swe", help="SWE-bench Lite subset (official Docker harness)")
    swe.add_argument("--ids", default="", help="comma-separated instance ids (default: the classic 5)")
    swe.add_argument("--work-root", default="", help="default ~/.wheel/eval/swe-runs")
    swe.add_argument("--no-replay", action="store_true", help="skip the recorded-response replay check")
    swe.add_argument("--list", action="store_true", help="print instance ids and exit")
    args = parser.parse_args(argv)

    if args.suite == "swe":
        return run_swe(args)

    catalog = CATALOGS[args.lang]
    if args.list:
        for name in catalog:
            print(name)
        print(f"# {len(catalog)} {args.lang} exercises", file=sys.stderr)
        return 0

    config = load_config(interactive=False)
    if args.lang == "java":
        if "grok" not in config.providers:
            parser.error("java eval requires a grok provider in .env")
        config = config.with_provider("grok").with_effort("low")
        if "grok-4.6" not in config.provider.model:
            parser.error(f"java eval requires grok-4.6, got {config.provider.model}")
    ids = [part.strip() for part in args.ids.split(",") if part.strip()] or None
    skip = [part.strip() for part in args.skip.split(",") if part.strip()]
    if args.remaining:
        skip.extend(JAVA_HAND_DONE)
    work_root = Path(args.work_root) if args.work_root else default_work_root(args.lang)
    config.runs_dir = (work_root / "wheel_runs").resolve()
    print(
        f"# lang={args.lang} model={config.provider.model} effort={config.effort} "
        f"max_turns={config.max_turns} work_root={work_root}",
        file=sys.stderr,
    )
    model = make_client(config.provider, effort=config.effort)
    report = evaluate_polyglot(
        config,
        model=model,
        work_root=work_root,
        cache=Path(args.cache) if args.cache else None,
        limit=args.limit,
        ids=ids,
        skip=skip or None,
        replay=args.replay,
        lang=args.lang,
    )
    sys.stdout.write(report.format())
    report_path = work_root / "report.txt"
    report_path.write_text(report.format(), encoding="utf-8")
    failed = sum(1 for item in report.outcomes if not item.resolved)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
