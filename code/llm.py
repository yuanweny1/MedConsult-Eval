"""
调用大模型的公共模块：读取配置、创建客户端、发送对话请求。

其他脚本通过 chat(model_key, messages) 调用模型，不需要关心各平台的接口地址和参数差异。
"""

import os
import time
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv
from openai import OpenAI

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


@lru_cache(maxsize=1)
def load_config() -> dict:
    return yaml.safe_load((ROOT / "config" / "models.yaml").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def load_cases() -> dict:
    """返回 {病例编号: 病例内容}。"""
    data = yaml.safe_load((ROOT / "cases" / "cases_v1.yaml").read_text(encoding="utf-8"))
    return {c["id"]: c for c in data["cases"]}


def model_config(model_key: str) -> dict:
    return load_config()["models"][model_key]


@lru_cache(maxsize=None)
def _client(api_key_env: str, base_url_env: str) -> OpenAI:
    # 思考模式的回答可能较慢，超时时间放宽到 5 分钟；网络类错误由 SDK 自动重试 3 次
    return OpenAI(
        api_key=os.getenv(api_key_env),
        base_url=os.getenv(base_url_env),
        timeout=300,
        max_retries=3,
    )


def chat(model_key: str, messages: list[dict]) -> dict:
    """
    向配置中的某个模型发送一组对话消息，返回：
      content    最终回答（模拟患者和评委只能看到这部分）
      reasoning  思考内容（没有则为空字符串；单独保存，用于事后分析）
      model      平台返回的模型名
      usage      输入、输出 tokens
      seconds    耗时
    """
    cfg = model_config(model_key)
    params = {"model": cfg["model"], "messages": messages}
    if cfg.get("temperature") is not None:
        params["temperature"] = cfg["temperature"]
    if cfg.get("extra_body"):
        params["extra_body"] = cfg["extra_body"]

    client = _client(cfg["api_key_env"], cfg["base_url_env"])
    start = time.time()
    resp = client.chat.completions.create(**params)
    msg = resp.choices[0].message
    return {
        "content": (msg.content or "").strip(),
        "reasoning": (getattr(msg, "reasoning_content", None) or "").strip(),
        "model": resp.model,
        "usage": {
            "input": resp.usage.prompt_tokens if resp.usage else None,
            "output": resp.usage.completion_tokens if resp.usage else None,
        },
        "seconds": round(time.time() - start, 1),
    }
