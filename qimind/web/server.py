"""QiMind 图形界面的后端：FastAPI + 后台任务。

设计要点：

* 引擎与 DeepSeek 都是慢操作，统一走「提交任务 → 轮询进度」的模式，
  界面可以一边分析一边展示已完成的部分；
* 棋盘相关的小请求（合法走法、走一步）直接同步返回，保证交互跟手；
* 引擎是单进程、串行使用的，任务队列默认单线程执行。
"""

from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..analyze import (
    PositionReport,
    analyze_position,
    annotate_game,
    explain_variation,
    generate_report,
    should_explain,
)
from ..cchess_bridge import (
    ChessBoard,
    FULL_INIT_FEN,
    fen_side,
    full_fen,
    iccs_to_chinese,
    iccs2pos,
    piece_name,
    pos2iccs,
)
from ..config import (
    DATA_DIR,
    PROJECT_ROOT,
    deepseek_api_key,
    engine_path,
    key_source,
    load_settings,
    mask_key,
    save_deepseek_key,
    save_settings,
)
from ..engine import EngineNotFoundError, get_engine
from ..export import (
    build_report_blocks,
    build_walkthrough_blocks,
    build_walkthrough_markdown,
    write_docx,
)
from ..facts import build_fact_sheet
from ..filedialog import ask_open_filename
from ..games import GameRecord, load_positions_txt, load_record, record_from_pgn_text
from ..llm import ExplainerError, check_api_key, explain_fact_sheet, reset_explainers
from ..practice import LEVELS, engine_move, get_session, new_session
from ..prompts import STYLES

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
UPLOAD_DIR = DATA_DIR / "uploads"
EXPORT_DIR = DATA_DIR / "exports"
UPLOAD_SUFFIXES = (".pgn", ".xqf", ".cbf", ".cbr", ".txt")
SAMPLE_DIRS = [
    PROJECT_ROOT / "cchess" / "tests" / "data",
    PROJECT_ROOT / "cchess" / "demo",
]


# --------------------------------------------------------------------------- 任务
@dataclass
class Job:
    """一次后台分析任务。"""

    id: str
    kind: str
    status: str = "queued"  # queued / running / done / error / cancelled
    message: str = ""
    total: int = 0
    done: int = 0
    data: Dict[str, Any] = field(default_factory=dict)
    error: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().strftime("%H:%M:%S"))
    updated_at: str = field(default_factory=lambda: datetime.now().strftime("%H:%M:%S"))

    def touch(self, **changes: Any) -> None:
        for key, value in changes.items():
            setattr(self, key, value)
        self.updated_at = datetime.now().strftime("%H:%M:%S")

    @property
    def cancelled(self) -> bool:
        return self.status == "cancelled"

    def to_dict(self, include_data: bool = True) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "message": self.message,
            "total": self.total,
            "done": self.done,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if include_data:
            payload["data"] = self.data
        return payload


class JobManager:
    """极简线程池任务管理：提交、查询、取消。"""

    def __init__(self, workers: int = 2) -> None:
        self._jobs: Dict[str, Job] = {}
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="qimind")

    def submit(self, kind: str, fn, *args, **kwargs) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], kind=kind)
        with self._lock:
            self._jobs[job.id] = job
        self._executor.submit(self._run, job, fn, args, kwargs)
        return job

    def _run(self, job: Job, fn, args, kwargs) -> None:
        job.touch(status="running", message="开始分析…")
        try:
            fn(job, *args, **kwargs)
            if not job.cancelled:
                job.touch(status="done", message=job.message or "分析完成")
        except Exception as exc:  # pragma: no cover - 任务失败要在界面上可见
            logger.exception("任务 %s 失败", job.id)
            job.touch(status="error", error=str(exc), message="分析失败")

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if not job or job.status in ("done", "error"):
            return False
        job.touch(status="cancelled", message="已取消")
        return True

    def cleanup(self, keep: int = 40) -> None:
        with self._lock:
            if len(self._jobs) <= keep:
                return
            order = sorted(self._jobs.values(), key=lambda item: item.created_at)
            for job in order[: len(self._jobs) - keep]:
                if job.status in ("done", "error", "cancelled"):
                    self._jobs.pop(job.id, None)


