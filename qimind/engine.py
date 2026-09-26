"""Pikafish（皮卡鱼）引擎客户端。

只实现 QiMind 需要的最小 UCI 子集：设置选项、多路（MultiPV）分析、
返回带评分与后续主线的候选走法。引擎是重量级资源，整个进程共用
一个实例，并用锁串行化调用。
"""

from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .cchess_bridge import ChessBoard, full_fen, iccs_to_chinese
from .config import cache_dir, engine_path, load_settings

logger = logging.getLogger(__name__)

MATE_SCORE = 30000


class EngineNotFoundError(RuntimeError):
    """找不到或无法启动引擎时抛出。"""


@dataclass
class Candidate:
    """一个候选走法（MultiPV 的一路）。"""

    rank: int
    iccs: str
    score_cp: Optional[int] = None
    mate_in: Optional[int] = None
    depth: int = 0
    pv: List[str] = field(default_factory=list)
    chinese: str = ""
    pv_chinese: List[str] = field(default_factory=list)

    @property
    def score_text(self) -> str:
        """人类可读的评分文本，例如 ``+32`` 或 ``红方 3 步杀``。"""
        if self.mate_in is not None:
            if self.mate_in > 0:
                return f"{self.mate_in} 步杀"
            return f"被{abs(self.mate_in)} 步杀"
        if self.score_cp is None:
            return "—"
        return f"{self.score_cp:+d}"


@dataclass
class EngineReport:
    """一次分析的完整结果。"""

    fen: str
    depth: int
    candidates: List[Candidate]
    best_iccs: str
    nodes: int = 0
    elapsed: float = 0.0
    engine_name: str = ""

    @property
    def best(self) -> Optional[Candidate]:
        return self.candidates[0] if self.candidates else None

    def find(self, iccs: str) -> Optional[Candidate]:
        """在候选里查找某步棋（不区分大小写）。"""
        target = iccs.lower()
        for candidate in self.candidates:
            if candidate.iccs.lower() == target:
                return candidate
        return None

    def to_dict(self) -> Dict[str, object]:
        return {
            "fen": self.fen,
            "depth": self.depth,
            "best_iccs": self.best_iccs,
            "nodes": self.nodes,
            "elapsed": round(self.elapsed, 3),
            "engine_name": self.engine_name,
            "candidates": [
                {
                    "rank": c.rank,
                    "iccs": c.iccs,
                    "chinese": c.chinese,
                    "score_cp": c.score_cp,
                    "mate_in": c.mate_in,
                    "score_text": c.score_text,
                    "depth": c.depth,
                    "pv": c.pv,
                    "pv_chinese": c.pv_chinese,
                }
                for c in self.candidates
            ],
        }


def pv_to_chinese(fen: str, pv_iccs: Sequence[str], limit: int = 10) -> List[str]:
    """把引擎返回的 ICCS 主线转换成中文记谱列表。"""
    board = ChessBoard(full_fen(fen))
    texts: List[str] = []
    for iccs in list(pv_iccs)[:limit]:
        try:
            move = board.move_iccs(iccs, check=False)
        except Exception:  # pragma: no cover - 引擎给出非法着法时截断
            break
        if move is None:
            break
        try:
            texts.append(move.to_text())
        except Exception:  # pragma: no cover
            texts.append(iccs)
    return texts


