/* QiMind 前端：棋盘绘制 + 任务轮询 + 讲解展示
 * 坐标约定：x = 0..8（左→右），y = 0..9（红方底线 → 黑方底线）
 */

const START_FEN = "rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR w - - 0 1";

const PIECE_TEXT = {
  K: "帅", A: "仕", B: "相", N: "马", R: "车", C: "炮", P: "兵",
  k: "将", a: "士", b: "象", n: "马", r: "车", c: "炮", p: "卒",
};

const CELL = 54;
const MARGIN = 30;
const BOARD_W = MARGIN * 2 + CELL * 8;
const BOARD_H = MARGIN * 2 + CELL * 9;

const state = {
  config: null,
  record: null,
  reports: {},        // ply index (1 起) -> report
  cursor: 0,          // 0 = 初始局面；k = 第 k 步之后
  flip: false,
  free: false,
  freeFen: START_FEN,
  selected: null,     // [x, y]
  legalMoves: [],     // 当前选中棋子的合法走法
  arrow: { played: null, best: null },
  jobId: null,
  pollTimer: null,
  positions: [],
  mode: "study",        // study = 棋谱讲解，practice = 人机对练
  practice: null,       // 当前对练会话
  practiceLevels: [],
  lastGameJobId: null,  // 最近一次整局分析任务（供报告/导出复用）
  report: null,         // 最近生成的对局报告
  variation: null,      // 当前展示的变招讲解
  variationKey: "",     // 变招对应的（局面+走法），用于切换时清空
};

const el = (id) => document.getElementById(id);
const canvas = el("board");
const ctx = canvas.getContext("2d");

/* ------------------------------------------------------------------ 工具函数 */

function fenBoard(fen) {
  const rows = (fen || START_FEN).trim().split(/\s+/)[0].split("/");
  const grid = Array.from({ length: 10 }, () => Array(9).fill(""));
  rows.forEach((row, r) => {
    let x = 0;
    for (const ch of row) {
      if (/\d/.test(ch)) {
        x += parseInt(ch, 10);
      } else {
        const y = 9 - r;
        if (x < 9 && y >= 0) grid[y][x] = ch;
        x += 1;
      }
    }
  });
  return grid;
}

function fenSide(fen) {
  const parts = (fen || "").trim().split(/\s+/);
  return parts[1] === "b" ? "black" : "red";
}

function iccsToPos(iccs) {
  if (!iccs || iccs.length < 4) return null;
  const file = (ch) => ch.charCodeAt(0) - 97;
  return {
    from: [file(iccs[0]), parseInt(iccs[1], 10)],
    to: [file(iccs[2]), parseInt(iccs[3], 10)],
  };
}

function currentFen() {
  if (state.mode === "practice" && state.practice) return state.practice.fen;
  if (state.free) return state.freeFen;
  if (!state.record) return START_FEN;
  if (state.cursor === 0) return state.record.init_fen;
  return state.record.plies[state.cursor - 1].fen_after;
}

function currentPly() {
  if (state.mode === "practice") return null;
  if (state.free || !state.record || state.cursor === 0) return null;
  return state.record.plies[state.cursor - 1];
}

function currentReport() {
  const ply = currentPly();
  if (!ply) return null;
  return state.reports[ply.index] || null;
}

/* ------------------------------------------------------------------ 棋盘绘制 */

function setupCanvas() {
  const dpr = window.devicePixelRatio || 1;
  canvas.width = BOARD_W * dpr;
  canvas.height = BOARD_H * dpr;
  canvas.style.width = BOARD_W + "px";
  canvas.style.height = BOARD_H + "px";
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}

function px(x) {
  const bx = state.flip ? 8 - x : x;
  return MARGIN + bx * CELL;
}

function py(y) {
  const by = state.flip ? 9 - y : y;
  return MARGIN + (9 - by) * CELL;
}

function drawBoard() {
  ctx.clearRect(0, 0, BOARD_W, BOARD_H);

  ctx.fillStyle = "#f0d9a7";
  ctx.fillRect(0, 0, BOARD_W, BOARD_H);
  drawCoordinates();
  ctx.strokeStyle = "#8a6a3a";
  ctx.lineWidth = 1;

  // 横线
  for (let y = 0; y <= 9; y += 1) {
    const line = y === 0 || y === 9 ? 2 : 1;
    ctx.lineWidth = line;
    ctx.beginPath();
    ctx.moveTo(px(0), py(y));
    ctx.lineTo(px(8), py(y));
    ctx.stroke();
  }

  // 竖线（中间 7 条被楚河汉界断开）
  for (let x = 0; x <= 8; x += 1) {
    ctx.lineWidth = x === 0 || x === 8 ? 2 : 1;
    if (x === 0 || x === 8) {
      ctx.beginPath();
      ctx.moveTo(px(x), py(0));
      ctx.lineTo(px(x), py(9));
      ctx.stroke();
    } else {
      ctx.beginPath();
      ctx.moveTo(px(x), py(0));
      ctx.lineTo(px(x), py(4));
      ctx.stroke();
      ctx.beginPath();
      ctx.moveTo(px(x), py(5));
      ctx.lineTo(px(x), py(9));
      ctx.stroke();
    }
  }

  // 九宫斜线
  ctx.lineWidth = 1;
  const palaces = [[0, 2], [7, 9]];
  palaces.forEach(([y0, y1]) => {
    ctx.beginPath();
    ctx.moveTo(px(3), py(y0));
    ctx.lineTo(px(5), py(y1));
    ctx.stroke();
    ctx.beginPath();
    ctx.moveTo(px(5), py(y0));
    ctx.lineTo(px(3), py(y1));
    ctx.stroke();
  });

  // 楚河汉界
  ctx.fillStyle = "rgba(138,106,58,.85)";
  ctx.font = '20px "KaiTi", "STKaiti", serif';
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  const midY = (py(4) + py(5)) / 2;
  ctx.fillText("楚 河", px(2), midY);
  ctx.fillText("漢 界", px(6), midY);
}

/* 棋盘外圈的坐标标注：
 * 红方一侧用中文数字（一在最右、九在最左，等同中文记谱的「路」），
 * 黑方一侧用全角数字（黑方视角的一在最右），
 * 两边的行号都从红方底线数起：1 是红方底线、10 是黑方底线。
 */
const CN_FILES = ["一", "二", "三", "四", "五", "六", "七", "八", "九"];
const BLACK_FILES = ["１", "２", "３", "４", "５", "６", "７", "８", "９"];

