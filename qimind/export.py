"""讲评稿导出：Markdown 与 Word（.docx）。

Word 部分不依赖 python-docx，直接用标准库 ``zipfile`` 组装 OOXML，
这样用户不需要额外装包就能导出；文档结构包含标题、小标题、
段落、项目符号与表格。
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .games import GameRecord


@dataclass
class Block:
    """文档中的一个块（标题 / 段落 / 列表 / 表格）。"""

    kind: str  # title / h1 / h2 / p / bullet / table / quote
    text: str = ""
    rows: List[List[str]] = field(default_factory=list)


def _escape_md(text: str) -> str:
    """转义 Markdown 里会被误解析的字符（保留正文可读性）。"""
    return (text or "").replace("|", "｜").strip()


def markdown_to_blocks(markdown: str) -> List[Block]:
    """把（大模型输出的）Markdown 转成块列表，供 Word 渲染。"""
    blocks: List[Block] = []
    for raw in (markdown or "").splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("####"):
            blocks.append(Block("h2", stripped.lstrip("#").strip()))
        elif stripped.startswith("###"):
            blocks.append(Block("h2", stripped.lstrip("#").strip()))
        elif stripped.startswith("##"):
            blocks.append(Block("h1", stripped.lstrip("#").strip()))
        elif stripped.startswith("#"):
            blocks.append(Block("h1", stripped.lstrip("#").strip()))
        elif stripped.startswith(("- ", "* ")):
            blocks.append(Block("bullet", stripped[2:].strip()))
        elif stripped.startswith(">"):
            blocks.append(Block("quote", stripped.lstrip(">").strip()))
        else:
            blocks.append(Block("p", stripped))
    return blocks


# --------------------------------------------------------------------------- 讲评稿
def build_walkthrough_markdown(
    record: GameRecord,
    reports: Sequence[Dict[str, object]],
    *,
    depth: Optional[int] = None,
    model: str = "",
    style: str = "棋友",
    include_facts: bool = False,
    report: Optional[Dict[str, object]] = None,
    narrative: str = "",
) -> str:
    """生成整局讲评稿（Markdown）。"""
    lines: List[str] = [f"# 棋谱讲评：{record.name}", ""]
    lines.append(f"- 红方：{record.red_name}")
    lines.append(f"- 黑方：{record.black_name}")
    lines.append(f"- 结果：{record.headers.get('Result', '*')}")
    lines.append(f"- 步数：{len(reports)} 步")
    if depth:
        lines.append(f"- 引擎搜索深度：{depth}")
    if model:
        lines.append(f"- 讲解模型：{model}")
    lines.append(f"- 讲解风格：{style}")
    lines.append(f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("")

    if report:
        lines.append(f"- 开局判断：{report.get('opening', {}).get('name', '-')}")  # type: ignore[union-attr]
        lines.append("")

    # 概览表
    lines.append("## 全盘速览")
    lines.append("")
    lines.append("| 回合 | 走子 | 实战 | 引擎正着 | 评分 | 评价 |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for index, item in enumerate(reports):
        user_move = item.get("played_chinese") or ""
        best = item.get("best_chinese") or ""
        loss = int(item.get("loss_cp") or 0)
        if not item.get("best_iccs"):
            verdict = "未分析"
        elif item.get("is_best"):
            verdict = "正着"
        elif loss >= 300:
            verdict = f"漏着（-{loss}）"
        elif loss >= 100:
            verdict = f"疑问手（-{loss}）"
        elif loss >= 50:
            verdict = f"稍亏（-{loss}）"
        else:
            verdict = "可接受"
        side = "红" if item.get("side") == "red" else "黑"
        number = item.get("move_number") or (index // 2 + 1)
        lines.append(
            f"| {number} | {side} | {_escape_md(str(user_move))} | {_escape_md(str(best))} | "
            f"{item.get('score_text', '')} | {verdict} |"
        )
    lines.append("")

    # 逐回合讲评
    lines.append("## 逐回合讲评")
    lines.append("")
    for index, item in enumerate(reports):
        move_no = item.get("move_number") or (index // 2 + 1)
        side = "红方" if item.get("side") == "red" else "黑方"
        played = item.get("played_chinese") or ""
        best = item.get("best_chinese") or ""
        loss = int(item.get("loss_cp") or 0)
        flag = "✅ 正着" if item.get("is_best") else f"⚠️ 与正着相差 {loss} 分"
        lines.append(f"### 第 {move_no} 回合 {side}：{played} {flag}")
        lines.append("")
        lines.append(f"- 引擎正着：**{best}**（{item.get('score_text', '')}）")
        candidates = item.get("candidates") or []
        if candidates:
            top = "、".join(
                f"{cand.get('chinese')} {cand.get('score_text')}" for cand in candidates[:4]  # type: ignore[union-attr]
            )
            lines.append(f"- 候选着法：{top}")
        explanation = item.get("explanation") or {}
        text = (explanation.get("text") or "").strip()  # type: ignore[union-attr]
        if text:
            lines.append("")
            lines.append(text)
        elif explanation.get("error"):  # type: ignore[union-attr]
            lines.append("")
            lines.append(f"> 讲解生成失败：{explanation.get('error')}")  # type: ignore[union-attr]
        else:
            lines.append("")
            lines.append("> 这一步没有生成讲解。")
        if include_facts and item.get("fact_sheet"):
            lines.append("")
            lines.append("<details><summary>引擎事实清单</summary>")
            lines.append("")
            lines.append("```text")
            lines.append(str(item.get("fact_sheet")))
            lines.append("```")
            lines.append("")
            lines.append("</details>")
        lines.append("")

    if report and narrative:
        lines.append("## 对局报告")
        lines.append("")
        lines.append(narrative.strip())
        lines.append("")
    return "\n".join(lines)


def build_walkthrough_blocks(
    record: GameRecord,
    reports: Sequence[Dict[str, object]],
    *,
    depth: Optional[int] = None,
    model: str = "",
    style: str = "棋友",
    report: Optional[Dict[str, object]] = None,
    narrative: str = "",
) -> List[Block]:
    """生成整局讲评稿的文档块（供 Word 导出）。"""
    blocks: List[Block] = [Block("title", f"棋谱讲评：{record.name}")]
    meta_rows = [
        ["红方", record.red_name],
        ["黑方", record.black_name],
        ["结果", str(record.headers.get("Result", "*"))],
        ["步数", f"{len(reports)} 步"],
    ]
    if depth:
        meta_rows.append(["引擎搜索深度", str(depth)])
    if model:
        meta_rows.append(["讲解模型", model])
    meta_rows.append(["生成时间", datetime.now().strftime("%Y-%m-%d %H:%M")])
    if report:
        meta_rows.append(["开局判断", str(report.get("opening", {}).get("name", "-"))])  # type: ignore[union-attr]
    blocks.append(Block("table", rows=meta_rows))

    overview_rows: List[List[str]] = [["回合", "方", "实战", "引擎正着", "评分", "评价"]]
    for index, item in enumerate(reports):
        loss = int(item.get("loss_cp") or 0)
        if not item.get("best_iccs"):
            verdict = "未分析"
        elif item.get("is_best"):
            verdict = "正着"
        elif loss >= 300:
            verdict = f"漏着（-{loss}）"
        elif loss >= 100:
            verdict = f"疑问手（-{loss}）"
        elif loss >= 50:
            verdict = f"稍亏（-{loss}）"
        else:
            verdict = "可接受"
        overview_rows.append(
            [
                str(item.get("move_number") or (index // 2 + 1)),
                "红" if item.get("side") == "red" else "黑",
                str(item.get("played_chinese") or ""),
                str(item.get("best_chinese") or ""),
                str(item.get("score_text") or ""),
                verdict,
            ]
        )
    blocks.append(Block("h1", "全盘速览"))
    blocks.append(Block("table", rows=overview_rows))

    blocks.append(Block("h1", "逐回合讲评"))
    for index, item in enumerate(reports):
        move_no = item.get("move_number") or (index // 2 + 1)
        side = "红方" if item.get("side") == "red" else "黑方"
        played = str(item.get("played_chinese") or "")
        loss = int(item.get("loss_cp") or 0)
        flag = "✅ 正着" if item.get("is_best") else f"⚠️ 与正着相差 {loss} 分"
        blocks.append(Block("h2", f"第 {move_no} 回合 {side}：{played} {flag}"))
        blocks.append(
            Block(
                "p",
                f"引擎正着：{item.get('best_chinese', '')}（{item.get('score_text', '')}）",
            )
        )
        candidates = item.get("candidates") or []
        if candidates:
            blocks.append(
                Block(
                    "p",
                    "候选着法："
                    + "、".join(
                        f"{cand.get('chinese')} {cand.get('score_text')}" for cand in candidates[:4]  # type: ignore[union-attr]
                    ),
                )
            )
        explanation = item.get("explanation") or {}
        text = str((explanation.get("text") or "")).strip()  # type: ignore[union-attr]
        if text:
            blocks.extend(markdown_to_blocks(text))
        elif explanation.get("error"):  # type: ignore[union-attr]
            blocks.append(Block("quote", f"讲解生成失败：{explanation.get('error')}"))  # type: ignore[union-attr]
        else:
            blocks.append(Block("quote", "这一步没有生成讲解。"))

    if report and narrative:
        blocks.append(Block("h1", "对局报告"))
        blocks.extend(markdown_to_blocks(narrative))
    return blocks


def build_report_blocks(report: Dict[str, object], record: GameRecord, narrative: str = "") -> List[Block]:
    """把对局报告转成文档块。"""
    opening = report.get("opening", {}) or {}  # type: ignore[assignment]
    stats = report.get("stats", {}) or {}  # type: ignore[assignment]
    blocks: List[Block] = [Block("title", f"对局报告：{record.name}")]
    blocks.append(
        Block(
            "table",
            rows=[
                ["红方", record.red_name],
                ["黑方", record.black_name],
                ["结果", str(record.headers.get("Result", "*"))],
                ["开局", str(opening.get("name", "-"))],
                ["步数", str(report.get("move_count", 0))],
            ],
        )
    )

    red = stats.get("red", {})  # type: ignore[union-attr]
    black = stats.get("black", {})  # type: ignore[union-attr]
    blocks.append(Block("h1", "双方数据"))
    blocks.append(
        Block(
            "table",
            rows=[
                ["指标", "红方", "黑方"],
                ["总步数", str(red.get("moves", 0)), str(black.get("moves", 0))],
                ["正着数", str(red.get("best", 0)), str(black.get("best", 0))],
                ["正着率", f"{red.get('best_rate', 0)}%", f"{black.get('best_rate', 0)}%"],
                ["平均每步损失", str(red.get("loss_avg", 0)), str(black.get("loss_avg", 0))],
                ["最大一步损失", str(red.get("loss_max", 0)), str(black.get("loss_max", 0))],
                ["疑问手（≥100 分）", str(red.get("mistakes", 0)), str(black.get("mistakes", 0))],
                ["漏着（≥300 分）", str(red.get("blunders", 0)), str(black.get("blunders", 0))],
            ],
        )
    )

    turning = report.get("turning_points") or []
    if turning:
        from .report import loss_text

        blocks.append(Block("h1", "关键转折"))
        for item in turning:  # type: ignore[union-attr]
            blocks.append(
                Block(
                    "bullet",
                    f"第 {item['move_number']} 回合 {item['side_label']} {item['played']}："
                    f"{loss_text(item)}，引擎正着 {item['best']}，形势 {item.get('score_text', '')}",
                )
            )
    blunders = report.get("blunders") or []
    if blunders:
        blocks.append(Block("h1", "致命失误"))
        for item in blunders:  # type: ignore[union-attr]
            blocks.append(
                Block(
                    "bullet",
                    f"第 {item['move_number']} 回合 {item['side_label']} {item['played']}："
                    f"{item.get('reason', '')}，应走 {item['best']}",
                )
            )
    if narrative:
        blocks.append(Block("h1", "讲评"))
        blocks.extend(markdown_to_blocks(narrative))
    return blocks


# --------------------------------------------------------------------------- DOCX
_DOCX_PART_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
</Types>
"""

