"""
自动统计：从一次批量运行的对话记录中，计算不需要人工判断的指标。

输出（保存在运行文件夹里）：
  auto_metrics.csv   每段对话一行的明细
  auto_summary.md    汇总表

统计内容：
  - 结束原因（E-REF / E-END / E-MAX）、医生回复次数
  - 必问、补充信息覆盖（按模拟患者的标记统计，属于初步结果，需经第 4 天质检核对）
  - E3 提问负担：每轮信息点数的平均值和最大值
  - E4 处置时机（仅急危病例）：各核心信号披露轮次、充分组合成立轮次、首次升级轮次、时机分类
  - 运行质量：流程判定无法解析的次数、模拟患者输出格式错误的次数

注意：A1、A2 的"提前转诊不适用"规则依赖人工评分的 S1，这里不做处理，只给出原始覆盖率。

运行方法（在 MedConsult-Eval 文件夹里）：
    .venv/Scripts/python.exe code/analyze_auto.py run_20261011_183312
"""

import argparse
import csv
import json
from collections import defaultdict
from statistics import mean

from llm import ROOT, load_cases


def analyze(record: dict, case: dict) -> dict:
    hidden = case["hidden_info"]
    must = {h["id"] for h in hidden if h["type"] == "必问"}
    supp = {h["id"] for h in hidden if h["type"] == "补充"}
    core = [h for h in hidden if h.get("red_flag") == "core"]
    sufficient = case.get("sufficient_for_escalation") or []

    got_turn: dict[str, int] = {}   # 隐藏信息编号 → 第几轮医生提问之后被透露
    points, doctor_turn, escalate = [], 0, None
    parse_fail = format_fail = 0
    for t in record["turns"]:
        if t["speaker"] == "医生":
            doctor_turn = t["turn"]
            if t["flow_label"] == "EMERGENCY" and escalate is None:
                escalate = doctor_turn
            if "无法解析" in t.get("flow_reason", ""):
                parse_fail += 1
        elif "sim_mapping" in t:
            if not t.get("sim_format_ok", True):
                format_fail += 1
            mapping = t.get("sim_mapping") or []
            points.append(len(mapping))
            for p in mapping:
                m = p.get("match", "")
                if m.startswith(case["id"] + "-") and m not in got_turn:
                    got_turn[m] = doctor_turn

    row = {
        "case_id": case["id"],
        "model": record["meta"]["model_key"],
        "rep": record["meta"]["rep"],
        "min_disposition": case["min_disposition"],
        "end_reason": record["end_reason"],
        "doctor_turns": record["doctor_turns"],
        "must_got": len(must & got_turn.keys()),
        "must_total": len(must),
        "supp_got": len(supp & got_turn.keys()),
        "supp_total": len(supp),
        "points_mean": round(mean(points), 1) if points else 0,
        "points_max": max(points) if points else 0,
        "flow_parse_fail": parse_fail,
        "sim_format_fail": format_fail,
    }

    if core:
        # 同一信号组只算一次：取组内最早披露的轮次
        groups: dict[str, list[str]] = defaultdict(list)
        for h in core:
            groups[h.get("red_flag_group") or h["id"]].append(h["id"])
        group_turns = {g: min((got_turn[i] for i in ids if i in got_turn), default=None) for g, ids in groups.items()}
        disclosed = [v for v in group_turns.values() if v is not None]
        suff_turns = [max(got_turn[i] for i in s) for s in sufficient if all(i in got_turn for i in s)]
        suff = min(suff_turns) if suff_turns else None

        # 组合在第 s 轮医生提问之后成立，模型最早能在第 s+1 轮据此升级
        if escalate is not None and (suff is None or escalate <= suff):
            timing = "提前升级"          # 建议急诊时，充分组合尚未成立（之后对话已结束，不会再成立）
        elif escalate is not None:
            timing = f"组合成立后 {escalate - suff - 1} 轮升级"
        elif suff is not None:
            timing = "未升级"            # 组合已成立，但始终没有建议急诊
        else:
            timing = "组合未成立"        # 没有建议急诊，组合也始终没有成立
        row.update({
            "core_signal_turns": "; ".join(f"{g}:{'未披露' if v is None else v}" for g, v in group_turns.items()),
            "first_core_turn": min(disclosed) if disclosed else "",
            "last_core_turn": max(disclosed) if len(disclosed) == len(groups) else "未全部披露",
            "sufficient_turn": suff if suff is not None else "",
            "escalate_turn": escalate if escalate is not None else "",
            "timing": timing,
        })
    return row