# --------------------------------------------------------------------------- 请求体
class GameLoadRequest(BaseModel):
    path: Optional[str] = None
    pgn: Optional[str] = None
    name: Optional[str] = None


class PositionAnalyzeRequest(BaseModel):
    fen: str
    played: Optional[str] = None
    depth: Optional[int] = None
    multipv: Optional[int] = None
    style: str = "棋友"
    explain: bool = True
    use_cache: bool = True
    #: 分析方式：engine=只跑引擎，llm=只调 DeepSeek（引擎结果走缓存），both=两者都跑
    mode: str = "both"
    regenerate: bool = False
    model: Optional[str] = None
    reasoning_effort: Optional[str] = None


class GameAnalyzeRequest(BaseModel):
    path: Optional[str] = None
    pgn: Optional[str] = None
    init_fen: Optional[str] = None
    moves: Optional[List[str]] = None
    name: Optional[str] = None
    depth: Optional[int] = None
    multipv: Optional[int] = None
    scope: str = "all"
    style: str = "棋友"
    explain: bool = True
    use_cache: bool = True
    mode: str = "both"
    regenerate: bool = False
    model: Optional[str] = None
    reasoning_effort: Optional[str] = None
    max_plies: Optional[int] = None


class BoardMovesRequest(BaseModel):
    fen: str


class BoardMoveRequest(BaseModel):
    fen: str
    iccs: str


class SettingsRequest(BaseModel):
    depth: Optional[int] = None
    multipv: Optional[int] = None
    threads: Optional[int] = None
    model: Optional[str] = None
    reasoning_effort: Optional[str] = None
    explain_scope: Optional[str] = None
    engine_path: Optional[str] = None


class SecretRequest(BaseModel):
    """在界面里填写 DeepSeek API Key。"""

    api_key: Optional[str] = None


class SecretTestRequest(BaseModel):
    """测试 API Key 是否可用（不传则测试当前已保存的 Key）。"""

    api_key: Optional[str] = None


class PracticeNewRequest(BaseModel):
    """开始一局人机对练。"""

    side: str = "red"
    level: str = "中等"
    depth: Optional[int] = None
    player_name: str = "玩家"


class PracticeMoveRequest(BaseModel):
    """玩家走一步。"""

    session_id: str
    iccs: str


class PracticeSessionRequest(BaseModel):
    session_id: str


class VariationAnalyzeRequest(BaseModel):
    """讲解某个局面下的一步变招。"""

    fen: str
    iccs: str
    depth: Optional[int] = None
    multipv: Optional[int] = None
    style: str = "棋友"
    use_cache: bool = True
    regenerate: bool = False
    model: Optional[str] = None
    reasoning_effort: Optional[str] = None


class ReportRequest(BaseModel):
    """生成对局报告（可直接复用已完成的整局分析任务）。"""

    job_id: Optional[str] = None
    path: Optional[str] = None
    pgn: Optional[str] = None
    init_fen: Optional[str] = None
    moves: Optional[List[str]] = None
    name: Optional[str] = None
    depth: Optional[int] = None
    multipv: Optional[int] = None
    style: str = "棋友"
    mode: str = "both"
    explain: bool = True
    use_cache: bool = True
    regenerate: bool = False
    max_plies: Optional[int] = None


class ExportRequest(BaseModel):
    """导出讲评稿 / 对局报告。"""

    kind: str = "walkthrough"  # walkthrough=逐回合讲评，report=对局报告
    format: str = "md"  # md / docx
    job_id: Optional[str] = None
    record: Optional[Dict[str, Any]] = None
    reports: Optional[List[Dict[str, Any]]] = None
    report: Optional[Dict[str, Any]] = None
    narrative: Optional[str] = None
    style: str = "棋友"
    include_facts: bool = False
    depth: Optional[int] = None
    model: Optional[str] = None


