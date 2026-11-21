"""Parallel matrix runner over benchmark tasks and arms."""
from __future__ import annotations

import argparse
import json
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from .common import RUN_ROOT, WORK
from . import loong, swe, tau2

LOADERS = {
    "loong": loong.load_tasks,
    "swe": swe.load_tasks,
    "tau2": tau2.load_tasks,
}


def load(bench: str, limit: int | None):
    if bench not in LOADERS:
        raise SystemExit(f"benchmark not wired yet: {bench}")
    return LOADERS[bench](limit)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bench", default="loong")
    parser.add_argument("--arms", default="sac,fha")
    parser.add_argument("--iteration", default="iter0")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--tasks", default="", help="comma-separated task ids")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    benches = [item.strip() for item in args.bench.split(",") if item.strip()]
    arms = [item.strip() for item in args.arms.split(",") if item.strip()]
    wanted = {item.strip() for item in args.tasks.split(",") if item.strip()}

    tasks = []
    for bench in benches:
        tasks.extend(load(bench, args.limit))
    if wanted:
        tasks = [task for task in tasks if task.task_id in wanted]
    if not tasks:
        raise SystemExit("no tasks selected")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = WORK / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = Path(args.out) if args.out else out_dir / f"{args.iteration}_{stamp}.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    done: set[str] = set()
    if out_path.exists():
        for line in out_path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
                done.add(record["key"])
            except (json.JSONDecodeError, KeyError):
                continue

    cells = []
    for task in tasks:
        for arm in arms:
            for repeat in range(args.repeats):
                key = f"{task.bench}|{task.task_id}|{arm}|{repeat}"
                if key not in done:
                    cells.append((key, task, arm, repeat))
    print(f"{len(cells)} cells to run -> {out_path}", flush=True)

    lock = threading.Lock()
    started = time.monotonic()

    def execute(cell):
        key, task, arm, repeat = cell
        run_dir = RUN_ROOT / args.iteration / task.bench / f"{task.task_id}__{arm}__r{repeat}"
        try:
            from .runner import run_task
            record = run_task(task, arm, run_dir)
        except Exception as error:  # noqa: BLE001
            record = {"bench": task.bench, "task_id": task.task_id, "arm": arm,
                      "ok": False, "score": 0.0,
                      "detail": f"harness error: {error}",
                      "trace": traceback.format_exc()[-2000:], "metrics": {},
                      "meta": task.meta}
        record["key"] = key
        record["repeat"] = repeat
        record["iteration"] = args.iteration
        with lock:
            with out_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        metrics = record.get("metrics") or {}
        print(f"[{'ok  ' if record.get('ok') else 'FAIL'}] {key} "
              f"score={record.get('score')} peak={metrics.get('peak_context')} "
              f"req={metrics.get('requests')} comp={metrics.get('compactions')} "
              f"wall={metrics.get('wall_s')}", flush=True)
        return record

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(execute, cell) for cell in cells]
        for future in as_completed(futures):
            future.result()
    print(f"done in {round(time.monotonic() - started, 1)}s; results at {out_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())