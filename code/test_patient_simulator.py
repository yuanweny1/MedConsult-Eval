"""
模拟患者的回归测试：用构造好的医生提问，检查模拟患者是否按规则透露信息、标记是否正确且稳定。

检查三类情况：
  1. 同义问法：意思相同、措辞不同的问题，是否都触发同一条隐藏信息
  2. 重复追问：已经说过的信息再次被问到时，是否如实一致地回答
  3. 默认否认与未设定：病例没写的症状回答"没有"，没写的细节只回答"没太注意"，不带模糊的程度或频率词

每条测试运行多次（默认 3 次），每次都包含期望的标记才算通过。修改模拟患者提示词后都应重新运行。

注意：
  - 回答原文的词表检查只是报警器，不是"没有泄露"的证明：泄露不一定命中固定词（漏报），
    命中词也不一定是泄露（误报，如"我不觉得严重"）。真正的标准是：患者说的每个事实都有病例内容或当前问题作依据，
    这需要在正式运行后做语义核对（见 docs/qc_plan.md）。
  - 测试全部通过，只说明这些场景表现良好，不等于正式对话中不会出现问题。
  - 模拟患者温度为 0，但平台输出仍有细微差异，不能视为确定性保证。

运行方法（在 MedConsult-Eval 文件夹里）：
    .venv/Scripts/python.exe code/test_patient_simulator.py
"""

import argparse

# 回答"未设定"时不应出现的模糊程度或频率词（会变相透露病例中没有设定的信息）
VAGUE_WORDS = ["挺多", "很多", "好多", "经常", "老是", "有一点", "有点多", "严重", "好几"]

from llm import load_cases
from run_dialogue import build_patient_prompt, patient_answer

# （病例, 之前的对话[(医生, 患者)], 本轮医生提问, 必须出现的标记, 不应出现的标记, 说明[, 回答原文中不应出现的词]）
# 只检查标记不够：模拟患者可能透露了信息却没有标记，所以对容易泄露的情况再检查回答原文
CASES = [
    # ---- 同义问法：C06 夜间腹泻（C06-H4）----
    ("C06", [], "晚上会不会因为拉肚子醒过来？", {"C06-H4"}, set(), "同义问法 1：夜间腹泻"),
    ("C06", [], "夜里睡着以后，还需要起来上厕所吗？", {"C06-H4"}, set(), "同义问法 2：夜间腹泻"),
    ("C06", [], "腹泻在一天里什么时候比较多？夜间有没有？", {"C06-H4"}, set(), "同义问法 3：夜间腹泻"),
    # ---- 同义问法：C01 放射痛（C01-H3）----
    ("C01", [], "除了胸口，别的地方比如肩膀、胳膊有没有不舒服？", {"C01-H3"}, set(), "同义问法 1：放射痛"),
    ("C01", [], "这种闷痛会不会往别处窜？", {"C01-H3"}, set(), "同义问法 2：放射痛"),
    # ---- 宽泛问题：只能透露 broad 的信息 ----
    ("C01", [], "还有别的地方不舒服吗？", {"C01-H4"}, {"C01-H3", "C01-H5"}, "宽泛问题只透露 broad 信息"),
    # ---- 重复追问：已说过的信息要一致 ----
    ("C06",
     [("最近体重有变化吗？", '{"info_points": [{"point": "体重变化", "match": "C06-H3"}], "reply": "瘦了大概五公斤吧，我没减肥。"}'),
      ("大便里有没有血？", '{"info_points": [{"point": "大便有没有血", "match": "C06-H2"}], "reply": "……大便里有时候有血，还有黏糊糊的东西。"}')],
     "我再确认一下：体重下降是这 3 个月里发生的吗？大便是水样的还是带血的？",
     {"C06-H3", "C06-H2"}, set(), "重复追问：已说过的信息如实一致回答"),
    # ---- 默认否认与未设定 ----
    ("C01", [], "有没有黑便或者吐血？", {"默认否认"}, {"未设定"}, "病例没写的症状 → 默认否认"),
    ("C06", [], "大便一天大概几次？", {"未设定"}, {"默认否认"}, "病例没写的细节 → 未设定（次数）"),
    # 先让患者说出黑便，再追问病例没写的"量"（医生不可能在患者说之前就知道有黑便）
    ("C07",
     [("大便颜色正常吗？", '{"info_points": [{"point": "大便颜色", "match": "C07-H2"}], "reply": "这几天大便是黑的，三天了吧。"}')],
     "黑便的量大概有多少？", {"未设定"}, {"默认否认"}, "病例没写的细节 → 未设定（程度）"),
    # 患者开场说的是"胃不太舒服、有点闷"，医生此时还不知道是胸口，提问不能预设隐藏信息
    ("C01", [], "你说的胃里闷，程度有多重？如果 0 到 10 分打几分？", {"未设定"}, {"默认否认", "C01-H1"},
     "病例没写的细节 → 未设定（程度），且不能顺带透露胸骨后压迫感", ["压着", "胸口", "胸骨"]),
]


def run_once(case_id: str, history: list, question: str) -> dict:
    case = load_cases()[case_id]
    msgs = [{"role": "system", "content": build_patient_prompt(case)}]
    for doctor, patient_raw in history:
        msgs += [{"role": "user", "content": doctor}, {"role": "assistant", "content": patient_raw}]
    msgs.append({"role": "user", "content": question})
    return patient_answer(msgs)


def main() -> None:
    parser = argparse.ArgumentParser(description="模拟患者回归测试")
    parser.add_argument("--repeat", type=int, default=3, help="每条测试运行几次")
    parser.add_argument("--case", help="只运行某个病例的测试，如 C01")
    args = parser.parse_args()

    failed = 0
    for case_id, history, question, must_have, must_not, note, *rest in CASES:
        banned_words = rest[0] if rest else []
        if args.case and case_id != args.case:
            continue
        results = []
        for _ in range(args.repeat):
            ans = run_once(case_id, history, question)
            matches = {p.get("match", "") for p in (ans["mapping"] or [])}
            ok = ans["format_ok"] and must_have <= matches and not (must_not & matches)
            # 只回答"未设定"的测试，还要检查回答原文里没有模糊的程度或频率词
            vague = [w for w in VAGUE_WORDS if w in ans["reply"]] if must_have == {"未设定"} else []
            leaked = [w for w in banned_words if w in ans["reply"]]
            ok = ok and not vague and not leaked
            results.append((ok, sorted(matches), ans["reply"]))
        all_ok = all(ok for ok, _, _ in results)
        failed += not all_ok
        print(f"[{'通过' if all_ok else '失败'}] {note}（{case_id}）：期望含 {sorted(must_have)}"
              + (f"，不应含 {sorted(must_not)}" if must_not else ""))
        for ok, matches, reply in results:
            print(f"      {'✓' if ok else '✗'} 标记 {matches} ｜ {reply[:60]}")

    print(f"\n结果：{len(CASES) - failed}/{len(CASES)} 条通过（每条运行 {args.repeat} 次，全部符合才算通过）")


if __name__ == "__main__":
    main()