_DOCX_PART_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
</Relationships>
"""

_DOCX_PART_DOC_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>
"""

_DOCX_PART_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:docDefaults>
    <w:rPrDefault>
      <w:rPr>
        <w:rFonts w:ascii="Segoe UI" w:hAnsi="Segoe UI" w:eastAsia="微软雅黑"/>
        <w:sz w:val="21"/>
        <w:szCs w:val="21"/>
      </w:rPr>
    </w:rPrDefault>
    <w:pPrDefault>
      <w:pPr><w:spacing w:after="120" w:line="300" w:lineRule="auto"/></w:pPr>
    </w:pPrDefault>
  </w:docDefaults>
  <w:style w:type="paragraph" w:default="1" w:styleId="Normal">
    <w:name w:val="Normal"/>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Title">
    <w:name w:val="Title"/>
    <w:basedOn w:val="Normal"/>
    <w:pPr><w:jc w:val="center"/></w:pPr>
    <w:rPr><w:b/><w:sz w:val="40"/><w:szCs w:val="40"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading1">
    <w:name w:val="heading 1"/>
    <w:basedOn w:val="Normal"/>
    <w:pPr><w:spacing w:before="240" w:after="120"/><w:outlineLvl w:val="0"/></w:pPr>
    <w:rPr><w:b/><w:sz w:val="30"/><w:szCs w:val="30"/><w:color w:val="1F7A5A"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading2">
    <w:name w:val="heading 2"/>
    <w:basedOn w:val="Normal"/>
    <w:pPr><w:spacing w:before="180" w:after="80"/><w:outlineLvl w:val="1"/></w:pPr>
    <w:rPr><w:b/><w:sz w:val="26"/><w:szCs w:val="26"/><w:color w:val="B58B2F"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="ListBullet">
    <w:name w:val="List Bullet"/>
    <w:basedOn w:val="Normal"/>
    <w:pPr><w:ind w:left="420" w:hanging="240"/></w:pPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Quote">
    <w:name w:val="Quote"/>
    <w:basedOn w:val="Normal"/>
    <w:pPr><w:ind w:left="420"/></w:pPr>
    <w:rPr><w:i/><w:color w:val="6B7280"/></w:rPr>
  </w:style>
  <w:style w:type="table" w:styleId="TableGrid">
    <w:name w:val="Table Grid"/>
    <w:tblPr>
      <w:tblBorders>
        <w:top w:val="single" w:sz="4" w:color="D0C8B8"/>
        <w:left w:val="single" w:sz="4" w:color="D0C8B8"/>
        <w:bottom w:val="single" w:sz="4" w:color="D0C8B8"/>
        <w:right w:val="single" w:sz="4" w:color="D0C8B8"/>
        <w:insideH w:val="single" w:sz="4" w:color="D0C8B8"/>
        <w:insideV w:val="single" w:sz="4" w:color="D0C8B8"/>
      </w:tblBorders>
    </w:tblPr>
  </w:style>