class PikafishEngine:
    """Pikafish 引擎的 UCI 封装（同步、线程安全）。"""

    def __init__(
        self,
        exe: Optional[Path] = None,
        threads: Optional[int] = None,
        hash_mb: Optional[int] = None,
        multipv: Optional[int] = None,
    ) -> None:
        settings = load_settings()
        self.exe = Path(exe) if exe else engine_path()
        if not self.exe or not self.exe.is_file():
            raise EngineNotFoundError(
                "未找到 Pikafish 引擎，请把引擎放到 cchess/Engine/pikafish_*/，"
                "或用环境变量 QIMIND_ENGINE 指定可执行文件路径。"
            )
        self.threads = threads or int(settings["threads"])
        self.hash_mb = hash_mb or int(settings["hash_mb"])
        self.default_multipv = multipv or int(settings["multipv"])

        self._proc: Optional[subprocess.Popen] = None
        self._queue: "queue.Queue[Optional[str]]" = queue.Queue()
        self._reader: Optional[threading.Thread] = None
        self._lock = threading.RLock()
        self.name = ""
        self._current_multipv = 0

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> "PikafishEngine":
        """启动引擎进程并完成 UCI 握手。"""
        with self._lock:
            if self._proc and self._proc.poll() is None:
                return self
            creationflags = 0
            if os.name == "nt":  # 避免弹出控制台窗口
                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            self._proc = subprocess.Popen(
                [str(self.exe)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                cwd=str(self.exe.parent),
                creationflags=creationflags,
            )
            self._queue = queue.Queue()
            self._reader = threading.Thread(
                target=self._read_loop, args=(self._proc,), daemon=True
            )
            self._reader.start()

            self._send("uci")
            for line in self._iter_lines(timeout=20):
                if line.startswith("id name"):
                    self.name = line[len("id name") :].strip()
                if line == "uciok":
                    break
            if not self.name:
                raise EngineNotFoundError(f"引擎 {self.exe} 无响应，无法完成 UCI 握手")

            self._send(f"setoption name Threads value {self.threads}")
            self._send(f"setoption name Hash value {self.hash_mb}")
            self._set_multipv(self.default_multipv)
            self._send("isready")
            for line in self._iter_lines(timeout=20):
                if line == "readyok":
                    break
            self._send("ucinewgame")
            logger.info("引擎就绪：%s (%s)", self.name, self.exe)
            return self

    def close(self) -> None:
        """退出引擎进程。"""
        with self._lock:
            proc = self._proc
            self._proc = None
            if not proc:
                return
            try:
                if proc.poll() is None:
                    proc.stdin.write("quit\n")
                    proc.stdin.flush()
                    proc.wait(timeout=5)
            except Exception:  # pragma: no cover
                try:
                    proc.kill()
                except Exception:
                    pass

    def __enter__(self) -> "PikafishEngine":
        return self.start()

    def __exit__(self, *_exc) -> None:
        self.close()

    def __del__(self):  # pragma: no cover - 解释器退出时的兜底
        try:
            self.close()
        except Exception:
            pass

    def _read_loop(self, proc: subprocess.Popen) -> None:
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                self._queue.put(line.rstrip("\r\n"))
        except Exception:  # pragma: no cover
            pass
        finally:
            self._queue.put(None)

    def _iter_lines(self, timeout: float):
        """按行读取引擎输出，超时结束迭代。"""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            try:
                line = self._queue.get(timeout=min(remaining, 0.5))
            except queue.Empty:
                continue
            if line is None:
                return
            yield line

    def _send(self, command: str) -> None:
        proc = self._proc
        if not proc or proc.poll() is not None:
            raise EngineNotFoundError("引擎进程已退出，请重新启动分析")
        proc.stdin.write(command + "\n")  # type: ignore[union-attr]
        proc.stdin.flush()  # type: ignore[union-attr]

    def _set_multipv(self, value: int) -> None:
        value = max(1, int(value))
        if value != self._current_multipv:
            self._send(f"setoption name MultiPV value {value}")
            self._current_multipv = value

    # ------------------------------------------------------------------ 分析
    def analyse(
        self,
        fen: str,
        depth: Optional[int] = None,
        multipv: Optional[int] = None,
        movetime_ms: Optional[int] = None,
        timeout: float = 120.0,
        with_chinese: bool = True,
    ) -> EngineReport:
        """分析局面，返回候选走法与评分。

        参数:
            fen: 两段式或六段式 FEN。
            depth: 搜索深度，默认取配置值。
            multipv: 候选路数，默认取配置值。
            movetime_ms: 给定则改用固定思考时间。
            timeout: 最长等待秒数，超时抛 ``TimeoutError``。
            with_chinese: 是否顺便生成中文记谱与中文主线。
        """
        with self._lock:
            self.start()
            settings = load_settings()
            depth = int(depth or settings["depth"])
            multipv = int(multipv or settings["multipv"])
            self._set_multipv(multipv)

            self._send(f"position fen {full_fen(fen)}")
            go = f"go movetime {int(movetime_ms)}" if movetime_ms else f"go depth {depth}"
            started = time.monotonic()
            self._send(go)

            best_iccs = ""
            nodes = 0
            latest: Dict[int, Candidate] = {}
            for line in self._iter_lines(timeout=timeout):
                if line.startswith("info "):
                    parsed = _parse_info(line)
                    if not parsed or "multipv" not in parsed and "pv" not in parsed:
                        continue
                    rank = int(parsed.get("multipv", 1))
                    moves = parsed.get("pv") or []
                    if not moves:
                        continue
                    candidate = Candidate(
                        rank=rank,
                        iccs=moves[0],
                        score_cp=parsed.get("cp"),
                        mate_in=parsed.get("mate"),
                        depth=int(parsed.get("depth", 0)),
                        pv=list(moves),
                    )
                    previous = latest.get(rank)
                    if previous is None or candidate.depth >= previous.depth:
                        latest[rank] = candidate
                    nodes = int(parsed.get("nodes", nodes) or nodes)
                elif line.startswith("bestmove"):
                    parts = line.split()
                    if len(parts) > 1:
                        best_iccs = parts[1]
                    break
            elapsed = time.monotonic() - started

            if not latest:
                raise TimeoutError("引擎在限定时间内没有给出分析结果")

            candidates = [latest[key] for key in sorted(latest)]
            if with_chinese:
                for candidate in candidates:
                    candidate.chinese = iccs_to_chinese(fen, candidate.iccs)
                    candidate.pv_chinese = pv_to_chinese(fen, candidate.pv)

            if not best_iccs:
                best_iccs = candidates[0].iccs
            else:
                ranked = [c for c in candidates if c.iccs.lower() == best_iccs.lower()]
                if ranked:
                    candidates.remove(ranked[0])
                    ranked[0].rank = 0
                    candidates.insert(0, ranked[0])
                    for index, candidate in enumerate(candidates):
                        candidate.rank = index + 1

            return EngineReport(
                fen=fen,
                depth=depth,
                candidates=candidates,
                best_iccs=best_iccs,
                nodes=nodes,
                elapsed=elapsed,
                engine_name=self.name,
            )

    def evaluate_move(self, fen: str, iccs: str, depth: Optional[int] = None, timeout: float = 120.0) -> Optional[int]:
        """用"走完这步后再让引擎分析"的方式估算某步棋的分数（红方视角为负）。

        返回**走子方视角**的评分（cp）；无法计算时返回 ``None``。
        """
        board = ChessBoard(full_fen(fen))
        move = board.move_iccs(iccs, check=False)
        if move is None:
            return None
        after_fen = board.to_fen()
        try:
            report = self.analyse(after_fen, depth=depth, multipv=1, timeout=timeout, with_chinese=False)
        except (TimeoutError, EngineNotFoundError):
            return None
        candidate = report.best
        if candidate is None:
            return None
        if candidate.mate_in is not None:
            return -MATE_SCORE + candidate.mate_in if candidate.mate_in > 0 else MATE_SCORE + candidate.mate_in
        if candidate.score_cp is None:
            return None
        return -candidate.score_cp


def _parse_info(line: str) -> Dict[str, object]:
    """解析一行 ``info`` 输出，``pv`` 之后的内容放入 ``pv`` 键。"""
    tokens = line.split()
    result: Dict[str, object] = {}
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token in ("depth", "seldepth", "multipv", "nodes", "nps", "hashfull", "tbhits", "time"):
            try:
                result[token] = int(tokens[index + 1])
            except (IndexError, ValueError):
                pass
            index += 2
        elif token == "score":
            kind = tokens[index + 1] if index + 1 < len(tokens) else ""
            value = tokens[index + 2] if index + 2 < len(tokens) else "0"
            try:
                result["cp" if kind == "cp" else "mate"] = int(value)
            except ValueError:
                pass
            index += 3
        elif token == "pv":
            result["pv"] = tokens[index + 1 :]
            break
        else:
            index += 1
    return result


_ENGINE: Optional[PikafishEngine] = None
_ENGINE_LOCK = threading.Lock()


def get_engine() -> PikafishEngine:
    """返回全局共享的引擎实例（首次调用时创建）。"""
    global _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is None:
            _ENGINE = PikafishEngine().start()
        return _ENGINE


def shutdown_engine() -> None:
    """关闭全局引擎（进程退出或测试清理时调用）。"""
    global _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is not None:
            _ENGINE.close()
            _ENGINE = None


def analyze_fen(
    fen: str,
    depth: Optional[int] = None,
    multipv: Optional[int] = None,
    *,
    use_cache: bool = True,
    movetime_ms: Optional[int] = None,
) -> EngineReport:
    """带磁盘缓存的局面分析（同一局面同一参数只算一次）。"""
    settings = load_settings()
    depth = int(depth or settings["depth"])
    multipv = int(multipv or settings["multipv"])
    key = f"{full_fen(fen)}|d{depth}|p{multipv}|m{movetime_ms or 0}"

    cache_file = cache_dir("engine") / f"{_hash(key)}.json"
    if use_cache and cache_file.is_file():
        try:
            payload = json.loads(cache_file.read_text(encoding="utf-8"))
            report = _report_from_dict(payload["report"])
            report.engine_name = payload.get("engine_name", report.engine_name)
            return report
        except (OSError, ValueError, KeyError):
            pass

    report = get_engine().analyse(fen, depth=depth, multipv=multipv, movetime_ms=movetime_ms)

    if use_cache:
        try:
            cache_file.write_text(
                json.dumps(
                    {"key": key, "engine_name": report.engine_name, "report": report.to_dict()},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except OSError:  # pragma: no cover
            pass
    return report


def _report_from_dict(payload: Dict[str, object]) -> EngineReport:
    candidates = [
        Candidate(
            rank=int(item.get("rank", index + 1)),
            iccs=str(item.get("iccs", "")),
            score_cp=item.get("score_cp"),
            mate_in=item.get("mate_in"),
            depth=int(item.get("depth", 0)),
            pv=list(item.get("pv", [])),
            chinese=str(item.get("chinese", "")),
            pv_chinese=list(item.get("pv_chinese", [])),
        )
        for index, item in enumerate(payload.get("candidates", []))
    ]
    return EngineReport(
        fen=str(payload.get("fen", "")),
        depth=int(payload.get("depth", 0)),
        candidates=candidates,
        best_iccs=str(payload.get("best_iccs", "")),
        nodes=int(payload.get("nodes", 0)),
        elapsed=float(payload.get("elapsed", 0.0)),
        engine_name=str(payload.get("engine_name", "")),
    )


def _hash(text: str) -> str:
    import hashlib

    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:20]
