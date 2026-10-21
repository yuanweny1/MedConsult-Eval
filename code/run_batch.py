"""
批量运行多轮问诊对话，带版本锁定和断点续跑。

版本锁定：新建运行时，把全部运行条件写入锁定清单 manifest.json：
  - Git 提交编号（要求工作区没有未提交的修改）
  - 病例、配置、全部提示词、标注规范、代码文件的内容哈希
  - 被测模型、模拟患者、流程判定器的模型 ID 和关键参数
断点续跑时，重新计算当前条件并与锁定清单逐项比对，任何一项不一致都拒绝运行，避免同一批次混入不同配置。

断点续跑：已经完成的对话（结果文件已存在）直接跳过，不会被覆盖；出错的对话记入 errors.log，可重新续跑补齐。

运行方法（在 MedConsult-Eval 文件夹里）：
    新建运行：  .venv/Scripts/python.exe code/run_batch.py --new
    续跑：      .venv/Scripts/python.exe code/run_batch.py --resume 运行ID
    只看计划：  .venv/Scripts/python.exe code/run_batch.py --new --dry-run
"""

import argparse
import hashlib
import json
import subprocess
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from llm import ROOT, load_cases, load_config, model_config
from run_dialogue import run, to_markdown

TESTED_MODELS = ["baichuan_m3", "deepseek_v4_pro"]
SUPPORT_MODELS = ["patient_simulator", "turn_classifier"]

# 需要锁定内容的文件（提示词文件从配置中读取，另行加入）
LOCKED_FILES = [
    "cases/cases_v1.yaml",
    "config/models.yaml",
    "guidelines/annotation_guideline_v1.md",
    "code/llm.py",
    "code/run_dialogue.py",
    "code/run_batch.py",
]


def sha256(path: Path) -> str:
    # 统一换行符后再计算，避免 Git 在 Windows 上转换换行符导致哈希不同
    data = path.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8").stdout.strip()


def current_conditions(run_cfg: dict) -> dict:
    """计算当前的全部运行条件，用于写入或比对锁定清单。"""
    cfg = load_config()
    files = list(LOCKED_FILES) + list(cfg["prompts"].values())
    models = {}
    for key in TESTED_MODELS + SUPPORT_MODELS:
        m = model_config(key)
        models[key] = {"model": m["model"], "platform": m["platform"],
                       "temperature": m.get("temperature"), "extra_body": m.get("extra_body")}
    return {
        "git_commit": git("rev-parse", "HEAD"),
        "files": {f: sha256(ROOT / f) for f in sorted(files)},
        "models": models,
        "run_params": {k: cfg["run"][k] for k in sorted(cfg["run"]) if k != "record_fields"},
        "plan": run_cfg,
    }


def plan_tasks(run_cfg: dict) -> list[tuple[str, str, int]]:
    return [(c, m, r) for c in run_cfg["cases"] for m in run_cfg["models"] for r in range(1, run_cfg["reps"] + 1)]


def main() -> None:
    parser = argparse.ArgumentParser(description="批量运行多轮问诊对话（版本锁定、断点续跑）")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--new", action="store_true", help="新建一次运行")
    group.add_argument("--resume", metavar="运行ID", help="续跑已有的运行")
    parser.add_argument("--reps", type=int, help="每个病例、每个模型运行几次（默认取配置 runs_per_case）")
    parser.add_argument("--cases", nargs="*", help="只运行指定病例（默认全部）")
    parser.add_argument("--models", nargs="*", choices=TESTED_MODELS, help="只运行指定被测模型（默认全部）")
    parser.add_argument("--workers", type=int, default=4, help="同时运行的对话数")
    parser.add_argument("--dry-run", action="store_true", help="只显示运行计划和锁定条件，不调用模型")
    args = parser.parse_args()

    runs_root = ROOT / "results" / "runs"

    if args.new:
        if git("status", "--porcelain"):
            raise SystemExit("拒绝运行：工作区有未提交的修改。请先提交，确保锁定清单对应一个确定的 Git 版本。")
        run_cfg = {
            "cases": args.cases or list(load_cases()),
            "models": args.models or TESTED_MODELS,
            "reps": args.reps or load_config()["run"]["runs_per_case"],
        }
        run_id = datetime.now().strftime("run_%Y%m%d_%H%M%S")
        run_dir = runs_root / run_id
        conditions = current_conditions(run_cfg)
        manifest = {"run_id": run_id, "created": datetime.now().isoformat(timespec="seconds"), **conditions}
        if args.dry_run:
            print(json.dumps(manifest, ensure_ascii=False, indent=2))
            print(f"\n计划：{len(plan_tasks(run_cfg))} 段对话（未实际运行）")
            return
        run_dir.mkdir(parents=True)
        (run_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已新建运行 {run_id}，锁定清单已保存")
    else:
        run_id = args.resume
        run_dir = runs_root / run_id
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        run_cfg = manifest["plan"]
        now = current_conditions(run_cfg)
        diffs = [k for k in ("git_commit", "files", "models", "run_params") if now[k] != manifest[k]]
        if diffs:
            detail = []
            if "files" in diffs:
                detail = [f for f in manifest["files"] if now["files"].get(f) != manifest["files"][f]]
            raise SystemExit(f"拒绝续跑：当前运行条件与锁定清单不一致（{', '.join(diffs)}）{('：' + ', '.join(detail)) if detail else ''}")
        print(f"续跑 {run_id}：运行条件与锁定清单一致")
        if args.dry_run:
            return

    tasks = plan_tasks(run_cfg)
    todo = [t for t in tasks if not (run_dir / f"{t[0]}_{t[1]}_r{t[2]}.json").exists()]
    print(f"共 {len(tasks)} 段，已完成 {len(tasks) - len(todo)} 段，本次运行 {len(todo)} 段（同时 {args.workers} 段）")

    lock = threading.Lock()
    done, failed = 0, 0

    def one(task: tuple[str, str, int]) -> str:
        case_id, model_key, rep = task
        record = run(case_id, model_key, rep)
        record["meta"]["run_id"] = run_id
        stem = f"{case_id}_{model_key}_r{rep}"
        # 先写临时文件再改名，避免中断时留下不完整的结果文件
        tmp = run_dir / f"{stem}.json.tmp"
        tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        (run_dir / f"{stem}.md").write_text(to_markdown(record), encoding="utf-8")
        tmp.replace(run_dir / f"{stem}.json")
        return f"{stem}：{record['end_reason']}，医生回复 {record['doctor_turns']} 次"

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(one, t): t for t in todo}
        for fut in as_completed(futures):
            task = futures[fut]
            with lock:
                try:
                    msg = fut.result()
                    done += 1
                    print(f"[{done + failed}/{len(todo)}] 完成 {msg}")
                except Exception as e:
                    failed += 1
                    stem = f"{task[0]}_{task[1]}_r{task[2]}"
                    print(f"[{done + failed}/{len(todo)}] 出错 {stem}：{type(e).__name__}: {str(e)[:150]}")
                    with open(run_dir / "errors.log", "a", encoding="utf-8") as f:
                        f.write(f"{datetime.now().isoformat(timespec='seconds')} {stem}\n{traceback.format_exc()}\n")

    remaining = sum(1 for t in tasks if not (run_dir / f"{t[0]}_{t[1]}_r{t[2]}.json").exists())
    print(f"\n本次完成 {done} 段，出错 {failed} 段；该运行还剩 {remaining} 段未完成")
    if remaining:
        print(f"续跑命令：.venv/Scripts/python.exe code/run_batch.py --resume {run_id}")


if __name__ == "__main__":
    main()
