"""QiMind 命令行入口。

常用用法::

    python -m qimind gui                  # 启动图形界面
    python -m qimind explain 对局.pgn      # 逐回合讲解整局棋
    python -m qimind pos "FEN" --played h2e2
    python -m qimind doctor               # 环境自检
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

from .analyze import analyze_position, annotate_game, should_explain
from .config import PROJECT_ROOT, deepseek_api_key, engine_path, load_settings, save_settings
from .games import load_positions_txt, load_record


def _setup_stdout() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover
        pass


def _mode_of(args: argparse.Namespace) -> str:
    """把命令行开关翻译成后端使用的分析方式。"""
    if getattr(args, "no_llm", False):
        return "engine"
    if getattr(args, "llm_only", False):
        return "llm"
    return "both"


def _print_exclusion(text: str, prefix: str = "") -> None:
    for line in (text or "").splitlines():
        print(f"{prefix}{line}")


# --------------------------------------------------------------------------- 子命令
def cmd_gui(args: argparse.Namespace) -> int:
    import uvicorn

    from .web.server import app

    host, port = args.host, args.port
    url = f"http://{host}:{port}/"
    print(f"QiMind 棋思 图形界面已启动：{url}")
    print("按 Ctrl+C 结束服务。")
    if not args.no_browser:
        import threading
        import webbrowser

        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=host, port=port, log_level=args.log_level)
    return 0


def cmd_doctor(_args: argparse.Namespace) -> int:
    print("== QiMind 环境自检 ==")
    print(f"项目目录：{PROJECT_ROOT}")

    # 1. cchess
    try:
        from .cchess_bridge import FULL_INIT_FEN, ChessBoard  # noqa: F401

        print("[OK] cchess 棋子库可用")
    except Exception as exc:
        print(f"[!!] cchess 不可用：{exc}")

    # 2. 引擎
    path = engine_path()
    if not path:
        print("[!!] 未找到 Pikafish 引擎，请设置 QIMIND_ENGINE")
    else:
        print(f"[OK] 引擎文件：{path}")
        try:
            from .engine import get_engine, shutdown_engine

            engine = get_engine()
            print(f"[OK] 引擎握手成功：{engine.name}（{engine.threads} 线程）")
            shutdown_engine()
        except Exception as exc:
            print(f"[!!] 引擎启动失败：{exc}")

    # 3. DeepSeek
    key = deepseek_api_key()
    if not key:
        print("[!!] 未配置 DeepSeek API Key（QIMIND_DEEPSEEK_KEY 或 data/secrets.json）")
    else:
        print(f"[OK] DeepSeek Key 已配置（{key[:6]}…{key[-4:]}）")
        try:
            from openai import OpenAI

            client = OpenAI(api_key=key, base_url="https://api.deepseek.com")
            models = [item.id for item in client.models.list().data]
            print(f"[OK] DeepSeek 可用，模型：{', '.join(models)}")
        except Exception as exc:
            print(f"[!!] DeepSeek 调用失败：{exc}")

    print("设置：", json.dumps(load_settings(), ensure_ascii=False))
    return 0


def cmd_explain(args: argparse.Namespace) -> int:
    mode = _mode_of(args)
    if args.path:
        suffix = Path(args.path).suffix.lower()
        if suffix == ".txt":
            positions = load_positions_txt(args.path)
            if not positions:
                print("没有从该文件读到局面。")
                return 1
            target = positions[0]
            print(f"从题库读取到 {len(positions)} 个局面，先讲解第一个：{target.name}")
            fen, played = target.fen, None
        else:
            record = load_record(args.path)
            return _explain_record(record, args)
    elif args.pgn:
        from .games import record_from_pgn_text

        return _explain_record(record_from_pgn_text(args.pgn), args)
    elif args.fen:
        fen, played = args.fen, args.played
    else:
        print("请提供棋谱文件路径（--path）、PGN 文本（--pgn）或局面（--fen）。")
        return 2

    report = analyze_position(
        fen,
        depth=args.depth,
        multipv=args.multipv,
        played=played,
        style=args.style,
        explain=mode != "engine",
        regenerate=args.regenerate,
    )
    _print_report(report, index=1)
    if args.json:
        Path(args.json).write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已保存 JSON：{args.json}")
    return 0


def _explain_record(record, args: argparse.Namespace) -> int:
    print(f"棋谱：{record.name}（{len(record.plies)} 步）")
    total_reports = []
    mode = _mode_of(args)

    def progress(done: int, total: int, ply, report) -> None:
        print(f"[{done}/{total}] 第 {ply.move_number} 回合 {ply.chinese} —— 正着：{report.best_chinese}")

    analysis = annotate_game(
        record,
        depth=args.depth,
        multipv=args.multipv,
        scope=args.scope,
        style=args.style,
        explain=mode != "engine",
        regenerate=args.regenerate,
        max_plies=args.max_plies,
        progress=progress,
    )
    if mode == "llm":
        print("（本次只调用 DeepSeek，引擎结果使用缓存）")
    for index, report in enumerate(analysis.reports, start=1):
        _print_report(report, index=index, record=record)
        total_reports.append(report.to_dict())

    if args.json:
        payload = dict(analysis.to_dict())
        payload["reports"] = total_reports
        Path(args.json).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已保存 JSON：{args.json}")
    _export_reports(record, total_reports, args)
    return 0


def _export_reports(record, reports, args: argparse.Namespace) -> None:
    """按需导出讲评稿（Markdown / Word），并可选生成对局报告。"""
    if not (args.out_md or args.out_docx or args.with_report):
        return
    from .analyze import generate_report
    from .export import build_walkthrough_blocks, build_walkthrough_markdown, write_docx
    from .report import render_report_markdown

    report = generate_report(
        record,
        reports,
        style=args.style,
        explain=args.with_report and not args.no_llm,
    ) if args.with_report else None
    narrative = (report or {}).get("narrative", "")

    if args.out_md:
        text = build_walkthrough_markdown(
            record,
            reports,
            depth=args.depth,
            style=args.style,
            include_facts=args.include_facts,
            report=report,
            narrative=narrative,
        )
        Path(args.out_md).write_text(text, encoding="utf-8")
        print(f"已导出讲评稿（Markdown）：{args.out_md}")
    if args.out_docx:
        blocks = build_walkthrough_blocks(
            record,
            reports,
            depth=args.depth,
            style=args.style,
            report=report,
            narrative=narrative,
        )
        write_docx(args.out_docx, f"棋谱讲评：{record.name}", blocks)
        print(f"已导出讲评稿（Word）：{args.out_docx}")
    if report and args.report_out:
        text = render_report_markdown(report, record, narrative)
        Path(args.report_out).write_text(text, encoding="utf-8")
        print(f"已导出对局报告：{args.report_out}")


def _print_report(report, index: int = 0, record=None) -> None:
    title = ""
    if record and 0 < index <= len(record.plies):
        ply = record.plies[index - 1]
        title = f"第 {ply.move_number} 回合（{ply.side == 'red' and '红' or '黑'}方）{ply.chinese}"
    else:
        title = "局面分析"
    print("\n" + "=" * 72)
    print(f"#{index} {title}")
    print(f"引擎正着：{report.best_chinese}（{report.score_text}）  深度 {report.depth}")
    if report.side and report.played_chinese:
        flag = "与正着一致" if report.is_best else f"与正着相差 {report.loss_cp} 分"
        print(f"实战走法：{report.played_chinese}（{flag}）")
    print("-" * 72)
    if report.explanation and report.explanation.text:
        _print_exclusion(report.explanation.text)
    elif report.explanation and report.explanation.error:
        print(f"[讲解失败] {report.explanation.error}")
    else:
        print("[未生成讲解]")


# --------------------------------------------------------------------------- 解析
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="qimind", description="QiMind 棋思：象棋引擎 + DeepSeek 讲解")
    sub = parser.add_subparsers(dest="command")

    gui = sub.add_parser("gui", help="启动图形界面（本地 Web 应用）")
    gui.add_argument("--host", default="127.0.0.1")
    gui.add_argument("--port", type=int, default=8765)
    gui.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    gui.add_argument("--log-level", default="warning")
    gui.set_defaults(func=cmd_gui)

    explain = sub.add_parser("explain", help="讲解棋谱或局面")
    explain.add_argument("path", nargs="?", help="棋谱文件（.pgn/.xqf/.cbf/.cbr/.txt）")
    explain.add_argument("--pgn", help="PGN 文本内容")
    explain.add_argument("--fen", help="直接指定局面 FEN")
    explain.add_argument("--played", help="实战走法（ICCS，如 h2e2）")
    explain.add_argument("--depth", type=int, default=None)
    explain.add_argument("--multipv", type=int, default=None)
    explain.add_argument("--scope", default="all", choices=["all", "red", "black", "key"])
    explain.add_argument("--style", default="棋友")
    explain.add_argument("--max-plies", type=int, default=None)
    explain.add_argument("--no-llm", action="store_true", help="只跑引擎，不调用 DeepSeek")
    explain.add_argument("--llm-only", action="store_true", help="只调用 DeepSeek 写讲解（引擎结果走缓存）")
    explain.add_argument("--regenerate", action="store_true", help="忽略讲解缓存，重新生成")
    explain.add_argument("--out-md", help="导出讲评稿（Markdown）到指定文件")
    explain.add_argument("--out-docx", help="导出讲评稿（Word .docx）到指定文件")
    explain.add_argument("--with-report", action="store_true", help="同时生成对局报告（开局/转折点/失误）")
    explain.add_argument("--report-out", help="对局报告的输出文件（Markdown）")
    explain.add_argument("--include-facts", action="store_true", help="讲评稿里附上引擎事实清单")
    explain.add_argument("--json", help="把结果保存为 JSON")
    explain.set_defaults(func=cmd_explain)

    doctor = sub.add_parser("doctor", help="检查引擎、棋子库与 DeepSeek 配置")
    doctor.set_defaults(func=cmd_doctor)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    _setup_stdout()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        args = parser.parse_args(["gui"])
    if args.command == "explain" and not (args.path or args.pgn or args.fen):
        parser.parse_args(["explain", "--help"])
        return 2
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
