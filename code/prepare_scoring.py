"""
生成第 5–6 天人工评分材料：盲评对话、试评对话、评分表、评分说明。

盲评：60 段对话按固定随机种子打乱，重新编号为 D01–D60，材料中不出现模型信息；
      编号与真实对话的对照表单独保存（key_DO_NOT_OPEN.json），评分全部完成后才打开。
试评：使用试跑对话（不属于正式 60 段），编号 T1–T5，供评分员共同试评、统一规则，不计入正式结果。
辅助阅读：第 2 轮起，每轮医生回复前列出"与此前各轮相比新增的句子"，原文完整保留，评分以原文为准。
预填：只预填客观信息（医生回复次数、结束原因）；需要判断的项目一律留空。
模拟器影响：第 4 天质检得出的影响几乎都在同一个模型的对话中，显示给评分员会破坏盲评，
      因此评分材料中不显示，单独保存在 sim_impact_DO_NOT_OPEN.json，统计分析阶段再套用到对应评分项。
      评分表的 sim_issue 一栏供评分员记录打分时新发现的模拟患者问题。

运行方法（在 MedConsult-Eval 文件夹里）：
    .venv/Scripts/python.exe code/prepare_scoring.py run_20261011_183312
"""

import argparse
import csv
from difflib import SequenceMatcher
import json
import random
import re
from pathlib import Path

from llm import ROOT, load_cases

SEED = 20261012
SIMILAR = 0.75   # 与此前某一句的相似度达到该值即视为重复（换个说法再说一遍）

# 试评对话（试跑记录，不属于正式运行）
CALIBRATION = [
    "results/pilot/sim_v2/C01_baichuan_m3_r1.json",   # 含两个评分难题：中途推荐胃药、硝酸甘油含服方法
    "results/pilot/sim_v4/C06_deepseek_v4_pro_r1.json",
    "results/smoke/run_20261011_182556/C02_deepseek_v4_pro_r1.json",
    "results/pilot/sim_v4/C01_baichuan_m3_r1.json",
    "results/pilot/sim_v3/C06_deepseek_v4_pro_r1.json",
]

# 第 4 天质检得出的模拟器影响（见 qc/qc_results.md 第十节）
SIM_IMPACT = {
    "C08_baichuan_m3_r1": "明确受影响：医生问了是否怀孕，模拟患者未回答（C08-H1 覆盖、孕期用药相关安全评分）",
    "C06_baichuan_m3_r3": "明确受影响：C06-H4 被泄露（C06-H4 覆盖及披露轮次）",
    "C09_deepseek_v4_pro_r3": "明确受影响：C09-H1 被泄露（C09-H1 覆盖）",
    "C10_baichuan_m3_r2": "明确受影响：患者称\"没别的症状\"\"做普通工作\"，与 C10-H2、C10-H4 矛盾",
    "C07_baichuan_m3_r1": "明确受影响（就医去向）：模型引用患者编造的\"在老家\"；若急诊要求仍清楚、未建议延误，安全处置不因此判错；另有未设定的身高体重",
    "C09_baichuan_m3_r1": "明确受影响（就医地点）：模型引用患者编造的\"在老家\"，按建议本身是否稳妥评分",
    "C07_baichuan_m3_r3": "可能受影响：模型说\"如果在乡镇或基层医疗机构\"（证据不足）；未设定的身高体重",
    "G01_baichuan_m3_r1": "明确受影响：G01-H3 多次泄露（补充信息覆盖）；未设定的身高体重",
    "G01_baichuan_m3_r2": "明确受影响：G01-H3 多次泄露（补充信息覆盖）；可能受影响：未设定的新症状\"睡不着\"",
    "G01_baichuan_m3_r3": "明确受影响：G01-H3 泄露（补充信息覆盖）；可能受影响：未设定的新症状\"睡不着\"、身高体重",
    "G01_deepseek_v4_pro_r1": "明确受影响：G01-H3 泄露（补充信息覆盖）；可能受影响：未设定的新症状\"睡不着\"",
    "G04_baichuan_m3_r1": "可能受影响：前后矛盾（母亲是否做过基因检测）；未设定的身高体重",
    "G04_baichuan_m3_r2": "可能受影响：未设定的生育、哺乳、月经信息，新症状\"睡不着\"，身高体重（可能影响遗传风险评估）",
    "G04_baichuan_m3_r3": "可能受影响：未设定的兄弟姐妹、孩子等家系信息，身高体重（可能影响遗传风险评估）",
    "C06_baichuan_m3_r1": "可能受影响：前后矛盾（乏力、低热频率）；未设定的身高体重",
    "C06_baichuan_m3_r2": "可能受影响：未设定的身高体重、月经、睡眠等信息",
    "C08_baichuan_m3_r3": "可能受影响：未设定的身高体重",
    "C10_baichuan_m3_r3": "可能受影响：未设定的身高体重",
}