function drawCoordinates() {
  ctx.save();
  ctx.fillStyle = "rgba(105,80,44,.92)";
  ctx.font = '12px "Microsoft YaHei", "PingFang SC", "Segoe UI", sans-serif';
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";

  // 红方底线一侧：一~九（红方视角，从右到左）
  for (let x = 0; x <= 8; x += 1) {
    ctx.fillText(CN_FILES[8 - x], px(x), py(0) + MARGIN / 2);
  }
  // 黑方底线一侧：１~９（黑方视角，从右到左 = 屏幕从左到右）
  for (let x = 0; x <= 8; x += 1) {
    ctx.fillText(BLACK_FILES[x], px(x), py(9) - MARGIN / 2);
  }
  // 行号：从红方底线数起 1~10，左右两侧都标
  for (let y = 0; y <= 9; y += 1) {
    const label = String(y + 1);
    ctx.fillText(label, px(0) - MARGIN / 2, py(y));
    ctx.fillText(label, px(8) + MARGIN / 2, py(y));
  }
  ctx.restore();
}

function drawMarker(pos, color, radius) {
  ctx.beginPath();
  ctx.arc(px(pos[0]), py(pos[1]), radius || CELL * 0.44, 0, Math.PI * 2);
  ctx.strokeStyle = color;
  ctx.lineWidth = 3;
  ctx.stroke();
}

function drawArrow(from, to, color) {
  const x1 = px(from[0]);
  const y1 = py(from[1]);
  const x2 = px(to[0]);
  const y2 = py(to[1]);
  const angle = Math.atan2(y2 - y1, x2 - x1);
  const head = 13;
  const shaft = CELL * 0.30;
  const sx = x1 + Math.cos(angle) * shaft;
  const sy = y1 + Math.sin(angle) * shaft;
  const ex = x2 - Math.cos(angle) * shaft;
  const ey = y2 - Math.sin(angle) * shaft;

  ctx.strokeStyle = color;
  ctx.fillStyle = color;
  ctx.lineWidth = 3.5;
  ctx.beginPath();
  ctx.moveTo(sx, sy);
  ctx.lineTo(ex, ey);
  ctx.stroke();
  ctx.beginPath();
  ctx.moveTo(ex, ey);
  ctx.lineTo(ex - head * Math.cos(angle - 0.42), ey - head * Math.sin(angle - 0.42));
  ctx.lineTo(ex - head * Math.cos(angle + 0.42), ey - head * Math.sin(angle + 0.42));
  ctx.closePath();
  ctx.fill();
}

function drawPieces(fen) {
  const grid = fenBoard(fen);
  for (let y = 0; y <= 9; y += 1) {
    for (let x = 0; x <= 8; x += 1) {
      const ch = grid[y][x];
      if (!ch) continue;
      const cx = px(x);
      const cy = py(y);
      const isRed = ch === ch.toUpperCase();
      const color = isRed ? "#b3261e" : "#23303f";

      const grad = ctx.createRadialGradient(cx - 6, cy - 8, 4, cx, cy, CELL * 0.44);
      grad.addColorStop(0, "#fffdf7");
      grad.addColorStop(1, "#efdfbf");
      ctx.beginPath();
      ctx.arc(cx, cy, CELL * 0.42, 0, Math.PI * 2);
      ctx.fillStyle = grad;
      ctx.fill();
      ctx.strokeStyle = color;
      ctx.lineWidth = 2;
      ctx.stroke();
      ctx.beginPath();
      ctx.arc(cx, cy, CELL * 0.35, 0, Math.PI * 2);
      ctx.strokeStyle = color;
      ctx.lineWidth = 1;
      ctx.stroke();

      ctx.fillStyle = color;
      ctx.font = `bold ${Math.round(CELL * 0.5)}px "KaiTi", "STKaiti", "SimSun", serif`;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(PIECE_TEXT[ch] || ch, cx, cy + 1);
    }
  }
}

function drawOverlays() {
  const ply = currentPly();
  const move = ply ? iccsToPos(ply.iccs) : null;
  if (move) {
    drawMarker(move.from, "rgba(35,48,63,.45)", CELL * 0.46);
    drawMarker(move.to, "rgba(179,38,30,.55)", CELL * 0.46);
  }
  if (state.arrow.played) drawArrow(state.arrow.played.from, state.arrow.played.to, "rgba(35,48,63,.55)");
  if (state.arrow.best) drawArrow(state.arrow.best.from, state.arrow.best.to, "rgba(31,122,90,.85)");

  if (state.selected) {
    drawMarker(state.selected, "rgba(181,139,47,.95)", CELL * 0.46);
  }
  if (state.free) {
    state.legalMoves.forEach((mv) => {
      ctx.beginPath();
      ctx.arc(px(mv.to[0]), py(mv.to[1]), 6, 0, Math.PI * 2);
      ctx.fillStyle = mv.capture ? "rgba(179,38,30,.75)" : "rgba(31,122,90,.65)";
      ctx.fill();
    });
  }
}

function render() {
  const fen = currentFen();
  drawBoard();
  drawPieces(fen);
  drawOverlays();

  const side = fenSide(fen);
  if (state.mode === "practice" && state.practice) {
    const session = state.practice;
    el("positionMeta").innerHTML =
      `对练中：你执 <b>${session.player_side_label}</b>，引擎 <b>${session.level}</b>，` +
      `共 ${session.move_count} 步 ｜ 轮到 <b>${session.side_to_move_label}</b>` +
      `<br />${session.player_turn ? "点自己的棋子走子" : "引擎思考中…"}` +
      `<br /><code>${fen}</code>`;
  } else {
    el("positionMeta").innerHTML =
      `当前局面：<code>${fen}</code><br />轮到 <b>${side === "red" ? "红方" : "黑方"}</b> 走棋` +
      (state.free ? " ｜ 自由摆子模式：点自己的棋子可以走子" : "");
  }
  el("chkFree").checked = state.free;
}

/* ------------------------------------------------------------------ 走法列表 */

function badgeFor(report) {
  if (!report) return '<span class="mv-badge pending">待分析</span>';
  if (report.error && !report.best_iccs) return '<span class="mv-badge bad">分析失败</span>';
  if (report.is_best) return '<span class="mv-badge best">正着</span>';
  const loss = report.loss_cp || 0;
  if (loss >= 400) return '<span class="mv-badge bad">漏着</span>';
  if (loss >= 150) return '<span class="mv-badge bad">疑问手</span>';
  return '<span class="mv-badge alt">可改进</span>';
}

