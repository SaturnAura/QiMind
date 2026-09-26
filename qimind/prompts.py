"""提示词构造：把事实清单变成一次「讲棋」请求。

提示词的三个原则：

1. **只给事实**：所有结论都要能追溯到事实清单里的数据；
2. **限制体裁**：固定小标题，便于界面排版，也避免大模型长篇大论；
3. **面向人**：术语要么避开，要么顺手解释一句。
"""

from __future__ import annotations

from typing import Dict, List, Optional

#: 提示词版本号，改动提示词时递增，用于让讲解缓存失效
PROMPT_VERSION = "2026-09-26.3"

SYSTEM_PROMPT = """你是中国象棋职业棋手兼讲解教练，擅长把引擎结论翻译成棋友听得懂的人话。

写作要求：
1. 只使用用户给出的「分析事实」，不得编造事实清单里没有的棋子、走法或威胁；
2. 讲清「这步棋在做什么」和「为什么这样走更好」，而不是复述走法；
3. 出现专业术语（顿挫、牵制、兑子、闪击、士角炮等）时，要用半句话解释；
4. 红方用「兵」，黑方用「卒」；坐标、评分等数字只在必要时引用；
5. 输出使用固定小标题的 Markdown：前三节简短，最后的「详细说明」展开讲；
6. 全文控制在 450 字以内，不要写成一大段，也不要在小节之间重复同一句话。"""

ANSWER_TEMPLATE = """请严格按照下面的格式输出，不要添加额外的标题或结尾客套：

### 一句话
（用一句话概括这步棋的作用，不超过 25 字）

### 为什么
- （第 1 条理由，最好点出它同时完成了哪两件事）
- （第 2 条理由，可以从子力、位置、威胁、王的安全里挑）
- （第 3 条理由，可选）

### 要注意
- （对方的合理应手，或走完之后自己还要小心的点）

### 详细说明
（3～5 句展开讲，依次说清四件事：这步棋的战术含义、对方最佳应对之后局面会怎样发展、
如果对方不理会这步棋会付出什么代价、以及一条可以迁移到其他对局的棋理。
可以引用引擎给出的后续主线，但要用自己的话串起来，不要逐条照抄数据。）"""

STYLES: Dict[str, str] = {
    "入门": "面向只懂基本规则的爱好者，少用术语，多用比喻，强调这步棋为什么重要。",
    "棋友": "面向常下棋的业余棋友，可以直接使用常见术语，重点讲思路与后续计划。",
    "进阶": "面向有打谱习惯的棋友，可以讲评分的意义、局面性质（优势/劣势/复杂）与转换思路。",
}

VARIATION_SYSTEM = """你是中国象棋讲解教练。现在要讲的不是引擎首选，而是一步「变招」。

写作要求：
1. 只使用给出的事实，不编造棋子、走法或威胁；
2. 说清三件事：这步变招想干什么、和引擎首选比差在哪里、走这条线之后双方的后续计划；
3. 语气中立：如果差别很小就明说「两种下法都成立，只是风格不同」；
4. 不超过 300 字，使用下面的固定小标题。"""

VARIATION_TEMPLATE = """请严格按照下面的格式输出：

### 变招评价
（一句话：这步棋的意图与可行性）

### 与首选的区别
（2～3 句：分数差距有多大、差别体现在哪里——子力、位置、先手还是风险）

### 后续计划
（2～3 句：走这条线之后双方大概会怎么下，适合什么风格的人使用）"""


REPORT_SYSTEM = """你是中国象棋教练，为学员写一份对局讲评稿。

写作要求：
1. 只依据给出的统计事实与关键节点，不编造具体着法；
2. 讲评要有教学价值：指出问题、说明原因、给出可执行的改进建议；
3. 语气客观、鼓励为主，不用「菜」「太差」这类评价；
4. 全文 500～800 字，使用下面的固定小标题。"""

REPORT_TEMPLATE = """请严格按照下面的格式输出：

### 开局
（3～4 句：开局类型、双方布局是否规范、开局结束时的形势）

### 中局转折
（3～4 句：形势在哪里发生变化，转折点那步棋的关键问题是什么）

### 失误复盘
（3～4 句：最严重的失误出现在哪里、当时应该怎么走、为什么）

### 双方对比
（2～3 句：结合正着率与平均损失，比较双方的稳定性）

### 改进建议
（2～3 条，每条一句话，给出可以马上练习的方向）"""


def build_messages(
    fact_sheet: str,
    *,
    played_move: Optional[str] = None,
    is_best: bool = True,
    style: str = "棋友",
) -> List[Dict[str, str]]:
    """构造一次讲解请求的 messages。

    参数:
        fact_sheet: :func:`qimind.facts.build_fact_sheet` 生成的文本。
        played_move: 实战走法的中文记谱（如 ``炮二平五``）。
        is_best: 实战走法是否就是引擎正着。
        style: 讲解深度，见 :data:`STYLES`。
    """
    style_text = STYLES.get(style, STYLES["棋友"])
    if played_move and not is_best:
        task = (
            f"本局实战走的是「{played_move}」，它并不是引擎推荐的正着。\n"
            "请先指出实战这步棋的问题（亏在哪里），再讲清引擎正着为什么更好、"
            "走正着之后是什么计划。"
        )
    elif played_move:
        task = (
            f"本局实战走的是「{played_move}」，与引擎正着完全一致。\n"
            "请讲清这步正着好在哪里：它同时解决了什么问题、给对手出了什么难题。"
        )
    else:
        task = "请讲清引擎正着好在哪里：它同时解决了什么问题、给对手出了什么难题。"

    user_content = (
        f"讲解对象：{style_text}\n\n"
        "下面是引擎分析得到的事实清单：\n---\n"
        f"{fact_sheet}\n---\n\n"
        f"{task}\n\n"
        f"{ANSWER_TEMPLATE}"
    )

    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


def build_variation_messages(
    fact_sheet: str,
    *,
    variation_move: str,
    best_move: str,
    style: str = "棋友",
) -> List[Dict[str, str]]:
    """构造一次「变招讲解」请求：解释某步候选着法的得失。"""
    style_text = STYLES.get(style, STYLES["棋友"])
    user_content = (
        f"讲解对象：{style_text}\n\n"
        "下面是这个局面的引擎分析事实：\n---\n"
        f"{fact_sheet}\n---\n\n"
        f"引擎首选是「{best_move}」，我要听的是变招「{variation_move}」的讲解：\n"
        "这步棋想干什么、与首选相比差在哪里、走这条线之后的后续计划是什么。\n\n"
        f"{VARIATION_TEMPLATE}"
    )
    return [
        {"role": "system", "content": VARIATION_SYSTEM},
        {"role": "user", "content": user_content},
    ]


def build_report_messages(
    report_facts: str,
    *,
    game_title: str,
    red_name: str,
    black_name: str,
    style: str = "棋友",
) -> List[Dict[str, str]]:
    """构造一次「对局报告」请求：把统计事实写成讲评稿。"""
    style_text = STYLES.get(style, STYLES["棋友"])
    user_content = (
        f"对局：{game_title}（红方 {red_name} vs 黑方 {black_name}）\n"
        f"读者水平：{style_text}\n\n"
        "下面是引擎逐回合分析后统计出的事实：\n---\n"
        f"{report_facts}\n---\n\n"
        "请写成一份对局讲评稿。\n\n"
        f"{REPORT_TEMPLATE}"
    )
    return [
        {"role": "system", "content": REPORT_SYSTEM},
        {"role": "user", "content": user_content},
    ]
