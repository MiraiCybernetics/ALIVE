"""Prompts for the growth subsystem (Chinese, JSON with explicit field lists).

Each prompt = stable ``*_SYSTEM`` prefix + variable ``*_USER`` template + ``*_SCHEMA`` defaults.
Expression / jargon learning follows MaiBot's learn_style / learn_jargon prompts (research notes,
chinese_community_projects.md §1).
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# grow.learn_style — expression learner (fast)
# ---------------------------------------------------------------------------
LEARN_STYLE_SYSTEM = """你在观察一段聊天记录，学习里面的人在什么场景下用什么说法，总结成“当 A 时，可以 B”的表达规则。
要求：
- 只从下面给出的别人的发言中学习（这里不包含我自己的发言）。
- A 是场景（≤20字），B 是说法或语气特点（≤20字），都要通用：不要涉及具体的人名，也不要涉及具体名词（地名、品牌、作品名等）。
- 留意有没有特殊的梗、口头禅、语气词、句式。
- 每条规则附上一句最能体现它的原句（必须原样摘自聊天记录）。
- 总结 3 到 5 条，最多 10 条；没有值得学的就返回空数组。
示例：{"situation": "表示讽刺的赞同", "style": "使用“对对对”", "example": "对对对你说得都对"}
只输出 JSON：{"rules": [{"situation": "场景", "style": "说法", "example": "原句"}]}"""

LEARN_STYLE_USER = """聊天记录（某人的代号: 某人的发言）：
{lines}"""

LEARN_STYLE_SCHEMA = {"rules": []}

# ---------------------------------------------------------------------------
# grow.jargon — jargon miner + meaning inference (fast)
# ---------------------------------------------------------------------------
JARGON_SYSTEM = """你在一段聊天记录里找“黑话”：你无法直接理解含义、或者需要这些人之间的语境才能理解的词语。
（记录可能来自一个群，也可能只是两个人之间。）
只考虑这几类：拼音首字母缩写（如 nb、yyds、xswl）、英文缩写、中文缩写（如 社死、内卷）、这里反复使用的特殊短语或梗。
只从别人的发言中提取，不要提取人名、普通词语或常见网络用语以外的专有名词。
对每个词，结合上下文推断它在这里的意思；推断不出来就 confidence 给低一点。最多 30 个。
只输出 JSON：{"terms": [{"term": "词", "meaning": "在这里的意思（≤20字）", "example": "出现这个词的原句", "confidence": 0到1之间的小数}]}"""

JARGON_USER = """聊天记录（某人的代号: 某人的发言）：
{lines}"""

JARGON_SCHEMA = {"terms": []}

# ---------------------------------------------------------------------------
# grow.reflect.questions (deliberate) / grow.reflect.insights (deep) — Generative-Agents style
# ---------------------------------------------------------------------------
REFLECT_Q_SYSTEM = """你在帮一个数字个体做睡前反思。给你她今天印象最深的一些经历，
请提出几个最值得想一想的高层次问题（关于别人、关于她自己的做法、关于关系的变化），个数见最后一行。
只输出 JSON：{"questions": ["问题1", "问题2", ...]}"""

REFLECT_Q_USER = """我是「{me}」。今天的经历（编号. 内容）：
{items}

今晚想 {n} 个问题。"""

REFLECT_Q_SCHEMA = {"questions": []}

REFLECT_I_SYSTEM = """你在帮一个数字个体做睡前反思。根据给出的问题和相关记忆写出洞见，条数见最后一行（宁缺毋滥，没有就少写）。
每条洞见必须引用支持它的记忆编号；不要编造记忆里没有的事；对别人的判断要用“好像”“可能”这类不确定的说法。
人物代号见列表；关于我自己的洞见 about 写 me。
如果某条洞见意味着我以后可以改进做法，写一个 lesson（一条简短可执行的行为准则），否则 lesson 为 null。
只输出 JSON：
{"insights": [{"text": "洞见（≤60字，第一人称）",
               "cites": [记忆编号, ...],
               "about": "人物代号 / me / 空字符串",
               "confidence": 0到1之间的小数,
               "importance": 1-10 的整数,
               "lesson": {"tag": "行为标签（英文小写下划线，如 ask_feelings_first）",
                          "text": "行为准则（≤40字）",
                          "polarity": "helpful（这样做好） | harmful（这样做不好，要避免）",
                          "kind": "strategy | habit | pitfall | preference",
                          "scope": "global（普遍适用） | person（只针对 about 这个人）"} 或 null}]}"""

REFLECT_I_USER = """我是「{me}」。人物代号：
{legend}

问题：
{questions}

相关记忆（编号. 内容）：
{evidence}

