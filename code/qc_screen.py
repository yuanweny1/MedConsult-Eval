"""
模拟患者质检：自动筛查（报警器）+ 分层抽样，生成人工核对表。

对一次运行中的每一轮患者回复做四项检查，任何一项报警即进入核对表：
  A 模糊词：回答原文出现"挺多""经常""严重"等模糊的程度或频率词
  B 隐藏信息片段：某条隐藏信息本轮没有标记、之前也没透露过，回答中却出现其参考说法中的片段
  C AI 事实核对：由 AI 评委模型（与模拟患者不同）逐句核对事实依据，报告疑似泄露、编造、矛盾
  D 标记异常：标记了病例中不存在的编号，或输出格式错误
自动检查都只是报警器，不作为判定依据；最终以人工核对为准（见 docs/qc_plan.md）。

抽样：报警的全部纳入；未报警的按"病例 × 模型"分层，每层随机抽取若干轮（随机种子固定）。

输出（保存在运行文件夹的 qc 子文件夹）：
  screen_all.json     全部患者回复轮次的检查结果
  qc_sheet.csv        人工核对表（Excel 可直接打开，空白列由人工填写）
  qc_review.md        核对用的阅读版（含病例要点、医生提问、患者回复、报警原因）

运行方法（在 MedConsult-Eval 文件夹里）：
    .venv/Scripts/python.exe code/qc_screen.py run_20261011_183312
"""

import argparse
import csv
import hashlib
import json
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from llm import ROOT, _client, load_cases, model_config

VAGUE_WORDS = ["挺多", "很多", "好多", "经常", "老是", "有一点", "有点多", "严重", "好几"]
CHECKER_PROMPT = "prompts/sim_fact_checker_v1.md"
SAMPLE_PER_STRATUM = 2
SEED = 20261011
# 质检核对单独指定思考档位：默认档位处理长提示词时可能长时间不返回，low 档会漏掉已知矛盾，high 档每轮约 5–70 秒
# 只在质检脚本中设置，不修改正式运行锁定的配置和代码
CHECKER_EFFORT = "high"


def fragments(text: str, n: int = 4) -> set[str]:
    """取参考说法中长度为 n 的连续片段（去掉标点和常见虚词），用于检查回答中是否出现。"""
    clean = re.sub(r"[，。！？、…（）()\"'“”‘’：；\s]|就是|这儿|有点|你这么一说|吧|了|的", "", text)
    return {clean[i:i + n] for i in range(len(clean) - n + 1)}


def case_text(case: dict) -> dict:
    p = case["patient"]
    return {
        "profile": f"{p['role']}，{p['age']}，{p['sex']}",
        "opening": case["opening"],
        "hidden_info": "\n".join(f"  - {h['id']}：{h['content']}（参考说法：{h['patient_says']}）" for h in case["hidden_info"]),
        "negatives": "\n".join(f"  - {n}" for n in case.get("negatives", [])) or "  - （无）",
    }


def ai_check(case: dict, previous: list[str], doctor: str, reply: str) -> list[dict]:
    template = (ROOT / CHECKER_PROMPT).read_text(encoding="utf-8")
    fill = {**case_text(case), "previous_patient": "\n".join(f"  - {s}" for s in previous) or "  - （无）",
            "doctor": doctor, "reply": reply}
    for k, v in fill.items():
        template = template.replace("{{" + k + "}}", v)
    cfg = model_config("judge")
    client = _client(cfg["api_key_env"], cfg["base_url_env"])
    resp = client.chat.completions.create(
        model=cfg["model"], messages=[{"role": "user", "content": template}], temperature=cfg.get("temperature"),
        timeout=240, extra_body={**(cfg.get("extra_body") or {}), "reasoning_effort": CHECKER_EFFORT})
    content = resp.choices[0].message.content or ""
    match = re.search(r"\{.*\}", content, re.S)
    try:
        return json.loads(match.group(0)).get("issues", []) if match else [{"type": "核对失败", "quote": "", "reason": "输出无法解析"}]
    except json.JSONDecodeError:
        return [{"type": "核对失败", "quote": "", "reason": "输出无法解析"}]


