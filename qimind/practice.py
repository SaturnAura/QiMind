"""人机对练：只和引擎下棋，不调用 DeepSeek，下完可以导出棋谱。

一条对练会话记录初始局面、双方走法与结果，并能导出成标准 PGN，
之后可以直接用棋谱讲解流程（引擎 + DeepSeek）复盘自己刚下的棋。
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from .cchess_bridge import (
    ChessBoard,
    FULL_INIT_FEN,
    fen_side,
    full_fen,
    iccs2pos,
    side_label,
)
from .config import DATA_DIR
from .games import replay_moves

logger = logging.getLogger(__name__)

#: 难度 → 引擎搜索深度（入门档故意下得弱一些，方便练手）
LEVELS: Dict[str, int] = {
    "入门": 2,
    "初级": 4,
    "中等": 8,
    "较强": 12,
    "最强": 16,
}


@dataclass
class PracticeSession:
    """一局人机对练。"""

    id: str
    player_side: str = "red"  # 玩家执红
    level: str = "中等"
    depth: int = 8
    init_fen: str = FULL_INIT_FEN
    fen: str = FULL_INIT_FEN
    moves: List[Dict[str, str]] = field(default_factory=list)
    result: str = "*"
    finished: bool = False
    message: str = ""
    player_name: str = "玩家"
    engine_name: str = "QiMind 引擎"
    created_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    # ------------------------------------------------------------------ 属性
    @property
    def engine_side(self) -> str:
        return "black" if self.player_side == "red" else "red"

    @property
    def side_to_move(self) -> str:
        return fen_side(self.fen)

    @property
    def player_turn(self) -> bool:
        return not self.finished and self.side_to_move == self.player_side

    def to_dict(self) -> Dict[str, object]:
        return {
            "id": self.id,
            "player_side": self.player_side,
            "player_side_label": side_label(self.player_side),
            "engine_side": self.engine_side,
            "level": self.level,
            "depth": self.depth,
            "init_fen": full_fen(self.init_fen),
            "fen": full_fen(self.fen),
            "side_to_move": self.side_to_move,
            "side_to_move_label": side_label(self.side_to_move),
            "player_turn": self.player_turn,
            "moves": list(self.moves),
            "move_count": len(self.moves),
            "result": self.result,
            "result_text": _result_text(self.result, self.player_side),
            "finished": self.finished,
            "message": self.message,
            "player_name": self.player_name,
            "engine_name": self.engine_name,
            "created_at": self.created_at,
        }

    # ------------------------------------------------------------------ 走子
    def apply_move(self, iccs: str, note_checks: bool = True) -> Dict[str, str]:
        """在会话里走一步（合法性由棋子库校验），返回这步棋的信息。"""
        if self.finished:
            raise ValueError("对局已经结束，请开始新对局")
        board = ChessBoard(full_fen(self.fen))
        side = fen_side(self.fen)
        pos_from, pos_to = iccs2pos(iccs)
        try:
            illegal = board.leaves_king_in_check(pos_from, pos_to)
        except Exception as exc:
            raise ValueError("这步棋不合法") from exc
        if illegal:
            raise ValueError("这步棋会让自己的将帅被将军")
        move = board.move(pos_from, pos_to, check=True)
        if move is None:
            raise ValueError("这步棋不合法")

        info = {
            "iccs": iccs,
            "chinese": move.to_text(),
            "side": side,
            "fen_after": board.to_fen(),
            "captured": move.captured or "",
        }
        if note_checks:
            info["is_check"] = "1" if getattr(move, "is_checking", False) else ""
        self.moves.append(info)
        self.fen = board.to_fen()
        self._check_finished()
        return info

    def _check_finished(self) -> None:
        """判断对局是否结束（将死或困毙）。"""
        board = ChessBoard(full_fen(self.fen))
        if not board.is_checkmate() and not board.has_no_legal_moves():
            return
        # 刚刚走子的一方获胜（象棋里困毙也判负）
        winner = "red" if fen_side(self.fen) == "black" else "black"
        self.finished = True
        self.result = "1-0" if winner == "red" else "0-1"
        self.message = (
            f"{side_label(winner)}将死/困毙对手，"
            f"{'你赢了！' if winner == self.player_side else '你输了，再来一局？'}"
        )

    def undo(self) -> bool:
        """悔棋：退回到玩家上一次该走棋的时候（一次退两手）。"""
        if not self.moves:
            return False
        self.finished = False
        self.result = "*"
        self.message = ""
        while self.moves:
            self.moves.pop()
            self.fen = self.moves[-1]["fen_after"] if self.moves else self.init_fen
            if self.side_to_move == self.player_side:
                break
        return True

    def resign(self) -> None:
        """玩家认输。"""
        self.finished = True
        self.result = "0-1" if self.player_side == "red" else "1-0"
        self.message = "你已认输。"

    # ------------------------------------------------------------------ 导出
    def to_pgn(self) -> str:
        """导出为标准 PGN 文本（中文记谱，可被本工具重新导入）。"""
        plies = replay_moves(self.init_fen, [item["iccs"] for item in self.moves])
        lines = [
            '[Game "Chinese Chess"]',
            '[Event "QiMind 人机对练"]',
            f'[Date "{datetime.now().strftime("%Y-%m-%d")}"]',
            f'[Red "{self.player_name if self.player_side == "red" else self.engine_name}"]',
            f'[Black "{self.player_name if self.player_side == "black" else self.engine_name}"]',
            f'[Result "{self.result}"]',
            f'[FEN "{full_fen(self.init_fen)}"]',
            f'[Engine "Pikafish / {self.level}"]',
            "",
        ]
        body = ""
        for index, ply in enumerate(plies, start=1):
            if (index - 1) % 2 == 0:
                body += f"{(index + 1) // 2}."
            body += f" {ply.chinese}"
            if index % 2 == 0:
                lines.append(body)
                body = ""
        if body:
            lines.append(body)
        if self.result != "*":
            lines.append(self.result)
        return "\n".join(lines) + "\n"

    def save(self, directory: Optional[Path] = None) -> Path:
        """把棋谱写入磁盘（默认 ``qimind/data/practice/``）。"""
        target_dir = directory or (DATA_DIR / "practice")
        target_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        side = "红方" if self.player_side == "red" else "黑方"
        path = target_dir / f"人机对练_{stamp}_{side}.pgn"
        path.write_text(self.to_pgn(), encoding="utf-8")
        return path


def _result_text(result: str, player_side: str) -> str:
    if result == "*":
        return "进行中"
    if result == "1-0":
        return "红方胜" + ("（你赢了）" if player_side == "red" else "（你输了）")
    if result == "0-1":
        return "黑方胜" + ("（你赢了）" if player_side == "black" else "（你输了）")
    return "和棋"


# ---------------------------------------------------------------------- 会话管理
_SESSIONS: Dict[str, PracticeSession] = {}
_LOCK = threading.Lock()


def new_session(
    player_side: str = "red",
    level: str = "中等",
    depth: Optional[int] = None,
    player_name: str = "玩家",
) -> PracticeSession:
    """创建一局新对练。"""
    player_side = "black" if str(player_side).lower() in ("black", "b", "黑方") else "red"
    level = level if level in LEVELS else "中等"
    session = PracticeSession(
        id=uuid.uuid4().hex[:12],
        player_side=player_side,
        level=level,
        depth=int(depth or LEVELS[level]),
        player_name=player_name or "玩家",
        engine_name=f"QiMind（{level}）",
    )
    with _LOCK:
        _SESSIONS[session.id] = session
        # 只保留最近 20 局，避免长时间运行后内存膨胀
        if len(_SESSIONS) > 20:
            for key in list(_SESSIONS)[:-20]:
                _SESSIONS.pop(key, None)
    return session


def get_session(session_id: str) -> PracticeSession:
    with _LOCK:
        session = _SESSIONS.get(session_id)
    if session is None:
        raise KeyError(f"对练会话不存在或已过期：{session_id}")
    return session


def engine_move(session: PracticeSession, use_cache: bool = True) -> Optional[Dict[str, str]]:
    """让引擎在会话当前局面上走一步，返回走法信息。"""
    if session.finished or session.side_to_move == session.player_side:
        return None
    from .engine import analyze_fen

    report = analyze_fen(session.fen, depth=session.depth, multipv=1, use_cache=use_cache)
    best = report.best
    if best is None:
        raise RuntimeError("引擎没有给出走法")
    info = session.apply_move(best.iccs)
    info["comment"] = f"引擎正着评分 {best.score_text}"
    return info
