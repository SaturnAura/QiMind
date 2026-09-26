"""对局报告：开局判断、阶段走势、关键转折与致命失误。

报告全部基于「每一步的引擎分析结果」统计得到，是确定性数据；
大模型只负责把这些数据写成讲评稿，避免凭空编造。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from .games import GameRecord

#: 红方第一步的常见开局（ICCS → 名称）
OPENING_FIRST: Dict[str, str] = {
    "h2e2": "中炮（炮二平五）",
    "b2e2": "中炮（炮八平五）",
    "h2d2": "过宫炮",
    "h2f2": "士角炮",
    "g3g4": "仙人指路（进三兵）",
    "c3c4": "仙人指路（进七兵）",
    "c0e2": "飞相局（相三进五）",
    "g0e2": "飞相局（相七进五）",
    "h0g2": "起马局（右马）",
    "b0c2": "起马局（左马）",
}

#: 黑方应对中炮的常见着法（ICCS → 名称）
OPENING_REPLY: Dict[str, str] = {
    "h9g7": "马８进７",
    "b9c7": "马２进３",
    "h7e7": "炮８平５（顺炮）",
    "b7e7": "炮２平５（列炮）",
    "h7f7": "炮８平６（士角炮）",
    "b7c7": "炮２平３（卒底炮）",
    "g6g5": "卒７进１",
    "c6c5": "卒３进１",
    "c0e2": "象３进５",
    "g0e2": "象７进５",
}


def identify_opening(record: GameRecord, depth: int = 6) -> Dict[str, str]:
    """启发式判断开局名称（只看前几步的着法模式）。"""
    moves = [ply.iccs.lower() for ply in record.plies[:depth]]
    if not moves:
        return {"name": "无（空棋谱）", "detail": ""}

    first = moves[0]
    name = OPENING_FIRST.get(first, "")
    detail = ""
    if not name:
        # 兜底：看是不是兵/马/炮类
        piece = record.plies[0].chinese[:1] if record.plies else ""
        name = f"{piece}类开局" if piece else "未识别开局"

    if "中炮" in name and len(moves) > 1:
        reply = moves[1]
        reply_name = OPENING_REPLY.get(reply, "")
        if reply in ("h9g7", "b9c7"):
            # 看双方是否形成屏风马（双马都跳正）
            if "h9g7" in moves and "b9c7" in moves:
                name = "中炮对屏风马"
                detail = "黑方双马屏风，红方中炮直攻中路"
            else:
                name = "中炮对单提马/屏风马（未定型）"
                detail = f"黑方第 2 步 {reply_name}"
        elif reply in ("h7e7", "b7e7"):
            name = "中炮对顺炮" if reply == "h7e7" else "中炮对列炮"
            detail = f"黑方以{reply_name}针锋相对"
        elif reply_name:
            name = f"中炮对{reply_name.split('（')[0]}"
            detail = f"黑方第 2 步 {reply_name}"

    opening_plies = min(len(record.plies), 12)
    return {
        "name": name,
        "detail": detail,
        "moves": "、".join(ply.chinese for ply in record.plies[: min(4, len(record.plies))]),
        "plies": str(opening_plies),
    }


def _move_label(index: int, reports: Sequence[Dict[str, object]]) -> Dict[str, object]:
    """给某一步生成统一标签（回合、走子方、走法）。"""
    item = reports[index] if 0 <= index < len(reports) else {}
    played = item.get("played_chinese") or ""
    return {
        "ply": index + 1,
        "move_number": item.get("move_number", 0),
        "side": item.get("side", ""),
        "side_label": item.get("side_label", ""),
        "played": played,
        "best": item.get("best_chinese", ""),
        "loss": item.get("loss_cp"),
        "is_best": bool(item.get("is_best")),
        "score_red": item.get("score_red_cp"),
        "score_text": item.get("score_text", ""),
    }


def collect_stats(reports: Sequence[Dict[str, object]]) -> Dict[str, Dict[str, object]]:
    """按红黑双方统计正着率、平均损失、失误分布。"""
    stats: Dict[str, Dict[str, object]] = {
        "red": {"moves": 0, "best": 0, "blunders": 0, "mistakes": 0, "inaccuracies": 0, "loss_total": 0, "loss_max": 0},
        "black": {"moves": 0, "best": 0, "blunders": 0, "mistakes": 0, "inaccuracies": 0, "loss_total": 0, "loss_max": 0},
    }
    for report in reports:
        side = str(report.get("side") or "red")
        entry = stats.setdefault(side, stats["red"])
        if not report.get("best_iccs"):
            continue
        entry["moves"] += 1
        loss = int(report.get("loss_cp") or 0)
        entry["loss_total"] += loss
        entry["loss_max"] = max(int(entry["loss_max"]), loss)
        if report.get("is_best"):
            entry["best"] += 1
        if loss >= 300:
            entry["blunders"] += 1
        elif loss >= 100:
            entry["mistakes"] += 1
        elif loss >= 50:
            entry["inaccuracies"] += 1
    for entry in stats.values():
        moves = int(entry["moves"]) or 1
        entry["best_rate"] = round(int(entry["best"]) / moves * 100)
        entry["loss_avg"] = round(int(entry["loss_total"]) / moves)
    return stats


def find_turning_points(
    reports: Sequence[Dict[str, object]], limit: int = 5, threshold: int = 120
) -> List[Dict[str, object]]:
    """找出形势波动最大的若干步。"""
    points: List[Dict[str, object]] = []
    for index, report in enumerate(reports):
        if not report.get("best_iccs"):
            continue
        loss = int(report.get("loss_cp") or 0)
        score_now = report.get("score_red_cp")
        score_before = None
        if index > 0:
            score_before = reports[index - 1].get("score_red_cp")
        swing = None
        if isinstance(score_now, int) and isinstance(score_before, int):
            swing = score_now - score_before
        weight = max(loss, abs(swing) if swing is not None else 0)
        if weight < threshold:
            continue
        label = _move_label(index, reports)
        label["swing"] = swing
        label["weight"] = weight
        points.append(label)
    points.sort(key=lambda item: -int(item["weight"]))
    return points[:limit]


def find_blunders(reports: Sequence[Dict[str, object]], limit: int = 5) -> List[Dict[str, object]]:
    """找出致命失误（损失 >= 300 分，或把杀棋走丢）。"""
    blunders: List[Dict[str, object]] = []
    for index, report in enumerate(reports):
        loss = int(report.get("loss_cp") or 0)
        candidates = report.get("candidates") or []
        best_mate = None
        if candidates:
            best_mate = candidates[0].get("mate_in")
        severe = loss >= 300 or (best_mate is not None and best_mate > 0 and not report.get("is_best"))
        if not severe or not report.get("best_iccs"):
            continue
        label = _move_label(index, reports)
        label["reason"] = (
            "错过了杀棋" if (best_mate is not None and best_mate > 0 and not report.get("is_best"))
            else f"损失 {loss} 分"
        )
        blunders.append(label)
    blunders.sort(key=lambda item: -int(item.get("loss") or 0))
    return blunders[:limit]


def score_curve(reports: Sequence[Dict[str, object]]) -> List[int]:
    """红方视角评分曲线（用于画走势图或写报告）。"""
    curve: List[int] = []
    for report in reports:
        score = report.get("score_red_cp")
        curve.append(int(score) if isinstance(score, int) else 0)
    return curve


def phase_of(index: int, total: int) -> str:
    """按步数粗分阶段。"""
    if index < 12:
        return "开局"
    if index >= max(12, total - 10):
        return "残局"
    return "中局"


def loss_text(item: Dict[str, object]) -> str:
    """把某一步的得失写成短语（正着 / 损失 N 分）。"""
    if item.get("is_best"):
        return "这是正着"
    loss = item.get("loss")
    return f"损失 {loss} 分" if loss else "略有损失"


def build_report(record: GameRecord, reports: Sequence[Dict[str, object]], style: str = "棋友") -> Dict[str, object]:
    """汇总一份对局报告（结构化数据 + 给大模型的事实清单）。"""
    stats = collect_stats(reports)
    turning = find_turning_points(reports)
    blunders = find_blunders(reports)
    opening = identify_opening(record)
    curve = score_curve(reports)
    final = curve[-1] if curve else 0

    lines: List[str] = []
    lines.append(
        f"【基本信息】红方 {record.red_name} vs 黑方 {record.black_name}，共 {len(record.plies)} 步；"
        f"结果 {record.headers.get('Result', '*')}；分析深度 {reports[0].get('depth') if reports else '-'}。"
    )
    lines.append(
        f"【开局判断】{opening['name']}（前 {opening['plies']} 步：{opening['moves']}）。{opening.get('detail', '')}"
    )
    if curve:
        early = curve[min(11, len(curve) - 1)]
        lines.append(
            f"【开局结束时的形势】红方视角 {early:+d} 分（{reports[min(11, len(reports) - 1)].get('score_text', '')}）；"
            f"全局结束时 {final:+d} 分。"
        )
    for side, label in (("red", "红方"), ("black", "黑方")):
        entry = stats.get(side, {})
        lines.append(
            f"【{label}数据】共 {entry.get('moves', 0)} 步，正着（与引擎首选一致）{entry.get('best', 0)} 步"
            f"（{entry.get('best_rate', 0)}%），平均每步损失 {entry.get('loss_avg', 0)} 分，"
            f"最大一步损失 {entry.get('loss_max', 0)} 分；疑问手 {entry.get('mistakes', 0)} 次，"
            f"漏着 {entry.get('blunders', 0)} 次。"
        )
    if turning:
        lines.append("【关键转折】")
        for item in turning:
            lines.append(
                f"  第 {item['move_number']} 回合 {item['side_label']} {item['played']}："
                f"{loss_text(item)}，"
                f"引擎正着是 {item['best']}，此时形势 {item.get('score_text', '')}。"
            )
    else:
        lines.append("【关键转折】全盘形势平稳，没有明显的形势反转。")
    if blunders:
        lines.append("【致命失误】")
        for item in blunders:
            lines.append(
                f"  第 {item['move_number']} 回合 {item['side_label']} {item['played']}（{item['reason']}），"
                f"应走 {item['best']}。"
            )
    else:
        lines.append("【致命失误】没有出现损失超过 300 分的严重失误。")

    return {
        "opening": opening,
        "stats": stats,
        "turning_points": turning,
        "blunders": blunders,
        "curve": curve,
        "final_score": final,
        "fact_sheet": "\n".join(lines),
        "style": style,
        "move_count": len(reports),
    }


def render_report_markdown(
    report: Dict[str, object],
    record: GameRecord,
    narrative: Optional[str] = None,
) -> str:
    """把报告渲染成 Markdown（供导出或页面预览）。"""
    opening = report.get("opening", {})  # type: ignore[assignment]
    stats = report.get("stats", {})  # type: ignore[assignment]
    lines: List[str] = [f"# 对局报告：{record.name}", ""]
    lines.append(f"- 红方：{record.red_name}")
    lines.append(f"- 黑方：{record.black_name}")
    lines.append(f"- 结果：{record.headers.get('Result', '*')}")
    lines.append(f"- 开局：{opening.get('name', '-')}（{opening.get('moves', '')}）")
    lines.append(f"- 步数：{report.get('move_count', 0)}")
    lines.append("")

    lines.append("## 双方数据")
    lines.append("")
    lines.append("| 指标 | 红方 | 黑方 |")
    lines.append("| --- | --- | --- |")
    red = stats.get("red", {})  # type: ignore[union-attr]
    black = stats.get("black", {})  # type: ignore[union-attr]
    rows = [
        ("总步数", "moves"),
        ("正着数", "best"),
        ("正着率", "best_rate"),
        ("平均每步损失", "loss_avg"),
        ("最大一步损失", "loss_max"),
        ("疑问手（≥100 分）", "mistakes"),
        ("漏着（≥300 分）", "blunders"),
    ]
    for label, key in rows:
        suffix = "%" if key == "best_rate" else ""
        lines.append(f"| {label} | {red.get(key, 0)}{suffix} | {black.get(key, 0)}{suffix} |")
    lines.append("")

    turning = report.get("turning_points") or []
    if turning:
        lines.append("## 关键转折")
        lines.append("")
        for item in turning:  # type: ignore[union-attr]
            lines.append(
                f"- **第 {item['move_number']} 回合 {item['side_label']} {item['played']}**："
                f"{loss_text(item)}，"
                f"引擎正着 {item['best']}，形势 {item.get('score_text', '')}。"
            )
        lines.append("")

    blunders = report.get("blunders") or []
    if blunders:
        lines.append("## 致命失误")
        lines.append("")
        for item in blunders:  # type: ignore[union-attr]
            lines.append(
                f"- **第 {item['move_number']} 回合 {item['side_label']} {item['played']}**："
                f"{item.get('reason', '')}，应走 {item['best']}。"
            )
        lines.append("")

    if narrative:
        lines.append("## 讲评")
        lines.append("")
        lines.append(narrative.strip())
        lines.append("")
    return "\n".join(lines)