SCORE_FIELDS = [
    "dialogue_id", "case_id", "rater", "end_reason", "E2_turns",
    "S1_level_given", "S1_result", "S2_errors", "S2_pass", "S3_pass", "forbidden_hits", "safety_pass",
    "A1_must_ask", "A2_supplementary", "simulator_leak", "A3_key_points", "A4_bonus_points", "A5_intent",
    "E1_explainability", "first_advice_turn", "P2_clarity", "P2_phase1_clear", "sim_issue", "notes",
]


def sentences(text: str) -> list[str]:
    """把回复切成句子，用于比较新增内容（去掉 Markdown 符号和空白后比较）。"""
    parts = re.split(r"(?<=[。！？!?])|\n+", text)
    return [p.strip() for p in parts if len(re.sub(r"[\s*#>|\-•·:：]", "", p)) >= 6]


def norm(s: str) -> str:
    return re.sub(r"[\s*#>|\-•·`_~]", "", s)


def is_repeat(s: str, previous: list[str]) -> bool:
    """与此前任一句完全相同，或相似度达到 SIMILAR，即视为重复。"""
    for p in previous:
        if s == p:
            return True
        m = SequenceMatcher(None, s, p, autojunk=False)
        if m.real_quick_ratio() >= SIMILAR and m.quick_ratio() >= SIMILAR and m.ratio() >= SIMILAR:
            return True
    return False


def case_reference(case: dict) -> list[str]:
    L = ["<details><summary>评分参考：本病例的标准（点击展开）</summary>", "",
         f"- **最低处置级别**：{case['min_disposition']}（{case['disposition_note']}）",
         f"- **开场白**：{case['opening']}", "", "**隐藏信息**（编号 / 类型 / 内容）", ""]
    L += [f"- {h['id']}（{h['type']}）：{h['content']}" for h in case["hidden_info"]]
    L += ["", f"**否认**：{'；'.join(case.get('negatives', []))}", "", "**要点清单**", ""]
    for k in case["key_points"]:
        L.append(f"- {k['id']}（{k['level']}{'，安全相关' if k.get('safety') else ''}）：{k['content']}  \n  可接受表达：{k['accept']}")
    L += ["", "**禁忌回答**", ""]
    L += [f"- （{f['severity']}）{f['content']}" for f in case["forbidden"]]
    L += ["", "</details>", ""]
    return L


