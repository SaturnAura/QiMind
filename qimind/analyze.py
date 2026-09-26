"""分析编排：局面 → 引擎事实 → 大模型讲解。

对外只有两个入口：

* :func:`analyze_position` —— 分析单个局面，给出正着与讲解；
* :func:`annotate_game` —— 逐回合分析整局棋，产出可展示的讲解列表。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

from .cchess_bridge import fen_side, full_fen, iccs_to_chinese, side_label
from .config import load_settings
from .engine import EngineReport, analyze_fen, get_engine
from .facts import build_fact_sheet
from .games import GameRecord, Ply
from .llm import Explanation, explain_fact_sheet, explain_messages
from .prompts import build_report_messages, build_variation_messages
from .report import build_report

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int, Optional[Ply], "PositionReport"], None]


@dataclass
class PositionReport:
    """一个局面的完整分析结果（引擎 + 讲解）。"""

    fen: str
    side: str
    move_number: int = 0
    depth: int = 0
    engine_name: str = ""
    best_iccs: str = ""
    best_chinese: str = ""
    score_text: str = ""
    score_red_cp: Optional[int] = None
    candidates: List[Dict[str, object]] = field(default_factory=list)
    facts: Dict[str, object] = field(default_factory=dict)
    fact_sheet: str = ""
    explanation: Optional[Explanation] = None
    played_iccs: Optional[str] = None
    played_chinese: str = ""
    played_score_cp: Optional[int] = None
    loss_cp: Optional[int] = None
    is_best: bool = False
    engine_elapsed: float = 0.0
    error: str = ""

    def to_dict(self) -> Dict[str, object]:
        return {
            "fen": self.fen,
            "side": self.side,
            "side_label": side_label(self.side),
            "move_number": self.move_number,
            "depth": self.depth,
            "engine_name": self.engine_name,
            "best_iccs": self.best_iccs,
            "best_chinese": self.best_chinese,
            "score_text": self.score_text,
            "score_red_cp": self.score_red_cp,
            "candidates": self.candidates,
            "facts": self.facts,
            "fact_sheet": self.fact_sheet,
            "explanation": self.explanation.to_dict() if self.explanation else None,
            "played_iccs": self.played_iccs,
            "played_chinese": self.played_chinese,
            "played_score_cp": self.played_score_cp,
            "loss_cp": self.loss_cp,
            "is_best": self.is_best,
            "engine_elapsed": round(self.engine_elapsed, 2),
            "error": self.error,
        }


@dataclass
class GameAnalysis:
    """整局棋的分析结果。"""

    record: GameRecord
    reports: List[PositionReport] = field(default_factory=list)
    style: str = "棋友"
    depth: int = 0
    scope: str = "all"
    explanation_count: int = 0
    elapsed: float = 0.0

    def to_dict(self) -> Dict[str, object]:
        return {
            "record": self.record.to_dict(),
            "style": self.style,
            "depth": self.depth,
            "scope": self.scope,
            "explanation_count": self.explanation_count,
            "elapsed": round(self.elapsed, 2),
            "reports": [report.to_dict() for report in self.reports],
        }


def analyze_position(
    fen: str,
    *,
    depth: Optional[int] = None,
    multipv: Optional[int] = None,
    played: Optional[str] = None,
    move_number: int = 0,
    style: str = "棋友",
    explain: bool = True,
    use_cache: bool = True,
    evaluate_played: bool = True,
    model: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
    regenerate: bool = False,
) -> PositionReport:
    """分析一个局面：引擎给出正着与评分，再让 DeepSeek 讲成人话。

    参数:
        fen: 待分析局面（走子方即将行棋）。
        played: 实战走法（ICCS），用于判断是否与正着一致并复算损失。
        explain: 是否调用大模型生成讲解（False 时只跑引擎）。
        evaluate_played: 实战走法不在候选里时，是否额外复算它的评分。
        model / reasoning_effort: 覆盖默认模型与思考强度。
        regenerate: 忽略已有的讲解缓存，重新生成。
    """
    settings = load_settings()
    depth = int(depth or settings["depth"])
    multipv = int(multipv or settings["multipv"])
    fen = full_fen(fen)
    side = fen_side(fen)

    started = time.monotonic()
    report: EngineReport = analyze_fen(fen, depth=depth, multipv=multipv, use_cache=use_cache)
    engine_elapsed = time.monotonic() - started

    played_score_cp: Optional[int] = None
    if played:
        candidate = report.find(played)
        if candidate is not None and candidate.mate_in is None:
            played_score_cp = candidate.score_cp
        elif candidate is not None and candidate.mate_in is not None:
            played_score_cp = None
        elif evaluate_played:
            played_score_cp = get_engine().evaluate_move(
                fen, played, depth=max(10, depth - 2)
            )

    sheet = build_fact_sheet(
        fen,
        report,
        played_iccs=played,
        played_score_cp=played_score_cp,
        move_number=move_number or None,
    )

    explanation = None
    error = ""
    if explain:
        explanation = explain_fact_sheet(
            sheet.text,
            played_move=_played_chinese(fen, report, played),
            is_best=bool(played and report.best_iccs and played.lower() == report.best_iccs.lower()),
            style=style,
            use_cache=use_cache and not regenerate,
            model=model,
            reasoning_effort=reasoning_effort,
        )
        if explanation.error:
            error = explanation.error

    best = report.best
    return PositionReport(
        fen=fen,
        side=side,
        move_number=move_number,
        depth=depth,
        engine_name=report.engine_name,
        best_iccs=report.best_iccs,
        best_chinese=best.chinese if best else "",
        score_text=str(sheet.data.get("score_text", "")),
        score_red_cp=sheet.data.get("score_red_cp"),  # type: ignore[arg-type]
        candidates=list(sheet.data.get("candidates", [])),  # type: ignore[arg-type]
        facts=sheet.data,
        fact_sheet=sheet.text,
        explanation=explanation,
        played_iccs=played,
        played_chinese=_played_chinese(fen, report, played),
        played_score_cp=played_score_cp,
        loss_cp=sheet.data.get("loss_cp"),  # type: ignore[arg-type]
        is_best=bool(sheet.data.get("is_best")),
        engine_elapsed=engine_elapsed,
        error=error,
    )


def _played_chinese(fen: str, report: EngineReport, played: Optional[str]) -> str:
    """得到实战走法的中文记谱（优先用引擎候选里的现成结果）。"""
    if not played:
        return ""
    candidate = report.find(played)
    if candidate is not None and candidate.chinese:
        return candidate.chinese
    from .cchess_bridge import iccs_to_chinese

    return iccs_to_chinese(fen, played)


def should_explain(report: PositionReport, scope: str) -> bool:
    """根据讲解范围判断某一步是否需要大模型讲解。"""
    if scope == "all":
        return True
    if scope == "red":
        return report.side == "red"
    if scope == "black":
        return report.side == "black"
    if scope == "key":
        if not report.is_best:
            return True
        return bool(report.loss_cp and report.loss_cp >= 50)
    return True


def explain_variation(
    fen: str,
    iccs: str,
    *,
    depth: Optional[int] = None,
    multipv: Optional[int] = None,
    style: str = "棋友",
    use_cache: bool = True,
    regenerate: bool = False,
    model: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
) -> Dict[str, object]:
    """讲解某个局面下的一步「变招」（候选着法之一）。

    返回结构与 :meth:`PositionReport.to_dict` 类似，额外带 ``variation`` 字段，
    便于界面直接把结果放进候选表下方展示。
    """
    settings = load_settings()
    depth = int(depth or settings["depth"])
    multipv = int(multipv or settings["multipv"])
    fen = full_fen(fen)

    report: EngineReport = analyze_fen(fen, depth=depth, multipv=multipv, use_cache=use_cache)
    candidate = report.find(iccs)
    if candidate is None:
        # 变招不在候选里：单独复算它的评分，再补进候选列表开头
        score = get_engine().evaluate_move(fen, iccs, depth=max(10, depth - 2))
        from .engine import Candidate

        candidate = Candidate(
            rank=len(report.candidates) + 1,
            iccs=iccs,
            score_cp=score,
            depth=depth,
        )
        candidate.chinese = iccs_to_chinese(fen, iccs)
        candidate.pv_chinese = []

    played_score = candidate.score_cp
    sheet = build_fact_sheet(fen, report, played_iccs=iccs, played_score_cp=played_score)
    best = report.best
    messages = build_variation_messages(
        sheet.text,
        variation_move=candidate.chinese or iccs,
        best_move=best.chinese if best else "",
        style=style,
    )
    explanation = explain_messages(
        messages,
        style=style,
        cache_tag="variation",
        use_cache=use_cache and not regenerate,
        model=model,
        reasoning_effort=reasoning_effort,
    )

    data = dict(sheet.data)
    data.update(
        {
            "variation_iccs": iccs,
            "variation_chinese": candidate.chinese or iccs,
            "variation_score": candidate.score_text,
            "variation_cp": candidate.score_cp,
            "explanation": explanation.to_dict(),
        }
    )
    return data


def generate_report(
    record: GameRecord,
    reports: Sequence[object],
    *,
    style: str = "棋友",
    explain: bool = True,
    use_cache: bool = True,
    regenerate: bool = False,
    model: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
) -> Dict[str, object]:
    """基于逐回合分析生成对局报告（统计 + 可选的大模型讲评）。"""
    payload = [
        item.to_dict() if hasattr(item, "to_dict") else dict(item)  # type: ignore[arg-type]
        for item in reports
    ]
    report = build_report(record, payload, style=style)
    if explain:
        messages = build_report_messages(
            str(report["fact_sheet"]),
            game_title=record.name,
            red_name=record.red_name,
            black_name=record.black_name,
            style=style,
        )
        explanation = explain_messages(
            messages,
            style=style,
            cache_tag="report",
            use_cache=use_cache and not regenerate,
            model=model,
            reasoning_effort=reasoning_effort,
        )
        report["narrative"] = explanation.text
        report["explanation"] = explanation.to_dict()
    else:
        report["narrative"] = ""
        report["explanation"] = None
    report["record"] = record.to_dict()
    return report


def annotate_game(
    record: GameRecord,
    *,
    depth: Optional[int] = None,
    multipv: Optional[int] = None,
    scope: str = "all",
    style: str = "棋友",
    explain: bool = True,
    use_cache: bool = True,
    model: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
    regenerate: bool = False,
    max_plies: Optional[int] = None,
    progress: Optional[ProgressCallback] = None,
) -> GameAnalysis:
    """逐回合分析整局棋。

    参数:
        scope: 讲解范围，``all`` / ``red`` / ``black`` / ``key``（仅关键步）。
        max_plies: 只分析前 N 步（便于快速预览或分段讲解）。
        progress: 进度回调 ``(已完成, 总数, 当前 Ply, 结果)``。
    """
    settings = load_settings()
    depth = int(depth or settings["depth"])
    multipv = int(multipv or settings["multipv"])

    plies: Sequence[Ply] = record.plies
    if max_plies:
        plies = plies[:max_plies]

    started = time.monotonic()
    reports: List[PositionReport] = []
    explanation_count = 0
    total = len(plies)

    for done, ply in enumerate(plies, start=1):
        try:
            report = analyze_position(
                ply.fen_before,
                depth=depth,
                multipv=multipv,
                played=ply.iccs,
                move_number=ply.move_number,
                style=style,
                explain=False,
                use_cache=use_cache,
            )
            if explain and should_explain(report, scope):
                explanation = explain_fact_sheet(
                    report.fact_sheet,
                    played_move=report.played_chinese,
                    is_best=report.is_best,
                    style=style,
                    use_cache=use_cache and not regenerate,
                    model=model,
                    reasoning_effort=reasoning_effort,
                )
                report.explanation = explanation
                if explanation.error:
                    report.error = explanation.error
                else:
                    explanation_count += 1
        except Exception as exc:  # pragma: no cover - 单步失败不应中断整局
            logger.exception("分析第 %s 步失败", ply.index)
            report = PositionReport(fen=ply.fen_before, side=ply.side, error=str(exc))

        reports.append(report)
        if progress:
            progress(done, total, ply, report)

    return GameAnalysis(
        record=record,
        reports=reports,
        style=style,
        depth=depth,
        scope=scope,
        explanation_count=explanation_count,
        elapsed=time.monotonic() - started,
    )