# --------------------------------------------------------------------------- 应用
def create_app() -> FastAPI:
    app = FastAPI(title="QiMind 棋思", description="象棋引擎 + DeepSeek 讲解", version="0.1.0")
    jobs = JobManager()
    app.state.jobs = jobs

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.exception_handler(Exception)
    async def _unhandled(_request, exc):  # pragma: no cover - 兜底
        logger.exception("请求异常")
        return JSONResponse(status_code=500, content={"error": str(exc)})

    @app.get("/")
    async def index():
        target = STATIC_DIR / "index.html"
        if not target.is_file():
            raise HTTPException(status_code=500, detail="缺少前端文件 index.html")
        return FileResponse(str(target))

    # ---------------------------------------------------------------- 配置
    @app.get("/api/config")
    async def api_config():
        settings = load_settings()
        path = engine_path()
        engine_name = ""
        engine_error = ""
        try:
            engine_name = get_engine().name
        except (EngineNotFoundError, Exception) as exc:  # noqa: B014 - 引擎不可用要提示
            engine_error = str(exc)
        return {
            "project_root": str(PROJECT_ROOT),
            "engine": {
                "path": str(path) if path else "",
                "name": engine_name,
                "error": engine_error,
                "depth": settings["depth"],
                "multipv": settings["multipv"],
                "threads": settings["threads"],
            },
            "model": settings["model"],
            "models": ["deepseek-flash", "deepseek-v4-pro"],
            "reasoning_effort": settings["reasoning_effort"],
            "styles": list(STYLES.keys()),
            "explain_scope": settings["explain_scope"],
            "has_key": bool(deepseek_api_key()),
            "deepseek": _secret_state(),
        }

    @app.post("/api/settings")
    async def api_settings(request: SettingsRequest):
        updated = save_settings(request.model_dump(exclude_none=True))
        return {"settings": updated}

    # ---------------------------------------------------------------- DeepSeek Key
    @app.post("/api/secret")
    def api_secret(request: SecretRequest):
        """保存或清除 DeepSeek API Key（写在 data/secrets.json，不会上传）。"""
        save_deepseek_key(request.api_key)
        reset_explainers()  # 让新的 Key 立刻生效
        return _secret_state()

    @app.post("/api/secret/test")
    def api_secret_test(request: SecretTestRequest):
        """测试（或校验刚填写的）API Key 是否可用。"""
        result = check_api_key(request.api_key)
        return result

    # ---------------------------------------------------------------- 棋谱
    @app.get("/api/samples")
    async def api_samples():
        samples: List[Dict[str, str]] = []
        for directory in SAMPLE_DIRS:
            if not directory.is_dir():
                continue
            for path in sorted(directory.iterdir()):
                if path.suffix.lower() in (".pgn", ".xqf", ".cbf", ".cbr", ".txt"):
                    samples.append({"name": path.stem, "path": str(path), "kind": path.suffix.lower().lstrip(".")})
        return {"samples": samples}

    @app.post("/api/game/load")
    async def api_game_load(request: GameLoadRequest):
        try:
            record = _resolve_record(request.path, request.pgn, request.name)
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"record": record.to_dict()}

    @app.get("/api/positions")
    async def api_positions(path: str):
        try:
            positions = load_positions_txt(path)
        except OSError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"positions": [item.to_dict() for item in positions]}

    # ---------------------------------------------------------------- 选文件
    @app.post("/api/dialog/open")
    def api_dialog_open(title: str = "选择棋谱文件"):
        """弹出系统「打开」对话框，返回用户选择的文件路径。

        同步接口（FastAPI 会放到线程池），对话框未关闭前该请求保持等待；
        用户取消时返回 ``cancelled=True``。若系统弹窗不可用，
        返回 500 并提示改用浏览器选择文件。
        """
        settings = load_settings()
        initial = str(settings.get("last_dir") or PROJECT_ROOT)
        try:
            path = ask_open_filename(title=title, initial_dir=initial)
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        if not path:
            return {"path": "", "cancelled": True}
        save_settings({"last_dir": str(Path(path).parent)})
        return {"path": path, "cancelled": False}

    @app.post("/api/game/upload")
    def api_game_upload(file: UploadFile = File(...)):
        """接收浏览器选择的棋谱文件（系统弹窗不可用时的兜底方案）。"""
        name = Path(file.filename or "upload.pgn").name
        suffix = Path(name).suffix.lower()
        if suffix not in UPLOAD_SUFFIXES:
            raise HTTPException(
                status_code=400,
                detail=f"只支持 {'、'.join(UPLOAD_SUFFIXES)} 格式的棋谱文件",
            )
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        target = UPLOAD_DIR / name
        if target.exists():
            target = UPLOAD_DIR / f"{target.stem}_{int(time.time())}{suffix}"
        target.write_bytes(file.file.read())

        if suffix == ".txt":
            positions = load_positions_txt(target)
            return {"path": str(target), "positions": [item.to_dict() for item in positions]}
        try:
            record = load_record(target)
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"这个文件解析失败：{exc}") from exc
        return {"path": str(target), "record": record.to_dict()}

    # ---------------------------------------------------------------- 棋盘交互
    @app.post("/api/board/moves")
    async def api_board_moves(request: BoardMovesRequest):
        board = ChessBoard(full_fen(request.fen))
        moves: List[Dict[str, Any]] = []
        for (from_pos, to_pos) in board.create_moves():
            frm, to = tuple(from_pos), tuple(to_pos)
            if board.leaves_king_in_check(frm, to):
                continue
            captured = piece_name(board.get_fench(to) or "")
            iccs = pos2iccs(frm, to)
            moves.append(
                {
                    "iccs": iccs,
                    "from": list(frm),
                    "to": list(to),
                    "chinese": iccs_to_chinese(request.fen, iccs),
                    "capture": captured,
                }
            )
        return {"fen": full_fen(request.fen), "side": fen_side(request.fen), "moves": moves}

    @app.post("/api/board/move")
    async def api_board_move(request: BoardMoveRequest):
        board = ChessBoard(full_fen(request.fen))
        try:
            pos_from, pos_to = iccs2pos(request.iccs)
        except Exception as exc:
            raise HTTPException(status_code=400, detail="走法格式不正确（应为 ICCS，如 h2e2）") from exc
        try:
            illegal = board.leaves_king_in_check(pos_from, pos_to)
        except Exception as exc:
            raise HTTPException(status_code=400, detail="这步棋不合法") from exc
        if illegal:
            raise HTTPException(status_code=400, detail="这步棋会让自己的将帅被将军")
        move = board.move(pos_from, pos_to, check=True)
        if move is None:
            raise HTTPException(status_code=400, detail="这步棋不合法")
        return {
            "fen": board.to_fen(),
            "side": fen_side(board.to_fen()),
            "chinese": move.to_text(),
            "is_check": bool(getattr(move, "is_checking", False)),
            "is_checkmate": bool(getattr(move, "is_checkmate", False)),
        }

    # ---------------------------------------------------------------- 分析任务
    @app.post("/api/analyze/position")
    async def api_analyze_position(request: PositionAnalyzeRequest):
        job = jobs.submit(
            "position",
            _run_position_job,
            request,
        )
        return {"job_id": job.id}

    @app.post("/api/analyze/game")
    async def api_analyze_game(request: GameAnalyzeRequest):
        try:
            record = _resolve_record(request.path, request.pgn, request.name, request.init_fen, request.moves)
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        job = jobs.submit("game", _run_game_job, record, request)
        return {"job_id": job.id, "record": record.to_dict()}

    @app.post("/api/analyze/variation")
    async def api_analyze_variation(request: VariationAnalyzeRequest):
        """讲某一步变招（候选表里的任意一路）。"""
        job = jobs.submit("variation", _run_variation_job, request)
        return {"job_id": job.id}

    @app.post("/api/report")
    async def api_report(request: ReportRequest):
        """生成对局报告；给出 job_id 时复用那次整局分析的结果。"""
        record: Optional[GameRecord] = None
        reports: List[Dict[str, Any]] = []
        if request.job_id:
            source = jobs.get(request.job_id)
            if not source:
                raise HTTPException(status_code=404, detail="找不到对应的分析任务")
            data = source.data or {}
            if data.get("record"):
                record = GameRecord(
                    name=str(data["record"].get("name", "棋谱")),
                    init_fen=str(data["record"].get("init_fen", FULL_INIT_FEN)),
                    headers=dict(data["record"].get("headers", {})),
                    source=str(data["record"].get("source", "")),
                )
                from ..games import replay_moves

                iccs = [item["iccs"] for item in data["record"].get("plies", [])]
                record.plies = replay_moves(record.init_fen, iccs)
            reports = list(data.get("reports") or [])
        if record is None:
            record = _resolve_record(request.path, request.pgn, request.name, request.init_fen, request.moves)
        if not record.plies:
            raise HTTPException(status_code=400, detail="这份棋谱里没有可分析的着法，请换一份棋谱")
        if not reports:
            analysis = annotate_game(
                record,
                depth=request.depth,
                multipv=request.multipv,
                style=request.style,
                explain=False,
                use_cache=request.use_cache,
                max_plies=request.max_plies,
            )
            reports = [item.to_dict() for item in analysis.reports]
        job = jobs.submit("report", _run_report_job, record, reports, request)
        return {"job_id": job.id, "record": record.to_dict(), "reports": reports}

    # ---------------------------------------------------------------- 导出
    @app.post("/api/export")
    def api_export(request: ExportRequest):
        """把讲评稿或对局报告导出为 Markdown / Word，并保存到磁盘。"""
        record = _record_from_dict(request.record) if request.record else None
        reports = list(request.reports or [])
        if record is None and request.job_id:
            source = jobs.get(request.job_id)
            data = (source.data if source else {}) or {}
            if data.get("record"):
                record = _record_from_dict(data["record"])
                reports = reports or list(data.get("reports") or [])
        if record is None:
            raise HTTPException(status_code=400, detail="缺少棋谱数据，无法导出")

        report = request.report or {}
        narrative = request.narrative or str(report.get("narrative") or "")
        base_name = record.name or "qimind"

        if request.kind == "report":
            if not report:
                raise HTTPException(status_code=400, detail="缺少对局报告数据，请先生成报告")
            blocks = build_report_blocks(report, record, narrative)
            title = f"对局报告：{record.name}"
            if request.format == "docx":
                path = _export_path(base_name + "_报告", ".docx")
                write_docx(path, title, blocks)
                return {"path": str(path), "filename": path.name, "format": "docx", "text": ""}
            from ..report import render_report_markdown

            text = render_report_markdown(report, record, narrative)
            path = _export_path(base_name + "_报告", ".md")
            path.write_text(text, encoding="utf-8")
            return {"path": str(path), "filename": path.name, "format": "md", "text": text}

        blocks = build_walkthrough_blocks(
            record,
            reports,
            depth=request.depth,
            model=request.model or "",
            style=request.style,
            report=report or None,
            narrative=narrative,
        )
        if request.format == "docx":
            path = _export_path(base_name + "_讲评稿", ".docx")
            write_docx(path, f"棋谱讲评：{record.name}", blocks)
            return {"path": str(path), "filename": path.name, "format": "docx", "text": ""}

        text = build_walkthrough_markdown(
            record,
            reports,
            depth=request.depth,
            model=request.model or "",
            style=request.style,
            include_facts=request.include_facts,
            report=report or None,
            narrative=narrative,
        )
        path = _export_path(base_name + "_讲评稿", ".md")
        path.write_text(text, encoding="utf-8")
        return {"path": str(path), "filename": path.name, "format": "md", "text": text}

    @app.get("/api/export/download")
    def api_export_download(path: str):
        """下载导出文件（仅限 QiMind 数据目录内的文件）。"""
        target = _safe_data_file(path)
        return FileResponse(str(target), filename=target.name)

    @app.get("/api/jobs/{job_id}")
    async def api_job(job_id: str):
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="任务不存在")
        return job.to_dict()

    @app.post("/api/jobs/{job_id}/cancel")
    async def api_job_cancel(job_id: str):
        if not jobs.cancel(job_id):
            raise HTTPException(status_code=400, detail="任务无法取消")
        return {"ok": True}

    # ---------------------------------------------------------------- 人机对练
    @app.get("/api/practice/levels")
    def api_practice_levels():
        return {"levels": list(LEVELS.keys()), "default": "中等"}

    @app.post("/api/practice/new")
    def api_practice_new(request: PracticeNewRequest):
        """开一局对练；玩家执黑时引擎先走一步。"""
        session = new_session(
            player_side=request.side,
            level=request.level,
            depth=request.depth,
            player_name=request.player_name,
        )
        session.message = f"对局开始：你执{ '红' if session.player_side == 'red' else '黑' }方，引擎{ session.level }。"
        if session.side_to_move != session.player_side:
            engine_move(session)
        return session.to_dict()

    @app.post("/api/practice/move")
    def api_practice_move(request: PracticeMoveRequest):
        """玩家走一步，随后引擎应招。"""
        try:
            session = get_session(request.session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        try:
            played = session.apply_move(request.iccs)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        reply = None
        if not session.finished and session.side_to_move != session.player_side:
            try:
                reply = engine_move(session)
            except Exception as exc:  # pragma: no cover - 引擎异常
                logger.exception("引擎应招失败")
                session.message = f"引擎应招失败：{exc}"
        payload = session.to_dict()
        payload["played"] = played
        payload["engine_move"] = reply
        return payload

    @app.post("/api/practice/undo")
    def api_practice_undo(request: PracticeSessionRequest):
        try:
            session = get_session(request.session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if not session.undo():
            raise HTTPException(status_code=400, detail="还没有走过棋，无法悔棋")
        if not session.finished and session.side_to_move != session.player_side:
            engine_move(session)
        return session.to_dict()

    @app.post("/api/practice/resign")
    def api_practice_resign(request: PracticeSessionRequest):
        try:
            session = get_session(request.session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        session.resign()
        return session.to_dict()

    @app.get("/api/practice/state")
    def api_practice_state(session_id: str):
        try:
            return get_session(session_id).to_dict()
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/practice/pgn")
    def api_practice_pgn(session_id: str, download: int = 0):
        """导出对练棋谱：``download=1`` 直接下载，否则返回文本。"""
        try:
            session = get_session(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        text = session.to_pgn()
        if not download:
            return {"pgn": text, "result": session.result, "move_count": len(session.moves)}
        filename = f"qimind_practice_{session.id}.pgn"
        return Response(
            content=text.encode("utf-8"),
            media_type="application/x-chess-pgn",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.post("/api/practice/save")
    def api_practice_save(request: PracticeSessionRequest):
        """把对练棋谱保存到磁盘，返回路径（便于之后拿去讲解）。"""
        try:
            session = get_session(request.session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        path = session.save()
        return {"path": str(path), "pgn": session.to_pgn(), "result": session.result}

    return app


# --------------------------------------------------------------------------- 任务实现
def _run_position_job(job: Job, request: PositionAnalyzeRequest) -> None:
    """单局面任务：先出引擎结论，再按需补讲解。

    ``mode`` 决定这次要做什么：
    ``engine`` 只跑引擎；``llm`` 复用引擎结果只调 DeepSeek；``both`` 两者都跑。
    """
    settings = load_settings()
    depth = request.depth or int(settings["depth"])
    multipv = request.multipv or int(settings["multipv"])
    want_llm = request.mode in ("llm", "both") and request.explain

    total = 2 if want_llm else 1
    job.touch(message=f"引擎分析中（深度 {depth}）…", total=total, done=0)
    report = analyze_position(
        request.fen,
        depth=depth,
        multipv=multipv,
        played=request.played,
        style=request.style,
        explain=False,
        use_cache=request.use_cache,
    )
    job.data["report"] = report.to_dict()
    job.touch(done=1, message="引擎分析完成")
    if job.cancelled:
        return

    if want_llm:
        job.touch(message="正在调用 DeepSeek 生成讲解…")
        explanation = explain_fact_sheet(
            report.fact_sheet,
            played_move=report.played_chinese or None,
            is_best=report.is_best or not request.played,
            style=request.style,
            use_cache=request.use_cache and not request.regenerate,
            model=request.model,
            reasoning_effort=request.reasoning_effort,
        )
        report.explanation = explanation
        report.error = explanation.error
        job.data["report"] = report.to_dict()
        job.touch(done=2, message="讲解完成")
    else:
        job.touch(done=1, message="引擎分析完成（未调用 DeepSeek）")


def _run_game_job(job: Job, record: GameRecord, request: GameAnalyzeRequest) -> None:
    """整局任务：逐步分析，边算边把结果推给界面。"""
    settings = load_settings()
    depth = request.depth or int(settings["depth"])
    multipv = request.multipv or int(settings["multipv"])
    plies = record.plies[: request.max_plies] if request.max_plies else record.plies
    want_llm = request.mode in ("llm", "both") and request.explain

    job.touch(
        total=len(plies),
        done=0,
        message=f"共 {len(plies)} 步，开始{'引擎分析' if not want_llm else '分析'}…",
    )
    job.data.update(
        {
            "record": record.to_dict(),
            "reports": [],
            "depth": depth,
            "scope": request.scope,
            "style": request.style,
            "mode": request.mode,
        }
    )

    started = time.monotonic()
    for index, ply in enumerate(plies, start=1):
        if job.cancelled:
            return
        job.touch(done=index - 1, message=f"分析第 {ply.move_number} 回合（{ply.chinese}）…")
        try:
            report = analyze_position(
                ply.fen_before,
                depth=depth,
                multipv=multipv,
                played=ply.iccs,
                move_number=ply.move_number,
                style=request.style,
                explain=False,
                use_cache=request.use_cache,
            )
        except Exception as exc:  # pragma: no cover - 单步失败不中断整局
            logger.exception("第 %s 步分析失败", ply.index)
            report = PositionReport(fen=ply.fen_before, side=ply.side, error=str(exc))

        if want_llm and should_explain(report, request.scope) and not job.cancelled:
            job.touch(message=f"正在讲解第 {ply.move_number} 回合（{ply.chinese}）…")
            try:
                explanation = explain_fact_sheet(
                    report.fact_sheet,
                    played_move=report.played_chinese,
                    is_best=report.is_best,
                    style=request.style,
                    use_cache=request.use_cache and not request.regenerate,
                    model=request.model,
                    reasoning_effort=request.reasoning_effort,
                )
                report.explanation = explanation
                if explanation.error:
                    report.error = explanation.error
            except ExplainerError as exc:  # pragma: no cover
                report.error = str(exc)

        job.data["reports"].append(report.to_dict())
        job.touch(done=index)

    analysed = len(job.data.get("reports", []))
    job.data["elapsed"] = round(time.monotonic() - started, 2)
    job.touch(message=f"{analysed} 步分析完成")


def _resolve_record(
    path: Optional[str],
    pgn: Optional[str],
    name: Optional[str] = None,
    init_fen: Optional[str] = None,
    moves: Optional[List[str]] = None,
) -> GameRecord:
    """把请求参数解析成棋谱对象，依次尝试：走法列表 > PGN 文本 > 文件路径。"""
    if moves:
        from ..games import record_from_iccs

        return record_from_iccs(moves, init_fen=init_fen or FULL_INIT_FEN, name=name or "手动录入")
    if pgn:
        return record_from_pgn_text(pgn, name=name or "粘贴的棋谱")
    if path:
        return load_record(path, name=name) if name else load_record(path)
    raise HTTPException(status_code=400, detail="请提供棋谱文件路径、PGN 文本或走法列表")


def _record_from_dict(payload: Dict[str, Any]) -> GameRecord:
    """把 ``GameRecord.to_dict()`` 的结果还原成棋谱对象。"""
    from ..games import replay_moves

    init_fen = str(payload.get("init_fen") or FULL_INIT_FEN)
    record = GameRecord(
        name=str(payload.get("name") or "棋谱"),
        init_fen=init_fen,
        headers={str(key): str(value) for key, value in dict(payload.get("headers") or {}).items()},
        source=str(payload.get("source") or ""),
    )
    iccs = [str(item.get("iccs")) for item in payload.get("plies") or [] if item.get("iccs")]
    record.plies = replay_moves(init_fen, iccs)
    return record


def _run_variation_job(job: Job, request: VariationAnalyzeRequest) -> None:
    """变招讲解：引擎分析该走法 + DeepSeek 讲解这条线的得失。"""
    settings = load_settings()
    depth = request.depth or int(settings["depth"])
    job.touch(total=1, done=0, message=f"正在分析变招（深度 {depth}）并生成讲解…")
    data = explain_variation(
        request.fen,
        request.iccs,
        depth=depth,
        multipv=request.multipv,
        style=request.style,
        use_cache=request.use_cache,
        regenerate=request.regenerate,
        model=request.model,
        reasoning_effort=request.reasoning_effort,
    )
    job.data["variation"] = data
    job.touch(done=1, message="变招讲解完成")


def _run_report_job(
    job: Job,
    record: GameRecord,
    reports: List[Dict[str, Any]],
    request: ReportRequest,
) -> None:
    """对局报告：统计 + 可选的大模型讲评稿。"""
    want_llm = request.mode != "engine" and request.explain
    job.touch(total=2, done=1, message="正在统计对局数据…")
    report = generate_report(
        record,
        reports,
        style=request.style,
        explain=want_llm,
        use_cache=request.use_cache,
        regenerate=request.regenerate,
    )
    job.data["report"] = report
    job.touch(done=2 if want_llm else 1, message="报告完成" if not want_llm else "报告与讲评完成")


def _export_path(base_name: str, suffix: str) -> Path:
    """生成导出文件路径（放在 ``qimind/data/exports/``）。"""
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = re.sub(r'[\\/:*?"<>|]', "_", base_name).strip()[:60] or "qimind"
    return EXPORT_DIR / f"{safe}_{stamp}{suffix}"


def _safe_data_file(raw_path: str) -> Path:
    """校验下载路径只能落在 QiMind 数据目录内。"""
    path = Path(raw_path).resolve()
    root = DATA_DIR.resolve()
    if root != path and root not in path.parents:
        raise HTTPException(status_code=400, detail="只允许下载 QiMind 数据目录内的文件")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return path


def _secret_state() -> Dict[str, Any]:
    """返回 API Key 的状态（只给打码后的信息，不回传完整 Key）。"""
    key = deepseek_api_key()
    source = key_source()
    source_text = {
        "saved": "界面保存（qimind/data/secrets.json）",
        "env": "环境变量 QIMIND_DEEPSEEK_KEY",
    }.get(source, "")
    return {
        "has_key": bool(key),
        "masked": mask_key(key),
        "source": source,
        "source_text": source_text,
        "file": str(DATA_DIR / "secrets.json"),
    }


app = create_app()
