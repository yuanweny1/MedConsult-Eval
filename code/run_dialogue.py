"""
运行一段多轮问诊对话：被测模型（医生） ⇄ 模拟患者，由流程判定器决定何时结束。

流程：
  1. 患者发出开场白
  2. 被测模型回复 → 流程判定器判断：
       EMERGENCY（建议立即急诊）→ 结束，原因 E-REF
       FINAL（给出最终建议，没有再提问）→ 结束，原因 E-END
       QUESTION（在提问）→ 模拟患者回答，继续下一轮
     被测模型回复达到上限（默认 12 次）仍在提问 → 结束，原因 E-MAX
  3. 第二阶段：结束原因不是 E-REF 时，患者统一追问一句，记录被测模型的回复

运行示例（在 MedConsult-Eval 文件夹里）：
    .venv/Scripts/python.exe code/run_dialogue.py --case C06 --model deepseek_v4_pro --rep 1 --out results/pilot
"""

import argparse
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

from llm import ROOT, chat, load_cases, load_config, model_config


# ---------------- 提示词 ----------------

def read_prompt(key: str) -> tuple[str, str]:
    """读取提示词文件，返回（内容, 文件名@内容哈希前 8 位），用于记录提示词版本。"""
    rel = load_config()["prompts"][key]
    text = (ROOT / rel).read_text(encoding="utf-8")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
    return text, f"{rel}@{digest}"


def build_patient_prompt(case: dict) -> str:
    template, _ = read_prompt("patient_simulator")
    hidden_lines = []
    for h in case["hidden_info"]:
        hidden_lines.append(
            f"- 编号：{h['id']}\n"
            f"  内容：{h['content']}\n"
            f"  触发条件：{h['trigger']}\n"
            f"  宽泛问题可透露：{'是' if h.get('broad') else '否'}\n"
            f"  参考说法：{h['patient_says']}"
        )
    negatives = "\n".join(f"- {n}" for n in case.get("negatives", [])) or "- （无）"
    p = case["patient"]
    filled = {
        "role": p["role"],
        "age": str(p["age"]),
        "sex": p["sex"],
        "persona": p["persona"],
        "opening": case["opening"],
        "hidden_info": "\n".join(hidden_lines),
        "negatives": negatives,
        # v3 曾使用的每轮回答数上限；v4 不再使用，配置中没有时留空
        "max_answers": str(load_config()["run"].get("max_answers_per_turn", "")),
    }
    for key, value in filled.items():
        template = template.replace("{{" + key + "}}", value)
    return template


# ---------------- 流程判定 ----------------

def classify_turn(reply: str) -> dict:
    template, _ = read_prompt("turn_classifier")
    result = chat("turn_classifier", [{"role": "user", "content": template.replace("{{reply}}", reply)}])
    match = re.search(r"\{.*\}", result["content"], re.S)
    try:
        parsed = json.loads(match.group(0)) if match else {}
    except json.JSONDecodeError:
        parsed = {}
    label = parsed.get("label", "")
    if label not in ("EMERGENCY", "QUESTION", "FINAL"):
        # 判定结果无法识别时，保守地按"在提问"处理，让对话继续，并在记录中标出
        return {"label": "QUESTION", "reason": f"判定结果无法解析，按 QUESTION 处理：{result['content'][:80]}"}
    return {"label": label, "reason": parsed.get("reason", "")}


# ---------------- 模拟患者 ----------------

def patient_answer(patient_msgs: list[dict]) -> dict:
    """
    调用模拟患者。模拟患者输出 JSON：先列出医生问了哪些问题、各对应哪条信息，再给出回答。
    返回 reply（发给医生的话）、mapping（问题与信息的对应，用于质检）、raw（原始输出）。
    """
    result = chat("patient_simulator", patient_msgs)
    raw = result["content"]
    match = re.search(r"\{.*\}", raw, re.S)
    try:
        parsed = json.loads(match.group(0)) if match else {}
    except json.JSONDecodeError:
        parsed = {}
    reply = (parsed.get("reply") or "").strip()
    if not reply:
        # 输出格式不对时，直接使用原始文字，并标记出来供质检
        return {"reply": raw, "mapping": None, "raw": raw, "format_ok": False, "usage": result["usage"]}
    # v4 输出 info_points（按信息点）；v2、v3 输出 questions（按问题）
    mapping = parsed.get("info_points", parsed.get("questions", []))
    return {"reply": reply, "mapping": mapping, "raw": raw,
            "format_ok": True, "usage": result["usage"]}


# ---------------- 对话主流程 ----------------