function renderMoves() {
  const box = el("movesList");
  const meta = el("recordMeta");
  if (state.mode === "practice") {
    const session = state.practice;
    meta.textContent = session
      ? `人机对练 ｜ 你执${session.player_side_label} ｜ 引擎${session.level} ｜ ${session.result_text}`
      : "人机对练";
    box.innerHTML = "";
    if (!session || !session.moves.length) {
      box.innerHTML = '<p class="hint">还没有走棋。点「开始对练」，然后在棋盘上点自己的棋子走子。</p>';
      return;
    }
    session.moves.forEach((move, index) => {
      const row = document.createElement("div");
      row.className = `move-row ${move.side}` + (index === session.moves.length - 1 ? " active" : "");
      const no = move.side === "red" ? `${Math.floor(index / 2) + 1}.` : "";
      const who = move.side === session.player_side ? "你" : "引擎";
      row.innerHTML = `<span class="mv-no">${no}</span><span class="mv-text">${move.chinese}</span>` +
        `<span class="mv-badge pending">${who}${move.is_check ? " 将军" : ""}</span>`;
      box.appendChild(row);
    });
    const active = box.querySelector(".move-row.active");
    if (active) active.scrollIntoView({ block: "nearest" });
    return;
  }
  if (state.record) {
    meta.textContent =
      `${state.record.name} ｜ 红 ${state.record.red_name} vs 黑 ${state.record.black_name} ｜ ` +
      `共 ${state.record.plies.length} 步`;
  } else if (state.positions.length) {
    meta.textContent = "残局题库";
  } else {
    meta.textContent = "";
  }
  if (state.positions.length) {
    box.innerHTML = `<p class="hint">残局题库：共 ${state.positions.length} 个局面，点击任意一行载入到棋盘（自由模式）。</p>`;
    state.positions.slice(0, 200).forEach((pos) => {
      const row = document.createElement("div");
      row.className = "move-row";
      row.innerHTML = `<span class="mv-no"></span><span class="mv-text">${pos.name}</span><span class="mv-badge pending">载入</span>`;
      row.onclick = () => loadPosition(pos);
      box.appendChild(row);
    });
    return;
  }

  if (!state.record) {
    box.innerHTML = '<p class="hint">还没有载入棋谱。上方可以载入示例、PGN 或 XQF 文件。</p>';
    return;
  }

  box.innerHTML = "";
  const start = document.createElement("div");
  start.className = "move-row" + (state.cursor === 0 ? " active" : "");
  start.innerHTML = `<span class="mv-no">00</span><span class="mv-text">初始局面</span><span></span>`;
  start.onclick = () => gotoPly(0);
  box.appendChild(start);

  state.record.plies.forEach((ply) => {
    const row = document.createElement("div");
    row.className = `move-row ${ply.side}` + (state.cursor === ply.index ? " active" : "");
    const no = ply.side === "red" ? `${ply.move_number}.` : "";
    row.innerHTML = `<span class="mv-no">${no}</span><span class="mv-text">${ply.chinese}</span>${badgeFor(state.reports[ply.index])}`;
    row.onclick = () => gotoPly(ply.index);
    box.appendChild(row);
  });

  const active = box.querySelector(".move-row.active");
  if (active) active.scrollIntoView({ block: "nearest" });
}

function gotoPly(index) {
  state.cursor = Math.max(0, Math.min(index, state.record ? state.record.plies.length : 0));
  state.selected = null;
  state.legalMoves = [];
  updateArrows();
  render();
  renderMoves();
  renderReport();
}

function updateArrows() {
  const report = currentReport();
  const ply = currentPly();
  state.arrow.played = ply ? iccsToPos(ply.iccs) : null;
  state.arrow.best = report && report.best_iccs ? iccsToPos(report.best_iccs) : null;
}

/* ------------------------------------------------------------------ 讲解面板 */

function mdToHtml(text) {
  const escape = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const lines = escape(text || "").split(/\r?\n/);
  let html = "";
  let inList = false;
  const closeList = () => { if (inList) { html += "</ul>"; inList = false; } };

  lines.forEach((raw) => {
    const line = raw.trim();
    if (!line) { closeList(); return; }
    if (line.startsWith("###")) { closeList(); html += `<h3>${line.replace(/^#+\s*/, "")}</h3>`; return; }
    if (line.startsWith("##")) { closeList(); html += `<h3>${line.replace(/^#+\s*/, "")}</h3>`; return; }
    if (/^[-*]\s+/.test(line)) {
      if (!inList) { html += "<ul>"; inList = true; }
      html += `<li>${line.replace(/^[-*]\s+/, "")}</li>`;
      return;
    }
    closeList();
    html += `<p>${line}</p>`;
  });
  closeList();
  return html.replace(/\*\*(.+?)\*\*/g, "<b>$1</b>");
}

function renderReport() {
  const header = el("moveHeader");
  const candidates = el("candidates");
  const explanation = el("explanation");
  const factBox = el("factBox");

  if (state.mode === "practice") {
    const session = state.practice;
    header.innerHTML = session
      ? `<span class="title">人机对练</span><span class="hint"> ｜ 你执${session.player_side_label}，引擎${session.level} ｜ ${session.result_text}</span>`
      : '<span class="hint">人机对练模式：只和引擎下棋，不调用 DeepSeek。</span>';
    candidates.innerHTML = "";
    explanation.innerHTML =
      '<p class="hint">对练模式不生成讲解。下完之后点「保存并去讲解」，' +
      '这一局会自动载入讲解模式，再逐回合分析正着与理由。</p>';
    factBox.hidden = true;
    return;
  }

  const report = currentReport() || state.freeReport;
  if (!report) {
    header.innerHTML = state.record
      ? "点击走法列表中的任意一步，查看引擎正着与讲解。"
      : "载入棋谱后逐步查看讲解，或直接对当前局面提问。";
    candidates.innerHTML = "";
    explanation.innerHTML = '<p class="hint">还没有讲解内容。</p>';
    factBox.hidden = true;
    return;
  }

  const ply = currentPly();
  const title = ply ? `${ply.move_number}. ${ply.chinese}` : (state.free ? "自由局面" : "当前局面");
  const tag = report.is_best
    ? '<span class="tag best">实战就是正着</span>'
    : `<span class="tag ${ (report.loss_cp || 0) >= 150 ? "bad" : "alt" }">与正着相差 ${report.loss_cp ?? "?"} 分</span>`;
  header.innerHTML =
    `<div><span class="title">${title}</span>${ply ? tag : ""}</div>` +
    `<div class="hint">引擎正着：<b>${report.best_chinese || "—"}</b>` +
    ` ｜ 形势：${report.score_text || "—"} ｜ 深度 ${report.depth} ｜ 引擎 ${report.engine_name || "-"}</div>`;

  const playedIccs = ply ? ply.iccs : null;
  candidates.innerHTML = (report.candidates || []).map((cand) => {
    const cls = ["cand"];
    if (cand.rank === 1) cls.push("best-row");
    if (playedIccs && cand.iccs === playedIccs && cand.rank !== 1) cls.push("played-row");
    const scoreCls = (cand.score_cp || 0) < 0 ? "sc neg" : "sc";
    const pv = (cand.pv_chinese || []).join(" · ");
    return `<div class="${cls.join(" ")}"><span>${cand.rank}</span>` +
      `<span class="cn">${cand.chinese}</span>` +
      `<span class="${scoreCls}">${cand.score_text}</span>` +
      `<span class="pv">${pv}</span>` +
      `<span class="cand-act" data-iccs="${cand.iccs}">讲这路</span></div>`;
  }).join("");
  candidates.querySelectorAll(".cand-act").forEach((node) => {
    node.onclick = () => analyzeVariation(report.fen, node.dataset.iccs);
  });

  const exp = report.explanation;
  if (exp && exp.text) {
    explanation.innerHTML = mdToHtml(exp.text) +
      `<p class="hint">模型 ${exp.model}${exp.cached ? "（缓存命中）" : ""} ｜ ${exp.created_at}</p>`;
  } else if (exp && exp.error) {
    explanation.innerHTML = `<p class="error">讲解生成失败：${exp.error}</p><p class="hint">引擎结论仍然可用，可检查 API Key 或网络后重试。</p>`;
  } else if (report.error) {
    explanation.innerHTML = `<p class="error">${report.error}</p>`;
  } else {
    explanation.innerHTML =
      '<p class="hint">已完成引擎分析，但还没调用 DeepSeek。' +
      '点击「只讲 DeepSeek」或「引擎 + 讲解」即可生成人话讲解。</p>';
  }

  if (report.fact_sheet) {
    factBox.hidden = false;
    el("factText").textContent = report.fact_sheet;
  } else {
    factBox.hidden = true;
  }
  renderVariation(report.fen);
}

