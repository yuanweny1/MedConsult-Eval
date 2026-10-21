"""
把 Markdown 文档转换成 PDF（先转为带样式的 HTML，再用 Edge 浏览器打印为 PDF）。

运行方法（在 MedConsult-Eval 文件夹里）：
    .venv/Scripts/python.exe code/md_to_pdf.py guidelines/annotation_guideline_v1.md 输出路径.pdf
"""

import re
import subprocess
import sys
import tempfile
from pathlib import Path

import markdown

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

CSS = """
@page { size: A4; margin: 16mm 14mm; }
body { font-family: "Microsoft YaHei", "微软雅黑", sans-serif; font-size: 10.5pt; line-height: 1.6; color: #222; }
h1 { font-size: 18pt; border-bottom: 2px solid #2F5597; padding-bottom: 4px; color: #1F3864; }
h2 { font-size: 14pt; color: #1F3864; margin-top: 22px; border-bottom: 1px solid #BDD7EE; padding-bottom: 2px; }
h3 { font-size: 12pt; color: #2F5597; margin-top: 16px; }
h4 { font-size: 11pt; color: #2F5597; margin-top: 12px; }
table { border-collapse: collapse; width: 100%; margin: 8px 0 12px; font-size: 9.5pt; page-break-inside: auto; }
tr { page-break-inside: avoid; }
th { background: #DDEBF7; }
th, td { border: 1px solid #BFBFBF; padding: 4px 6px; vertical-align: top; text-align: left; }
blockquote { margin: 8px 0; padding: 6px 10px; background: #F2F2F2; border-left: 4px solid #8EA9DB; }
code { font-family: Consolas, monospace; background: #F2F2F2; padding: 0 3px; }
img { max-width: 100%; height: auto; }
"""

LIST_ITEM = re.compile(r"([-*]|\d+\.) ")


def normalize(text: str) -> str:
    """调整为 Python-Markdown 能识别的列表格式：列表前补空行；两个空格的嵌套缩进改为四个。"""
    out: list[str] = []
    prev = ""
    for line in text.replace("\r\n", "\n").split("\n"):
        stripped = line.lstrip(" ")
        indent = len(line) - len(stripped)
        is_item = bool(LIST_ITEM.match(stripped))
        if is_item and indent:
            line = " " * (indent * 2) + stripped
        if line.startswith("> ") and prev.startswith("> "):
            out[-1] = out[-1] + "  "   # 引用块内的相邻行保留换行
        prev_is_item = bool(LIST_ITEM.match(prev.lstrip(" ")))
        if is_item and prev.strip() and not prev_is_item and not prev.startswith(" "):
            out.append("")
        out.append(line)
        prev = line
    return "\n".join(out)


def main() -> None:
    src, dst = Path(sys.argv[1]), Path(sys.argv[2]).resolve()
    body = markdown.markdown(normalize(src.read_text(encoding="utf-8")), extensions=["tables", "sane_lists"])
    # 临时网页不在原文件夹里，用 <base> 指回原文件夹，文中的相对路径图片才能显示
    base = src.resolve().parent.as_uri() + "/"
    html = (f'<!doctype html><html><head><meta charset="utf-8"><base href="{base}"><style>{CSS}</style></head>'
            f'<body>{body}</body></html>')
    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / "doc.html"
        page.write_text(html, encoding="utf-8")
        subprocess.run([EDGE, "--headless", "--disable-gpu", "--no-pdf-header-footer",
                        f"--print-to-pdf={dst}", page.as_uri()], check=True, capture_output=True, timeout=120)
    print(f"已生成：{dst}")


if __name__ == "__main__":
    main()