def collect_turns(run_dir: Path) -> list[dict]:
    cases = load_cases()
    items = []
    for f in sorted(run_dir.glob("*.json")):
        if f.name == "manifest.json":
            continue
        rec = json.loads(f.read_text(encoding="utf-8"))
        case = cases[rec["meta"]["case_id"]]
        hidden = {h["id"]: h for h in case["hidden_info"]}
        revealed: set[str] = set()
        previous: list[str] = [case["opening"]]
        doctor, k = "", 0
        for t in rec["turns"]:
            if t["speaker"] == "医生":
                doctor = t["content"]
                continue
            if "sim_mapping" not in t:
                continue
            k += 1
            mapping = t.get("sim_mapping") or []
            marks = {m.get("match", "") for m in mapping}
            flags = []
            vague = [w for w in VAGUE_WORDS if w in t["content"]]
            if vague:
                flags.append(f"A 模糊词：{'、'.join(vague)}")
            for hid, h in hidden.items():
                if hid in marks or hid in revealed:
                    continue
                hit = [frag for frag in fragments(h["patient_says"]) if frag in t["content"]]
                if hit:
                    flags.append(f"B 疑似泄露 {hid}：回答含'{hit[0]}'")
            unknown = [m for m in marks if re.match(r"[CG]\d\d-H\d", m) and m not in hidden]
            if unknown or not t.get("sim_format_ok", True):
                flags.append(f"D 标记异常：{'、'.join(unknown) or '输出格式错误'}")
            items.append({
                "uid": f"{f.stem}#P{k}",
                "case_id": case["id"], "model": rec["meta"]["model_key"], "rep": rec["meta"]["rep"],
                "patient_turn": k, "doctor": doctor, "reply": t["content"],
                "mapping": mapping, "previous": list(previous), "flags": flags,
            })
            revealed |= {m for m in marks if m in hidden}
            previous.append(t["content"])
    return items


