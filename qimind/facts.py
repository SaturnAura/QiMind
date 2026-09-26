"""局面事实提取：把棋盘翻译成"讲解员需要知道的事实清单"。

这些事实全部是可验证的（子力、吃子、将军、悬子、王的安全、机动性、
引擎评分与主线），先给大模型事实、再让它组织语言，可以显著减少
"一本正经地胡说"的情况。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .cchess_bridge import (
    SIDE_BLACK,
    SIDE_RED,
    ChessBoard,
    fen_side,
    full_fen,
    legal_move_count,
    material_summary,
    move_facts,
    piece_name,
    piece_value,
    pos_label,
    route_label,
    side_label,
    square_attackers,
)
from .engine import EngineReport, MATE_SCORE

#: 各棋子中文名（用于渲染子力表）
PIECE_ORDER = ("r", "n", "c", "a", "b", "p")
PIECE_LABEL = {"r": "车", "n": "马", "c": "炮", "a": "士", "b": "象", "p": "兵"}


def color_of(side: str) -> int:
    """``"red"``/``"black"`` → cchess 颜色常量。"""
    return SIDE_RED if side == "red" else SIDE_BLACK


def red_perspective(side: str, score_cp: Optional[int], mate_in: Optional[int]) -> Tuple[Optional[int], Optional[int]]:
    """把引擎评分（走子方视角）换算成红方视角。"""
    sign = 1 if side == "red" else -1
    cp = None if score_cp is None else score_cp * sign
    mate = None if mate_in is None else mate_in * sign
    return cp, mate


def score_label(score_cp: Optional[int], mate_in: Optional[int]) -> str:
    """把评分翻译成人类语言（红方视角）。"""
    if mate_in is not None:
        if mate_in > 0:
            return f"红方 {mate_in} 步内绝杀"
        return f"黑方 {abs(mate_in)} 步内绝杀"
    if score_cp is None:
        return "无明显优势"
    if score_cp >= MATE_SCORE - 2000 or score_cp <= -(MATE_SCORE - 2000):
        return "已接近绝杀"
    magnitude = abs(score_cp)
    who = "红方" if score_cp > 0 else "黑方"
    if magnitude < 40:
        return "大致均势"
    if magnitude < 100:
        return f"{who}稍占上风"
    if magnitude < 250:
        return f"{who}占优"
    if magnitude < 600:
        return f"{who}明显优势"
    if magnitude < 1500:
        return f"{who}大优"
    return f"{who}胜势"


def phase_label(fen: str) -> str:
    """粗判对局阶段：开局 / 中局 / 残局。"""
    material = material_summary(fen)
    total = int(material["red_value"]) + int(material["black_value"]) - 2 * 100000
    board = ChessBoard(full_fen(fen))
    piece_count = len(list(board.get_all_fench_positions()))
    if piece_count <= 12 or total <= 2000:
        return "残局"
    if total >= 4200 and piece_count >= 28:
        return "开局"
    return "中局"


def material_text(fen: str) -> str:
    """生成子力对比说明。"""
    material = material_summary(fen)
    red, black = material["red"], material["black"]  # type: ignore[assignment]

    def render(counts: Dict[str, int], side: str) -> str:
        parts = []
        for key in PIECE_ORDER:
            if not counts.get(key):
                continue
            fench = key.upper() if side == "red" else key
            parts.append(f"{piece_name(fench)}{counts[key]}")
        return " ".join(parts) if parts else "无"

    red_value = int(material["red_value"]) - 100000
    black_value = int(material["black_value"]) - 100000
    diff = red_value - black_value
    if diff > 0:
        compare = f"红方净多约 {diff} 分（按车 900/炮 450/马 400/兵 100 折算）"
    elif diff < 0:
        compare = f"黑方净多约 {-diff} 分（按车 900/炮 450/马 400/兵 100 折算）"
    else:
        compare = "双方子力完全相当"
    return (
        f"红方：{render(red, 'red')}（子力 {red_value}）；"
        f"黑方：{render(black, 'black')}（子力 {black_value}）。{compare}。"
    )


@dataclass
class ThreatInfo:
    """一个受威胁的棋子。"""

    pos: Tuple[int, int]
    fench: str
    name: str
    value: int
    attackers: List[str]
    defended: bool

    @property
    def hanging(self) -> bool:
        return not self.defended

    def describe(self) -> str:
        where = pos_label(self.pos)
        attacker_text = "、".join(self.attackers) if self.attackers else "无"
        if self.hanging:
            return f"{self.name}（坐标 {where}）被{attacker_text}攻击且没有保护"
        return f"{self.name}（坐标 {where}）被{attacker_text}攻击，但有己方子力保护"


def square_attackers_detail(fen: str, pos: Tuple[int, int], by_color: int) -> List[str]:
    """返回攻击某格子的棋子名列表（按吃子语义）。"""
    return [piece_name(fench) for _pos, fench in square_attackers(fen, pos, by_color)]


def threatened_pieces(
    fen: str, target_side: str, limit: int = 6, hanging_only: bool = False
) -> List[ThreatInfo]:
    """列出 ``target_side`` 一方被攻击的棋子（含是否有保护）。

    ``hanging_only=True`` 时只保留「被攻击且无人保护」的棋子，
    适合在讲解前过滤掉无关的兑子威胁。
    """
    board = ChessBoard(full_fen(fen))
    ours = color_of(target_side)
    theirs = SIDE_BLACK if ours == SIDE_RED else SIDE_RED
    result: List[ThreatInfo] = []
    for fench, pos in board.get_all_fench_positions(ours):
        if fench.lower() == "k":
            continue
        pos = tuple(pos)
        attackers = square_attackers_detail(fen, pos, theirs)
        if not attackers:
            continue
        defenders = square_attackers_detail(fen, pos, ours)
        if hanging_only and defenders:
            continue
        result.append(
            ThreatInfo(
                pos=pos,
                fench=fench,
                name=piece_name(fench),
                value=piece_value(fench, pos),
                attackers=attackers,
                defended=bool(defenders),
            )
        )
    result.sort(key=lambda item: (0 if item.hanging else 1, -item.value))
    return result[:limit]


def king_safety_text(fen: str) -> str:
    """士象完整性（王的安全）简述。"""
    board = ChessBoard(full_fen(fen))
    counts = {"A": 0, "B": 0, "a": 0, "b": 0}
    for fench, _pos in board.get_all_fench_positions():
        if fench in counts:
            counts[fench] += 1

    def render(advisor: int, elephant: int, side: str) -> str:
        missing: List[str] = []
        if advisor < 2:
            missing.append("缺士" if advisor == 0 else "少一士")
        if elephant < 2:
            missing.append("缺象" if elephant == 0 else "少一象")
        name = "红方" if side == "red" else "黑方"
        return f"{name}士象完整" if not missing else f"{name}{'、'.join(missing)}"

    return "；".join([render(counts["A"], counts["B"], "red"), render(counts["a"], counts["b"], "black")]) + "。"


def urgency_text(report: EngineReport) -> str:
    """根据首选与次选的差距判断这步棋的"紧迫性/唯一性"。"""
    if len(report.candidates) < 2:
        return "引擎只给出了一路候选，无法比较。"
    best, second = report.candidates[0], report.candidates[1]
    if best.mate_in is not None and best.mate_in > 0:
        return "引擎已经找到杀棋，这一步是唯一的取胜路线。"
    if best.score_cp is None or second.score_cp is None:
        return "候选着法之间的差距无法直接比较。"
    gap = best.score_cp - second.score_cp
    if gap >= 150:
        return f"首选比次选好 {gap} 分，属于**唯一正着**，走错会明显变差。"
    if gap >= 50:
        return f"首选比次选好 {gap} 分，正着比较明确，但还有其他可下的棋。"
    if gap >= 20:
        return f"首选比次选只好 {gap} 分，属于「稍有差别」的好棋。"
    return f"首选与次选只差 {gap} 分，这几步棋差别不大，都算合理。"


@dataclass
class FactSheet:
    """给大模型的事实清单（同时保留结构化数据供界面展示）。"""

    text: str
    data: Dict[str, object] = field(default_factory=dict)


def build_fact_sheet(
    fen: str,
    report: EngineReport,
    played_iccs: Optional[str] = None,
    played_score_cp: Optional[int] = None,
    move_number: Optional[int] = None,
) -> FactSheet:
    """把局面 + 引擎结果整理成中文事实清单。

    参数:
        fen: 分析时的局面（走子方即将行棋）。
        report: 引擎分析结果。
        played_iccs: 实战走法（棋谱里的那一步），用于判断是否与正着一致。
        played_score_cp: 实战走法的评分（走子方视角），由引擎复算得到。
        move_number: 回合数（用于说明是第几回合）。
    """
    side = fen_side(fen)
    best = report.best
    data: Dict[str, object] = {}

    red_cp, red_mate = (None, None)
    if best:
        red_cp, red_mate = red_perspective(side, best.score_cp, best.mate_in)

    lines: List[str] = []
    lines.append(
        "【坐标说明】列用中文数字、按红方视角（一在最右、九在最左，与中文记谱的「路」一致）；"
        "行从红方底线数起，1 是红方底线、10 是黑方底线。棋盘边缘标有同样的数字。"
    )
    header = f"【阶段】{phase_label(fen)}"
    if move_number:
        header += f"（第 {move_number} 回合）"
    header += f"；当前轮到{side_label(side)}走棋"
    lines.append(header)

    if best:
        lines.append(
            f"【引擎结论】引擎首选：{best.chinese}（{best.score_text}，"
            f"红方视角 {red_cp if red_cp is not None else '—'}），形势判断：{score_label(red_cp, red_mate)}"
        )

    lines.append("【候选着法】")
    for candidate in report.candidates[:5]:
        cp_red, mate_red = red_perspective(side, candidate.score_cp, candidate.mate_in)
        tag = " ← 引擎首选" if candidate.rank == 1 else ""
        if played_iccs and candidate.iccs.lower() == played_iccs.lower():
            tag = " ← 实战走法" + ("（与正着一致）" if candidate.rank == 1 else f"（引擎排第 {candidate.rank}）")
        lines.append(
            f"  {candidate.rank}. {candidate.chinese}  评分 {candidate.score_text}"
            f"（{score_label(cp_red, mate_red)}），后续：{'、'.join(candidate.pv_chinese[:5])}{tag}"
        )
    lines.append(f"【紧迫性】{urgency_text(report)}")

    lines.append(f"【子力】{material_text(fen)}")
    lines.append(f"【王的安全】{king_safety_text(fen)}")

    board = ChessBoard(full_fen(fen))
    ours = color_of(side)
    theirs = SIDE_BLACK if ours == SIDE_RED else SIDE_RED
    try:
        red_moves = legal_move_count(fen, SIDE_RED)
        black_moves = legal_move_count(fen, SIDE_BLACK)
        lines.append(
            f"【机动性】当前局面红方约 {red_moves} 种走法，黑方约 {black_moves} 种走法。"
        )
        data["mobility"] = {"red": red_moves, "black": black_moves}
    except Exception:  # pragma: no cover - 极端局面下放弃机动性
        pass

    if board.is_checking():
        lines.append(f"【当前状态】{side_label(side)}正在被将军，必须先应将。")

    # 双方的受威胁棋子（走子前只关心真正的悬子，避免噪音）
    opponent_threats = threatened_pieces(fen, "black" if side == "red" else "red", hanging_only=True)
    own_threats = threatened_pieces(fen, side, hanging_only=True)
    if opponent_threats:
        lines.append(
            "【对方受威胁的棋子】"
            + "；".join(item.describe() for item in opponent_threats)
            + "。"
        )
    if own_threats:
        lines.append("【我方受威胁的棋子】" + "；".join(item.describe() for item in own_threats) + "。")
    if not opponent_threats and not own_threats:
        lines.append("【威胁】双方暂时没有棋子处在「被攻击且无保护」的状态。")

    # 实战走法的具体分析
    played_facts = None
    if played_iccs:
        played_facts = move_facts(fen, played_iccs)
    if played_facts and best:
        route = route_label(played_facts.from_pos, played_facts.to_pos)
        lines.append(
            f"【实战走法】{played_facts.chinese}（{played_facts.moving_name} {route}）："
            f"{played_facts.describe()}。"
        )
        after_fen = played_facts.fen_after
        # 走完之后，这枚棋子和对方的受威胁情况
        next_side = "black" if side == "red" else "red"
        counter_threats = threatened_pieces(after_fen, next_side)
        own_after = threatened_pieces(after_fen, side)
        if counter_threats:
            lines.append(
                "【走完后对方的麻烦】"
                + "；".join(item.describe() for item in counter_threats)
                + "。"
            )
        if own_after:
            lines.append(
                "【走完后自己的风险】"
                + "；".join(item.describe() for item in own_after)
                + "。"
            )
        if played_score_cp is not None:
            best_cp = best.score_cp if best.score_cp is not None else 0
            if best.mate_in is not None and best.mate_in > 0:
                loss = 0
            else:
                loss = max(0, best_cp - played_score_cp)
            played_red, played_mate = red_perspective(side, played_score_cp, None)
            lines.append(
                f"【实战走法评分】{played_facts.chinese} 复算评分 {played_score_cp:+d}"
                f"（红方视角 {played_red}），与正着相差 {loss} 分"
                f"（{_loss_label(loss)}）。"
            )
            data["loss_cp"] = loss

    text = "\n".join(lines)
    data.update(
        {
            "fen": fen,
            "side": side,
            "phase": phase_label(fen),
            "best_iccs": report.best_iccs,
            "best_chinese": best.chinese if best else "",
            "score_red_cp": red_cp,
            "score_text": score_label(red_cp, red_mate),
            "is_best": bool(played_iccs and report.best_iccs and played_iccs.lower() == report.best_iccs.lower()),
            "candidates": [
                {
                    "rank": c.rank,
                    "iccs": c.iccs,
                    "chinese": c.chinese,
                    "score_text": c.score_text,
                    "score_cp": c.score_cp,
                    "mate_in": c.mate_in,
                    "pv_chinese": c.pv_chinese,
                }
                for c in report.candidates
            ],
        }
    )
    return FactSheet(text=text, data=data)


def _loss_label(loss: int) -> str:
    """把与正着的分差翻译成人话。"""
    if loss <= 20:
        return "与正着基本等价"
    if loss <= 60:
        return "小有损失，仍属可下之着"
    if loss <= 150:
        return "明显亏了"
    if loss <= 400:
        return "属于疑问手"
    return "是比较严重的漏着"