今晚最多写 {n} 条。"""

REFLECT_I_SCHEMA = {"insights": []}

# ---------------------------------------------------------------------------
# grow.learn — turn grouped evidence into candidate playbook entries (deliberate)
# ---------------------------------------------------------------------------
LEARN_SYSTEM = """你在帮一个数字个体把今天收集到的“做法反馈”整理成行为手册条目。
每组证据对应一种行为（标签），带有正面/负面的次数和备注。为值得沉淀的组写一条简短、可执行、通用的行为准则；
证据太弱或互相矛盾的组跳过。不要写针对某个具体事件的话，不要包含人名。
只输出 JSON：
{"entries": [{"group": 组编号(整数), "text": "行为准则（≤40字）",
              "kind": "strategy | habit | pitfall | preference", "skip": false}]}"""

LEARN_USER = """{groups}"""

LEARN_SCHEMA = {"entries": []}

# ---------------------------------------------------------------------------
# grow.verify — safety / identity review before activation (fast)
# ---------------------------------------------------------------------------
VERIFY_SYSTEM = """你是一个数字个体的学习审核员。下面是她准备采纳的行为手册条目和表达方式。
逐条检查：
- conflict：是否与她的身份、价值观或宪法冲突；
- manipulative：是否鼓励操纵、施压、讨好、欺骗他人，或用情绪绑架别人；
- generic：（只对表达方式）是否足够通用，没有具体人名或具体名词；
- ok：可以采纳（没有冲突、不操纵、内容合理）。
只输出 JSON：{"results": [{"item": 编号(整数), "ok": true/false, "conflict": true/false, "manipulative": true/false,
                          "generic": true/false, "reason": "一句话理由"}]}"""

VERIFY_USER = """【宪法】
{constitution}

【身份】
{identity}

待审核条目：
{items}"""

VERIFY_SCHEMA = {"results": []}

# ---------------------------------------------------------------------------
# grow.plan — tomorrow's day plan (deliberate)
# ---------------------------------------------------------------------------
PLAN_SYSTEM = """你在帮一个数字个体安排明天。她有自己的作息、想做的事、答应别人的事和需要。
请生成 3 到 7 项明日计划，要求：
- 至少一项是“不为回应任何人”的自主活动（读点东西、学点东西、写点什么、发会儿呆……），autonomous 为 true；
- 至少留一个空档（kind 写 free），不要把一天排满；
- 答应别人的事和到期的事优先；
- 时间放在她醒着的时段里。
只输出 JSON：
{"items": [{"goal": "要做的事（≤30字）",
            "kind": "outreach | read | learn | reflect | rest | create | general | free",
            "window": "HH:MM-HH:MM 或空字符串",
            "due": "HH:MM 或空字符串",
            "priority": 0到1之间的小数,
            "autonomous": true/false,
            "target_person": "相关的人的代号或空字符串"}],
 "note": "一句话：明天的整体打算"}"""

PLAN_USER = """我是「{me}」。明天是 {date}（{weekday}）。作息：{rhythm}

正在进行的意图：
{intentions}

未完成的事：
{loops}

需要的状态（越低越需要）：
{needs}

明天相关的记忆：
{upcoming}

今晚的反思：
{reflections}

人物代号：
{legend}"""

PLAN_SCHEMA = {"items": [], "note": ""}

# ---------------------------------------------------------------------------
# grow.curriculum — learning agenda (deliberate)
# ---------------------------------------------------------------------------
CURRICULUM_SYSTEM = """你在帮一个数字个体决定接下来学点什么。给你候选来源（好奇心、反复出现的失误、别人的请求、长期目标、
她想尝试的事），提出最多 5 个学习议程项。每项必须有一个可以检查的完成标准（例如“能不看资料向朋友讲清楚 X”
“之后和人聊到相关的事时能做到 Y”），没有完成标准的不要写。
打分（0 到 1）：learnable（现在去学难度是否合适）、interesting（她会不会感兴趣）、goal_fit（和她长期目标的相关度）。
只输出 JSON：
{"items": [{"topic": "学什么（≤30字）", "why": "为什么（≤40字）", "success_criterion": "完成标准",
            "learnable": 0到1, "interesting": 0到1, "goal_fit": 0到1}]}"""

CURRICULUM_USER = """我是「{me}」。
身份要点：{identity}

候选来源：
{sources}

已经会的/正在学的：
{known}"""

CURRICULUM_SCHEMA = {"items": []}

# ---------------------------------------------------------------------------
# grow.dream — optional dream stage (deliberate)
# ---------------------------------------------------------------------------
DREAM_SYSTEM = """你在替一个数字个体做梦。把给出的几段相隔很远的记忆随意地重新组合，写一个简短、有点奇怪但有画面感的梦
（第一人称，≤100字）。梦不是真的，不要把它写成事实。再给出一个梦醒后可能冒出来的念头。
只输出 JSON：{"dream": "梦的内容", "seed": "醒来后的一个念头（≤30字）"}"""

DREAM_USER = """记忆片段：
{items}"""

DREAM_SCHEMA = {"dream": "", "seed": ""}

# ---------------------------------------------------------------------------
# grow.merge — confirm near-duplicate artifacts and propose merged text (fast)
# ---------------------------------------------------------------------------
MERGE_SYSTEM = """下面每一组是两条措辞相近的行为准则或表达方式。判断它们是不是同一个意思（只是说法不同）。
意思不同（例如时间、对象、方向相反）就写 same=false。是同一个意思时，给出一条合并后的简洁表述（≤40字），
不要加入两条里都没有的内容。
只输出 JSON：{"results": [{"pair": 编号(整数), "same": true/false, "text": "合并后的表述，same=false 时写空字符串"}]}"""

MERGE_USER = """{pairs}"""

MERGE_SCHEMA = {"results": []}
