"""Prompts for the memory subsystem (Chinese, JSON output with explicit field lists).

Every prompt has a stable ``*_SYSTEM`` prefix (cache friendly) and a ``*_USER`` template for the
variable part. Each is paired with a ``*_SCHEMA`` dict of defaults passed to ``kernel.llm.json``.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# mem.extract — episodic segment + candidate facts (fast tier)
# ---------------------------------------------------------------------------
EXTRACT_SYSTEM = """你是一个数字个体的记忆整理器。你读一段她参与的聊天记录，替她写下“值得记住的东西”。
只输出一个 JSON 对象，不要任何解释。字段：
{
  "episodes": [            // 按话题切分的情节片段，通常 1 个，话题明显切换时可以 2-3 个
    {"summary": "第一人称、简洁地写发生了什么：和谁、聊了什么、结果如何、我当时的感受（≤80字）",
     "importance": 1-10 的整数（1=日常琐碎，5=有点意义，8=重要的事，10=刻骨铭心）,
     "peak": true/false（是否是情绪很强烈的时刻）,
     "entities": ["涉及的话题、物品、地点、组织（不含人名代号）"],
     "lines": [起始行号, 结束行号]}
  ],
  "facts": [               // 候选事实：关于某个人或世界的、以后可能用得上的陈述
    {"content": "一句完整的陈述，用名字而不是代词，例如“阿杰下周三有一场面试”",
     "kind": "semantic（客观事实/偏好/计划） | relational（某人和别人的关系、对我的态度） | self_claim（别人说的关于“我”的事）",
     "about": "这条事实说的是谁：参与者代号（如 p1），说的是我就写 me，不针对人写空字符串",
     "told_by": "谁说的：参与者代号或 me",
     "line": 出自第几行（整数）,
     "when": "原文里的时间说法（如“下周三”“明天下午3点”），没有就写空字符串",
     "entities": ["相关话题/物品/地点"],
     "importance": 1-10 的整数,
     "sensitive": true/false（是否涉及健康、政治、宗教、性取向等敏感类别）,
     "explicit_remember": true/false（对方是否明确要求我记住）}
  ]
}
规则：
- 只抽取聊天里真的说出来的内容，不要推测，不要编造。
- 时间保持原文说法写进 when，不要自己换算日期。
- 寒暄、表情、无信息量的闲聊不要抽成事实。
- 别人声称的关于我的事（例如“你上次答应过我……”）kind 写 self_claim，about 写 me。
- 不要把我（me）自己随口说的玩笑当成事实。
- 没有值得记的就返回空数组。"""

EXTRACT_USER = """我是「{me}」。参与者代号：
{legend}

此前的话题摘要：{prev}

聊天记录（行号. [时间] 代号 名字: 内容）：
{lines}"""

EXTRACT_SCHEMA = {"episodes": [], "facts": []}

# ---------------------------------------------------------------------------
# mem.reconcile — Mem0-style ADD / UPDATE / INVALIDATE / NOOP (fast tier)
# ---------------------------------------------------------------------------
RECONCILE_SYSTEM = """你负责把新得知的事实和已有记忆对账。对每条新事实，从它的候选旧记忆里判断应该怎么处理：
- ADD：新信息，与候选都不重复也不矛盾，新增。
- UPDATE：是对某条旧记忆的补充或细化（不矛盾），给出合并后的完整新表述。
- INVALIDATE：与某条旧记忆矛盾，说明旧的已经不再成立（例如换了工作、改了时间）。
- NOOP：旧记忆已经包含这条信息，什么都不用做。
只输出 JSON：
{"decisions": [{"fact": 新事实编号(整数), "action": "ADD|UPDATE|INVALIDATE|NOOP",
                "target": "被更新/失效/重复的旧记忆编号，如 r3；ADD 时写空字符串",
                "content": "UPDATE 时写合并后的完整表述，其他情况写空字符串",
                "reason": "一句话理由"}]}
每条新事实恰好给出一个决定。拿不准时选 ADD。"""

RECONCILE_USER = """现在时间：{now}

{items}"""

RECONCILE_SCHEMA = {"decisions": []}

# ---------------------------------------------------------------------------
# mem.deep — iterative retrieval planner (deliberate tier)
# ---------------------------------------------------------------------------
DEEP_SYSTEM = """你在帮一个数字个体“认真回想”一件事。给你她想回忆的问题和目前想起来的片段，判断是否已经够回答；
不够的话，提出新的检索词（换个说法、换个角度、按人、按时间）。只输出 JSON：
{"enough": true/false,
 "queries": ["新的检索短语，最多 3 个，每个不超过 15 字"],
 "time_hint": "如果问题跟某段时间有关，写原文时间说法如“上周”“去年夏天”，否则空字符串",
 "note": "一句话：还缺什么"}"""

DEEP_USER = """问题：{cue}

目前想起来的（编号. 内容）：
{hits}"""

DEEP_SCHEMA = {"enough": False, "queries": [], "time_hint": "", "note": ""}

# ---------------------------------------------------------------------------
# mem.recheck — sleep-time re-evaluation of stale records (deliberate tier)
# ---------------------------------------------------------------------------
RECHECK_SYSTEM = """你在睡眠整理中复查一些“可能过时”的记忆：它们是从别的记忆推导出来的，而那些依据已经变了。
对每条记忆判断：
- keep：依据虽然变了，但这条仍然成立。
- revise：需要改写，给出改写后的完整表述（基于新的情况）。
- invalid：已经不成立了。
只输出 JSON：
{"results": [{"item": 编号(整数), "verdict": "keep|revise|invalid", "content": "revise 时的新表述，否则空字符串",
              "reason": "一句话理由"}]}"""

RECHECK_USER = """{items}"""

RECHECK_SCHEMA = {"results": []}

# ---------------------------------------------------------------------------
# mem.conflict — resolve conflicting records (deliberate tier)
# ---------------------------------------------------------------------------
CONFLICT_SYSTEM = """下面每一组是两条互相矛盾的记忆，带着来源和可信度。判断该信哪一条：
- a 或 b：明显该信这一条（另一条视为不成立）。
- both：其实不矛盾，可以并存。
- unknown：现在无法判断，继续保留冲突标记。
可信度高的来源（例如本人亲口说的、我亲历的）通常优先；拿不准就选 unknown。只输出 JSON：
{"results": [{"pair": 编号(整数), "winner": "a|b|both|unknown", "reason": "一句话理由"}]}"""

CONFLICT_USER = """{items}"""

CONFLICT_SCHEMA = {"results": []}

# ---------------------------------------------------------------------------
# mem.compress — forgetting: compress weak low-importance episodes (fast tier)
# ---------------------------------------------------------------------------
COMPRESS_SYSTEM = """你在帮一个数字个体整理已经淡忘的日常片段：把同一天、同一个聊天里的几条琐碎经历压缩成一段简短的回忆。
要求：第一人称；保留谁、大概做了什么、关键结果；去掉细枝末节；不超过 80 字；不要编造没有的内容。
如果给了“已有的摘要”，新摘要必须保留已有摘要里的每一条要点，再并入新的内容。
只输出 JSON：{"summary": "压缩后的回忆"}"""

COMPRESS_USER = """{existing}要压缩的片段：
{items}"""

COMPRESS_SCHEMA = {"summary": ""}
