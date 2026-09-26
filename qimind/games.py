"""棋谱读取：把 PGN / XQF / CBF / CBR / 文本棋谱变成统一的 ``GameRecord``。

统一格式的目的，是让分析层只面对一件事：一串「局面 + 走法」。
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from .cchess_bridge import (
    ChessBoard,
    FULL_INIT_FEN,
    fen_side,
    full_fen,
)
from .config import ensure_cchess_importable

ensure_cchess_importable()

from cchess import Book  # noqa: E402  (需要先插入 sys.path)

SUPPORTED_SUFFIXES = (".pgn", ".xqf", ".cbf", ".cbr")

#: PGN 头部形如 ``[Red "Saturn"]``
PGN_HEADER_RE = re.compile(r'^\[\s*([A-Za-z_][A-Za-z0-9_]*)\s+"(.*?)"\s*\]\s*$', re.MULTILINE)

#: 需要从 PGN 头部补齐的字段（cchess 只解析部分头部）
HEADER_ALIASES = {
    "Red": ("Red", "红方", "RedName"),
    "Black": ("Black", "黑方", "BlackName"),
    "Event": ("Event", "赛事"),
    "Date": ("Date", "日期"),
    "Result": ("Result", "结果"),
}


def read_text_file(path: str | Path) -> str:
    """按常见编码读取文本棋谱（UTF-8 / GBK / GB18030 等）。"""
    raw = Path(path).read_bytes()
    for encoding in ("utf-8-sig", "gb18030", "utf-8", "big5", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def parse_pgn_headers(text: str) -> Dict[str, str]:
    """提取 PGN 头部键值对。"""
    return {match.group(1): match.group(2).strip() for match in PGN_HEADER_RE.finditer(text)}


def merge_headers(record: GameRecord, raw_headers: Dict[str, str]) -> GameRecord:
    """把 PGN 头部补进棋谱信息（已有值不覆盖）。"""
    for canonical, aliases in HEADER_ALIASES.items():
        if record.headers.get(canonical):
            continue
        for alias in aliases:
            value = raw_headers.get(alias)
            if value:
                record.headers[canonical] = value
                break
    if not record.name or record.name in ("未命名棋谱",):
        event = raw_headers.get("Event")
        if event:
            record.name = event
    return record


@dataclass
class Ply:
    """一步棋（半步，含走子前的局面）。"""

    index: int  # 从 1 开始的步数
    move_number: int  # 回合数（红黑各一步为一回合）
    side: str  # "red" / "black"
    iccs: str
    chinese: str
    fen_before: str
    fen_after: str
    comment: str = ""

    def to_dict(self) -> Dict[str, object]:
        return {
            "index": self.index,
            "move_number": self.move_number,
            "side": self.side,
            "iccs": self.iccs,
            "chinese": self.chinese,
            "fen_before": self.fen_before,
            "fen_after": self.fen_after,
            "comment": self.comment,
        }


@dataclass
class GameRecord:
    """一局棋（或一个待分析的局面序列）。"""

    name: str = "未命名棋谱"
    init_fen: str = FULL_INIT_FEN
    plies: List[Ply] = field(default_factory=list)
    source: str = ""
    headers: Dict[str, str] = field(default_factory=dict)

    @property
    def red_name(self) -> str:
        return str(self.headers.get("Red") or self.headers.get("红方") or "红方")

    @property
    def black_name(self) -> str:
        return str(self.headers.get("Black") or self.headers.get("黑方") or "黑方")

    def to_dict(self) -> Dict[str, object]:
        return {
            "name": self.name,
            "init_fen": full_fen(self.init_fen),
            "source": self.source,
            "headers": self.headers,
            "red_name": self.red_name,
            "black_name": self.black_name,
            "plies": [ply.to_dict() for ply in self.plies],
        }


@dataclass
class Position:
    """待分析的单个局面（常见于残局谱）。"""

    name: str
    fen: str

    def to_dict(self) -> Dict[str, object]:
        return {"name": self.name, "fen": full_fen(self.fen), "side": fen_side(self.fen)}


def replay_moves(init_fen: str, moves: Sequence[str], chinese: Optional[Sequence[str]] = None) -> List[Ply]:
    """从初始局面开始重放走法，生成带前后局面的 ``Ply`` 列表。"""
    board = ChessBoard(full_fen(init_fen))
    plies: List[Ply] = []
    for index, iccs in enumerate(moves, start=1):
        fen_before = board.to_fen()
        side = fen_side(fen_before)
        move = board.move_iccs(iccs, check=False)
        if move is None:
            raise ValueError(f"第 {index} 步无法在局面上执行：{iccs}")
        try:
            text = move.to_text()
        except Exception:  # pragma: no cover
            text = iccs
        plies.append(
            Ply(
                index=index,
                move_number=(index + 1) // 2,
                side=side,
                iccs=iccs,
                chinese=(chinese[index - 1] if chinese and index - 1 < len(chinese) else text),
                fen_before=full_fen(fen_before),
                fen_after=full_fen(board.to_fen()),
            )
        )
    return plies


def record_from_iccs(iccs_moves: Iterable[str], init_fen: str = FULL_INIT_FEN, name: str = "自定义局面") -> GameRecord:
    """用 ICCS 走法列表构造棋谱。"""
    moves = list(iccs_moves)
    return GameRecord(name=name, init_fen=init_fen, plies=replay_moves(init_fen, moves))


def record_from_text_moves(moves: Iterable[str], init_fen: str = FULL_INIT_FEN, name: str = "自定义局面") -> GameRecord:
    """用中文走法列表（如 ``炮二平五``）构造棋谱。"""
    board = ChessBoard(full_fen(init_fen))
    plies: List[Ply] = []
    for index, text in enumerate(moves, start=1):
        fen_before = board.to_fen()
        move = board.move_text(text, check=False)
        if move is None:
            raise ValueError(f"第 {index} 步中文走法无法识别：{text}")
        plies.append(
            Ply(
                index=index,
                move_number=(index + 1) // 2,
                side=fen_side(fen_before),
                iccs=move.to_iccs(),
                chinese=text,
                fen_before=full_fen(fen_before),
                fen_after=full_fen(board.to_fen()),
            )
        )
    return GameRecord(name=name, init_fen=full_fen(init_fen), plies=plies)


def _book_to_record(book: Book, name: str, source: str = "") -> GameRecord:
    """把 cchess 的 ``Book`` 转换成一局 ``GameRecord``。"""
    init_fen = book.init_board.to_fen()
    moves = book.move_line_to_list()
    iccs: List[str] = []
    comments: List[str] = []
    for move in moves:
        try:
            iccs.append(move.to_iccs())
        except Exception:  # pragma: no cover
            continue
        comments.append(getattr(move, "annote", "") or "")

    plies = replay_moves(init_fen, iccs)
    for ply, comment in zip(plies, comments):
        ply.comment = comment

    headers = {str(k): str(v) for k, v in dict(getattr(book, "info", {}) or {}).items()}
    return GameRecord(
        name=name,
        init_fen=full_fen(init_fen),
        plies=plies,
        source=source,
        headers=headers,
    )


def load_record(path: str | Path, name: Optional[str] = None) -> GameRecord:
    """读取单个棋谱文件（.pgn/.xqf/.cbf/.cbr）。"""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"棋谱文件不存在：{path}")
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise ValueError(f"暂不支持的文件格式：{suffix}（支持 {'、'.join(SUPPORTED_SUFFIXES)}）")
    book = Book.read_from(str(path))
    record = _book_to_record(book, name or path.stem, source=str(path))
    if suffix == ".pgn":
        # cchess 只解析部分头部，这里补齐红黑双方等信息
        merge_headers(record, parse_pgn_headers(read_text_file(path)))
    return record


def record_from_pgn_text(text: str, name: str = "粘贴的棋谱") -> GameRecord:
    """从 PGN 文本构造棋谱（内部借道临时文件，复用 cchess 的解析器）。"""
    text = text.strip()
    if not text:
        raise ValueError("棋谱内容为空")
    with tempfile.NamedTemporaryFile("w", suffix=".pgn", encoding="utf-8", delete=False) as fp:
        fp.write(text)
        temp_path = Path(fp.name)
    try:
        book = Book.read_from(str(temp_path))
        record = _book_to_record(book, name, source="<粘贴文本>")
        return merge_headers(record, parse_pgn_headers(text))
    finally:
        try:
            temp_path.unlink()
        except OSError:  # pragma: no cover
            pass


def load_positions_txt(path: str | Path) -> List[Position]:
    """读取「名称,FEN」逐行排列的局面集合（如残局题库）。

    这类文件常见 GBK 编码，这里会逐个尝试常见编码。
    """
    text = read_text_file(path)
    positions: List[Position] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or "," not in line:
            continue
        label, fen = line.split(",", 1)
        fen = fen.strip()
        if not fen:
            continue
        try:
            ChessBoard(full_fen(fen))
        except Exception:
            continue
        positions.append(Position(name=label.strip(), fen=full_fen(fen)))
    return positions
