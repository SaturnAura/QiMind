"""QiMind（棋思）—— 用中国象棋引擎 + 大模型把"正着"讲成人话。

Copyright (C) 2026 Saturn_Aura
SPDX-License-Identifier: GPL-3.0-or-later
本文件是 QiMind 的一部分，按 GNU GPLv3（或更新版本）发布。

QiMind 把三个部件串起来：

1. **棋谱**：读取 PGN / XQF / CBF / CBR 等记谱文件，或直接给定 FEN 局面；
2. **引擎**：调用 Pikafish（皮卡鱼）做多路（MultiPV）分析，得到评分、正着与后续主线；
3. **讲解**：把引擎结论整理成结构化的事实清单，交给 DeepSeek 生成通俗的中文讲解。

对外最常用的入口是 :func:`qimind.analyze.analyze_position` 与
:func:`qimind.analyze.annotate_game`，以及图形界面 ``python -m qimind gui``。
"""

from __future__ import annotations

from .config import PROJECT_ROOT, load_settings

__all__ = ["PROJECT_ROOT", "load_settings", "__version__"]

__version__ = "0.1.0"
