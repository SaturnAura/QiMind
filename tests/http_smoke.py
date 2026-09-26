"""对运行中的 QiMind 服务做一次 HTTP 冒烟测试。

用法::

    python -m qimind gui --port 8611 --no-browser
    python tests/http_smoke.py 8611
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import requests

BASE = "http://127.0.0.1:{port}"
START_FEN = "rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR w - - 0 1"
CASE_PGN = Path(__file__).resolve().parents[1] / "case.pgn"


def run_job(base: str, payload: dict, seconds: int = 300) -> dict:
    """提交一个分析任务并等到结束。"""
    job_id = requests.post(base + "/api/analyze/position" if "fen" in payload else base + "/api/analyze/game",
                           json=payload, timeout=60).json()["job_id"]
    for _ in range(seconds):
        job = requests.get(base + f"/api/jobs/{job_id}", timeout=30).json()
        if job["status"] in ("done", "error", "cancelled"):
            return job
        time.sleep(1)
    return job


def main(port: int, with_llm: bool = False) -> int:
    base = BASE.format(port=port)
    ok = True

    def check(name, response, extra=""):
        nonlocal ok
        status = "OK " if response.status_code == 200 else "!! "
        if response.status_code != 200:
            ok = False
        print(f"[{status}] {name} {response.status_code} {extra}")
        return response

    check("首页", requests.get(base + "/", timeout=10))
    check("前端脚本", requests.get(base + "/static/app.js", timeout=10))
    config = check("配置", requests.get(base + "/api/config", timeout=30)).json()
    print("      引擎:", config["engine"]["name"] or config["engine"]["error"])
    samples = check("示例列表", requests.get(base + "/api/samples", timeout=10)).json()["samples"]
    print(f"      示例 {len(samples)} 个")

    moves = check("合法走法", requests.post(base + "/api/board/moves", json={"fen": START_FEN}, timeout=30)).json()
    print(f"      初始局面走法 {len(moves['moves'])} 种")

    move = check(
        "走子",
        requests.post(base + "/api/board/move", json={"fen": START_FEN, "iccs": "h2e2"}, timeout=30),
    ).json()
    print("      ", move["chinese"], move["fen"][:30], "…")

    if samples:
        pgn = next((item for item in samples if item["kind"] == "pgn"), samples[0])
        record = check(
            "载入棋谱", requests.post(base + "/api/game/load", json={"path": pgn["path"]}, timeout=30)
        ).json()["record"]
        print(f"      {record['name']}：{len(record['plies'])} 步")

    job_id = check(
        "提交局面分析",
        requests.post(
            base + "/api/analyze/position",
            json={"fen": START_FEN, "depth": 10, "multipv": 3, "explain": with_llm},
            timeout=30,
        ),
    ).json()["job_id"]
    for _ in range(180):
        job = requests.get(base + f"/api/jobs/{job_id}", timeout=30).json()
        if job["status"] in ("done", "error"):
            break
        time.sleep(1)
    check("分析结果", requests.get(base + f"/api/jobs/{job_id}", timeout=30), job["message"])
    report = job.get("data", {}).get("report") or {}
    print("      正着:", report.get("best_chinese"), report.get("score_text"))
    if with_llm:
        explanation = (report.get("explanation") or {}).get("text", "")
        print("      讲解:", explanation.splitlines()[:3])
    assert job["status"] == "done", job

    # 只跑引擎：不调用 DeepSeek，也应该有正着与候选
    engine_job = run_job(base, {"fen": START_FEN, "depth": 10, "multipv": 3, "mode": "engine"})
    engine_report = engine_job["data"]["report"]
    print("[OK ] 只跑引擎:", engine_report["best_chinese"], engine_report["score_text"],
          "| 候选", len(engine_report["candidates"]),
          "| 讲解:", "无" if not engine_report.get("explanation") else "有")
    assert engine_job["status"] == "done" and engine_report["candidates"]
    assert not engine_report.get("explanation"), "只跑引擎时不应生成讲解"

    if with_llm:
        # 只讲 DeepSeek：复用引擎缓存，只调大模型
        llm_job = run_job(base, {"fen": START_FEN, "depth": 10, "multipv": 3, "mode": "llm"})
        llm_report = llm_job["data"]["report"]
        text = (llm_report.get("explanation") or {}).get("text", "")
        print("[OK ] 只讲 DeepSeek:", text.splitlines()[:2])
        assert llm_job["status"] == "done" and text, llm_job

    # case.pgn：整局只跑引擎（不消耗大模型额度）
    if CASE_PGN.is_file():
        loaded = requests.post(base + "/api/game/load", json={"path": str(CASE_PGN)}, timeout=30).json()["record"]
        print(f"[OK ] case.pgn 导入：{loaded['name']} {len(loaded['plies'])} 步 "
              f"（{loaded['red_name']} vs {loaded['black_name']}）")
        case_job = run_job(
            base,
            {"path": str(CASE_PGN), "depth": 10, "multipv": 2, "mode": "engine", "max_plies": 4},
        )
        reports = case_job["data"]["reports"]
        print("[OK ] case.pgn 引擎分析:", case_job["message"],
              "| 第一步正着", reports[0]["best_chinese"], f"（实战 {reports[0]['played_chinese']}）")
        assert case_job["status"] == "done" and len(reports) == 4

    print("冒烟测试", "通过" if ok else "存在失败项")
    return 0 if ok else 1


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8611
    with_llm = "--llm" in sys.argv
    raise SystemExit(main(port, with_llm))
