"""与 cchess 库之间的薄封装：FEN、坐标、中文记谱、吃子与将军判定。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .config import ensure_cchess_importable

ensure_cchess_importable()

from cchess import (  # noqa: E402  (必须先插入 sys.path)
    SIDE_BLACK,
    SIDE_RED,
    ChessBoard,
    FULL_INIT_FEN,
    iccs2pos,
    pos2iccs,
)

__all__ = [
    "ChessBoard",
    "FULL_INIT_FEN",
    "SIDE_RED",
    "SIDE_BLACK",
    "PIECE_NAMES",
    "PIECE_VALUES",
    "MoveFacts",
    "full_fen",
    "fen_side",
    "side_label",
    "piece_name",
    "piece_value",
    "pos_text",
    "pos_label",
    "route_label",
    "cn_file",
    "iccs_to_chinese",
    "move_facts",
    "board_after",
    "legal_move_count",
    "square_attackers",
    "is_square_attacked",
    "material_summary",
]

#: FEN 字符 → 中文棋子名（逗号前为红方，逗号后为黑方）
PIECE_NAMES: Dict[str, Tuple[str, str]] = {
    "k": ("帅", "将"),
    "a": ("仕", "士"),
    "b": ("相", "象"),
    "n": ("马", "马"),
    "r": ("车", "车"),
    "c": ("炮", "炮"),
    "p": ("兵", "卒"),
}

#: 常规子力价值（兵未过河 100，过河兵按 200 计）
PIECE_VALUES: Dict[str, int] = {
    "k": 100000,
    "a": 200,
    "b": 200,
    "n": 400,
    "r": 900,
    "c": 450,
    "p": 100,
}

_CN_DIGITS = "〇一二三四五六七八九"  # 索引即数字，0 位占位

#: 红方视角的列（路）名：x=8 是最右边，即「一路」；x=0 是最左边，即「九路」
CN_FILE_DIGITS = "一二三四五六七八九"

def full_fen(fen: str) -> str:
    """把 cchess 的两段式 FEN 补全为引擎可用的六段式 FEN。"""
    parts = fen.strip().split()
    board = parts[0] if parts else ""
    side = parts[1] if len(parts) > 1 else "w"
    side = "b" if side in ("b", "B", "black") else "w"
    return f"{board} {side} - - 0 1"


def fen_side(fen: str) -> str:
    """返回 ``"red"`` 或 ``"black"``：FEN 中的走子方。"""
    parts = fen.strip().split()
    return "black" if len(parts) > 1 and parts[1] in ("b", "B") else "red"


def side_label(side: str) -> str:
    """把 ``"red"``/``"black"`` 转成中文。"""
    return "红方" if side in ("red", "r", "w") else "黑方"


def piece_name(fench: str) -> str:
    """FEN 字符 → 中文棋子名（大写为红方）。"""
    if fench in (".", "", None):
        return ""
    names = PIECE_NAMES.get(fench.lower())
    if not names:
        return fench
    return names[0] if fench.isupper() else names[1]


def piece_value(fench: str, pos: Optional[Tuple[int, int]] = None) -> int:
    """棋子价值；未过河的兵按 100，过河兵按 200。"""
    if not fench or fench == ".":
        return 0
    lower = fench.lower()
    if lower == "p" and pos is not None:
        # 红兵向 y 增大方向前进，黑卒向 y 减小方向前进
        crossed = pos[1] >= 5 if fench.isupper() else pos[1] <= 4
        return 200 if crossed else 100
    return PIECE_VALUES.get(lower, 0)


def pos_text(pos: Tuple[int, int]) -> str:
    """坐标转 ICCS 文本，例如 ``(0, 9) -> "a9"``。"""
    return f"{chr(ord('a') + int(pos[0]))}{int(pos[1])}"


def cn_file(x: int) -> str:
    """列号转中文「路」名：红方视角，一在最右、九在最左。

    与中文记谱法一致：红方二路炮就是 ``x=7`` 上的炮（炮二平五）。
    """
    x = int(x)
    return CN_FILE_DIGITS[8 - x] if 0 <= x <= 8 else "?"


def pos_label(pos: Tuple[int, int]) -> str:
    """坐标转棋盘上看得见的中文标注，例如 ``(4, 2) -> "五路3行"``。

    列用中文数字（红方视角，一在最右），行从红方底线数起（1 是红方底线、10 是黑方底线），
    与界面棋盘边缘标注完全一致。
    """
    return f"{cn_file(int(pos[0]))}路{int(pos[1]) + 1}行"


def route_label(pos_from: Tuple[int, int], pos_to: Tuple[int, int]) -> str:
    """走法路径的中文标注，例如 ``五路3行→五路7行``。"""
    return f"{pos_label(pos_from)}→{pos_label(pos_to)}"


def iccs_to_chinese(fen: str, iccs: str) -> str:
    """把 ICCS 走法（如 ``b2e2``）转成中文记谱（如 ``炮八平五``）。"""
    board = ChessBoard(full_fen(fen))
    move = board.move_iccs(iccs, check=False)
    if move is None:
        return iccs
    try:
        return move.to_text()
    except Exception:  # pragma: no cover - 记谱失败时退回 ICCS
        return iccs


@dataclass
class MoveFacts:
    """单步走法的基础事实（不依赖引擎）。"""

    iccs: str
    chinese: str
    side: str
    from_pos: Tuple[int, int]
    to_pos: Tuple[int, int]
    moving_fench: str
    moving_name: str
    captured_fench: Optional[str]
    captured_name: str
    captured_value: int
    is_check: bool
    is_checkmate: bool
    fen_before: str
    fen_after: str

    @property
    def is_capture(self) -> bool:
        return bool(self.captured_fench)

    def describe(self) -> str:
        """一句话描述这步棋做了什么。"""
        bits: List[str] = []
        if self.captured_fench:
            bits.append(f"吃掉黑方{self.captured_name}" if self.side == "red" else f"吃掉红方{self.captured_name}")
        if self.is_checkmate:
            bits.append("直接将死对方")
        elif self.is_check:
            bits.append("将军")
        if not bits:
            bits.append("既不吃子也不将军")
        return "，".join(bits)


def board_after(fen: str, iccs: str) -> Optional[Tuple[ChessBoard, str]]:
    """在 ``fen`` 局面上走 ``iccs``，返回（新棋盘, 新 FEN）；非法走法返回 ``None``。"""
    board = ChessBoard(full_fen(fen))
    move = board.move_iccs(iccs, check=False)
    if move is None or board is None:
        return None
    return board, board.to_fen()


def move_facts(fen: str, iccs: str) -> Optional[MoveFacts]:
    """解析一步走法，给出中文记谱、吃子、将军等事实。"""
    board = ChessBoard(full_fen(fen))
    side = fen_side(fen)
    pos_from, pos_to = iccs2pos(iccs)
    moving_before = board.get_fench(pos_from)
    move = board.move(pos_from, pos_to, check=True)
    if move is None:
        return None

    captured = getattr(move, "captured", None) or getattr(move, "captured_fench", None)
    captured_fench = captured or None
    try:
        chinese = move.to_text()
    except Exception:  # pragma: no cover
        chinese = iccs

    return MoveFacts(
        iccs=iccs,
        chinese=chinese,
        side=side,
        from_pos=pos_from,
        to_pos=pos_to,
        moving_fench=moving_before,
        moving_name=piece_name(moving_before),
        captured_fench=captured_fench,
        captured_name=piece_name(captured_fench or ""),
        captured_value=piece_value(captured_fench or "", pos_to),
        is_check=bool(getattr(move, "is_checking", False)),
        is_checkmate=bool(getattr(move, "is_checkmate", False)),
        fen_before=full_fen(fen),
        fen_after=board.to_fen(),
    )


def legal_move_count(fen: str, color: Optional[int] = None) -> int:
    """统计某一方的大致可走招数（只做棋子规则过滤，含送将走法）。"""
    board = ChessBoard(full_fen(fen))
    if color is not None:
        board = board.copy().set_move_side(color)
    return sum(1 for _ in board.create_moves())


def square_attackers(fen: str, pos: Tuple[int, int], by_color: int) -> List[Tuple[Tuple[int, int], str]]:
    """列出能"吃掉站在 pos 上的棋子"的 ``by_color`` 方棋子。

    判断口径是**吃子语义**：把 ``pos`` 临时换成一枚对方颜色的兵，
    再看 ``by_color`` 方哪些棋子能走到/吃到这一格。这样既能正确判断
    "某格是否被保护"（走掉原棋子后，队友能不能补上），也能正确处理
    炮"必须隔一子才能吃"的规则。
    """
    board = ChessBoard(full_fen(fen))
    hostile = "p" if by_color == SIDE_RED else "P"
    board.put_fench(hostile, (int(pos[0]), int(pos[1])))
    board.set_move_side(by_color)
    attackers: List[Tuple[Tuple[int, int], str]] = []
    for fench, piece_pos in board.get_all_fench_positions(by_color):
        for _frm, to_pos in board.create_piece_moves(piece_pos):
            if tuple(to_pos) == tuple(pos):
                attackers.append((tuple(piece_pos), fench))
                break
    return attackers


def is_square_attacked(fen: str, pos: Tuple[int, int], by_color: int) -> bool:
    """判断空格或棋子格是否被 ``by_color`` 方攻击。"""
    return bool(square_attackers(fen, pos, by_color))


def material_summary(fen: str) -> Dict[str, object]:
    """统计双方子力，返回数量、价值与差值（正数表示红方占优）。"""
    board = ChessBoard(full_fen(fen))
    red: Dict[str, int] = {}
    black: Dict[str, int] = {}
    red_value = 0
    black_value = 0
    for fench, pos in board.get_all_fench_positions():
        key = fench.lower()
        value = piece_value(fench, tuple(pos))
        if fench.isupper():
            red[key] = red.get(key, 0) + 1
            red_value += value
        else:
            black[key] = black.get(key, 0) + 1
            black_value += value
    return {
        "red": red,
        "black": black,
        "red_value": red_value,
        "black_value": black_value,
        "diff": red_value - black_value,
    }