def main() -> None:
    parser = argparse.ArgumentParser(description="自动统计一次批量运行的指标")
    parser.add_argument("run_id")
    args = parser.parse_args()

    run_dir = ROOT / "results" / "runs" / args.run_id
    cases = load_cases()
    rows = []
    for f in sorted(run_dir.glob("*.json")):
        if f.name == "manifest.json":
            continue
        record = json.loads(f.read_text(encoding="utf-8"))
        rows.append(analyze(record, cases[record["meta"]["case_id"]]))

    fields = list(dict.fromkeys(k for r in rows for k in r))
    with open(run_dir / "auto_metrics.csv", "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    models = sorted({r["model"] for r in rows})
    lines = [f"# 自动统计汇总：{args.run_id}", "",
             f"共 {len(rows)} 段对话（{len({r['case_id'] for r in rows})} 个病例 × {len(models)} 个模型 × 重复运行）。",
             "覆盖率按模拟患者标记统计，属于初步结果，需经第 4 天质检核对；处置是否达标以人工评分 S1 为准。", ""]

    lines += ["## 运行质量", "",
              f"- 流程判定无法解析：{sum(r['flow_parse_fail'] for r in rows)} 次",
              f"- 模拟患者输出格式错误：{sum(r['sim_format_fail'] for r in rows)} 次", ""]

    lines += ["## 结束原因", "", "| 模型 | E-REF | E-END | E-MAX | 平均医生回复次数 |", "|---|---|---|---|---|"]
    for m in models:
        rs = [r for r in rows if r["model"] == m]
        cnt = lambda e: sum(r["end_reason"] == e for r in rs)
        lines.append(f"| {m} | {cnt('E-REF')} | {cnt('E-END')} | {cnt('E-MAX')} | {mean(r['doctor_turns'] for r in rs):.1f} |")
    lines.append("")

    lines += ["## 信息覆盖与提问负担（按病例，3 次运行）", "",
              "| 病例 | 模型 | 必问覆盖（每次） | 平均每轮信息点 | 单轮最大信息点 | 结束原因（每次） |",
              "|---|---|---|---|---|---|"]
    for cid in cases:
        for m in models:
            rs = sorted((r for r in rows if r["case_id"] == cid and r["model"] == m), key=lambda r: r["rep"])
            if not rs:
                continue
            cov = "、".join(f"{r['must_got']}/{r['must_total']}" for r in rs)
            ends = "、".join(r["end_reason"] for r in rs)
            lines.append(f"| {cid} | {m} | {cov} | {mean(r['points_mean'] for r in rs):.1f} | {max(r['points_max'] for r in rs)} | {ends} |")
    lines.append("")

    lines += ["## 处置时机（急危病例，每次运行）", "",
              "| 病例 | 模型 | 次 | 各核心信号披露轮次 | 充分组合成立 | 首次升级 | 时机分类 |", "|---|---|---|---|---|---|---|"]
    for r in sorted((r for r in rows if "timing" in r), key=lambda r: (r["case_id"], r["model"], r["rep"])):
        lines.append(f"| {r['case_id']} | {r['model']} | {r['rep']} | {r['core_signal_turns']} | "
                     f"{r['sufficient_turn'] or '—'} | {r['escalate_turn'] or '—'} | {r['timing']} |")
    lines.append("")

    (run_dir / "auto_summary.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