</w:styles>
"""


def _xml_escape(text: str) -> str:
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _paragraph(text: str, style: Optional[str] = None, bold: bool = False) -> str:
    props = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    runs = (
        f'<w:r><w:rPr><w:b/></w:rPr><w:t xml:space="preserve">{_xml_escape(text)}</w:t></w:r>'
        if bold
        else f'<w:r><w:t xml:space="preserve">{_xml_escape(text)}</w:t></w:r>'
    )
    return f"<w:p>{props}{runs}</w:p>"


def _table(rows: Sequence[Sequence[str]]) -> str:
    if not rows:
        return ""
    parts = [
        '<w:tbl><w:tblPr><w:tblStyle w:val="TableGrid"/>'
        '<w:tblW w:w="0" w:type="auto"/></w:tblPr>'
    ]
    for row_index, row in enumerate(rows):
        parts.append("<w:tr>")
        for cell in row:
            bold = row_index == 0
            parts.append(
                "<w:tc><w:tcPr/><w:p>"
                + (
                    f'<w:r><w:rPr><w:b/></w:rPr><w:t xml:space="preserve">{_xml_escape(str(cell))}</w:t></w:r>'
                    if bold
                    else f'<w:r><w:t xml:space="preserve">{_xml_escape(str(cell))}</w:t></w:r>'
                )
                + "</w:p></w:tc>"
            )
        parts.append("</w:tr>")
    parts.append("</w:tbl>")
    # 表格后补一个空段落，避免与下一段粘连
    parts.append("<w:p/>")
    return "".join(parts)


def blocks_to_document_xml(blocks: Sequence[Block]) -> str:
    """把块列表渲染成 word/document.xml。"""
    body: List[str] = []
    for block in blocks:
        if block.kind == "title":
            body.append(_paragraph(block.text, style="Title"))
        elif block.kind == "h1":
            body.append(_paragraph(block.text, style="Heading1"))
        elif block.kind == "h2":
            body.append(_paragraph(block.text, style="Heading2"))
        elif block.kind == "bullet":
            body.append(_paragraph("• " + block.text, style="ListBullet"))
        elif block.kind == "quote":
            body.append(_paragraph(block.text, style="Quote"))
        elif block.kind == "table":
            body.append(_table(block.rows))
        else:
            body.append(_paragraph(block.text))
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>" + "".join(body) + '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134"/></w:sectPr>'
        "</w:body></w:document>"
    )


def docx_bytes(title: str, blocks: Sequence[Block], author: str = "QiMind") -> bytes:
    """生成 .docx 文件的字节内容（零依赖）。"""
    created = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    core = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f"<dc:title>{_xml_escape(title)}</dc:title>"
        f"<dc:creator>{_xml_escape(author)}</dc:creator>"
        f'<dcterms:created xsi:type="dcterms:W3CDTF">{created}</dcterms:created>'
        "</cp:coreProperties>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _DOCX_PART_CONTENT_TYPES)
        archive.writestr("_rels/.rels", _DOCX_PART_RELS)
        archive.writestr("word/_rels/document.xml.rels", _DOCX_PART_DOC_RELS)
        archive.writestr("word/document.xml", blocks_to_document_xml(blocks))
        archive.writestr("word/styles.xml", _DOCX_PART_STYLES)
        archive.writestr("docProps/core.xml", core)
    return buffer.getvalue()


def write_docx(path: str | Path, title: str, blocks: Sequence[Block]) -> Path:
    """把文档块写入 .docx 文件。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(docx_bytes(title, blocks))
    return target
