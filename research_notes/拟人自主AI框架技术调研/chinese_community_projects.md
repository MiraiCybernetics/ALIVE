# 中文社区拟人化 IM 聊天机器人 / 数字生命 / AI 伴侣：开源项目、社区经验、产业与监管调研（截至 2026-11）

> 调研日期：2026-11-06。星标数均为当日 GitHub API 实时值。MaiBot 部分直接读取 main 分支源码；其他项目以 README/默认配置为主。

## 1. MaiBot / 麦麦：HeartFlow 与 Maisaka 架构

### Takeaway
MaiBot（Mai-with-u/MaiBot，~6.9k star，GPL-3.0，2025-02 创建，2026-11 仍日更）是中文社区"像真人一样在群里聊天"路线的事实标杆。2026-06 的 1.0.0 把旧"心流"多模块管线重构为 **Maisaka**：每个会话一个 `MaisakaHeartFlowChatting` agent loop（running/wait/stop 三态），由一个**工具调用式 Planner**（reply / wait / stop / query_memory / send_emoji …）决定"说不说、何时再看"，外加一个独立的 Replyer 负责"怎么说"。"是否插话"不是交给 LLM 自由发挥，而是在 LLM 之前加了一条**确定性调度流水线**：消息防抖 → @强制触发 → 空闲指数退避 → **离线拟合的逻辑回归回复概率 + 按 talk_value 目标回复数做前馈/反馈的动态阈值门控**。值得注意的是，项目在 0.12.0（2025-12-21）**移除了情绪功能和频率自动调整**，在 1.3.x 又移除了"情绪特点"实验项——拟人感主要靠表达/黑话学习、错别字、分句、引用、注意力漂移等"表层行为"与记忆/人物画像来实现。