function renderVariation(fen) {
  const box = el("variationBox");
  if (!state.variation || !state.variation.fen || state.variation.fen !== fen) {
    box.hidden = true;
    box.innerHTML = "";
    return;
  }
  const data = state.variation;
  const explanation = data.explanation || {};
  box.hidden = false;
  box.innerHTML =
    `<div class="vtitle">变招讲解：${data.variation_chinese || data.variation_iccs}` +
    `（评分 ${data.variation_score || "—"}，引擎首选 ${data.best_chinese || "—"}）</div>` +
    (explanation.text
      ? mdToHtml(explanation.text)
      : `<p class="error">讲解生成失败：${explanation.error || "未知原因"}</p>`);
}

/* ------------------------------------------------------------------ 接口调用 */

async function api(path, options) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || data.error || `请求失败（${response.status}）`);
  return data;
}

async function loadConfig() {
  const config = await api("/api/config");
  state.config = config;
  const engine = config.engine || {};
  el("engineStatus").innerHTML = engine.name
    ? `引擎：${engine.name}（深度 ${engine.depth} / ${engine.threads} 线程）<br />模型：${config.model}${config.has_key ? "" : "（未配置 API Key）"}`
    : `引擎不可用：${engine.error || "未找到 Pikafish"}<br />请把引擎放到 cchess/Engine/ 或设置 QIMIND_ENGINE`;
  el("depth").value = engine.depth || 16;
  el("multipv").value = engine.multipv || 4;
  el("depthValue").textContent = el("depth").value;
  el("multipvValue").textContent = el("multipv").value;
  el("model").value = config.model || "deepseek-flash";
  el("scope").value = config.explain_scope || "all";
}

async function loadSamples() {
  const data = await api("/api/samples");
  const select = el("sampleSelect");
  data.samples.forEach((sample) => {
    const option = document.createElement("option");
    option.value = sample.path;
    option.dataset.kind = sample.kind;
    option.textContent = `${sample.name}（${sample.kind}）`;
    select.appendChild(option);
  });
}

async function loadFromPath(path, kind) {
  if ((kind || path.split(".").pop()) === "txt") {
    const data = await api(`/api/positions?path=${encodeURIComponent(path)}`);
    state.positions = data.positions;
    state.record = null;
    state.reports = {};
    renderMoves();
    if (state.positions.length) loadPosition(state.positions[0]);
    return;
  }
  const data = await api("/api/game/load", { method: "POST", body: JSON.stringify({ path }) });
  applyRecord(data.record);
}

function applyRecord(record) {
  state.record = record;
  state.reports = {};
  state.lastGameJobId = null;
  state.report = null;
  state.variation = null;
  state.positions = [];
  state.free = false;
  state.cursor = 0;
  state.selected = null;
  state.legalMoves = [];
  updateArrows();
  render();
  renderMoves();
  renderReport();
  el("reportBox").innerHTML = '<p class="hint">点「生成对局报告」，会给出开局判断、形势转折与失误统计。</p>';
  el("reportProgress").hidden = true;
}

function loadPosition(position) {
  state.free = true;
  state.freeFen = position.fen;
  state.freeReport = null;
  state.selected = null;
  state.legalMoves = [];
  state.arrow = { played: null, best: null };
  render();
  renderReport();
}

/* ------------------------------------------------------------------ 任务轮询 */

