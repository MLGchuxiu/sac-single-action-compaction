#!/usr/bin/env python3
"""Matrix runner for the SAC context-compression study.

Example:
    python3 harness/run.py --models qwen3.7-flash --regimes sac --tasks code_edit --verbose
    python3 harness/run.py --models qwen3.7-flash,deepseek-v4.1-flash \
        --regimes fha,eocc,sac --state-versions v1,v2 --repeats 1
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sac.agent import INPUT_BUDGET, OUTPUT_BUDGET, Runner  # noqa: E402
from sac.llm import Client  # noqa: E402
from sac.tasks import TASKS  # noqa: E402
from sac.tokens import TokenMeter  # noqa: E402

DEFAULT_MODELS = "qwen3.7-flash,deepseek-v4.1-flash"
DEFAULT_TASKS = "code_edit,doc_read,multimodal,long_output,state_retention"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", default=DEFAULT_MODELS)
    parser.add_argument("--regimes", default="fha,eocc,sac")
    parser.add_argument("--state-versions", default="v1")
    parser.add_argument("--tasks", default=DEFAULT_TASKS)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--max-actions", type=int, default=24)
    parser.add_argument("--eocc-threshold", type=int, default=96_000)
    parser.add_argument("--state-budget", type=int, default=2048)
    parser.add_argument("--input-budget", type=int, default=INPUT_BUDGET)
    parser.add_argument("--output-budget", type=int, default=OUTPUT_BUDGET)
    parser.add_argument("--out", default="results")
    parser.add_argument("--label", default="")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--force", action="store_true", help="rerun cells already present")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    models = [item.strip() for item in args.models.split(",") if item.strip()]
    regimes = [item.strip() for item in args.regimes.split(",") if item.strip()]
    tasks = [item.strip() for item in args.tasks.split(",") if item.strip()]
    versions = [item.strip() for item in args.state_versions.split(",") if item.strip()]

    unknown = [task for task in tasks if task not in TASKS]
    if unknown:
        raise SystemExit(f"unknown tasks: {unknown}")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    result_path = out_dir / f"runs_{args.label or stamp}.jsonl"

    done: set[str] = set()
    if result_path.exists() and not args.force:
        for line in result_path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
                done.add(record["key"])
            except (json.JSONDecodeError, KeyError):
                continue

    cells = []
    for model in models:
        for regime in regimes:
            for task_id in tasks:
                for version in (versions if regime == "sac" else ["-"]):
                    for repeat in range(args.repeats):
                        key = f"{model}|{regime}|{task_id}|{version}|{repeat}"
                        cells.append((key, model, regime, task_id, version, repeat))

    pending = [cell for cell in cells if args.force or cell[0] not in done]
    print(f"{len(cells)} cells, {len(pending)} to run -> {result_path}", flush=True)

    client = Client()
    meter = TokenMeter()
    lock = __import__("threading").Lock()
    failures = 0

    def execute(cell):
        key, model, regime, task_id, version, repeat = cell
        task = TASKS[task_id]
        workdir = Path(str(task.workdir) + f"_{model.replace('/', '_')}_{regime}_{version}_{repeat}")
        scoped = type(task)(id=task.id, prompt=task.prompt, input_details=task.input_details,
                            workdir=workdir, setup=task.setup, verify=task.verify,
                            category=task.category, max_actions=task.max_actions)
        runner = Runner(client, meter, model=model, regime=regime, state_version=version,
                        input_budget=args.input_budget, output_budget=args.output_budget,
                        max_actions=min(args.max_actions, task.max_actions), eocc_threshold=args.eocc_threshold,
                        state_budget=args.state_budget, verbose=args.verbose)
        started = time.monotonic()
        try:
            result = runner.run(scoped)
            payload = result.as_dict()
        except Exception as error:  # noqa: BLE001 - a failed cell must not stop the matrix
            payload = {"task_id": task_id, "model": model, "regime": regime, "state_version": version,
                       "ok": False, "score": 0.0, "detail": f"harness error: {error}",
                       "trace": traceback.format_exc()[-2000:], "wall_s": round(time.monotonic() - started, 2)}
        payload.update({"key": key, "repeat": repeat, "run_at": stamp,
                        "limits": {"input": args.input_budget, "output": args.output_budget}})
        with lock:
            with result_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        status = "ok " if payload.get("ok") else "FAIL"
        print(f"[{status}] {model} {regime} {task_id} {version} r{repeat} "
              f"score={payload.get('score')} peak={payload.get('peak_prompt_tokens')} "
              f"req={payload.get('requests')} comp={payload.get('compactions')} "
              f"{payload.get('detail', '')[:110]}", flush=True)
        return payload

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(execute, cell): cell for cell in pending}
        for future in as_completed(futures):
            if future.exception():
                failures += 1
                print("cell crashed:", future.exception(), flush=True)

    print(f"done; {failures} crashed cell(s); results at {result_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
