"""DeepSeek 接口最小示例（QiMind 使用同样的一套调用方式）。

依赖：``pip install openai``
Key ： ``set QIMIND_DEEPSEEK_KEY=sk-xxxx``（Windows）
       或写入 ``qimind/data/secrets.json`` 的 ``deepseek_api_key`` 字段
运行：``python ds_demo.py``
"""

import os
from pathlib import Path

from openai import OpenAI


def load_api_key() -> str:
    """优先读环境变量，其次读 qimind/data/secrets.json。"""
    key = os.environ.get("QIMIND_DEEPSEEK_KEY") or os.environ.get("DEEPSEEK_API_KEY")
    if key:
        return key.strip()
    secrets = Path(__file__).parent / "qimind" / "data" / "secrets.json"
    if secrets.is_file():
        import json

        return str(json.loads(secrets.read_text(encoding="utf-8"))["deepseek_api_key"]).strip()
    raise SystemExit("未找到 DeepSeek API Key，请设置 QIMIND_DEEPSEEK_KEY 环境变量。")


client = OpenAI(api_key=load_api_key(), base_url="https://api.deepseek.com")

response = client.chat.completions.create(
    model="deepseek-flash",
    messages=[
        {"role": "system", "content": "你是中国象棋职业棋手兼讲解员。"},
        {
            "role": "user",
            "content": "用两句话向初学者解释：开局为什么常说「炮二平五」是好棋？",
        },
    ],
    stream=False,
    reasoning_effort="high",
    extra_body={"thinking": {"type": "enabled"}},
)

print(response.choices[0].message.content)