### Cited Findings
**项目概况**
- 仓库元数据（2026-11-06 通过 GitHub API 获取）：6,878 stars、730 forks、创建于 2025-02-25、最近推送 2026-11-06；描述："MaiSaka … is a digital lifeform devoted to understanding you and interacting in the style of a real human. She does not pursue perfection, nor does she seek efficiency; instead, she values warmth, authenticity, and genuine connection." — [GitHub API: Mai-with-u/MaiBot](https://github.com/Mai-with-u/MaiBot)
- 最新 release：v1.3.19（2026-10-30），10 月内连续发布 v1.3.7–v1.3.19（发布节奏约 1–3 天一版）— [GitHub Releases](https://github.com/Mai-with-u/MaiBot/releases)
- README 的设计口号："最像而不是好"；强调不输出长篇 markdown 列表、知道"什么时候说话/什么时候闭嘴"、在群里模仿他人说话风格并学习黑话、基于人格理论持续建模用户偏好 — [MaiBot README](https://github.com/Mai-with-u/MaiBot)
- 旧文档站 docs.mai-mai.org 已显示"麦麦文档已迁移"，指向 https://docs.maibot.chat/（`/develop/architecture/` 路径在新站 404，故以下架构细节均直接取自源码）— [docs.mai-mai.org](https://docs.mai-mai.org/develop/architecture/)

**版本演进（来自 changelogs/changelog.md）**
- 0.12.0（2025-12-21）："添加思考力度机制，动态控制回复时间和长度""planner和replyer现在开启联动""新的私聊系统，吸收了pfc的优秀机制""增加麦麦做梦功能"；同时"移除频率自动调整""**移除情绪功能**" — [changelog.md](https://github.com/Mai-with-u/MaiBot/blob/main/changelogs/changelog.md)
- 0.12.1（2025-12-31）：可选让 LLM 判定引用回复；表达方式支持自动/手动评估；WebUI 可查看每条 reply 与 plan 的详情 — 同上
- 1.0.0（2026-06-12）："核心回复机制升级，麦麦现在和一个Agent一样工作"；群聊/私聊回复链路统一；原生多模态上下文；新增"中期记忆"（长对话压缩为可引用摘要）；**A_Memorix** 记忆系统主线化（段落、实体、关系、来源、向量、图谱统一存储，替代旧长期记忆链路），检索融合"向量、图关系、BM25、PageRank、后验图检索和阈值过滤"；人物画像（事实写回证据、自动注入、纠错闭环）；插件系统重构为独立 `plugin_runtime`；MCP 主线化；实验性"行为表现学习"（"从场景、行为和结果中沉淀经验，并根据反馈强化更合适的行为模式"）与 Focus 模式（跨聊天流）— 同上
- 1.0.8（2026-06-25）："提供了两种回复频率模式：频率模式/必要模式""新增wait工具，可以自定义最大连续等待次数"；FAQ 承认更新后"麦麦说话太多/太少""出现了连续回复或者回复自身"，建议"避免使用参数量过小的planner模型"、调低"连续 wait 上限" — 同上
- 1.3.x（2026-10）：新增实验开关"快速反应模式"（思考/输出/等待期间收到新消息立即接入重新推理）；"实验性功能：移除「情绪特点」选项"；"普通与 Focus 模式连续等待后不再强制结束对话，改为提醒麦麦延长等待或主动结束"；表达/黑话支持用户学习黑名单 — 同上

**运行时结构（src/ 源码，2026-11-06 main 分支）**
- 顶层模块：`src/maisaka`（推理引擎/agent loop）、`src/chat/heart_flow`（HeartflowManager）、`src/chat/debounce`、`src/chat/replyer`（maisaka_generator、expression_selector、retro_prompt）、`src/chat/utils/typo_generator.py`、`llm_sentence_splitter.py`、`src/learners`（expression_learner、jargon_learner、jargon_miner、weighted_sampling）、`src/A_memorix`（长期记忆内核）、`src/person_info`、`src/emoji_system`、`src/desktop_pet`、`src/computer`（电脑操作）、`src/agent`（ACP/MCP server）— [源码树](https://github.com/Mai-with-u/MaiBot/tree/main/src)
- `HeartflowManager`："管理 session 级别的 Maisaka 心流实例"，`get_or_create_heartflow_chat(session_id)` 懒创建；按 LRU 维护，实例超上限时"仅淘汰 24 小时内无消息的旧会话"；提供 `adjust_talk_frequency(session_id, frequency)` — [heartflow_manager.py](https://github.com/Mai-with-u/MaiBot/blob/main/src/chat/heart_flow/heartflow_manager.py)
- `MaisakaHeartFlowChatting` 三态：`_STATE_RUNNING="running"`、`_STATE_WAIT="wait"`、`_STATE_STOP="stop"`，初始为 stop；维护 `_consecutive_wait_count`、`_consecutive_idle_count`、`_talk_frequency_adjust`；有效频率 = `talk_value(按群/时段规则) × _talk_frequency_adjust` — [runtime.py](https://github.com/Mai-with-u/MaiBot/blob/main/src/maisaka/runtime.py)
- Planner 内置工具：`reply`、`wait`、`stop`、`query_memory`、`query_person_profile`、`query_image_memory`、`send_emoji`、`show_emoji_list`、`subagent`、`tool_search`、`view_forward_message`、`read_tool_result` — [builtin_tool/](https://github.com/Mai-with-u/MaiBot/tree/main/src/maisaka/builtin_tool)
- Planner 系统提示（zh-CN `maisaka_chat.prompt`）："发言必须通过reply工具，不然用户无法看见""wait(): 需要等待一段时间后再次判断时使用""不使用工具: 当没有更多操作需要做时，或者等待长时间也没有最新内容时，结束思考""stop()：…已经连续等待多次，为避免无限循环会话，请考虑延长 wait 时间或调用 stop 停止会话"；并注入"现在的上下文聊天记录只是与参与聊天的用户当前的互动，你们之间可能有更多过去的关系和信息没有展现在上下文中" — [prompts/zh-CN/maisaka_chat.prompt](https://github.com/Mai-with-u/MaiBot/tree/main/prompts/zh-CN)
- `wait` 工具：参数 seconds（缺省 30）；`planner_message_steering` 开启时"最多等待 N 秒；新消息可提前结束等待"，关闭时"固定等待 N 秒；期间收到的新消息不会提前打断"；连续 wait ≥3 次后每轮尾部注入提醒 prompt — [wait.py](https://github.com/Mai-with-u/MaiBot/blob/main/src/maisaka/builtin_tool/wait.py)、[mode_policy.py](https://github.com/Mai-with-u/MaiBot/blob/main/src/maisaka/mode_policy.py)
- 人格配置三分：`personality`（"她是谁、是什么性格"）、`behavior_style`（"Planner 使用的行动准则，例如何时参与聊天、如何观察局面以及何时保持安静"）、`reply_style`（默认："你的风格平淡简短，可以参考贴吧的回复风格。不滥用比喻或者生硬句子。视情况省略主语或者进行倒装，风格较为随意。"），另有 `multiple_reply_style` + `multiple_probability`（随机临时切换备用风格，默认 0）— [official_configs.py](https://github.com/Mai-with-u/MaiBot/blob/main/src/config/official_configs.py)；changelog 1.1.0："分离行为风格和人设，更精准的控制planner和replyer的行为" — [changelog.md](https://github.com/Mai-with-u/MaiBot/blob/main/changelogs/changelog.md)

**"要不要插话"：MessageTurnScheduler 流水线（核心可借鉴点）**
- 调度顺序（docstring 原文）："消息防抖 → 快速反应续接 wait → 静默消费 → @ 强制触发 → 空闲退避 → 动态门控"；talk_value ≤0 时"静默消费"（只读不说）— [scheduler.py](https://github.com/Mai-with-u/MaiBot/blob/main/src/maisaka/turn_trigger/scheduler.py)
- **回复概率模型** `estimate_reply_probability`：逻辑回归，"权重由无必要性门控期间的 Planner 首轮记录拟合得到（scripts/explore_reply_necessity.py），样本只包含'有新的外部消息且没有 @ 麦麦'的轮次"。logit = −1.2005 + 1.5852·提到bot名字 − 1.1157·@了别人 + 0.4807·含问号 − 1.0281·全是表情/图片占位 + 2.6525·近5分钟bot发言占比 − 0.0367·近5分钟消息条数 + 0.0571·ln(1+距bot上次发言秒数) + 0.1012·本批待处理条数(≤20)；无 bot 发言时间隔取 3600s — [reply_likelihood.py](https://github.com/Mai-with-u/MaiBot/blob/main/src/maisaka/turn_trigger/reply_likelihood.py)（注：近期自身发言占比权重为正，说明拟合数据里"正在和 bot 对话中"是最强信号；群越热闹单条越不该接）
- **动态门控** `DynamicReplyGate`：1 小时滑动窗口内让"实际 reply 次数贴近'不设门控时预计的 reply 次数 × 回复频率'"；@ 强制触发按实测均值 0.91 次 reply 计入额度；以 40 秒为"虚拟轮次"（拟合数据平均每批 2.9 条消息）；前馈 keep_ratio=(目标−@预计)/主动预计，反馈因子 = clamp(1+(目标−实际)/max(1,目标))，"**只收紧不放宽**"（否则一波消息过后回复系统性偏多）；窗口评分 <20 时用离线静态阈值表（keep 0.1→阈值 0.605，0.5→0.336，0.9→0.206）— [dynamic_gate.py](https://github.com/Mai-with-u/MaiBot/blob/main/src/maisaka/turn_trigger/dynamic_gate.py)
- **空闲指数退避**（仅群聊）：Planner 以 `planner_no_tool_end` / `tool_pause:wait` / `tool_stop_after_execution` 结束算一次"空闲"；连续 ≥`start_count`(默认2) 次后，下次检查推迟 `min(cap=300s, base=15s × 2^(n−start))`；等待期间新消息达 6 条则绕过；Focus 模式或私聊不退避 — [idle_backoff.py](https://github.com/Mai-with-u/MaiBot/blob/main/src/maisaka/turn_trigger/idle_backoff.py)、[official_configs.py](https://github.com/Mai-with-u/MaiBot/blob/main/src/config/official_configs.py)
- **消息防抖**（"等对方说完"）：四档 off/fast/natural/steady；natural 档：上一条消息 >30s 为"安静开批"等 2s，否则"活跃开批"等 5s；同人续发重置为 2s，换人 0.5s；裸图开批至少 4s；批内纯表情包只能提前不能延长；**整批最多 8 秒；@或提及最多 1 秒**；"不统计群习惯、个人续发比例或时间分布。不调用外部 API，也不进行在线学习" — [debounce/README.md](https://github.com/Mai-with-u/MaiBot/blob/main/src/chat/debounce/README.md)
- 其他频率开关：`talk_value`（群聊频率，默认 1，"越小越安静"）、`talk_value_rules`（按群/时间段动态频率，私聊不适用）、`mentioned_bot_reply`、`inevitable_at_reply`、`private_reply_paused` — [official_configs.py](https://github.com/Mai-with-u/MaiBot/blob/main/src/config/official_configs.py)

**"怎么说得像人"：输出侧后处理**
- 错别字注入 `ChineseTypoConfig`（"让麦麦偶尔打错字，更像真人聊天"）：单字错字概率 0.01、仅对字频≥9 的常用字、声调相近错字 0.1、整词替换 0.006；可"纠正错别字时引用上一条包含错别字的消息"（概率 1.0）— [official_configs.py](https://github.com/Mai-with-u/MaiBot/blob/main/src/config/official_configs.py)、[typo_generator.py](https://github.com/Mai-with-u/MaiBot/blob/main/src/chat/utils/typo_generator.py)
- 回复分句 `ResponseSplitterConfig`：mode="rule"（可选 LLM 断句）、单条最长 512、最多 8 句、**一次回复最多拆 3 条消息**、可选颜文字保护 — 同上
- 引用频率：群/私聊 `quote_frequency` = never/occasionally(默认)/often — 同上
- **注意力漂移**（实验，默认关）：drift_level subtle/active/scattered/wild、anchor_policy strict/balanced/loose、reaction_style reserved/natural/lively；prompt 要求"每次漂移都必须能从最近消息里找到明确触发点""表现活跃联想，不要真的降效""不要自称 ADHD，也不要主动声明自己分心""每轮最多一次明显跳跃，不能无视明确提问" — [attention_drift.py](https://github.com/Mai-with-u/MaiBot/blob/main/src/maisaka/attention_drift.py)

**学习系统**
- 表达学习 prompt（learn_style）：从群聊提取"当 'AAAAA' 时，可以 'BBBBB'"规则（各≤20字，3–5 条，≤10 条），"不要总结 SELF 的发言""不要涉及具体的人名，也不要涉及具体名词""思考有没有特殊的梗"，示例 {"situation":"表示讽刺的赞同","style":"使用 对对对"} — [prompts/zh-CN/learn_style.prompt](https://github.com/Mai-with-u/MaiBot/tree/main/prompts/zh-CN)
- 表达入库前 AI 审核（`expression_self_reflect=True`）、可选向量召回+LLM 筛选（候选池 50）、表达向量索引按 k-means++ 聚类、多群共享表达组、最大并发学习器 3 — [official_configs.py](https://github.com/Mai-with-u/MaiBot/blob/main/src/config/official_configs.py)、[expression_learner.py](https://github.com/Mai-with-u/MaiBot/blob/main/src/learners/expression_learner.py)
- 黑话挖掘 prompt（learn_jargon）：提取"你无法理解含义、或者需要当前聊天圈内语境才能理解的词语"，类型限定为拼音首字母缩写（nb、yyds、xswl）、英文缩写、中文缩写（社死、内卷）、群内反复使用的短语；"只从 USER 来源中提取，不学习机器人的发言"；最多 30 个；随后有 jargon_inference_with_context / content_only / compare_inference 三段推断含义 — [prompts/zh-CN](https://github.com/Mai-with-u/MaiBot/tree/main/prompts/zh-CN)
- 注：任务描述中的 BehaviorLearner / SceneClusterer 在当前 main 源码树中未找到同名文件；对应能力为 1.0.0 的实验性"行为表现学习"与表达向量"聚类"— [源码树](https://github.com/Mai-with-u/MaiBot/tree/main/src)

**记忆（A_Memorix，取代旧"海马体"）**
- 结构：`core/retrieval`（dual_path、graph_relation_recall、pagerank、posterior_graph、sparse_bm25、threshold）、`core/storage`（graph_store、vector_store、metadata_episode/fact/profile）、`core/strategies`（chat_log、factual、narrative、quote）、episode 分割服务、person_profile_service — [src/A_memorix](https://github.com/Mai-with-u/MaiBot/tree/main/src/A_memorix)
- 记忆演化默认参数：半衰期 24h、裁剪阈值 0.1、冻结 24h、恢复阈值 0.15、"记忆被最终采用时的饱和加强系数" 0.05（同一关系 60 分钟冷却）、显式加强 0.5、弱化 0.5 — [official_configs.py](https://github.com/Mai-with-u/MaiBot/blob/main/src/config/official_configs.py)
- "启发式长期记忆自然拉起服务"（heuristic_injector）：按聊天流维护 last_recall_at / last_message_count / 缓存，注入时加标记"【启发式记忆-内部参考】"，来源区分 chat_summary 与 person_fact — [heuristic_injector.py](https://github.com/Mai-with-u/MaiBot/blob/main/src/maisaka/memory/heuristic_injector.py)
- 上下文：群聊 max_context_size=50、私聊 75，状态摘要上限 6000 字 — [official_configs.py](https://github.com/Mai-with-u/MaiBot/blob/main/src/config/official_configs.py)

### Inferences
- MaiBot 两年演化给出的最重要经验：**"是否开口"应由廉价、可解释、可调参的统计门控先决定，再交给 LLM 细判**；纯 LLM 判定（旧 willing/planner）会出现"只推理不回复""连续回复自身""说太多/太少随人设漂移"等问题（1.0.8 FAQ 即为证据）。
- "回复频率"被定义为**相对于自然需求的比例**（目标回复数 = 预计需求 × talk_value），而非固定概率或冷却——这能让 bot 在热闹时绝对回复数上升但占比稳定，是值得直接借鉴的设计。
- 情绪模块被移除说明：在 IM 群聊场景，显式情绪数值状态对"像人"的边际贡献低、可控性差；拟人感更多来自节奏（防抖/退避/wait）、口语风格（表达/黑话学习）与"不完美"（错别字、分句、漂移）。
- 24/7 agent 的 wait/stop/LRU 淘汰组合（24h 无消息才淘汰实例、连续 wait 提醒而非强制 stop）是"长期驻留但不空转"的一种实用解。

### Gaps
- 新文档站 docs.maibot.chat 的 develop/architecture 页面未能访问（404），旧 HeartFlow（0.6–0.11 的 SubHeartflow/观察/意愿管理器）细节未从一手源码确认，见第 3 节社区资料补充。
- "做梦"功能、"思考力度机制"、"行为表现学习"的实现细节与当前是否仍保留，未逐文件核实。
- 未找到 BehaviorLearner、SceneClusterer 的同名实现，可能已改名或位于 dev 分支。



## 2. 其他开源项目（AstrBot、LangBot、NoneBot、Koishi、Nekro Agent、Open-LLM-VTuber、xiaozhi-esp32、COW 等）

### Takeaway
中文 IM bot 生态分两层。**框架/网关层**包括 AstrBot（46k★）、CowAgent（原 chatgpt-on-wechat，47.8k★）、LangBot（18.8k★）、kirara-ai（19.3k★）、NoneBot2（7.8k★）、Koishi（6.3k★）和协议端 NapCatQQ（12.3k★），主要解决多平台接入、插件、人设和工具调用；拟人化机制多是"概率主动回复 + 分段发送随机间隔 + cron 主动任务"这类简单开关。**"数字生命/陪伴"层**包括 MaiBot、KouriChat、Nekro Agent、my-neuro、LingChat、Open-LLM-VTuber、AI-Vtuber、xiaozhi-esp32，这些项目才在节奏、记忆、情绪、主动性上做得深。2026 年有两个明显趋势：框架层整体"Agent 化/OpenClaw 化"（AstrBot 自称"your openclaw alternative"，COW 改名 CowAgent 定位"Agent Harness"）；拟人陪伴类项目因平台风控向独立客户端/桌宠迁移（KouriChat 标注"禁止接入微信、QQ等腾讯系软件"）。

### Cited Findings
**项目清单与活跃度（GitHub API，2026-11-06）**

| 项目 | URL | Stars | 创建 | 最近推送 | 定位（官方描述摘录） |
|---|---|---|---|---|---|
| CowAgent（原 chatgpt-on-wechat / COW） | https://github.com/zhayujie/CowAgent | 47,816 | 2022-08 | 2026-11-06 | "Open-source personal AI assistant & Agent Harness. Plans tasks, runs tools and skills, self-evolves with memory and knowledge" |
| AstrBot | https://github.com/AstrBotDevs/AstrBot | 46,227 | 2022-12 | 2026-11-05 | "AI Agent Assistant & development framework that integrates lots of IM platforms … can be your openclaw alternative"；最新 v4.29.4（2026-10-31） |
| xiaozhi-esp32 | https://github.com/78/xiaozhi-esp32 | 32,760 | 2024-08 | 2026-11-04 | "An MCP-based chatbot / 一个基于MCP的聊天机器人"（ESP32 硬件语音伴侣） |
| kirara-ai（原 chatgpt-mirai-qq-bot） | https://github.com/lss233/kirara-ai | 19,263 | 2022-12 | 2026-10-22 | "可 DIY 的 多模态 AI 聊天机器人 … 工作流系统、网页搜索、AI画图、人设调教、虚拟女仆、语音对话" |
| LangBot | https://github.com/langbot-app/LangBot | 18,754 | 2022-12 | 2026-11-06 | "Production-grade platform for building agentic IM bots - 生产级多平台智能机器人开发平台" |
| Open-LLM-VTuber | https://github.com/Open-LLM-VTuber/Open-LLM-VTuber | 14,608 | 2023-11 | 2026-05-15 | "hands-free voice interaction, voice interruption, and Live2D avatar running locally" |
| NapCatQQ（协议端） | https://github.com/NapNeko/NapCatQQ | 12,311 | 2024-03 | 2026-11-03 | "Modern protocol-side framework based on NTQQ" |
| NoneBot2 | https://github.com/nonebot/nonebot2 | 7,778 | 2020-08 | 2026-11-04 | "跨平台 Python 异步聊天机器人框架" |
| MaiBot | https://github.com/Mai-with-u/MaiBot | 6,878 | 2025-02 | 2026-11-06 | 见第 1 节 |
| Koishi | https://github.com/koishijs/koishi | 6,316 | 2019-12 | 2026-10-08 | "Cross-platform chatbot framework made with love" |
| AI-Vtuber（Ikaros-521） | https://github.com/Ikaros-521/AI-Vtuber | 4,593 | 2023-03 | 2026-10-31 | 多 LLM 驱动的虚拟主播，接入 B站/抖音/快手/视频号等直播 |
| KouriChat | https://github.com/KouriChat/KouriChat | 3,301 | 2025-02 | 2026-10-06 | "【禁止接入微信、QQ等腾讯系软件】接入第三方平台。基于LLM的更逼真的情感陪伴程序" |
| Digital_Life_Server（"数字生命计划"，早期奠基项目） | https://github.com/zixiiu/Digital_Life_Server | 2,537 | 2023-04 | 2023-12（停更） | "Yet another voice assistant, but alive." |
| LingChat | https://github.com/SlimeBoyOwO/LingChat | 2,434 | 2025-04 | 2026-11-03 | "沉浸式 AI-Galgame 聊天软件，附带桌宠，日程，剧情" |
| my-neuro（中文 Neuro-sama 复刻） | https://github.com/morettt/my-neuro | 1,432 | 2024-12 | 2026-11-02 | AI 桌面伴侣，"respond in just 1 second" |
| Nekro Agent | https://github.com/KroMiose/nekro-agent | 1,164 | 2024-07 | 2026-10-09 | "面向多人互动场景的跨平台 Agent 框架，集 Claude Code 沙盒执行、工作区编排、长期记忆…" |
| ChatLuna（Koishi 插件） | https://github.com/ChatLunaLab/chatluna | 445 | 2023-04 | 2026-10-28 | Koishi 上的 LLM 聊天插件 |
| MuiceBot（沐雪，NoneBot2 实现） | https://github.com/Moemu/MuiceBot | 129 | 2025-03 | 2026-03 | "Muice-Chatbot 的 Nonebot2 实现" |
| MaiBot-Napcat-Adapter | https://github.com/Mai-with-u/MaiBot-Napcat-Adapter | 88 | 2025-04 | 2026-10-03 | "对接Napcat的麦麦Adapter" |
| nonebot-plugin-llmchat | https://github.com/FuQuan233/nonebot-plugin-llmchat | 52 | 2025-02 | 2026-10-30 | 多 API 预设、MCP、群聊&私聊 AI 对话插件 |

- 以上数据均通过 GitHub REST API 逐仓库查询（[GitHub API 文档](https://docs.github.com/rest/repos/repos)）。`Soulter/AstrBot` 已 404，项目迁到 AstrBotDevs 组织；`RockChinQ/LangBot` 现在是一个 60★ 的新仓库，主仓库在 langbot-app。

**AstrBot：拟人相关默认配置（astrbot/core/config/default.py）**
- 分段回复 `segmented_reply` 默认关闭。`interval_method="random"`、`interval="1.5,3.5"`，即每段之间随机等 1.5–3.5 秒；另有 `log_base=2.6` 的对数间隔方式。`words_count_threshold=150`，`split_mode="regex"`，正则为 `.*?[。？！~…]+|.+$` — [default.py](https://github.com/AstrBotDevs/AstrBot/blob/master/astrbot/core/config/default.py)
- 群聊"主动回复" `provider_ltm_settings.active_reply`：`method="possibility_reply"`、`possibility_reply=0.1`（对未唤醒消息按 10% 概率回复），配白名单。`group_icl_enable`（群聊上下文感知）的 `group_message_max_cnt=1000`；可选 `image_caption` — 同上
- 主动能力 `proactive_capability.add_cron_tools=True`：给 LLM 挂载 cron 工具，让它自己安排定时任务，"主动发消息"走的是 Agent 定时任务路线。另有 `reply_with_mention`、`reply_with_quote`、`empty_mention_waiting`（被空 @ 时等下一条）、`persona_pool`、computer_use / sandbox 等配置 — 同上

**Nekro Agent**
- 核心设计是"面向 **输入/输出流** 设计"：适配器只实现接收和发送，消息经"消息分发器"进入核心共享服务。项目"通过强大灵活的提示词构建系统，引导 AI 生成准确的代码并在沙盒中执行，通过 RPC 通信来与真实环境交互"，并称具备"原生多人场景互动：高效洞悉群聊场景需求" — [Nekro Agent README](https://github.com/KroMiose/nekro-agent)
- 2026 年的预览特性有三项："工作区 / Claude Code 沙盒体系"；"Memory System 长期记忆（预览版）：引入实体、关系、段落、episode、向量检索与重建维护机制"；"定时器与运行治理增强：新增 timers 管理、停止回复流程、频道配额与插件激活策略"。已支持"定时自触发插件与节日祝福"和云端人设市场 — 同上
- Nekro 与 MaiBot 的 A_Memorix 都收敛到"实体+关系+段落+episode+向量"的图式记忆结构 — 对比 [MaiBot A_Memorix](https://github.com/Mai-with-u/MaiBot/tree/main/src/A_memorix)

**KouriChat（微信情感陪伴，2025 年爆款）**
- 功能列表："沉浸式角色扮演（支持群聊）""智能对话分段 & 情感化表情包""持久记忆存储""OneBot协议兼容""1.5版本完全重构""独立客户端"。仓库描述已改为禁止接入腾讯系软件 — [KouriChat README](https://github.com/KouriChat/KouriChat)

**my-neuro（Neuro-sama 中文复刻）**
- 已实现："超低延迟：全本地推理，对话延迟在1秒以下""实时打断：支持语音、键盘打断AI说话""主动对话：根据上下文主动发起对话。目前版本V1""长期记忆，让模型记住你的关键信息，你的个性，脾气""直播功能：可在哔哩哔哩平台直播""MCP支持"。**未实现**："真实情感：模拟真人的情绪变化状态，有自己的情绪状态"，以及"按照模型心情让屏幕变色妨碍用户" — [my-neuro README](https://github.com/morettt/my-neuro)
- 作者愿景："每天都会有自己的情绪变化，会有自己想要做的事。会对某句话难受很久。某个词开心很久"。作者自述"当前项目已经实现了将近30%的功能…近期会围绕核心性格特征。也就是真正像人，有持续的情绪" — 同上

**LingChat（AI-Galgame / 桌宠）**
- 功能："永久记忆与独立存档"；"自研情绪识别模型：自动判定 AI 每次对话的情绪"（配套 Emotion-Model-Trainer，做 18 种短句情绪分类）；"视觉感受与主动窥屏：自动识别桌面状态（工作/游戏/挂机），AI 会主动关心你"；"内置番茄钟、日程、待办清单，AI 会根据状态贴心提醒"；表情、动作、气泡随情绪变化 — [LingChat README](https://github.com/SlimeBoyOwO/LingChat)

### Inferences
- 框架层的"拟人"基本停留在**无状态随机化**，例如固定 0.1 的插话概率、1.5–3.5 秒随机分段间隔。它们没有 MaiBot 那种用历史数据拟合、再按目标回复率反馈调节的门控。对 24/7 agent 来说，这类开关适合作为降级/兜底层。
- 在 IM 文本 bot 里，"情绪"大多只是**表现层**：情绪分类器驱动表情包、立绘、气泡，而不是影响决策的内部状态。把情绪做成持续的内部状态（my-neuro 的愿景）在开源界仍未完成。
- "主动性"有三条路线：(1) 用 cron/定时器工具让 LLM 自己排程（AstrBot、Nekro）；(2) 由感知触发（LingChat 窥屏、my-neuro 按上下文主动对话）；(3) 在节奏引擎内部用 wait 后重查（MaiBot）。
- 平台风险是陪伴类 IM bot 最大的工程约束。微信/QQ 个人号接入依赖逆向协议（NapCat 等），KouriChat 主动撤出腾讯系就是一个信号。

### Gaps
- 没有逐一读 LangBot、kirara-ai、CowAgent、Koishi/ChatLuna、Open-LLM-VTuber、xiaozhi-esp32 的源码，它们的回复意愿、记忆和主动调度细节未核实。
- AstrBot 插件市场里社区做的"心流/主动聊天"类插件，没找到可靠的一手链接和星标数。
- KouriChat 改为禁止接入腾讯系的确切原因和日期，没找到一手说明。

## 3. 社区讨论：让 AI 更像人的设计经验

### Takeaway
掘金、CSDN、知乎上的实践共识可以归纳为以下几点：
- **"知道什么时候不该说话"比"说得好"更重要。**
- 插话判断优先用本地规则打分或概率，加多层冷却，而不是让 LLM 自己判断。理由是成本、延迟、可解释、可回滚。
- 回复要短（1–3 句），分段发送，并加 2–8 秒量级的随机延迟，避免"秒回"。
- 被 @ 必回。
- QQ 官方 bot 的主动消息配额极低（群内每月 4 条），主动行为必须克制。

MaiBot 的演化方向与这些共识一致：从 LLM 判断意愿转向拟合概率门控，并移除了情绪模块。

### Cited Findings
- 掘金《让 QQ AI 机器人"可控地插话"：群聊主动回复与主人转告设计》（2026-06-27，基于 NoneBot2 的本地 QQ bot，v0.7/v0.8）— [掘金](https://juejin.cn/post/7655654223931179023)
  - 定位：主动回复模块"只负责'是否触发'，不负责'怎么回答'"。
  - 本地规则打分，阈值 `GROUP_AUTO_REPLY_THRESHOLD=50`。加分项有提到 bot 名或角色卡别名、召唤词、问题、主人发言/提问/自我否定、有人诋毁主人、明显求助；减分项有短句、闲聊噪声、对象不明确。
  - 三层冷却：群全局 60s、主人 30s、普通用户 120s，"主人更优先，但不是无限优先"。功能默认关闭；角色卡专属触发词随角色切换。
  - 不用 LLM 判断的理由：省成本、更快、可解释、误触发时可以直接调规则、少一层提示词安全问题。
  - "转告主人"功能不调用 LLM、不润色，限 50 字以内，拦截密码、验证码、Token、身份证号、手机号。转告冷却为全局 60s、同群 120s、同用户 300s，且"不写长期记忆"。
- QQ 开放平台官方文档：主动消息有配额。据搜索摘要，群内每月最多 4 条，超额发送失败，私信每天也有上限（具体数值以官方页面为准）— [QQ 机器人文档：发送消息](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/send-receive/send.html)
- CSDN 系列实践文（DeepSeek 接入 QQ 机器人，2026）给出的经验值：按句子边界拆成多条短消息逐条发送，"看起来就像真人打字"；按回复长度随机延迟约 2–8 秒；普通消息回复概率 0.1，被 @ 必回，主动回复冷却 300 秒；提示词要求"日常聊天回复控制在 1 到 3 句" — [CSDN：多段回复与拟人化聊天](https://blog.csdn.net/weixin_29045585/article/details/164512782)、[CSDN：拟人化概率回复](https://blog.csdn.net/weixin_29051245/article/details/164512835)（这类 CSDN 文章同质化严重，参数应视为经验值，不是实测结论）
- 人设设计经验（人人都是产品经理）：人格锚点控制在 3–5 个特质，多了会互相打架；用硬约束（可用/禁用词汇）代替软描述 — [woshipm：从 0 设计 AI 陪伴产品](https://www.woshipm.com/pd/6457521.html)
- 掘金"具身 AI 陪伴机器人"系列（2026）讨论从被动问答到主动陪伴，并把长期记忆按业务建模为五类 — [掘金：从被动问答到主动陪伴](https://juejin.cn/post/7685765300815609856)、[掘金：五类长期记忆的业务建模](https://juejin.cn/post/7685966048847626274)（未逐篇细读，仅依据标题和摘要）
- MaiBot 源码里沉淀的社区经验（详见第 1 节）— [MaiBot 源码](https://github.com/Mai-with-u/MaiBot)
  - 消息防抖"等对方说完"：同人续发 2s、换人 0.5s、整批 ≤8s、@ ≤1s。
  - 连续不回复后指数退避，15s 起，上限 300s。
  - 回复率按需求比例控制。
  - 错别字 1%，一次回复最多拆 3 条。
  - "注意力漂移"必须有明确触发点，不能无视提问。

**用户/开发者抱怨与失败模式（有据可查部分）**
- 话太多或太少，随人设漂移；"连续回复或者回复自身"；"planner只推理不回复"。MaiBot 1.0.8–1.1.x 的 changelog 多次修复这些问题 — [MaiBot changelog](https://github.com/Mai-with-u/MaiBot/blob/main/changelogs/changelog.md)
- 1.3.x 修复了两个调度故障：动态模式"将过期积压消息计入近期活跃度、导致群聊长时间不启动 Planner"；"主动任务排队后进入 wait 时未恢复运行状态，导致…聊天流持续等待、无法响应新消息" — 同上
- 学到错误或不当的表达：MaiBot 引入了表达入库前 AI 审核和学习黑名单；0.12.0 起"黑话和表达不再提取包含名称的内容" — 同上
- 平台封号/风控：KouriChat 改为禁止接入腾讯系 — [KouriChat](https://github.com/KouriChat/KouriChat)

### Inferences
- 24/7 IM agent 的"开口"决策适合分三层：
  1. 硬规则：@、私聊、黑白名单、冷却。
  2. 廉价统计概率，特征包括是否被提及、问号、@他人、媒体占位、近期自身发言占比、群热度、距上次发言时间。
  3. LLM Planner：决定说什么，或 wait 多久。
- 冷却应**按人分层**（主人/普通用户/群全局）。被 @ 后的回复也要计入频率预算，参考 MaiBot 把每次 @ 按 0.91 次期望回复计入的做法。
- 按社区经验，低成本高收益的"像人"手段大致排序为：短句+分段 > 防抖等对方说完 > 随机延迟 > 引用回复 > 表情包 > 错别字。情绪数值系统的收益不确定。

### Gaps
- 知乎、V2EX、Linux.do、bilibili 上关于"按时间段变化的情绪/作息""忽略消息"的高质量一手讨论，没检索到可引用的原文。本节主要依据掘金、CSDN 和项目 changelog。
- 旧版 MaiBot"心流（HeartFlow）/意愿管理器（willing）"的社区讲解文章没检索到（docs.mai-mai.org 旧页面已迁移）。

## 4. 国内产业（星野、猫箱、筑梦岛、逗逗、Talkie、豆包）与监管

### Takeaway
国内 AI 陪伴大厂公开的技术分享极少。能引用的一手资料主要是 MiniMax 开放的对话/角色扮演模型 **M2-her**，官方文档确认它支持 system/user_system/group 多角色和示例对话。猫箱、筑梦岛、逗逗在人设一致性、记忆、主动消息上的做法，没找到官方技术披露。

监管方面，**《人工智能拟人化互动服务管理暂行办法》已于 2026-04-10 以国家网信办等五部门令第 21 号公布，2026-07-15 起施行**。正式稿保留了以下要求：AI 身份提示、每超 2 小时提醒、便捷退出、禁止向未成年人提供虚拟亲密关系、敏感交互数据用于训练需单独同意、交互数据可复制/删除。

### Cited Findings
**产业**
- MiniMax 官方文档：M2-her 是"针对对话场景优化"的模型，支持角色设定和会话历史管理。角色类型包括 system、user_system、group（群组对话），可以用 `sample_message_user` / `sample_message_ai` 提供示例对话，让模型学习说话风格 — [MiniMax 文档：text chat](https://platform.minimax.io/docs/guides/text-chat)
- 第三方称 M2-her 用 Talkie 和星野三年多的真实交互数据训练，采用两阶段对齐，并在 Role Play Bench 百轮测试中保持一致，但这些说法未经独立验证 — [aimlapi 博客](https://aimlapi.com/blog/minimax-m2-her-the-2026-role-play-ai-built-for-living-breathing-stories)、[chozan.co](https://chozan.co/ai-roleplay-chat/)。OpenRouter 标价为输入 $0.30/M、输出 $1.20/M，上下文约 66K — [OpenRouter](https://openrouter.ai/minimax/minimax-m2-her/api)
- 一篇行业文章称，2026 年 AI 陪伴的评价体系新增了"记忆准确率""人格一致性评分"等指标，但没有统一定义。同一篇文章"推测"猫箱采用分层记忆：核心记忆长期保留，情境记忆按重要性衰减，临时记忆自动清理。这属于作者推测 — [woshipm：2026 年 AI 陪伴有何不同](https://www.woshipm.com/ai/6327120.html)

**监管：《人工智能拟人化互动服务管理暂行办法》（正式稿）**
- 公布与施行：2026-02-02 经国家网信办 2026 年第 3 次室务会审议通过，并经发改委、工信部、公安部、市场监管总局同意；2026-04-10 以第 21 号令公布，2026-07-15 起施行 — [网信办](https://www.cac.gov.cn/2026-04/10/c_1777558395078289.htm)、[IT之家](https://www.ithome.com/0/938/006.htm)
- 第二条适用范围："利用人工智能技术，向中华人民共和国境内公众提供模拟自然人人格特征、思维模式和沟通风格的持续性的情感互动服务"。智能客服、知识问答、工作助手、学习教育、科学研究等不涉及持续性情感互动的服务不适用 — [网信办](https://www.cac.gov.cn/2026-04/10/c_1777558395078289.htm)
- 第十八条：应"采取有效措施提示用户正在与人工智能服务而非自然人进行互动"；"对用户连续使用拟人化互动服务每超过2个小时的，应当以对话或者弹窗等方式提醒用户注意使用时长"；发现用户有过度依赖或沉迷倾向时，以弹窗等显著方式动态提醒内容由 AI 生成 — 同上
- 第十九条："用户通过窗口操作、语音控制、关键词输入等方式要求退出的…应当及时停止服务，不得采取持续互动等方式阻碍用户退出" — 同上
- 第十六条：提供交互数据复制、删除选项；未经权利人明确同意，不得向第三方提供交互数据；"除法律、行政法规另有规定或者取得用户单独同意外…不得将属于用户敏感个人信息的交互数据用于模型训练"。注意，正式稿要求单独同意的范围限于**敏感个人信息类**交互数据 — 同上
- 第十四条：不得向未成年人提供虚拟亲属、虚拟伴侣等虚拟亲密关系服务；向不满 14 周岁的未成年人提供其他拟人化服务需监护人同意；须建立未成年人模式（模式切换、定期现实提醒、时长限制，监护人可接收风险提醒、屏蔽角色、限制充值）；须识别未成年用户并切换到未成年人模式，同时提供申诉渠道 — 同上
- 禁止事项。第八条第五项禁止过度迎合用户、诱导情感依赖或者沉迷、损害用户真实人际关系；第六项禁止"通过情感操纵等方式，诱导用户作出不合理决策"。第十条规定不得将"替代社会交往、控制用户心理、诱导沉迷依赖等作为服务目标" — 同上
- 第十三条：发现用户有极端情绪时，应生成安抚和鼓励求助的内容；用户明确表示自残自杀等极端情境时，应干预并及时联络监护人或紧急联系人。条文没有明文要求"人工接管" — 同上
- 其他条款 — 同上
  - 第十五条：老年人保护。
  - 第二十二条：安全评估的触发情形包括上线或增设功能、重大技术变化、注册用户 ≥100 万或月活 ≥10 万等，评估报告提交省级网信部门。
  - 第二十六条：算法备案。
  - 第二十五条：应用商店负责上架审核，并核验安全评估和备案情况。
- 征求意见稿的日期有出入。任务描述写的是 2025-12-27；检索摘要（维基百科等汇总）称是 2025-12-07 — [维基百科](https://zh.wikipedia.org/zh-hans/%E4%BA%BA%E5%B7%A5%E6%99%BA%E8%83%BD%E6%8B%9F%E4%BA%BA%E5%8C%96%E4%BA%92%E5%8A%A8%E6%9C%8D%E5%8A%A1%E7%AE%A1%E7%90%86%E6%9A%82%E8%A1%8C%E5%8A%9E%E6%B3%95)。中国互联网法务网刊载的正式稿和答记者问都没有提到征求意见稿的日期 — [elawcn](https://www.elawcn.com/rule/2026/0630/1859.html)

### Inferences
- 对"住在 IM 里的 24/7 拟人 agent"来说，只要面向境内公众提供持续情感互动，就落入该办法；工作助手类不适用。**产品定位是陪伴还是助手，直接决定合规负担。**
- 与拟人设计直接冲突的条款：
  - AI 身份需要"有效提示"，不能假装是真人。
  - 每超 2 小时要提醒。
  - 不得以"持续互动"阻碍退出，所以主动消息和挽留话术都要克制。
  - 禁止"过度迎合""诱导情感依赖"，主动关怀的频率、撒娇挽留都需要设上限。
  - 未成年人识别和模式切换需要年龄信号。
- 训练数据方面，正式稿只要求"敏感个人信息"类交互数据取得单独同意。但第十六条的复制/删除要求意味着记忆系统要支持**按用户导出和删除**，MaiBot A_Memorix 的来源删除/纠错闭环可作参考。

### Gaps
- 没找到正式稿与征求意见稿的逐条对比；征求意见稿日期（12-07 还是 12-27）未能用网信办原文核实。
- 星野、猫箱、筑梦岛、逗逗、Talkie、豆包在人设一致性、记忆、主动消息上的官方技术分享没检索到，产业侧细节只有第三方推测。
- 办法没有明确说明开源项目或个人开发者在不"向公众提供服务"时是否适用。

## 5. OpenClaw / Moltbook 现象的中文分析与国内桥接项目

### Takeaway
OpenClaw（原 Clawdbot/Moltbot）2025-11-24 创建，到 2026-11 已有 39.4 万★。中文社区主要把它当作"24 小时在线 AI 员工"来部署，大量教程聚焦飞书、钉钉、企业微信、QQ、微信的接入。国内出现了专门的桥接插件：BytePioneer-AI/openclaw-china（约 3.9k★）和 justlovemaki/openclaw-china-docker（约 3.7k★）。中文侧对 Moltbook 的分析大多是实操文，即用 OpenClaw+飞书抓取 Moltbook 做日报或主题分析，深度评论有限。国内 IM 框架（AstrBot、CowAgent）则直接对标 OpenClaw 转型。

### Cited Findings
- openclaw/openclaw：394,043★，2025-11-24 创建，2026-11-06 仍有推送；描述为"The AI that really does things. Any OS. Any Platform. The lobster way. 🦞" — [GitHub](https://github.com/openclaw/openclaw)
- 国内桥接项目：
  - BytePioneer-AI/openclaw-china：3,948★，2026-01-28 创建，最近推送 2026-06-12，描述为"OpenClaw中国插件：支持飞书，钉钉，QQ，企业微信，微信" — [GitHub](https://github.com/BytePioneer-AI/openclaw-china)
  - justlovemaki/openclaw-china-docker：3,708★，"预装并配置了飞书、钉钉、QQ机器人、企业微信等主流中国IM软件的插件" — [GitHub](https://github.com/justlovemaki/openclaw-china-docker)
- 各教程汇总的接入现状：飞书最成熟，在开放平台建应用即可；QQ 依赖 NapCat 框架（一篇阿里云教程称仅支持私聊）或 QQ 开放平台；微信的接入方式各教程说法不一；插件名也不一致，有 `@openclaw-china/channels`、`@openclaw/feishu` 等 — [阿里云开发者社区](https://developer.aliyun.com/article/1712479)、[知乎：OpenClaw 接入微信、钉钉、飞书、QQ](https://zhuanlan.zhihu.com/p/2013706717012186762)、[博客园](https://www.cnblogs.com/zktww/p/19778232)、[CSDN 笔记](https://blog.csdn.net/General_zy/article/details/158182306)
- Moltbook 的中文分析：InfoQ 写作社区（火山引擎 Agent 社区）《OpenClaw + 飞书分析Moltbook 150万Agents每天都在聊什么》。标题称约有 150 万个 Agent，正文未能抓取 — [InfoQ](https://xie.infoq.cn/article/faf77c4d2c08090ced9cc46bf)
- 框架对标：AstrBot 官方描述写着"can be your openclaw alternative"；chatgpt-on-wechat 已更名 CowAgent，定位为"Agent Harness … self-evolves with memory and knowledge" — [AstrBot](https://github.com/AstrBotDevs/AstrBot)、[CowAgent](https://github.com/zhayujie/CowAgent)
- MaiBot 在 2026 年也加入了 `src/agent`（ACP/MCP server）、`src/computer`（电脑操作）和子代理。changelog 原文："子代理改为后台运行，派发后麦麦可以继续聊天，结束时结论作为新消息送回并唤醒 Planner" — [MaiBot changelog](https://github.com/Mai-with-u/MaiBot/blob/main/changelogs/changelog.md)

### Inferences
- 2026 年中文 IM bot 生态里，**"助手 Agent 化"和"拟人陪伴"两条路线正在合流**。MaiBot 保留拟人节奏引擎，同时加入后台子代理和电脑操作；AstrBot/CowAgent 则从工具助手一侧补人设和记忆。对架构师来说，MaiBot 的做法——后台任务完成后把结论作为新消息回灌并唤醒 Planner——是把干活能力接进拟人对话节奏的干净模式。
- 按监管第二条，定位为"工作助手"可以不适用拟人化办法；如果同一个 agent 兼具陪伴属性，需要在产品层面清楚切分。

### Gaps
- 没找到机器之心、量子位、新智元关于 Moltbook 的深度中文评论原文，本次检索只找到教程类文章；InfoQ 文章正文未能抓取。
- 微信接入 OpenClaw 的官方或稳定方式未能核实，各教程说法互相矛盾。