function stopPolling() {
  if (state.pollTimer) {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
}

function pollJob(jobId, onUpdate) {
  stopPolling();
  state.jobId = jobId;
  el("btnCancel").disabled = false;
  state.pollTimer = setInterval(async () => {
    try {
      const job = await api(`/api/jobs/${jobId}`);
      onUpdate(job);
      const done = ["done", "error", "cancelled"].includes(job.status);
      el("progressFill").style.width =
        `${job.total ? Math.round((job.done / job.total) * 100) : (done ? 100 : 12)}%`;
      el("progressText").textContent = `${job.message}${job.error ? " ｜ " + job.error : ""}`;
      if (done) {
        stopPolling();
        el("btnCancel").disabled = true;
        toggleAnalysisButtons(false);
      }
    } catch (err) {
      stopPolling();
      el("btnCancel").disabled = true;
      el("progressText").textContent = `任务轮询失败：${err.message}`;
    }
  }, 900);
}

function analyzeGame(mode) {
  if (!state.record || !state.record.plies.length) {
    alert("请先载入棋谱，或点击「用初始局面开始」后再补充走法。");
    return;
  }
  const payload = {
    path: state.record.source && state.record.source !== "<粘贴文本>" ? state.record.source : null,
    pgn: state.record.source === "<粘贴文本>" ? el("pgnInput").value : null,
    depth: parseInt(el("depth").value, 10),
    multipv: parseInt(el("multipv").value, 10),
    scope: el("scope").value,
    style: el("style").value,
    mode: mode || "both",
    explain: (mode || "both") !== "engine",
    use_cache: true,
  };
  if (!payload.path && !payload.pgn) {
    // 手动录入的局面：直接把走法列表发给后端
    payload.init_fen = state.record.init_fen;
    payload.moves = state.record.plies.map((ply) => ply.iccs);
  }

  el("progressWrap").hidden = false;
  el("progressFill").style.width = "2%";
  el("progressText").textContent =
    mode === "engine" ? "正在提交引擎分析任务…" : "正在提交任务…";
  toggleAnalysisButtons(true);

  api("/api/analyze/game", { method: "POST", body: JSON.stringify(payload) })
    .then((data) => {
      if (data.record && (!state.record || !state.record.plies.length)) applyRecord(data.record);
      state.lastGameJobId = data.job_id;
      state.report = null;
      el("reportBox").innerHTML =
        '<p class="hint">分析完成后点「生成对局报告」，查看开局判断、转折点与失误统计。</p>';
      pollJob(data.job_id, (job) => {
        const reports = (job.data && job.data.reports) || [];
        reports.forEach((report, index) => {
          state.reports[index + 1] = report;
        });
        if (reports.length !== Object.keys(state.reports).length ||
            reports.length !== state.record.plies.length) {
          renderMoves();
        }
        if (state.cursor > 0 && state.reports[state.cursor]) {
          updateArrows();
          render();
          renderReport();
        }
      });
    })
    .catch((err) => {
      toggleAnalysisButtons(false);
      el("progressText").textContent = `提交失败：${err.message}`;
    });
}

function toggleAnalysisButtons(busy) {
  ["btnAnalyzeGame", "btnAnalyzeGameEngine", "btnAnalyzeGameLlm"].forEach((id) => {
    el(id).disabled = busy;
  });
}

function analyzeHere(mode, regenerate) {
  const fen = currentFen();
  const ply = currentPly();
  mode = mode || "both";
  let payload;

  if (!state.free && ply) {
    const cached = state.reports[ply.index];
    if (cached && cached.explanation && cached.explanation.text && !regenerate && mode === "both") {
      state.freeReport = cached;
      renderReport();
      return;
    }
    payload = { fen: ply.fen_before, played: ply.iccs };
  } else {
    payload = { fen };
  }
  payload.depth = parseInt(el("depth").value, 10);
  payload.multipv = parseInt(el("multipv").value, 10);
  payload.style = el("style").value;
  payload.mode = mode;
  payload.explain = mode !== "engine";
  payload.regenerate = !!regenerate;

  const header = el("moveHeader");
  header.innerHTML = mode === "engine"
    ? '<span class="hint">正在跑引擎分析（不调用 DeepSeek）…</span>'
    : (mode === "llm"
      ? '<span class="hint">正在调用 DeepSeek 生成讲解…</span>'
      : '<span class="hint">正在分析当前局面…（引擎 → DeepSeek，通常几秒）</span>');
  el("explanation").innerHTML = mode === "engine"
    ? '<p class="hint">引擎计算中…</p>'
    : '<p class="hint">请稍候，正在思考。</p>';
  el("progressWrap").hidden = false;
  el("progressFill").style.width = "25%";
  el("progressText").textContent = mode === "llm" ? "调用 DeepSeek 中…" : "引擎分析中…";
  el("btnAnalyzeHere").disabled = true;
  el("btnEngineHere").disabled = true;
  el("btnEngineOnlyHere").disabled = true;
  el("btnLlmOnlyHere").disabled = true;
  el("btnReExplain").disabled = true;

  api("/api/analyze/position", { method: "POST", body: JSON.stringify(payload) })
    .then((data) => pollJob(data.job_id, (job) => {
      const report = job.data && job.data.report;
      if (report) {
        state.freeReport = report;
        const target = (!state.free && state.cursor > 0) ? state.cursor : null;
        if (target) state.reports[target] = report;
        updateArrows();
        render();
        renderReport();
        renderMoves();
      }
      if (["done", "error", "cancelled"].includes(job.status)) {
        ["btnAnalyzeHere", "btnEngineHere", "btnEngineOnlyHere", "btnLlmOnlyHere", "btnReExplain"]
          .forEach((id) => { el(id).disabled = false; });
      }
    }))
    .catch((err) => {
      ["btnAnalyzeHere", "btnEngineHere", "btnEngineOnlyHere", "btnLlmOnlyHere", "btnReExplain"]
        .forEach((id) => { el(id).disabled = false; });
      el("explanation").innerHTML = `<p class="error">${err.message}</p>`;
    });
}

/* ------------------------------------------------------------------ 棋盘交互 */

function eventToBoard(evt) {
  const rect = canvas.getBoundingClientRect();
  const scaleX = BOARD_W / rect.width;
  const scaleY = BOARD_H / rect.height;
  const lx = (evt.clientX - rect.left) * scaleX;
  const ly = (evt.clientY - rect.top) * scaleY;
  let bx = Math.round((lx - MARGIN) / CELL);
  let by = 9 - Math.round((ly - MARGIN) / CELL);
  if (state.flip) {
    bx = 8 - bx;
    by = 9 - by;
  }
  if (bx < 0 || bx > 8 || by < 0 || by > 9) return null;
  const cx = MARGIN + (state.flip ? 8 - bx : bx) * CELL;
  const cy = MARGIN + (9 - (state.flip ? 9 - by : by)) * CELL;
  if (Math.abs(lx - cx) > CELL * 0.62 || Math.abs(ly - cy) > CELL * 0.62) return null;
  return [bx, by];
}

async function handleBoardClick(evt) {
  const pos = eventToBoard(evt);
  if (!pos) return;
  if (state.mode === "practice") {
    await handlePracticeClick(pos);
    return;
  }
  if (!state.free) {
    // 棋谱模式：点击棋盘不改变局面
    return;
  }
  const fen = state.freeFen;
  const grid = fenBoard(fen);
  const side = fenSide(fen);
  const target = state.legalMoves.find((mv) => mv.to[0] === pos[0] && mv.to[1] === pos[1]);

  if (target) {
    try {
      const result = await api("/api/board/move", {
        method: "POST",
        body: JSON.stringify({ fen, iccs: target.iccs }),
      });
      state.freeFen = result.fen;
      state.freeReport = null;
      state.selected = null;
      state.legalMoves = [];
      state.arrow = { played: null, best: null };
      render();
      renderReport();
    } catch (err) {
      alert(err.message);
    }
    return;
  }

  const ch = grid[pos[1]][pos[0]];
  if (!ch) {
    state.selected = null;
    state.legalMoves = [];
    render();
    return;
  }
  const isRed = ch === ch.toUpperCase();
  if ((side === "red") !== isRed) {
    state.selected = null;
    state.legalMoves = [];
    render();
    return;
  }
  state.selected = pos;
  const data = await api("/api/board/moves", { method: "POST", body: JSON.stringify({ fen }) });
  state.legalMoves = (data.moves || []).filter((mv) => mv.from[0] === pos[0] && mv.from[1] === pos[1]);
  render();
}

/* ------------------------------------------------------------------ 事件绑定 */

/* ------------------------------------------------------------------ 人机对练 */

/* ------------------------------------------------------------------ 变招 / 报告 / 导出 */

/* ------------------------------------------------------------------ DeepSeek Key 设置 */

function renderKeyState(state) {
  const status = el("keyStatus");
  if (!state) return;
  if (state.has_key) {
    status.innerHTML =
      `已配置：<b>${state.masked}</b>` +
      (state.source_text ? ` ｜ 来源：${state.source_text}` : "") +
      ` ｜ 保存在 <code>${state.file || "qimind/data/secrets.json"}</code>`;
    status.className = "hint";
  } else {
    status.textContent = "尚未配置 API Key：只能跑引擎分析，无法生成人话讲解。";
    status.className = "error";
  }
  const engineStatus = el("engineStatus");
  if (engineStatus && state.has_key === false) {
    engineStatus.dataset.nokey = "1";
  }
}

async function loadKeyState() {
  const config = await api("/api/config");
  state.config = config;
  renderKeyState(config.deepseek);
  return config.deepseek;
}

async function saveKey() {
  const value = el("keyInput").value.trim();
  if (!value) {
    el("keyStatus").textContent = "请先粘贴 API Key；如果想清空已保存的 Key，点「清除」。";
    el("keyStatus").className = "error";
    return;
  }
  try {
    const data = await api("/api/secret", { method: "POST", body: JSON.stringify({ api_key: value }) });
    el("keyInput").value = "";
    renderKeyState(data);
    el("keyStatus").innerHTML += "<br />保存成功，现在可以点「测试连接」确认是否可用。";
  } catch (err) {
    el("keyStatus").textContent = `保存失败：${err.message}`;
    el("keyStatus").className = "error";
  }
}

async function testKey() {
  const value = el("keyInput").value.trim();
  el("keyStatus").textContent = "正在测试连接…";
  el("keyStatus").className = "hint";
  try {
    const data = await api("/api/secret/test", {
      method: "POST",
      body: JSON.stringify(value ? { api_key: value } : {}),
    });
    if (data.ok) {
      el("keyStatus").textContent =
        `连接成功，可用模型：${(data.models || []).join("、") || "（未列出）"}`;
      el("keyStatus").className = "hint";
    } else {
      el("keyStatus").textContent = `连接失败：${data.error}`;
      el("keyStatus").className = "error";
    }
  } catch (err) {
    el("keyStatus").textContent = `测试失败：${err.message}`;
    el("keyStatus").className = "error";
  }
}

async function clearKey() {
  try {
    const data = await api("/api/secret", { method: "POST", body: JSON.stringify({ api_key: "" }) });
    renderKeyState(data);
    el("keyStatus").textContent = "已清除本机保存的 API Key。";
  } catch (err) {
    el("keyStatus").textContent = `清除失败：${err.message}`;
  }
}

async function analyzeVariation(fen, iccs) {
  if (!fen || !iccs) return;
  const box = el("variationBox");
  box.hidden = false;
  box.innerHTML = '<p class="hint">正在分析这一步变招（引擎 + DeepSeek）…</p>';
  try {
    const payload = {
      fen,
      iccs,
      depth: parseInt(el("depth").value, 10),
      multipv: parseInt(el("multipv").value, 10),
      style: el("style").value,
    };
    const data = await api("/api/analyze/variation", { method: "POST", body: JSON.stringify(payload) });
    const result = await waitJob(data.job_id, (job) => {
      box.innerHTML = `<p class="hint">${job.message}</p>`;
    });
    state.variation = Object.assign({}, result.data.variation, { fen });
    renderVariation(fen);
  } catch (err) {
    box.innerHTML = `<p class="error">变招讲解失败：${err.message}</p>`;
  }
}

function waitJob(jobId, onUpdate) {
  return new Promise((resolve, reject) => {
    const timer = setInterval(async () => {
      try {
        const job = await api(`/api/jobs/${jobId}`);
        if (onUpdate) onUpdate(job);
        if (job.status === "done") {
          clearInterval(timer);
          resolve(job);
        } else if (job.status === "error" || job.status === "cancelled") {
          clearInterval(timer);
          reject(new Error(job.error || job.message || "任务失败"));
        }
      } catch (err) {
        clearInterval(timer);
        reject(err);
      }
    }, 900);
  });
}

async function generateGameReport() {
  if (!state.record || !state.record.plies.length) {
    el("reportBox").innerHTML = '<p class="hint">请先载入棋谱（或先对练一局并保存）。</p>';
    return;
  }
  const payload = {
    depth: parseInt(el("depth").value, 10),
    multipv: parseInt(el("multipv").value, 10),
    style: el("style").value,
    mode: el("chkReportLlm").checked ? "both" : "engine",
  };
  if (state.lastGameJobId) {
    payload.job_id = state.lastGameJobId;
  } else if (state.record.source && state.record.source !== "<粘贴文本>") {
    payload.path = state.record.source;
  } else {
    payload.init_fen = state.record.init_fen;
    payload.moves = state.record.plies.map((ply) => ply.iccs);
    payload.name = state.record.name;
  }

  el("reportProgress").hidden = false;
  el("reportFill").style.width = "20%";
  el("reportText").textContent = "正在生成对局报告…";
  el("reportBox").innerHTML = '<p class="hint">正在统计逐回合数据，稍候…</p>';
  el("btnReport").disabled = true;
  try {
    const data = await api("/api/report", { method: "POST", body: JSON.stringify(payload) });
    if (data.reports && state.lastGameJobId === null) {
      data.reports.forEach((report, index) => { state.reports[index + 1] = report; });
      renderMoves();
    }
    const job = await waitJob(data.job_id, (current) => {
      el("reportFill").style.width = `${current.total ? (current.done / current.total) * 100 : 40}%`;
      el("reportText").textContent = current.message;
    });
    state.report = job.data.report;
    renderGameReport();
    el("reportFill").style.width = "100%";
    el("reportText").textContent = "报告完成";
  } catch (err) {
    el("reportBox").innerHTML = `<p class="error">生成报告失败：${err.message}</p>`;
    el("reportText").textContent = `失败：${err.message}`;
  } finally {
    el("btnReport").disabled = false;
  }
}

function renderGameReport() {
  const box = el("reportBox");
  const report = state.report;
  if (!report) {
    box.innerHTML = '<p class="hint">还没有报告。</p>';
    return;
  }
  const opening = report.opening || {};
  const stats = report.stats || {};
  const red = stats.red || {};
  const black = stats.black || {};
  const turning = report.turning_points || [];
  const blunders = report.blunders || [];

  const statRow = (label, key, suffix) =>
    `<tr><th>${label}</th><td>${red[key] ?? 0}${suffix || ""}</td><td>${black[key] ?? 0}${suffix || ""}</td></tr>`;

  box.innerHTML =
    `<h3>开局</h3><p>${opening.name || "-"}${opening.detail ? "，" + opening.detail : ""}` +
    `（${opening.moves || ""}）</p>` +
    `<h3>双方数据</h3><table><tr><th>指标</th><th>红方</th><th>黑方</th></tr>` +
    statRow("总步数", "moves") +
    statRow("正着率", "best_rate", "%") +
    statRow("平均每步损失", "loss_avg") +
    statRow("最大一步损失", "loss_max") +
    statRow("疑问手（≥100）", "mistakes") +
    statRow("漏着（≥300）", "blunders") +
    `</table>` +
    `<h3>关键转折</h3>` +
    (turning.length
      ? turning.map((item) =>
          `<div class="rp-item">第 ${item.move_number} 回合 ${item.side_label} ${item.played}：` +
          `${item.is_best ? "正着" : `<span class="badge-loss">损失 ${item.loss} 分</span>`}` +
          `，引擎正着 ${item.best}，形势 ${item.score_text || ""}</div>`).join("")
      : '<p class="hint">形势平稳，没有明显反转。</p>') +
    `<h3>致命失误</h3>` +
    (blunders.length
      ? blunders.map((item) =>
          `<div class="rp-item">第 ${item.move_number} 回合 ${item.side_label} ${item.played}：` +
          `${item.reason}，应走 ${item.best}</div>`).join("")
      : '<p class="hint">没有损失超过 300 分的严重失误。</p>') +
    (report.narrative ? `<h3>讲评</h3>${mdToHtml(report.narrative)}` : "");
}

async function exportDoc(kind, format) {
  const report = state.report || {};
  const narrative = kind === "report" ? report.narrative || "" : (report.narrative || "");
  const payload = {
    kind,
    format,
    style: el("style").value,
    depth: parseInt(el("depth").value, 10),
    report: kind === "report" || report.fact_sheet ? report : null,
    narrative,
  };
  if (state.record) {
    payload.record = state.record;
    payload.reports = Object.keys(state.reports)
      .sort((a, b) => Number(a) - Number(b))
      .map((key) => state.reports[key]);
  } else if (state.lastGameJobId) {
    payload.job_id = state.lastGameJobId;
  }
  if (kind === "report" && !state.report) {
    alert("请先生成对局报告。");
    return;
  }
  if (kind === "walkthrough" && !payload.reports.length && !payload.job_id) {
    alert("还没有可导出的分析结果，请先分析整局。");
    return;
  }
  const status = kind === "report" ? el("reportText") : el("progressText");
  try {
    status.textContent = `正在导出 ${format.toUpperCase()}…`;
    const data = await api("/api/export", { method: "POST", body: JSON.stringify(payload) });
    status.textContent = `已保存：${data.path}`;
    const link = document.createElement("a");
    link.href = `/api/export/download?path=${encodeURIComponent(data.path)}`;
    link.download = data.filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
  } catch (err) {
    status.textContent = `导出失败：${err.message}`;
  }
}

function setMode(mode) {
  state.mode = mode;
  state.selected = null;
  state.legalMoves = [];
  el("btnModeStudy").classList.toggle("active", mode === "study");
  el("btnModePractice").classList.toggle("active", mode === "practice");
  [ "btnFirst", "btnPrev", "btnNext", "btnLast" ].forEach((id) => {
    el(id).disabled = mode === "practice";
  });
  updateArrows();
  render();
  renderMoves();
  renderReport();
}

async function loadPracticeLevels() {
  try {
    const data = await api("/api/practice/levels");
    state.practiceLevels = data.levels || [];
    const select = el("practiceLevel");
    select.innerHTML = state.practiceLevels
      .map((level) => `<option value="${level}"${level === data.default ? " selected" : ""}>${level}</option>`)
      .join("");
  } catch (err) {
    el("practiceStatus").textContent = `读取难度列表失败：${err.message}`;
  }
}

async function startPractice() {
  const status = el("practiceStatus");
  status.textContent = "正在开局（引擎思考中）…";
  try {
    const data = await api("/api/practice/new", {
      method: "POST",
      body: JSON.stringify({
        side: el("practiceSide").value,
        level: el("practiceLevel").value,
      }),
    });
    state.practice = data;
    state.selected = null;
    state.legalMoves = [];
    state.arrow = { played: null, best: null };
    setMode("practice");
    status.textContent = data.message || "对局开始。";
  } catch (err) {
    status.textContent = `开局失败：${err.message}`;
  }
}

async function sendPracticeMove(iccs) {
  const session = state.practice;
  state.selected = null;
  state.legalMoves = [];
  render();
  el("practiceStatus").textContent = "引擎思考中…";
  try {
    const data = await api("/api/practice/move", {
      method: "POST",
      body: JSON.stringify({ session_id: session.id, iccs }),
    });
    state.practice = data;
    state.arrow.played = data.played ? iccsToPos(data.played.iccs) : null;
    state.arrow.best = data.engine_move ? iccsToPos(data.engine_move.iccs) : null;
    render();
    renderMoves();
    renderReport();
    const reply = data.engine_move ? `，引擎应 ${data.engine_move.chinese}` : "";
    el("practiceStatus").textContent =
      `${data.result_text} ｜ 共 ${data.move_count} 步${reply}`;
  } catch (err) {
    el("practiceStatus").textContent = `走子失败：${err.message}`;
    render();
  }
}

async function handlePracticeClick(pos) {
  const session = state.practice;
  const status = el("practiceStatus");
  if (!session) {
    status.textContent = "请先点「开始对练」。";
    return;
  }
  if (session.finished) {
    status.textContent = "这局已经结束，点「开始对练」开新局，或点「保存并去讲解」复盘。";
    return;
  }
  if (!session.player_turn) {
    status.textContent = "轮到引擎走棋，请稍候…";
    return;
  }
  const target = state.legalMoves.find((mv) => mv.to[0] === pos[0] && mv.to[1] === pos[1]);
  if (target) {
    await sendPracticeMove(target.iccs);
    return;
  }
  const grid = fenBoard(session.fen);
  const ch = grid[pos[1]][pos[0]];
  if (!ch) {
    state.selected = null;
    state.legalMoves = [];
    render();
    return;
  }
  const isRed = ch === ch.toUpperCase();
  if ((session.player_side === "red") !== isRed) {
    state.selected = null;
    state.legalMoves = [];
    render();
    return;
  }
  state.selected = pos;
  const data = await api("/api/board/moves", { method: "POST", body: JSON.stringify({ fen: session.fen }) });
  state.legalMoves = (data.moves || []).filter((mv) => mv.from[0] === pos[0] && mv.from[1] === pos[1]);
  render();
}

async function practiceUndo() {
  if (!state.practice) return;
  try {
    const data = await api("/api/practice/undo", {
      method: "POST",
      body: JSON.stringify({ session_id: state.practice.id }),
    });
    state.practice = data;
    state.selected = null;
    state.legalMoves = [];
    render();
    renderMoves();
    el("practiceStatus").textContent = "已悔棋，回到你上一次该走棋的局面。";
  } catch (err) {
    el("practiceStatus").textContent = `悔棋失败：${err.message}`;
  }
}

async function practiceResign() {
  if (!state.practice) return;
  try {
    const data = await api("/api/practice/resign", {
      method: "POST",
      body: JSON.stringify({ session_id: state.practice.id }),
    });
    state.practice = data;
    render();
    renderMoves();
    el("practiceStatus").textContent = "你已认输。可以导出棋谱，或开新局。";
  } catch (err) {
    el("practiceStatus").textContent = `操作失败：${err.message}`;
  }
}

async function practiceExportPgn() {
  if (!state.practice) return;
  const id = state.practice.id;
  try {
    const response = await fetch(`/api/practice/pgn?session_id=${id}&download=1`);
    if (!response.ok) throw new Error(`导出失败（${response.status}）`);
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `qimind_practice_${id}.pgn`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    el("practiceStatus").textContent = "已导出 PGN 到下载目录。";
  } catch (err) {
    el("practiceStatus").textContent = err.message;
  }
}

async function practiceSave(thenStudy) {
  if (!state.practice) return "";
  try {
    const data = await api("/api/practice/save", {
      method: "POST",
      body: JSON.stringify({ session_id: state.practice.id }),
    });
    el("practiceStatus").textContent = `已保存到：${data.path}`;
    if (thenStudy && data.path) {
      await loadFromPath(data.path);
      setMode("study");
      el("progressText").textContent = "已载入对练棋谱，可以点「引擎 + 讲解整局」复盘。";
      el("progressWrap").hidden = false;
    }
    return data.path;
  } catch (err) {
    el("practiceStatus").textContent = `保存失败：${err.message}`;
    return "";
  }
}

function saveSettings() {
  api("/api/settings", {
    method: "POST",
    body: JSON.stringify({
      depth: parseInt(el("depth").value, 10),
      multipv: parseInt(el("multipv").value, 10),
      model: el("model").value,
      explain_scope: el("scope").value,
    }),
  }).catch(() => {});
}

function bind() {
  el("btnFirst").onclick = () => state.record && gotoPly(0);
  el("btnPrev").onclick = () => state.record && gotoPly(state.cursor - 1);
  el("btnNext").onclick = () => state.record && gotoPly(state.cursor + 1);
  el("btnLast").onclick = () => state.record && gotoPly(state.record.plies.length);
  el("btnFlip").onclick = () => { state.flip = !state.flip; render(); };
  el("chkFree").onchange = (evt) => {
    state.free = evt.target.checked;
    if (state.free) {
      state.freeFen = currentFen();
      state.freeReport = null;
    }
    state.selected = null;
    state.legalMoves = [];
    updateArrows();
    render();
    renderReport();
  };

  el("btnAnalyzeGame").onclick = () => analyzeGame("both");
  el("btnAnalyzeGameEngine").onclick = () => analyzeGame("engine");
  el("btnAnalyzeGameLlm").onclick = () => analyzeGame("llm");
  el("btnAnalyzeHere").onclick = () => analyzeHere("both", false);
  el("btnEngineHere").onclick = () => analyzeHere("engine", false);
  el("btnEngineOnlyHere").onclick = () => analyzeHere("engine", false);
  el("btnLlmOnlyHere").onclick = () => analyzeHere("llm", false);
  el("btnReExplain").onclick = () => analyzeHere("llm", true);

  el("btnModeStudy").onclick = () => setMode("study");
  el("btnModePractice").onclick = () => {
    if (!state.practice) {
      startPractice();
      return;
    }
    setMode("practice");
  };
  el("btnPracticeNew").onclick = startPractice;
  el("btnPracticeUndo").onclick = practiceUndo;
  el("btnPracticeResign").onclick = practiceResign;
  el("btnPracticeExport").onclick = practiceExportPgn;
  el("btnPracticeSave").onclick = () => practiceSave(false);
  el("btnPracticeStudy").onclick = () => practiceSave(true);

  el("btnExportMd").onclick = () => exportDoc("walkthrough", "md");
  el("btnExportDocx").onclick = () => exportDoc("walkthrough", "docx");
  el("btnReport").onclick = generateGameReport;
  el("btnReportMd").onclick = () => exportDoc("report", "md");
  el("btnReportDocx").onclick = () => exportDoc("report", "docx");
  el("btnSaveKey").onclick = saveKey;
  el("btnTestKey").onclick = testKey;
  el("btnClearKey").onclick = clearKey;
  el("btnCancel").onclick = async () => {
    if (!state.jobId) return;
    try { await api(`/api/jobs/${state.jobId}/cancel`, { method: "POST" }); } catch (_) {}
  };

  el("btnLoadSample").onclick = async () => {
    const select = el("sampleSelect");
    if (!select.value) return;
    const kind = select.options[select.selectedIndex].dataset.kind;
    try { await loadFromPath(select.value, kind); } catch (err) { alert(err.message); }
  };
  el("btnLoadPath").onclick = async () => {
    const path = el("pathInput").value.trim();
    if (!path) return;
    try { await loadFromPath(path); } catch (err) { alert(err.message); }
  };
  el("btnBrowse").onclick = async () => {
    const status = el("browseStatus");
    status.textContent = "已在系统窗口中打开文件选择框，请在其中选择棋谱…";
    try {
      const data = await api("/api/dialog/open", { method: "POST", body: "{}" });
      if (data.cancelled || !data.path) {
        status.textContent = "已取消选择。";
        return;
      }
      status.textContent = `已选择：${data.path}`;
      el("pathInput").value = data.path;
      await loadFromPath(data.path);
    } catch (err) {
      status.textContent = `系统弹窗不可用（${err.message}），改用浏览器选择文件…`;
      el("fileInput").click();
    }
  };
  el("fileInput").onchange = async (evt) => {
    const file = evt.target.files && evt.target.files[0];
    if (!file) return;
    const status = el("browseStatus");
    status.textContent = `正在上传并解析：${file.name}…`;
    try {
      const form = new FormData();
      form.append("file", file);
      const response = await fetch("/api/game/upload", { method: "POST", body: form });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || `上传失败（${response.status}）`);
      if (data.positions) {
        state.positions = data.positions;
        state.record = null;
        state.reports = {};
        renderMoves();
        if (state.positions.length) loadPosition(state.positions[0]);
      } else if (data.record) {
        applyRecord(data.record);
      }
      status.textContent = `已载入：${data.path}`;
      el("pathInput").value = data.path || "";
    } catch (err) {
      status.textContent = `载入失败：${err.message}`;
    } finally {
      evt.target.value = "";
    }
  };
  el("btnLoadPgn").onclick = async () => {
    const pgn = el("pgnInput").value.trim();
    if (!pgn) return;
    try {
      const data = await api("/api/game/load", { method: "POST", body: JSON.stringify({ pgn }) });
      applyRecord(data.record);
    } catch (err) { alert(err.message); }
  };
  el("btnLoadStart").onclick = () => {
    applyRecord({
      name: "初始局面",
      init_fen: START_FEN,
      source: "",
      headers: {},
      red_name: "红方",
      black_name: "黑方",
      plies: [],
    });
    state.free = true;
    state.freeFen = START_FEN;
    render();
  };

  el("depth").oninput = (evt) => { el("depthValue").textContent = evt.target.value; };
  el("multipv").oninput = (evt) => { el("multipvValue").textContent = evt.target.value; };
  ["depth", "multipv", "model", "scope"].forEach((id) => {
    el(id).onchange = saveSettings;
  });

  canvas.addEventListener("click", handleBoardClick);

  document.addEventListener("keydown", (evt) => {
    if (!state.record) return;
    if (evt.key === "ArrowLeft") gotoPly(state.cursor - 1);
    if (evt.key === "ArrowRight") gotoPly(state.cursor + 1);
  });
}

async function main() {
  setupCanvas();
  bind();
  render();
  try {
    await loadConfig();
    await loadSamples();
    await loadPracticeLevels();
    await loadKeyState();
  } catch (err) {
    el("engineStatus").textContent = `初始化失败：${err.message}`;
  }
}

main();