def main() -> None:
    parser = argparse.ArgumentParser(description="模拟患者质检：自动筛查与分层抽样")
    parser.add_argument("run_id")
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()

    run_dir = ROOT / "results" / "runs" / args.run_id
    qc_dir = run_dir / "qc"
    qc_dir.mkdir(exist_ok=True)
    cases = load_cases()
    items = collect_turns(run_dir)
    print(f"共 {len(items)} 轮患者回复，开始 AI 事实核对（{model_config('judge')['model']}）……")

    # 边做边存：每完成一轮就写入缓存，再次运行时跳过已完成的（缓存文件与核对提示词版本、思考档位绑定）
    checker_hash = hashlib.sha256((ROOT / CHECKER_PROMPT).read_bytes().replace(b"\r\n", b"\n")).hexdigest()[:8]
    cache_file = qc_dir / f"ai_check_cache_{checker_hash}_{CHECKER_EFFORT}.jsonl"
    cache: dict[str, list] = {}
    if cache_file.exists():
        for line in cache_file.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            cache[rec["uid"]] = rec["issues"]
    print(f"缓存中已有 {len(cache)} 轮核对结果，本次需核对 {sum(1 for i in items if i['uid'] not in cache)} 轮")
    lock = threading.Lock()
    progress = {"done": 0}

    def check(item: dict) -> None:
        if item["uid"] in cache:
            issues = cache[item["uid"]]
        else:
            issues = []
            # 网络、超时等错误：间隔 5、15、30 秒重试；仍失败则标为"核对失败"，列入人工核对（宁可多查）
            for attempt, wait in enumerate([5, 15, 30, None], start=1):
                try:
                    issues = ai_check(cases[item["case_id"]], item["previous"], item["doctor"], item["reply"])
                    break
                except Exception as e:
                    if wait is None:
                        issues = [{"type": "核对失败", "quote": "",
                                   "reason": f"{type(e).__name__}，重试 {attempt - 1} 次后仍失败"}]
                    else:
                        time.sleep(wait)
            if not any(i.get("type") == "核对失败" for i in issues):
                with lock, open(cache_file, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"uid": item["uid"], "issues": issues}, ensure_ascii=False) + "\n")
        with lock:
            progress["done"] += 1
            if progress["done"] % 20 == 0:
                print(f"  已核对 {progress['done']}/{len(items)}")
        item["ai_issues"] = issues
        for i in issues:
            item["flags"].append(f"C AI核对-{i.get('type', '')}：{i.get('quote', '')[:30]}（{i.get('reason', '')[:50]}）")

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(check, items))

    flagged = [i for i in items if i["flags"]]
    unflagged = [i for i in items if not i["flags"]]
    rng = random.Random(SEED)
    strata: dict[tuple, list] = {}
    for i in unflagged:
        strata.setdefault((i["case_id"], i["model"]), []).append(i)
    sampled = []
    for key in sorted(strata):
        pool_items = sorted(strata[key], key=lambda x: x["uid"])
        sampled += rng.sample(pool_items, min(SAMPLE_PER_STRATUM, len(pool_items)))
    for i in flagged:
        i["sample_reason"] = "报警"
    for i in sampled:
        i["sample_reason"] = "分层随机抽样"
    review = sorted(flagged + sampled, key=lambda x: x["uid"])

    meta = {"run_id": args.run_id, "total_turns": len(items), "flagged": len(flagged), "unflagged": len(unflagged),
            "sampled_unflagged": len(sampled), "sample_per_stratum": SAMPLE_PER_STRATUM, "seed": SEED,
            "checker_model": model_config("judge")["model"], "checker_effort": CHECKER_EFFORT,
            "checker_prompt": f"{CHECKER_PROMPT}@{checker_hash}",
            "vague_words": VAGUE_WORDS}
    (qc_dir / "screen_all.json").write_text(
        json.dumps({"meta": meta, "items": [{k: v for k, v in i.items() if k != "previous"} for i in items]},
                   ensure_ascii=False, indent=2), encoding="utf-8")

    with open(qc_dir / "qc_sheet.csv", "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["编号", "病例", "模型", "第几次运行", "第几轮患者回复", "纳入原因", "自动报警",
                    "患者回复", "模拟器标记",
                    "【人工】泄露（无/有：编号）", "【人工】编造（无/有：内容）", "【人工】前后矛盾（无/有：内容）",
                    "【人工】遗漏（无/有：内容）", "【人工】影响的评分项", "【人工】备注"])
        for i in review:
            marks = "；".join(f"{m.get('point', '')}→{m.get('match', '')}" for m in i["mapping"])
            w.writerow([i["uid"], i["case_id"], i["model"], i["rep"], i["patient_turn"], i["sample_reason"],
                        " | ".join(i["flags"]) or "—", i["reply"], marks, "", "", "", "", "", ""])

    lines = [f"# 模拟患者质检核对：{args.run_id}", "",
             f"- 患者回复轮次共 {len(items)} 轮；自动报警 {len(flagged)} 轮；未报警 {len(unflagged)} 轮，"
             f"按病例 × 模型分层每层随机抽取 {SAMPLE_PER_STRATUM} 轮，共 {len(sampled)} 轮（随机种子 {SEED}）",
             f"- 本次需人工核对：{len(review)} 轮",
             f"- AI 事实核对：{meta['checker_model']}，提示词 {meta['checker_prompt']}；自动报警只是线索，以人工判断为准",
             "- 核对标准：患者说的每个事实，都要在病例资料或医生当前的提问中找到依据；结果填入 qc_sheet.csv", ""]
    for i in review:
        c = cases[i["case_id"]]
        lines += [f"## {i['uid']}（{i['sample_reason']}）", "",
                  "<details><summary>病例隐藏信息</summary>", "",
                  *[f"- {h['id']}：{h['content']}" for h in c["hidden_info"]],
                  f"- 否认：{'；'.join(c.get('negatives', []))}", "", "</details>", "",
                  "<details><summary>医生本轮的话</summary>", "", i["doctor"], "", "</details>", "",
                  f"**患者回复**：{i['reply']}", "",
                  "**模拟器标记**：" + ("；".join(f"{m.get('point', '')} → {m.get('match', '')}" for m in i["mapping"]) or "—"), "",
                  "**自动报警**：" + (("\n" + "\n".join(f"- {fl}" for fl in i["flags"])) if i["flags"] else "无"), ""]
    (qc_dir / "qc_review.md").write_text("\n".join(lines), encoding="utf-8")

    by_type: dict[str, int] = {}
    for i in items:
        for fl in i["flags"]:
            key = fl.split("：")[0]
            by_type[key] = by_type.get(key, 0) + 1
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    print("报警类型统计（一轮可能有多条）：", by_type)


if __name__ == "__main__":
    main()
