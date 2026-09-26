"""QiMind 基础测试。

默认只跑不依赖外部资源的部分；需要启动 Pikafish 的用例带 ``slow`` 标记：

    python -m pytest tests/test_qimind.py -q
    python -m pytest tests/test_qimind.py -q -m slow
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from qimind.cchess_bridge import (  # noqa: E402
    FULL_INIT_FEN,
    cn_file,
    iccs_to_chinese,
    material_summary,
    move_facts,
    pos_label,
    pos_text,
    square_attackers,
)
from qimind.games import (  # noqa: E402
    load_positions_txt,
    load_record,
    parse_pgn_headers,
    record_from_pgn_text,
    record_from_text_moves,
)

DATA_DIR = PROJECT_ROOT / "cchess" / "tests" / "data"


class TestBridge:
    def test_pos_text(self):
        assert pos_text((0, 9)) == "a9"
        assert pos_text((4, 0)) == "e0"

    def test_chinese_position_label(self):
        # 列按红方视角编号：最右是「一路」，最左是「九路」
        assert cn_file(8) == "一"
        assert cn_file(0) == "九"
        # 行从红方底线数起
        assert pos_label((8, 0)) == "一路1行"
        assert pos_label((4, 2)) == "五路3行"
        # 红方八路炮（x=1）—— 也就是「炮八平五」的那门炮
        assert pos_label((1, 2)) == "八路3行"
        assert pos_label((1, 9)) == "八路10行"

    def test_chinese_notation(self):
        assert iccs_to_chinese(FULL_INIT_FEN, "h2e2") == "炮二平五"
        assert iccs_to_chinese(FULL_INIT_FEN, "b2e2") == "炮八平五"

    def test_move_facts_capture_and_check(self):
        # 红车 d3 吃掉黑士 d7，顺势将军 d9 的黑将
        fen = "3k5/9/3a5/9/9/9/3R5/9/9/4K4 w - - 0 1"
        facts = move_facts(fen, "d3d7")
        assert facts is not None
        assert facts.captured_fench == "a"
        assert facts.is_capture
        assert facts.describe().startswith("吃掉黑方士")

    def test_material_summary_initial(self):
        material = material_summary(FULL_INIT_FEN)
        assert material["diff"] == 0
        assert material["red"]["r"] == 2 and material["black"]["p"] == 5

    def test_square_attackers_cannon_needs_screen(self):
        # 初始局面：红炮 b2 隔着黑炮 b7 可以打到黑马 b9
        assert square_attackers(FULL_INIT_FEN, (1, 9), 1)
        # 而 e3 的红兵（无炮架）不应被任何黑炮攻击
        names = [fench for _pos, fench in square_attackers(FULL_INIT_FEN, (4, 3), 2)]
        assert "c" not in names


class TestGames:
    def test_load_pgn_file(self):
        record = load_record(DATA_DIR / "中炮对列炮黑先平士角炮.pgn")
        assert len(record.plies) == 23
        assert record.plies[0].chinese == "炮二平五"
        assert record.plies[0].side == "red"
        assert record.plies[1].side == "black"
        assert record.plies[0].fen_after != record.plies[0].fen_before

    def test_load_xqf_file(self):
        record = load_record(DATA_DIR / "030-黄松轩先胜冯敬如.XQF")
        assert len(record.plies) > 10

    def test_record_from_text_moves(self):
        record = record_from_text_moves(["炮二平五", "马８进７", "马二进三"])
        assert [ply.iccs for ply in record.plies] == ["h2e2", "h9g7", "h0g2"]

    def test_load_endgame_positions(self):
        positions = load_positions_txt(PROJECT_ROOT / "cchess" / "demo" / "马兵类杀法.txt")
        assert len(positions) > 100
        assert positions[0].fen.endswith("w - - 0 1")

    def test_parse_pgn_headers_and_names(self):
        text = (
            '[Game "Chinese Chess"]\n[Event "Practice"]\n'
            '[Red "Saturn"]\n[Black "入门"]\n[Result "1-0"]\n\n'
            "1. 兵三进一 象７进５\n2. 马八进七 卒３进１\n"
        )
        headers = parse_pgn_headers(text)
        assert headers["Red"] == "Saturn" and headers["Event"] == "Practice"
        record = record_from_pgn_text(text)
        assert record.red_name == "Saturn"
        assert record.black_name == "入门"
        assert len(record.plies) == 4


class TestPractice:
    """人机对练：走子、悔棋、导出棋谱（不依赖引擎的部分）。"""

    def test_levels_mapping(self):
        from qimind.practice import LEVELS

        assert LEVELS["入门"] == 2
        assert LEVELS["最强"] == 16

    def test_pgn_roundtrip(self):
        from qimind.practice import new_session

        session = new_session("red", "中等")
        session.apply_move("h2e2")
        session.apply_move("h9g7")
        session.apply_move("h0g2")
        pgn = session.to_pgn()
        record = record_from_pgn_text(pgn)
        assert [ply.iccs for ply in record.plies] == ["h2e2", "h9g7", "h0g2"]
        assert record.red_name == "玩家"
        assert "QiMind" in record.black_name
        assert record.headers["Event"] == "QiMind 人机对练"

    def test_illegal_move_rejected(self):
        from qimind.practice import new_session

        session = new_session("red", "中等")
        with pytest.raises(ValueError):
            session.apply_move("a0a9")

    def test_undo_returns_to_player_turn(self):
        from qimind.practice import new_session

        session = new_session("red", "中等")
        session.apply_move("h2e2")
        session.apply_move("h9g7")
        assert session.undo() is True
        assert session.moves == []
        assert session.player_turn


def _fake_reports() -> list:
    """构造一份「看起来像引擎分析结果」的数据，用于离线测试报告与导出。"""
    rows = [
        ("red", 1, "炮二平五", "炮二平五", True, 0, 26),
        ("black", 1, "马８进７", "马８进７", True, 0, 22),
        ("red", 2, "马二进三", "马八进七", False, 4, 18),
        ("black", 2, "炮８平５", "卒７进１", False, 210, -160),
        ("red", 3, "车一平二", "车一平二", True, 0, -160),
        ("black", 3, "车９平８", "马２进３", False, 520, -900),
    ]
    reports = []
    for index, (side, move_no, played, best, is_best, loss, score) in enumerate(rows):
        reports.append(
            {
                "fen": FULL_INIT_FEN,
                "side": side,
                "side_label": "红方" if side == "red" else "黑方",
                "move_number": move_no,
                "played_chinese": played,
                "best_chinese": best,
                "best_iccs": "h2e2",
                "is_best": is_best,
                "loss_cp": loss,
                "score_red_cp": score,
                "score_text": "红方稍占上风" if score > 0 else "黑方占优",
                "depth": 12,
                "candidates": [
                    {"rank": 1, "iccs": "h2e2", "chinese": best, "score_text": "+20", "score_cp": 20, "mate_in": None, "pv_chinese": ["炮二平五"]}
                ],
                "explanation": {"text": f"### 一句话\n第 {index + 1} 步讲解示例。", "model": "deepseek-flash", "cached": False, "error": ""},
                "fact_sheet": f"【测试事实 {index}】",
            }
        )
    return reports


class TestReport:
    """对局报告统计（纯离线）。"""

    def _record(self):
        record = record_from_text_moves(["炮二平五", "马８进７", "马二进三", "卒７进１", "车一平二", "马２进３"])
        return record

    def test_stats_and_blunders(self):
        from qimind.report import build_report, find_blunders, find_turning_points

        record = self._record()
        reports = _fake_reports()
        report = build_report(record, reports)
        stats = report["stats"]
        assert stats["red"]["moves"] >= 1 and stats["black"]["moves"] >= 1
        assert stats["black"]["blunders"] >= 1  # 有一手损失 520 分
        # 红方 3 步里有 2 步是正着
        assert stats["red"]["moves"] == 3 and stats["red"]["best"] == 2
        assert stats["red"]["best_rate"] == 67
        turning = find_turning_points(reports)
        assert turning and turning[0]["loss"] >= 200
        blunders = find_blunders(reports)
        assert blunders and "520" in str(blunders[0]["reason"])
        assert "开局判断" in report["fact_sheet"]

    def test_opening_detection(self):
        from qimind.report import identify_opening

        record = self._record()
        opening = identify_opening(record)
        assert "中炮" in opening["name"]

    def test_report_markdown(self):
        from qimind.report import build_report, render_report_markdown

        record = self._record()
        report = build_report(record, _fake_reports())
        text = render_report_markdown(report, record, "讲评正文示例。")
        assert "对局报告" in text
        assert "关键转折" in text and "致命失误" in text
        assert "讲评正文示例" in text


class TestExport:
    """讲评稿导出：Markdown 与零依赖 docx。"""

    def _record(self):
        return record_from_text_moves(["炮二平五", "马８进７", "马二进三"])

    def test_walkthrough_markdown(self):
        from qimind.export import build_walkthrough_markdown

        text = build_walkthrough_markdown(self._record(), _fake_reports()[:2], depth=12, model="deepseek-flash")
        assert "棋谱讲评" in text
        assert "全盘速览" in text and "逐回合讲评" in text
        assert "炮二平五" in text
        assert "| 正着 |" in text

    def test_docx_is_valid_package(self):
        import xml.etree.ElementTree as ET
        import zipfile
        from io import BytesIO

        from qimind.export import Block, build_walkthrough_blocks, docx_bytes

        blocks = build_walkthrough_blocks(self._record(), _fake_reports(), depth=12)
        data = docx_bytes("测试文档", blocks)
        with zipfile.ZipFile(BytesIO(data)) as archive:
            names = set(archive.namelist())
            assert {"[Content_Types].xml", "_rels/.rels", "word/document.xml", "word/styles.xml"} <= names
            document = archive.read("word/document.xml").decode("utf-8")
            # XML 必须可解析，且正文内容确实写入
            root = ET.fromstring(document)
            assert root.tag.endswith("document")
            assert "棋谱讲评" in document
            assert "炮二平五" in document
            assert "TableGrid" in document
            ET.fromstring(archive.read("word/styles.xml").decode("utf-8"))
            ET.fromstring(archive.read("[Content_Types].xml").decode("utf-8"))

    def test_export_blocks_cover_sections(self):
        from qimind.export import build_report_blocks, build_walkthrough_blocks
        from qimind.report import build_report

        record = self._record()
        report = build_report(record, _fake_reports())
        kinds = [block.kind for block in build_report_blocks(report, record, "讲评")]
        assert "title" in kinds and "table" in kinds and "h1" in kinds
        walk = build_walkthrough_blocks(record, _fake_reports()[:2])
        assert any(block.kind == "table" for block in walk)


@pytest.mark.slow
class TestEngine:
    def test_analyse_returns_candidates(self):
        from qimind.engine import analyze_fen, shutdown_engine

        try:
            report = analyze_fen(FULL_INIT_FEN, depth=8, multipv=3, use_cache=False)
            assert report.best_iccs
            assert len(report.candidates) == 3
            assert report.candidates[0].chinese
            assert report.candidates[0].pv
        finally:
            shutdown_engine()

    def test_position_report_without_llm(self):
        from qimind.analyze import analyze_position

        report = analyze_position(FULL_INIT_FEN, depth=8, multipv=2, explain=False)
        assert report.best_iccs
        assert "【引擎结论】" in report.fact_sheet
        assert report.candidates


class TestWeb:
    """用 FastAPI 测试客户端跑一遍界面后端的主要接口（不调用 DeepSeek）。"""

    @pytest.fixture(scope="class")
    def client(self):
        from fastapi.testclient import TestClient

        from qimind.web.server import create_app

        with TestClient(create_app()) as test_client:
            yield test_client

    def test_index_and_static(self, client):
        assert client.get("/").status_code == 200
        assert client.get("/static/app.js").status_code == 200

    def test_config(self, client):
        data = client.get("/api/config").json()
        assert "engine" in data and "model" in data
        assert data["styles"]

    def test_samples(self, client):
        samples = client.get("/api/samples").json()["samples"]
        assert any(item["kind"] == "pgn" for item in samples)

    def test_board_moves_and_move(self, client):
        moves = client.post("/api/board/moves", json={"fen": FULL_INIT_FEN}).json()["moves"]
        assert len(moves) == 44
        assert all(mv["iccs"] for mv in moves)
        result = client.post("/api/board/move", json={"fen": FULL_INIT_FEN, "iccs": "h2e2"}).json()
        assert result["chinese"] == "炮二平五"
        assert result["fen"].startswith("rnbakabnr")

    def test_game_load(self, client):
        path = str(DATA_DIR / "中炮对列炮黑先平士角炮.pgn")
        record = client.post("/api/game/load", json={"path": path}).json()["record"]
        assert len(record["plies"]) == 23

    def test_dialog_open_returns_path(self, client, monkeypatch):
        from qimind.web import server

        target = str(DATA_DIR / "中炮对列炮黑先平士角炮.pgn")
        monkeypatch.setattr(server, "ask_open_filename", lambda **kwargs: target)
        data = client.post("/api/dialog/open").json()
        assert data["path"] == target and data["cancelled"] is False

        monkeypatch.setattr(server, "ask_open_filename", lambda **kwargs: "")
        assert client.post("/api/dialog/open").json()["cancelled"] is True

        def boom(**kwargs):
            raise RuntimeError("系统缺少 Tk 运行库")

        monkeypatch.setattr(server, "ask_open_filename", boom)
        assert client.post("/api/dialog/open").status_code == 500

    def test_game_upload(self, client):
        text = (
            '[Event "Upload"]\n[Red "A"]\n[Black "B"]\n\n'
            "1. 炮二平五 马８进７\n2. 马二进三 车９平８\n"
        ).encode("utf-8")
        response = client.post(
            "/api/game/upload",
            files={"file": ("upload_test.pgn", text, "application/x-chess-pgn")},
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["record"]["red_name"] == "A"
        assert len(payload["record"]["plies"]) == 4
        Path(payload["path"]).unlink(missing_ok=True)

    def test_export_markdown_and_docx(self, client):
        record = record_from_text_moves(
            ["炮二平五", "马８进７", "马二进三", "卒７进１"]
        ).to_dict()
        payload = {
            "kind": "walkthrough",
            "format": "md",
            "record": record,
            "reports": _fake_reports(),
            "style": "棋友",
            "depth": 12,
        }
        data = client.post("/api/export", json=payload).json()
        assert data["format"] == "md" and "棋谱讲评" in data["text"]
        md_path = Path(data["path"])
        assert md_path.is_file()
        download = client.get("/api/export/download", params={"path": str(md_path)})
        assert download.status_code == 200
        md_path.unlink()

        payload["format"] = "docx"
        docx = client.post("/api/export", json=payload).json()
        docx_path = Path(docx["path"])
        assert docx_path.suffix == ".docx" and docx_path.stat().st_size > 2000
        docx_path.unlink()

    def test_export_download_guard(self, client):
        response = client.get("/api/export/download", params={"path": r"C:\Windows\win.ini"})
        assert response.status_code == 400

    def test_secrets_are_never_echoed(self):
        """报错信息里如果带 Key，必须被打码。"""
        from qimind.config import mask_key, scrub_secrets

        leaked = "Error code: 401 - {'message': 'Authentication Fails, Your api key: sk-abcdef1234567890 is invalid'}"
        scrubbed = scrub_secrets(leaked)
        assert "sk-abcdef1234567890" not in scrubbed
        assert "sk-***已隐藏***" in scrubbed
        assert mask_key("sk-abcdef1234567890") == "sk-abc…7890"
        assert mask_key(None) == ""

    def test_secret_flow(self, client, monkeypatch, tmp_path):
        """界面上填写 / 清除 DeepSeek API Key（写进临时目录，不动真实凭据）。"""
        from qimind import config

        monkeypatch.setattr(config, "DATA_DIR", tmp_path)
        monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "settings.json")
        monkeypatch.setattr(config, "_RUNTIME_SETTINGS", None)
        for name in ("QIMIND_DEEPSEEK_KEY", "DEEPSEEK_API_KEY", "QIMIND_DEEPSEEK_API_KEY"):
            monkeypatch.delenv(name, raising=False)

        empty = client.post("/api/secret", json={"api_key": ""}).json()
        assert empty["has_key"] is False
        assert client.post("/api/secret/test", json={}).json()["ok"] is False

        saved = client.post("/api/secret", json={"api_key": "sk-test-1234567890abcdef"}).json()
        assert saved["has_key"] is True
        assert saved["masked"].startswith("sk-tes") and saved["masked"].endswith("cdef")
        assert saved["source"] == "saved"
        assert (tmp_path / "secrets.json").is_file()

        config_state = client.get("/api/config").json()
        assert config_state["has_key"] is True
        assert config_state["deepseek"]["source"] == "saved"

        cleared = client.post("/api/secret", json={"api_key": ""}).json()
        assert cleared["has_key"] is False
        assert "sk-test" not in (tmp_path / "secrets.json").read_text(encoding="utf-8")

    @pytest.mark.slow
    def test_report_job_and_variation_job(self, client):
        path = str(DATA_DIR / "中炮对列炮黑先平士角炮.pgn")
        report_job = client.post(
            "/api/report",
            json={"path": path, "depth": 8, "multipv": 2, "mode": "engine", "max_plies": 6},
        ).json()["job_id"]
        payload = {}
        for _ in range(240):
            payload = client.get(f"/api/jobs/{report_job}").json()
            if payload["status"] in ("done", "error"):
                break
            time.sleep(0.5)
        assert payload["status"] == "done", payload
        report = payload["data"]["report"]
        assert report["opening"]["name"]
        assert report["stats"]["red"]["moves"] >= 1
        assert report["fact_sheet"]

        variation_job = client.post(
            "/api/analyze/variation",
            json={"fen": FULL_INIT_FEN, "iccs": "b2e2", "depth": 8, "multipv": 2},
        ).json()["job_id"]
        for _ in range(240):
            payload = client.get(f"/api/jobs/{variation_job}").json()
            if payload["status"] in ("done", "error"):
                break
            time.sleep(0.5)
        variation = payload["data"]["variation"]
        assert payload["status"] == "done", payload
        assert variation["variation_chinese"]
        assert "candidates" in variation

    @pytest.mark.slow
    def test_position_job_without_llm(self, client):
        job_id = client.post(
            "/api/analyze/position",
            json={"fen": FULL_INIT_FEN, "depth": 8, "multipv": 2, "explain": False},
        ).json()["job_id"]
        for _ in range(120):
            job = client.get(f"/api/jobs/{job_id}").json()
            if job["status"] in ("done", "error"):
                break
        assert job["status"] == "done", job
        assert job["data"]["report"]["best_chinese"]

    @pytest.mark.slow
    def test_practice_flow(self, client):
        """对练接口：开局 → 走子（引擎应招）→ 悔棋 → 导出 → 保存。"""
        state = client.post(
            "/api/practice/new", json={"side": "red", "level": "入门", "depth": 2}
        ).json()
        assert state["player_side"] == "red" and state["player_turn"]
        assert state["move_count"] == 0

        after = client.post(
            "/api/practice/move", json={"session_id": state["id"], "iccs": "h2e2"}
        ).json()
        assert after["played"]["chinese"] == "炮二平五"
        assert after["engine_move"] and after["engine_move"]["iccs"]
        assert after["move_count"] == 2
        assert after["player_turn"] is True

        undone = client.post("/api/practice/undo", json={"session_id": state["id"]}).json()
        assert undone["move_count"] == 0

        pgn = client.get(f"/api/practice/pgn?session_id={state['id']}").json()["pgn"]
        assert "人机对练" in pgn
        download = client.get(f"/api/practice/pgn?session_id={state['id']}&download=1")
        assert download.status_code == 200
        assert "attachment" in download.headers.get("content-disposition", "")

        saved = client.post("/api/practice/save", json={"session_id": state["id"]}).json()
        saved_path = Path(saved["path"])
        assert saved_path.is_file()
        assert len(record_from_pgn_text(saved_path.read_text(encoding="utf-8")).plies) == 0
        saved_path.unlink()

    @pytest.mark.slow
    def test_practice_player_as_black(self, client):
        """玩家执黑时，开局后引擎（红方）应该先走一步。"""
        state = client.post(
            "/api/practice/new", json={"side": "black", "level": "入门", "depth": 2}
        ).json()
        assert state["player_side"] == "black"
        assert state["move_count"] == 1
        assert state["moves"][0]["side"] == "red"
        assert state["player_turn"] is True
