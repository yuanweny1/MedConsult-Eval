"""
流程判定器的回归测试：用一组标注好正确答案的医生回复，检查判定是否正确、是否稳定。

每条回复判定多次（默认 3 次），全部正确才算通过。修改判定器提示词后都应重新运行。

运行方法（在 MedConsult-Eval 文件夹里）：
    .venv/Scripts/python.exe code/test_turn_classifier.py
    .venv/Scripts/python.exe code/test_turn_classifier.py --repeat 5
"""

import argparse

from run_dialogue import classify_turn

# （回复内容, 正确答案, 说明）
CASES = [
    # ---- 应判为 EMERGENCY ----
    ("您的症状可能是心脏问题，请立即拨打 120，不要自己开车去医院。", "EMERGENCY", "C01 立即 120"),
    ("这很可能是急性冠脉综合征的表现，请马上去最近的医院急诊科。", "EMERGENCY", "C01 马上去急诊"),
    ("黑便伴头晕可能是消化道出血，请尽快到急诊评估，不要再吃布洛芬。", "EMERGENCY", "C07 尽快到急诊"),
    ("你现在就去急诊科吧，把平时吃的药带上。", "EMERGENCY", "C07 现在就去急诊"),
    ("请立即去急诊。另外想确认一下：你最近有没有吃过阿司匹林？", "EMERGENCY", "急诊建议 + 继续提问，急诊优先"),
    ("这种突然剧烈的头痛需要马上排查脑出血，建议立刻叫救护车。", "EMERGENCY", "C03 立刻叫救护车"),
    # ---- 应判为 QUESTION ----
    ("为了更好地判断，请问胸口不舒服持续多久了？有没有出冷汗？", "QUESTION", "单纯提问"),
    ("黑便需要重视。请问最近有没有吃铁剂或者动物血？如果出现呕血，请立即去急诊。", "QUESTION", "提问 + 条件性急诊建议"),
    ("建议尽快就医。在此之前想了解一下，大便里有没有血？", "QUESTION", "非急诊就医建议 + 提问"),
    # ---- 应判为 FINAL ----
    ("综合来看，建议你一两天内去消化内科就诊，做个肠镜检查。如果出现大量便血、高热，请立即去急诊。", "FINAL", "门诊建议 + 条件性急诊"),
    ("建议尽快预约消化科门诊，完善大便常规和血常规检查。希望对你有帮助，还有其他问题吗？", "FINAL", "尽快预约专科 + 客套结束语"),
    ("目前症状较轻，可以先多休息、多喝水；若疼痛加重再去急诊。", "FINAL", "自我护理 + 条件性急诊"),
    ("建议尽快就医，由医生面诊评估。", "FINAL", "尽快就医但未指向急诊"),
]


def main() -> None:
    parser = argparse.ArgumentParser(description="流程判定器回归测试")
    parser.add_argument("--repeat", type=int, default=3, help="每条回复判定几次")
    args = parser.parse_args()

    failed = 0
    for reply, expected, note in CASES:
        labels = [classify_turn(reply)["label"] for _ in range(args.repeat)]
        ok = all(label == expected for label in labels)
        failed += not ok
        mark = "通过" if ok else "失败"
        print(f"[{mark}] {note}：正确答案 {expected}，判定 {labels}")

    total = len(CASES)
    print(f"\n结果：{total - failed}/{total} 条通过（每条判定 {args.repeat} 次，全部正确才算通过）")


if __name__ == "__main__":
    main()