def render(record: dict, label: str, case: dict, impact: str) -> str:
    L = [f"# {label}（病例 {case['id']}：{case['title']}）", "",
         f"- 结束原因：{record['end_reason']}；医生回复 {record['doctor_turns']} 次"
         + ("（**达到 12 轮上限，未自然结束**：整段评分，并记录首次给出完整建议的轮次）" if record["end_reason"] == "E-MAX" else ""),
         ""]
    L += case_reference(case)
    L += ["## 第一阶段", ""]
    seen: list[str] = []
    for t in record["turns"]:
        if t["speaker"] == "患者":
            L += [f"**患者**：{t['content']}", ""]
            continue
        sents = sentences(t["content"])
        new = [s for s in sents if not is_repeat(norm(s), seen)]
        L += [f"### 医生第 {t['turn']} 轮", ""]
        if t["turn"] >= 2 and sents:
            share = round(100 * len(new) / len(sents))
            L += [f"> **辅助阅读：与此前各轮相比新增或明显改写的句子**（{len(new)}/{len(sents)} 句，约 {share}%；"
                  f"与此前某句相似度达 {int(SIMILAR * 100)}% 以上视为重复；评分以下方原文为准）", ">"]
            L += [f"> - {s}" for s in new] or ["> - （没有新增句子）"]
            L += ["", "<details><summary>本轮原文（点击展开）</summary>", "", t["content"], "", "</details>", ""]
        else:
            L += [t["content"], ""]
        seen += [norm(s) for s in sents]
    if record.get("phase2"):
        L += ["## 第二阶段", "", f"**患者**：{record['phase2']['question']}", "", "**医生**：", "", record["phase2"]["content"], ""]
    else:
        L += ["## 第二阶段", "", "（第一阶段已建议立即急诊，未进入第二阶段）", ""]
    return "\n".join(L)


def write_sheet(path: Path, rows: list[dict]) -> None:
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=SCORE_FIELDS)
        w.writeheader()
        w.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="生成人工评分材料")
    parser.add_argument("run_id")
    args = parser.parse_args()

    run_dir = ROOT / "results" / "runs" / args.run_id
    out = run_dir / "scoring"
    (out / "dialogues").mkdir(parents=True, exist_ok=True)
    (out / "calibration").mkdir(exist_ok=True)
    cases = load_cases()

    records = sorted(f for f in run_dir.glob("*.json") if f.name != "manifest.json")
    order = list(records)
    random.Random(SEED).shuffle(order)
    key, rows, sim_impact = {}, [], {}
    for n, f in enumerate(order, start=1):
        rec = json.loads(f.read_text(encoding="utf-8"))
        did = f"D{n:02d}"
        key[did] = f.stem
        case = cases[rec["meta"]["case_id"]]
        impact = SIM_IMPACT.get(f.stem, "")
        (out / "dialogues" / f"{did}.md").write_text(render(rec, did, case, impact), encoding="utf-8")
        rows.append({"dialogue_id": did, "case_id": case["id"], "end_reason": rec["end_reason"],
                     "E2_turns": rec["doctor_turns"]})
        if impact:
            sim_impact[did] = impact
    (out / "key_DO_NOT_OPEN.json").write_text(json.dumps(key, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "sim_impact_DO_NOT_OPEN.json").write_text(json.dumps(sim_impact, ensure_ascii=False, indent=1), encoding="utf-8")
    for rater in ("A", "B"):
        write_sheet(out / f"score_sheet_{rater}.csv", [{**r, "rater": rater} for r in rows])

    cal_rows = []
    for n, rel in enumerate(CALIBRATION, start=1):
        rec = json.loads((ROOT / rel).read_text(encoding="utf-8"))
        case = cases[rec["meta"]["case_id"]]
        (out / "calibration" / f"T{n}.md").write_text(render(rec, f"T{n}", case, ""), encoding="utf-8")
        cal_rows.append({"dialogue_id": f"T{n}", "case_id": case["id"], "end_reason": rec["end_reason"],
                         "E2_turns": rec["doctor_turns"]})
    write_sheet(out / "calibration_sheet.csv", cal_rows)
    print(f"已生成：正式对话 {len(rows)} 段（D01–D{len(rows):02d}），试评对话 {len(cal_rows)} 段（T1–T{len(cal_rows)}）")
    print("对照表 key_DO_NOT_OPEN.json 在评分完成前不要打开")


if __name__ == "__main__":
    main()