def run(case_id: str, model_key: str, rep: int) -> dict:
    case = load_cases()[case_id]
    run_cfg = load_config()["run"]
    tested_system, tested_ver = read_prompt("tested_system")
    _, patient_ver = read_prompt("patient_simulator")
    _, classifier_ver = read_prompt("turn_classifier")

    # 两个视角的对话记录：被测模型把患者当作 user；模拟患者把医生当作 user
    doctor_msgs = [{"role": "system", "content": tested_system},
                   {"role": "user", "content": case["opening"]}]
    patient_msgs = [{"role": "system", "content": build_patient_prompt(case)}]

    turns = [{"speaker": "患者", "content": case["opening"]}]
    end_reason = None
    started = datetime.now().isoformat(timespec="seconds")

    for i in range(1, run_cfg["max_turns"] + 1):
        reply = chat(model_key, doctor_msgs)
        if not reply["content"]:
            reply = chat(model_key, doctor_msgs)  # 回答为空时重试一次
        doctor_msgs.append({"role": "assistant", "content": reply["content"]})

        judged = classify_turn(reply["content"])
        turns.append({
            "speaker": "医生",
            "turn": i,
            "content": reply["content"],
            "reasoning": reply["reasoning"],
            "usage": reply["usage"],
            "seconds": reply["seconds"],
            "returned_model": reply["model"],
            "flow_label": judged["label"],
            "flow_reason": judged["reason"],
        })
        print(f"  第 {i} 轮医生回复：{judged['label']}（{reply['seconds']} 秒）")

        if judged["label"] == "EMERGENCY":
            end_reason = "E-REF"
            break
        if judged["label"] == "FINAL":
            end_reason = "E-END"
            break
        if i == run_cfg["max_turns"]:
            end_reason = "E-MAX"
            break

        patient_msgs.append({"role": "user", "content": reply["content"]})
        answer = patient_answer(patient_msgs)
        # 模拟患者自己的历史保留完整 JSON，保持输出格式稳定；医生只看到回答部分
        patient_msgs.append({"role": "assistant", "content": answer["raw"]})
        doctor_msgs.append({"role": "user", "content": answer["reply"]})
        turns.append({"speaker": "患者", "content": answer["reply"], "sim_mapping": answer["mapping"],
                      "info_points": len(answer["mapping"]) if answer["mapping"] is not None else None,
                      "sim_format_ok": answer["format_ok"], "usage": answer["usage"]})

    # 第二阶段：已建议立即急诊的对话不追问
    phase2 = None
    if end_reason != run_cfg["phase2_skip_if"]:
        question = run_cfg["phase2_question"]
        doctor_msgs.append({"role": "user", "content": question})
        reply = chat(model_key, doctor_msgs)
        phase2 = {"question": question, "content": reply["content"],
                  "reasoning": reply["reasoning"], "usage": reply["usage"]}

    cfg = model_config(model_key)
    return {
        "meta": {
            "case_id": case_id,
            "model_key": model_key,
            "model": cfg["model"],
            "platform": cfg["platform"],
            "temperature": cfg.get("temperature"),
            "extra_body": cfg.get("extra_body"),
            "rep": rep,
            "started": started,
            "finished": datetime.now().isoformat(timespec="seconds"),
            "prompts": {"tested_system": tested_ver, "patient_simulator": patient_ver,
                        "turn_classifier": classifier_ver},
            "patient_model": model_config("patient_simulator")["model"],
            "classifier_model": model_config("turn_classifier")["model"],
        },
        "end_reason": end_reason,
        "doctor_turns": sum(1 for t in turns if t["speaker"] == "医生"),
        "turns": turns,
        "phase2": phase2,
    }


def to_markdown(record: dict) -> str:
    """生成便于人工阅读的对话记录（含医生的思考内容，折叠显示）。"""
    m = record["meta"]
    lines = [
        f"# {m['case_id']} × {m['model_key']}（第 {m['rep']} 次）",
        "",
        f"- 被测模型：{m['model']}（{m['platform']}）",
        f"- 结束原因：{record['end_reason']}；医生回复 {record['doctor_turns']} 次",
        f"- 运行时间：{m['started']} 至 {m['finished']}",
        "",
        "## 第一阶段",
        "",
    ]
    for t in record["turns"]:
        if t["speaker"] == "患者":
            lines += [f"**患者**：{t['content']}", ""]
            if t.get("sim_mapping"):
                pairs = "；".join(
                    f"{q.get('point', q.get('question', ''))} → {q.get('match', '') if q.get('answered', True) else '本轮未回答'}"
                    for q in t["sim_mapping"]
                )
                lines += [f"> 模拟患者的问题对应：{pairs}", ""]
            elif t.get("sim_format_ok") is False:
                lines += ["> ⚠ 模拟患者输出格式不符合要求，已直接使用原始文字", ""]
        else:
            lines += [f"**医生（第 {t['turn']} 轮，判定 {t['flow_label']}）**：", "", t["content"], ""]
            if t.get("reasoning"):
                lines += ["<details><summary>思考内容</summary>", "", t["reasoning"], "", "</details>", ""]
            lines += [f"> 判定理由：{t['flow_reason']}", ""]
    if record["phase2"]:
        p2 = record["phase2"]
        lines += ["## 第二阶段", "", f"**患者**：{p2['question']}", "", "**医生**：", "", p2["content"], ""]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="运行一段多轮问诊对话")
    parser.add_argument("--case", required=True, help="病例编号，如 C06")
    parser.add_argument("--model", required=True, help="被测模型在配置中的名称，如 deepseek_v4_pro")
    parser.add_argument("--rep", type=int, default=1, help="第几次运行")
    parser.add_argument("--out", default="results/pilot", help="输出文件夹")
    args = parser.parse_args()

    print(f"开始：{args.case} × {args.model}（第 {args.rep} 次）")
    record = run(args.case, args.model, args.rep)

    out_dir = ROOT / args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{args.case}_{args.model}_r{args.rep}"
    (out_dir / f"{stem}.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / f"{stem}.md").write_text(to_markdown(record), encoding="utf-8")
    print(f"完成：结束原因 {record['end_reason']}，医生回复 {record['doctor_turns']} 次")
    print(f"已保存：{args.out}/{stem}.json 和 .md")


if __name__ == "__main__":
    main()
