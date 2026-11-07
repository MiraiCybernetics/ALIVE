# 长期在线 LLM Agent 的记忆架构（截至 2026-11）

> 调研日期：2026-11-06。用途：为 24/7 自主"拟人" IM 聊天 Agent 设计记忆子系统。本文侧重机制（数据结构、算法、管线、参数、基准数字）和失败模式。
> 标注约定：【奠基】= 2023 及更早的奠基性工作；【厂商自报】= 厂商发布、未经第三方复现的数字；【二手】= 只见于搜索摘要或二手报道，未读到原文；【推断】= 本人推断或计算。
> 方法说明：主要读 arXiv 全文 HTML 和官方文档；OpenAI Dreaming 官方页抓取返回 403，相关内容只来自搜索摘要。

## Q1. 主流记忆架构（2024–2026）：记忆类型 / 写入路径 / 读取路径 / 巩固与遗忘 / 失败模式

### Takeaway
2025–2026 年各家逐渐收敛到一套共同模式：
- **保留原始 episode 作为不可变底账**
- **LLM 抽取原子事实或观察**，冲突时把旧记录标记失效而不删除，带双时间戳
- **混合检索**：向量 + BM25 + 图扩展 + 时间过滤，再用 RRF 和 cross-encoder 重排
- **在线轻写入，离线 "sleep/dreaming" 巩固**

形态上分三类：图派（Zep/Graphiti、Mem0g、Hindsight），分层 OS 派（MemGPT/Letta、MemoryOS、MemOS、LightMem），文件/文本派（OpenClaw Markdown、Letta MemFS、Mastra Observational Memory）。2026 年 LongMemEval 高分多来自"文本观察日志 + 强模型"或"多网络 + 多路检索"的方案。

### Cited Findings

#### 【奠基】Generative Agents（Park 等，arXiv 2304.03442，2023-04）
- **记忆流（memory stream）**：观察、计划、反思都以自然语言记录存入同一条流。
- **检索打分**：score = α_rec·recency + α_imp·importance + α_rel·relevance，三个 α 都取 1，三项各自 min-max 归一化到 [0,1] — [Generative Agents HTML](https://arxiv.org/html/2304.03442)
  - recency：按"距上次被检索以来的游戏小时数"指数衰减，衰减因子 0.995。
  - importance：由 LLM 在创建时一次性打 1–10 分（mundane→poignant；例：打扫房间 2 分，约心仪对象 8 分）。
  - relevance：query 与记忆 embedding 的 cosine。
- **反思**：最近事件的 importance 累计超过 150 时触发，约每天 2–3 次 — [Generative Agents HTML](https://arxiv.org/html/2304.03442)
  1. 取最近 100 条记录，让 LLM 提 3 个最显著的高层问题；
  2. 以这些问题为 query 检索相关记忆（含已有反思）；
  3. 生成 5 条洞见，每条注明引用的记录编号；
  4. 以指针形式存回记忆流，形成 **reflection tree**。
- **计划**：先根据人物摘要和前一天摘要生成 5–8 段粗略日程，再递归分解到小时级、5–15 分钟级动作，并存入记忆流；遇到需要反应的观察时，从反应时刻起重新生成计划 — [Generative Agents HTML](https://arxiv.org/html/2304.03442)
- **消融（TrueSkill μ）**：完整架构 29.89；去掉反思 26.88；只保留观察 25.64；人类众包 22.95；全部去掉 21.21。完整架构对全消融的 Cohen's d = 8.16 — [Generative Agents HTML](https://arxiv.org/html/2304.03442)

#### 【奠基】MemGPT（Packer 等，arXiv 2310.08560，2023-10-12，v2 2024-02-12）→ Letta
- **主上下文分三段**：只读的 system instructions；固定大小、只能通过函数调用改写的 working context；FIFO 消息队列（首条是被逐出消息的递归摘要）— [MemGPT HTML](https://arxiv.org/html/2310.08560v2)
- **外部上下文**：recall storage 保存全部消息（含被逐出的），可通过函数检索；archival storage 是可读写的任意长文本库 — [MemGPT HTML](https://arxiv.org/html/2310.08560v2)
- **内存压力机制**（论文中的示例值）：上下文达到 70% 时插入 memory-pressure 警告，让 LLM 自己保存要点；达到 100% 时 flush，逐出约 50% 上下文的消息，并由旧摘要 + 被逐出消息生成新的递归摘要。函数调用可带 `request_heartbeat=true` 请求连续推理，否则一直等到下一个外部事件 — [MemGPT HTML](https://arxiv.org/html/2310.08560v2)
- **DMR（accuracy）**：GPT-4 32.1% → 92.5%（+MemGPT）；GPT-4 Turbo 35.3% → 93.4%；GPT-3.5 38.7% → 66.9%。nested KV 任务中，GPT-4 基线到 3 层嵌套就降到 0%，MemGPT+GPT-4 不受嵌套层数影响 — [MemGPT HTML](https://arxiv.org/html/2310.08560v2)
- **Letta 2026 现状**：文档重点已转到 **Dreaming + MemFS** — [Letta docs](https://docs.letta.com/guides/agents/architectures/sleeptime)
  - MemFS 是 git 版本化的记忆文件系统，在 agent 的所有会话间共享。
  - 后台 subagent 回顾近期对话、巩固经验并更新记忆，在"完成一定数量的 agent steps"后或上下文压缩时触发；可选 "Agent reviews before applying"，由第二个后台会话复核修改。
  - 云端限额：Free 每个记忆仓库 5 MiB，Pro 50 MiB。
  - 拆大文件、合并重复、重组层级之前先备份仓库。

#### Mem0（Chhikara 等，arXiv 2504.19413，2025-04-28）
- **写入·抽取**：输入 P = (S, {m_{t−m}…m_{t−2}}, m_{t−1}, m_t) — [Mem0 HTML](https://arxiv.org/html/2504.19413v1)
  - S：从数据库取出的会话摘要，**异步刷新**，不增加延迟；
  - 最近窗口取 m = 10 条消息；
  - (m_{t−1}, m_t)：新的 user/assistant 消息对；
  - 由 LLM 抽取函数 φ 产出候选事实。
- **写入·更新**：每个候选事实用向量相似度取 top s = 10 条已有记忆，由 LLM 通过 function-calling 在 ADD / UPDATE（补充）/ DELETE（被否定）/ NOOP 中选一个；不用单独的分类器 — [Mem0 HTML](https://arxiv.org/html/2504.19413v1)
- **Mem0g（图变体）** — [Mem0 HTML](https://arxiv.org/html/2504.19413v1)
  - 实体抽取 + 关系三元组 (v_s, r, v_d)，例如 lives_in、prefers、happened_on；
  - 节点存类型、embedding、创建时间；相似度超过阈值 t 时复用已有节点；
  - **冲突时由 LLM 把旧关系标记 invalid，不物理删除**，以保留时间推理能力；
  - 检索分两路：实体中心子图扩展 + 整句 query 与三元组 embedding 匹配；
  - 实现用 Neo4j + GPT-4o-mini。
- **LoCoMo J 分（%）** — [Mem0 HTML](https://arxiv.org/html/2504.19413v1)
  - Overall：**Full-context 72.90** > Mem0g 68.44 > Mem0 66.88 > Zep 65.99 > best RAG (k=2, chunk 256) 60.97 > LangMem 58.10 > OpenAI 52.90 > A-Mem 48.38。
  - Temporal：Mem0g 58.13、Mem0 55.51、Zep 49.31、LangMem 23.43、OpenAI 21.71。
- **延迟与存储** — [Mem0 HTML](https://arxiv.org/html/2504.19413v1)
  - search p95：Mem0 0.200s，Mem0g 0.657s，Zep 0.778s，LangMem 59.82s；
  - total p95：Mem0 1.44s，full-context 17.12s；
  - 每段对话的记忆 token 占用：Mem0 约 7k，Mem0g 约 14k，Zep 超过 600k，原始对话约 26k。
- 后续：有二手报道称 Mem0 在 2026-04 发布了重写的记忆管线，LoCoMo 自报 92.5%【二手，未见原文】— [rywalker.com 汇总](https://rywalker.com/research/mem0)；Exabase 新闻稿引用 Mem0 的 LongMemEval 为 94.8%【厂商自报】— [HPCwire PR](https://www.hpcwire.com/off-the-wire/exabase-achieves-highest-reported-score-on-leading-ai-memory-benchmark-using-a-smaller-cheaper-model/)

#### Zep / Graphiti（Rasmussen 等，arXiv 2501.13956，2025-01-20）
- **三层子图** — [Zep HTML](https://arxiv.org/html/2501.13956v1)
  - Episode 子图：原始消息/文本/JSON，无损保存；
  - Semantic entity 子图：实体和事实边；
  - Community 子图：强连接实体簇及其高层摘要。
- **写入** — [Zep HTML](https://arxiv.org/html/2501.13956v1)
  - 实体抽取用当前消息 + 前 n = 4 条（两轮）；说话人总被抽为实体；有 reflexion 式自检步骤。
  - 实体名 embedding 为 1024 维，候选由 cosine + 全文检索召回，由 LLM 判定是否重复并合并名称和摘要。
  - 图更新用**预定义 Cypher**，不让 LLM 生成查询。
  - 事实边只在同一实体对之间去重，可表示 hyper-edge。
- **双时间模型** — [Zep HTML](https://arxiv.org/html/2501.13956v1)
  - 系统时间线：t'_created / t'_expired；事实时间线：t_valid / t_invalid。
  - LLM 把新边与语义相关的旧边比较；时间重叠的矛盾发生时，令旧边 t_invalid = 新边 t_valid。新信息优先，历史保留。
- **社区**：用 label propagation 而非 Leiden，便于增量扩展；新实体加入邻居中占多数的社区；增量结果会逐渐偏离全量结果，需要周期性全量刷新 — [Zep HTML](https://arxiv.org/html/2501.13956v1)
- **读取**：cosine、BM25（Lucene）、n 跳 BFS 三种搜索 + 五种 reranker：RRF、MMR、episode-mentions、node distance、cross-encoder（最准也最贵）— [Zep HTML](https://arxiv.org/html/2501.13956v1)
- **结果** — [Zep HTML](https://arxiv.org/html/2501.13956v1)
  - DMR：94.8% vs MemGPT 93.4%（gpt-4-turbo）。
  - LongMemEval_S（gpt-4o）：71.2% vs full-context 60.2%；上下文 115k → 1.6k tokens；延迟 28.9s → 2.58s。
  - 分题型（gpt-4o，full-context → Zep）：temporal 45.1 → 62.4；multi-session 44.3 → 57.9；preference 20.0 → 56.7。
  - **退化**：single-session-assistant 94.6 → 80.4；gpt-4o-mini 的 knowledge-update 76.9 → 74.4。

#### A-MEM（Xu 等，Rutgers，arXiv 2502.12110，v1 2025-02，v11 2025-10-08）
- **Zettelkasten 笔记**：m_i = {c 原文, t 时间, K 关键词, G 标签, X 上下文描述, e = Emb(c‖K‖G‖X), L 链接}；encoder 为 all-MiniLM-L6-v2 — [A-MEM HTML](https://arxiv.org/html/2502.12110)
- **建链与演化**：按 cosine 取 top-k = 10 近邻作候选，由 LLM 决定建哪些链接；**记忆演化**：对每个近邻 m_j 调一次 LLM，用新笔记改写它的 context/keywords/tags（m_j* 替换 m_j）— [A-MEM HTML](https://arxiv.org/html/2502.12110)
- **LoCoMo F1（GPT-4o-mini）**：Temporal 45.85，MemGPT 25.52；Multi-hop 27.02，MemGPT 26.65；**Adversarial 50.03，LoCoMo 基线 69.23**。每题约 2.5k tokens，MemGPT 约 16.9k；每次记忆操作低于 $0.0003 — [A-MEM HTML](https://arxiv.org/html/2502.12110)
- **复现差异**：Mem0 论文复跑的 A-Mem J 只有 48.38% — [Mem0 HTML](https://arxiv.org/html/2504.19413v1)
- **局限（原文自述）**：组织质量取决于底层 LLM；只支持文本 — [A-MEM HTML](https://arxiv.org/html/2502.12110)

#### HippoRAG 2（Gutiérrez 等，OSU，arXiv 2502.14802，2025-02-20，ICML 2025）
- 标题 *From RAG to Memory: Non-Parametric Continual Learning for LLMs*。在 HippoRAG 1 的 Personalized PageRank 上加入更深的 passage 整合和更有效的在线 LLM 使用；在事实、sense-making、associative 三类任务上都优于标准 RAG；associative 任务比 SOTA embedding 模型高约 7% — [arXiv 2502.14802](https://arxiv.org/abs/2502.14802)
- 在 MemoryAgentBench 中，HippoRAG-v2 是 Long-Range Understanding 上最好的 RAG 方法（36.2，长上下文模型约 66.2）— [MemoryAgentBench HTML](https://arxiv.org/html/2507.05257v4)

#### MemoryOS（Kang 等，BUPT，arXiv 2506.06326，2025-05-30）
- **三层存储** — [MemoryOS HTML](https://arxiv.org/html/2506.06326)
  - STM：长度 7 的对话页队列。
  - MTM：话题段；页与段的得分 = embedding cosine + 关键词 Jaccard，超过 θ = 0.6 即入段；每段最多 200 页，段有 LLM 摘要。
  - LPM：用户档案（静态属性 + User KB + **90 维 User Traits**）+ Agent 档案（固定人设 + 动态 Agent Traits）；User KB 和 Agent Traits 都是容量 100 的 FIFO。
- **热度**：Heat = N_visit + L_interaction + exp(−Δt/μ)，μ = 1e7 秒（约 116 天【推断：换算】）— [MemoryOS HTML](https://arxiv.org/html/2506.06326)
  - STM 满后按 FIFO 移入 MTM；
  - MTM 超容量时逐出最低热度段；
  - 热度超过 τ = 5 的段晋升 LPM，更新 Traits/KB 后把 L_interaction 清零。
- **检索**：MTM 先取 top-5 段，再取 top-k 页（LoCoMo k = 10）；LPM 的 KB 和 Traits 各取 top-10；Profile 始终注入 — [MemoryOS HTML](https://arxiv.org/html/2506.06326)
- **结果**：LoCoMo（GPT-4o-mini）对基线平均提升 F1 +49.11%、BLEU-1 +46.18%；平均 3,874 tokens、4.9 次调用；temporal F1 只有 20.02（弱项）。正文称 tokens 和调用数都优于基线，但其 Table 3 不完全支持（A-Mem* 2,712 tokens，MemGPT 4.3 次调用）— [MemoryOS HTML](https://arxiv.org/html/2506.06326)

#### LightMem（浙大 Ningyu Zhang 组，arXiv 2510.18866，v4 2026-02-28）
- **基于 Atkinson–Shiffrin 模型的三段式** — [LightMem HTML](https://arxiv.org/html/2510.18866)
  1. 感觉记忆：LLMLingua-2 预压缩，保留比例 r；512-token sensory buffer；注意力信号与相似度信号一致处才切话题边界。
  2. 短期记忆：按话题分组缓冲，达到 token 阈值 th 时由 LLM 把每组摘要成记忆条目。
  3. 长期记忆：在线只做 soft 直接插入；**离线 sleep-time update** 时为每条记忆建一个"相似且更新的条目"队列，各队列并行处理。
- **结果** — [LightMem HTML](https://arxiv.org/html/2510.18866)
  - 准确率最多提升 LongMemEval +7.7%、LoCoMo +29.3%；
  - 总 token 最多减少 38× / 20.9×；
  - 仅在线部分 token 减少 106×–117×，API 调用减少 159×–310×；
  - 运行时间最多快 12.4×；
  - 骨干模型：GPT-4o-mini、Qwen3-30B-A3B、GLM-4.6。

#### MemOS（MemTensor 等，arXiv 2507.03724，2025-07-04，v4 2025-12-03）
- 三类记忆：plaintext、activation-based（激活/KV）、parameter-level；基本单元 **MemCube** 封装内容和 provenance、versioning 等元数据，可组合、迁移、融合，支持不同记忆类型之间的转换 — [arXiv 2507.03724](https://arxiv.org/abs/2507.03724)

#### 参数化/架构级记忆：Memory³ 与 Memory Layers
- **Memory³**（Yang 等，arXiv 2407.01178，2024-07，Journal of Machine Learning 2024）：在隐式记忆（参数）和工作记忆（上下文 KV）之外引入第三种"显式记忆"。把知识库预编码为**稀疏 attention key-values**，推理时召回并注入 self-attention。2.4B 概念验证模型优于更大的 LLM 和 RAG，解码速度高于 RAG — [Memory³ HTML](https://arxiv.org/html/2407.01178v1)；[JML](https://global-sci.com/jml/article/view/13203)
- **Memory Layers at Scale**（Berges 等，Meta FAIR，arXiv 2412.09764，2024-12）：可训练的 key-value 查找层，增加参数但不增加 FLOPs；用 product-key 把大键空间拆成两个小键表的笛卡尔积；规模达数百万 KV 对，容量比以往 memory layer 高两个数量级；代码在 Lingua 的 product_key 模块 — [alphaXiv 2412.09764](https://alphaxiv.org/abs/2412.09764)

#### Hindsight（Vectorize.io + Virginia Tech 等，arXiv 2512.12818，2025-12-14，MIT 开源）
- **四个记忆网络** — [Hindsight HTML](https://arxiv.org/html/2512.12818v1)
  - World：客观事实；
  - Experience：agent 自身行为的第一人称记录；
  - Observation：由前两者综合出的、偏好中立的实体摘要，后台再生成；
  - **Opinion**：主观信念，存为 (text, confidence∈[0,1], timestamp)。
- **事实 schema**：text、embedding、**发生区间 (start, end)**、**提及时间**、事实类型、可选 confidence、元数据 — [Hindsight HTML](https://arxiv.org/html/2512.12818v1)
- **TEMPR 写入**：LLM 抽取少量叙事化事实，消解共指和相对日期并分网；实体解析综合字符串相似、共现和时间邻近。建四类边 — [Hindsight HTML](https://arxiv.org/html/2512.12818v1)
  - temporal：随时间间隔指数衰减；
  - semantic：cosine 超过阈值；
  - entity：共享实体；
  - causal：LLM 抽取的因果。
- **TEMPR 读取**：四路并行（向量、BM25、从语义种子出发的 spreading activation、时间范围过滤），RRF 融合，cross-encoder 重排，最后**按调用方给的 token 预算装包** — [Hindsight HTML](https://arxiv.org/html/2512.12818v1)
- **CARA 反思** — [Hindsight HTML](https://arxiv.org/html/2512.12818v1)
  - 行为档案含三个 1–5 分的 disposition（skepticism、literalism、empathy）和 bias-strength∈[0,1]，用文字写入 system prompt。
  - 新事实到来时，按实体重叠或 embedding 找到相关 opinion，由 LLM 判为 reinforce / weaken / contradict / neutral。
  - confidence 更新：reinforce/weaken 移动 ±α，contradict 移动 2α，neutral 不变，截断到 [0,1]。
- **结果** — [Hindsight HTML](https://arxiv.org/html/2512.12818v1)
  - LongMemEval_S：OSS-20B 83.6、OSS-120B 89.0、Gemini-3 作答 91.4（full-context GPT-4o 60.2）。
  - LoCoMo：Gemini-3 89.61、OSS-120B 85.67。
  - 20B 配置的设计目标是单块高端消费级 GPU。

#### Mastra Observational Memory（Tyler Barnes，2026-02-09，2026-02-17 更新）
- **机制** — [Mastra OM](https://mastra.ai/research/observational-memory)
  - Observer：未观察消息达到 token 阈值时把原文压成观察，并替换掉被覆盖的原消息；
  - Reflector：观察总量超过第二阈值时合并、反思，丢弃被取代的内容；
  - 只按 token 触发，文中没给具体阈值。
- **格式**：纯文本两级 bullet，按日期分节，emoji 标优先级（🔴 高、🟡 中、🟢 低）；**每条观察带三种日期**：观察日期、内容引用的日期、相对日期 — [Mastra OM](https://mastra.ai/research/observational-memory)
- **上下文布局**：上部是稳定、只追加的观察前缀（利于 prompt cache），下部是原始消息尾部。平均上下文约 30k；文本压缩 3–6×，工具调用负载 5–40×（anecdotal）— [Mastra OM](https://mastra.ai/research/observational-memory)

#### OpenClaw（Markdown 文件记忆 + 每日日志 + Dreaming）
- **文件布局**（工作区 `~/.openclaw/workspace`，"there is no hidden state"）— [OpenClaw Memory](https://docs.openclaw.ai/concepts/memory)
  - `USER.md`：偏好和档案，写成指令，会话开始时以单独的小预算加载；
  - `MEMORY.md`：持久的非档案事实与决策，会话开始时加载，经过策展；
  - `memory/YYYY-MM-DD.md`：每日笔记，**只建索引、不每轮注入**，裸 `/new` 或 `/reset` 时载入今天和昨天两份；
  - `DREAMS.md`：梦境日记；
  - `MEMORY.md` 超出 bootstrap 预算时只截断注入副本。
- **Pre-compaction memory flush**（默认开启）：压缩前跑一个静默回合，让 agent 保存要点；**只能追加到当天日志**，必须是新文本，拒绝重放和复制的段落；失败不阻塞压缩 — [OpenClaw Memory](https://docs.openclaw.ai/concepts/memory)
- **读取** — [OpenClaw Memory](https://docs.openclaw.ai/concepts/memory)
  - `memory_search`：向量 + 关键词混合，关键词用于 ID、代码符号这类精确词；可选检索会话 transcript；
  - `memory_get`：读指定文件或行区间；
  - `intent`：事件触发的 standing intents；
  - 自动回忆有 1 秒准备预算，超时则不带候选继续；
  - 存储默认 SQLite，可选 Honcho、LanceDB、PostgreSQL。
- **Dreaming** 见 Q2 — [OpenClaw Dreaming](https://docs.openclaw.ai/concepts/dreaming)

#### 产品级记忆（Anthropic / OpenAI）
- **Claude** — [Claude Help Center](https://support.claude.com/en/articles/11817273-use-claude-s-chat-search-and-memory-to-build-on-previous-context)
  - 当前形态：边聊边保存为"一组独立的 topics"，不在对话结束后做摘要；
  - 旧形态：对非项目聊天做摘要，**每 24 小时更新**；
  - 每个 project 有独立记忆；
  - 过往聊天搜索是 RAG，以 tool call 形式出现在对话中（付费档）；
  - 健康、政治、宗教等敏感话题默认不存；政府 ID、犯罪记录等永不存；
  - **删除对话不会删除由它生成的记忆条目**。
- Claude 自动记忆于 2025-10 向 Pro/Max 推出 — [MacRumors 2025-10-23](https://www.macrumors.com/2025/10/23/anthropic-automatic-memory-claude/)
- **ChatGPT** — [OpenAI Memory FAQ](https://help.openai.com/en/articles/8590148-memory-in-chatgpt-remembering-what-you-chat-about)
  - 两套机制：saved memories（用户要求记住的"notepad"，与聊天记录分开存储）和 reference chat history（从过往聊天中归纳的 insights）；
  - 关闭 chat history 后，所学内容在 30 天内删除；
  - Temporary Chat 既不读也不写记忆。
- **ChatGPT Dreaming（2026-06-04 公告）**：后台进程跨大量对话学习并综合记忆状态；记忆会随时间自动更新（例如"即将去旅行"在旅行结束后被改写）；提供记忆摘要页；先向美国 Plus/Pro 推出；为 Free 用户提供 dreaming 的算力需求降低约 5×【官方页 403，内容来自搜索摘要】— [OpenAI: Dreaming](https://openai.com/index/chatgpt-memory-dreaming/)
- **Dreaming 用户反馈**（单个用户意见）：无法 pin 记忆，无法阻止自动改写；官方回复可切回 legacy saved memories，pin 和修订历史尚无时间表 — [OpenAI Community](https://community.openai.com/t/dreaming-v3-sacrifices-real-personalization-for-compute-savings-a-power-user-s-perspective/1383010)

### Inferences
- 【推断】Generative Agents 的 recency 半衰期约 ln0.5/ln0.995 ≈ 138 游戏小时（约 5.8 天）；MemoryOS 的 μ = 1e7 s 对应 e 折时间约 116 天。两者差一个数量级，说明衰减常数没有共识，应按业务时间尺度调参或学习。
- 【推断】三家生产系统（Zep、Mem0g、ChatGPT Dreaming）都在做"时间敏感事实的自动失效"。对年级别的拟人 agent，**bi-temporal 事实 + 失效不删除 + 原始 episode 不可变**应是底座。
- 【推断】LLM 改写记忆是破坏性操作。A-MEM 改写邻居、Dreaming 重写 `MEMORY.md`、ChatGPT 自动修订都可能丢信息。OpenClaw（`maxPriorEntryLossFraction=0.25` + 原像备份）和 Letta（git MemFS）给出的护栏值得照搬，并应加上"pinned/不可改"的核心记忆层（呼应 ChatGPT 用户的 pin 诉求）。
- 【推断】检索层的收敛设计是向量 + BM25 + 图/实体扩展 + 时间过滤，再 RRF + cross-encoder，最后按 token 预算装包（Zep、Hindsight、OpenClaw 都是这一路）。

### Gaps
- **MemoryBank**（Zhong 等，arXiv 2305.10250，2023）的 Ebbinghaus 遗忘曲线公式本次没有抓原文核实。据我记忆是 R = e^{−t/S}，被回忆时 S 加 1、t 重置，需核实后再用。
- HippoRAG 2 的内部细节（phrase/passage 节点、synonym 边、recognition memory 过滤、PPR 阻尼）只读到摘要。
- MemOS 的 MemScheduler、生命周期状态和 LoCoMo 数字没有读到全文。
- Mem0 2026-04 重写版的管线细节和 92.5% LoCoMo 只见二手来源。
- Google Gemini 的个人化记忆、Anthropic API 的 memory tool / context editing、MIRIX（六类记忆）、Memory-R1 / Mem-α（RL 学记忆操作）本次都没有覆盖。
- Memory Layers at Scale 的具体 benchmark 增益数字没有拿到。

## Q2. 记忆巩固 / "睡眠" / 离线反思

### Takeaway
2025–2026 年"睡眠式离线巩固"已从研究走进主流产品：OpenClaw Dreaming（每日 3:00 cron）、Letta Dreaming（按步数或压缩触发）、ChatGPT Dreaming（2026-06）、LightMem sleep-time update、Mastra Reflector。最硬的定量证据来自 Letta 的 Sleep-time Compute 论文：test-time 算力约减 5×，准确率最多 +13% / +18%，多 query 分摊后单 query 成本降 2.5×，且 query 越可预测，收益越大。其余多为工程设计，缺少严格的消融。

### Cited Findings
- **Sleep-time Compute**（Lin, Snell, Wang, Packer, Wooders, Stoica, Gonzalez；arXiv 2504.13171；2025-04-17）— [arXiv 2504.13171](https://arxiv.org/abs/2504.13171)
  - 让模型在 query 到来前离线"思考"上下文，预测可能的问题并预计算有用的量；
  - Stateful GSM-Symbolic 和 Stateful AIME：同等准确率所需 test-time compute 约降 **5×**；
  - 加大 sleep-time compute，准确率最多 **+13%**（GSM）/ **+18%**（AIME）；
  - Multi-Query GSM-Symbolic：分摊到同一上下文的多个 query 后，平均每个 query 成本降 **2.5×**；
  - query 可预测性与 sleep-time 收益正相关；另有一个 agentic SWE 案例研究。
- **【奠基】Generative Agents reflection**：importance 累计超过 150 触发；3 个问题 → 检索 → 5 条带引用的洞见 → reflection tree。去掉反思，可信度 TrueSkill 从 29.89 降到 26.88 — [Generative Agents HTML](https://arxiv.org/html/2304.03442)
- **OpenClaw Dreaming**（默认开启；`dreaming.frequency` 默认 `0 3 * * *`）— [OpenClaw Dreaming](https://docs.openclaw.ai/concepts/dreaming)
  - 三相顺序 light → REM → deep。Light：整理、暂存近期短期材料；REM：反思主题和反复出现的想法；两者都不写 `MEMORY.md`。
  - **Deep 是唯一的持久写入**，打分权重：relevance 0.30、frequency 0.24、query diversity 0.15、recency 0.15、consolidation 0.10、conceptual richness 0.06；Light/REM 命中另加一个随时间衰减的小 boost。
  - 必须通过 minScore / minRecallCount / minUniqueQueries 三道门（文档未给默认值）。
  - 每个晋升片段最多 160 tokens；单次重写最多删除 25% 的旧条目；每条附最多 3 个概念标签和 1–10 的 importance。
  - 每次重写前，`MEMORY.md` 原像存入 SQLite，可回滚。
  - 只摄入交互式会话，cron、heartbeat、subagent 会话排除在外；不可信和系统派生的候选不得晋升。
  - Dream Diary（叙事日记）写入 `DREAMS.md`，**不作为晋升来源**。
- **LightMem sleep-time update**：在线 soft 插入，离线为每条记忆建"相似且更新的条目"队列并行合并；离线与在线合计 token 最多降 38× — [LightMem HTML](https://arxiv.org/html/2510.18866)
- **Letta Dreaming**：后台 subagent 回顾近期对话并更新 MemFS；按步数或压缩时触发；可选第二个后台会话复核修改（多耗 token，不找用户确认）— [Letta docs](https://docs.letta.com/guides/agents/architectures/sleeptime)
- **Mastra Reflector**：观察日志超过 token 阈值后合并相关条目、反思模式、丢弃被取代的内容；优先级 emoji 告诉 Reflector "当时什么重要" — [Mastra OM](https://mastra.ai/research/observational-memory)
- **Hindsight**：Observation（实体摘要）在后台再生成；CARA 反思形成带 confidence 的 opinion，新证据到来时按 ±α / 2α 更新 — [Hindsight HTML](https://arxiv.org/html/2512.12818v1)
- **Zep**：社区增量更新会偏离全量结果，需要周期性全量刷新，本质上也是一种离线巩固 — [Zep HTML](https://arxiv.org/html/2501.13956v1)
- **ChatGPT Dreaming（2026-06-04）**：后台综合记忆状态，时间敏感记忆自动修订；为 Free 用户提供 dreaming 的算力需求降低约 5×【来自搜索摘要】— [OpenAI](https://openai.com/index/chatgpt-memory-dreaming/)
- **Claude 旧版**：记忆摘要每 24 小时重新综合 — [Claude Help](https://support.claude.com/en/articles/11817273-use-claude-s-chat-search-and-memory-to-build-on-previous-context)

### Inferences
- 【推断】IM 拟人 agent 有天然的空闲期（夜间、对方离线），正好用于 sleep-time compute。按"query 可预测性"排序预计算，例如对方明天要考试、出差，事先准备好跟进话题和相关记忆。
- 【推断】OpenClaw 的设计值得直接借鉴：多相分离（前两相只暂存、最后一相才持久写入）、多信号加权晋升门槛、回滚原像、排除自言自语来源（cron/heartbeat/subagent），可以防止 agent 用自己生成的内容污染长期记忆（self-reinforcing confabulation）。
- 【推断】反思产物（洞见/opinion）应与事实分开存放，并带 confidence 和来源指针（Generative Agents 的 citation、Hindsight 的 Opinion 网络），避免推断被当作事实。

### Gaps
- 没有找到"做 vs 不做离线巩固"在长期对话基准（LoCoMo/LongMemEval）上的严格消融。OpenClaw、Letta、ChatGPT 的 Dreaming 都没有公开定量评估。
- 没有找到 replay、海马体式"梦境重放"对参数化记忆（持续微调）的有效实证。
- Sleep-time Compute 论文里 c → c' 的具体 prompt 设计只读到摘要层面。

## Q3. 基准与结果：LoCoMo / LongMemEval / MemoryAgentBench / LongMemEval-V2 等

### Takeaway
LoCoMo 已被多方质疑：full-context 72.9 高于 Mem0；Letta 用纯文件系统 + gpt-4o-mini 拿到 74.0；Zep 指出它缺少知识更新类题目；Mastra 拒绝报告 LoCoMo。LongMemEval_S 在 2026 年被刷到 89–96%（厂商自报，模型各异），**multi-session（约 87%）是共同天花板**。真正暴露短板的是 MemoryAgentBench：selective forgetting / 冲突更新多跳题所有方法不超过 28%，Mem0 只有 2%；长程理解上记忆 agent 只有 16–22，长上下文模型约 66。2026-05 的 LongMemEval-V2 把场景扩到 25M–115M tokens，揭示了准确率与延迟之间的强权衡。

### Cited Findings
- **LongMemEval_S 2026 排行（多为厂商自报，读题/判题模型各异）** — [Mastra OM](https://mastra.ai/research/observational-memory)
  | 系统 | 模型 | Overall |
  |---|---|---|
  | Mastra OM | gpt-5-mini | 94.87% |
  | Mastra OM | gemini-3-pro | 93.27% |
  | Hindsight | gemini-3-pro | 91.40% |
  | Mastra OM | gemini-3-flash | 89.20% |
  | Hindsight | GPT-OSS-120B | 89.00% |
  | EmergenceMem（internal，不可复现） | — | 86.0% |
  | Supermemory | gemini-3-pro | 85.2% |
  | Mastra OM | gpt-4o | 84.23% |
  | Oracle | gpt-4o | 82.4% |
  | Mastra RAG topK20 | gpt-4o | 80.05% |
  | Zep | gpt-4o | 71.2% |
  | full-context | gpt-4o | 60.2% |
- **OM 分题型（gpt-5-mini）**：KU 96.2 / MS 87.2 / SSA 94.6 / SSP 100 / SSU 95.7 / TR 95.5。multi-session 最难，87.2 与 Hindsight 持平。SSP 只有 30 题，翻 1 题即波动 3.3 分。Mastra 以评分和 judge prompt 不一致为由不发布 LoCoMo — [Mastra OM](https://mastra.ai/research/observational-memory)
- **Hindsight LongMemEval 分题型（OSS-120B）**：SSU 100、SSA 98.2、SSP 86.7、KU 92.3、TR 85.7、MS 81.2；同表 full-context GPT-4o 的 SSP 只有 20.0、TR 45.1 — [Hindsight HTML](https://arxiv.org/html/2512.12818v1)
- **Exabase M-1（2026-05）** 自称 LongMemEval top-50 检索下 96.4%，对比 Mem0 94.8%、Honcho 92.6%、HydraDB 90.79%、Supermemory 85.2%【厂商自报，新闻稿】— [HPCwire PR](https://www.hpcwire.com/off-the-wire/exabase-achieves-highest-reported-score-on-leading-ai-memory-benchmark-using-a-smaller-cheaper-model/)
- **冲突数据点**：有聚合站列出 Zep 63–71%、Mem0 49%，与上面 Mem0 94.8% 的说法冲突（版本或配置不同）【二手】— [StudioMeyer academy](https://academy.studiomeyer.io/concepts/longmemeval)
- **LoCoMo 争议** — [Letta blog](https://www.letta.com/blog/benchmarking-ai-agent-memory)；[Zep blog](https://blog.getzep.com/lies-damn-lies-statistics-is-mem0-really-sota-in-agent-memory/)
  - Mem0 论文中 full-context 72.90 高于所有记忆系统（Mem0g 68.44）— [Mem0 HTML](https://arxiv.org/html/2504.19413v1)
  - Letta（2025-08-12）把 LoCoMo 对话放进单个文件，配 `grep` / `search_files` / `open` / `close` 工具，gpt-4o-mini 拿到 **74.0%**，高于 Mem0 自报的 68.5%。Letta 认为 LoCoMo 测的是检索而非 agentic memory，模型因编码训练而擅长文件操作，迭代查询胜过单跳检索。
  - Zep 反驳 Mem0 的评测：如果全文直接喂给模型就更好，说明基准没有压到记忆能力；LoCoMo 缺少知识更新类题目。
- **Hindsight LoCoMo**：Gemini-3 89.61；文中引用的最强开源前作 Memobase 75.78；Backboard 自报 90.00（作者未复现）— [Hindsight HTML](https://arxiv.org/html/2512.12818v1)
- **MemoryAgentBench**（Hu, Wang, McAuley；arXiv 2507.05257；v1 2025-07-07，v4 2026-06-28）— [MemoryAgentBench HTML](https://arxiv.org/html/2507.05257v4)
  - 四项能力：Accurate Retrieval、Test-Time Learning、Long-Range Understanding、Selective Forgetting。
  - **Selective Forgetting**（FactConsolidation，基于 MQuAKE 反事实编辑，6K–262K tokens），单跳 / 多跳准确率：
    | 系统 | 单跳 | 多跳 |
    |---|---|---|
    | GPT-5-mini（长上下文） | 78 | 28 |
    | GPT-4o | 60 | 5 |
    | HippoRAG-v2 | 54 | 5 |
    | BM25 | 48 | 3 |
    | Cognee | 28 | 3 |
    | MemGPT | 28 | 3 |
    | **Mem0** | **18** | **2** |
  - Long-Range Understanding：GPT-5-mini 66.2，HippoRAG-v2 36.2，MemGPT / Mem0 / Cognee 约 16–22。
  - 小 chunk、多 chunk 有利于 AR 但损害 LRU。o4-mini 在 6K 多跳上 80.0，到 32K 只剩 14.0。
  - agentic memory 对骨干模型最敏感：MIRIX 换成 GPT-4.1-mini 后平均分 +9.7。
- **LongMemEval-V2**（Di Wu 等，UCLA，arXiv 2605.12493，约 2026-05）— [LongMemEval-V2](https://xiaowu0162.github.io/longmemeval-v2/)
  - 451 题，多模态 web-agent 轨迹，haystack 100–498 个会话、约 25M–115M tokens。
  - 五种能力：static state recall、dynamic state tracking、workflow knowledge、environment gotchas、premise awareness。
  - 接口为 Insert / Query；按准确率–延迟前沿上的 LAFS Gain 排名。
  - 结果：无检索 1.3%；RAG 42.8% / 0.1s；RAG + notes 51.0%；AgentRunbook-R 58.6% / 26.9s；Codex 69.9% / 177s；AgentRunbook-C 74.9% / 108s。
- **其他 2026 自报数字【二手】**：TiMem LongMemEval-S 76.88%（GPT-4o-mini）；Memanto 89.8%；Redis 组合策略 86.5% 任务平均 — [emergentmind LongMemEval topic](https://api.emergentmind.com/topics/longmemeval-benchmark)；[Redis report](https://redis.io/resources/agent-memory-benchmark-report/)
- **情感陪伴类基准**：LifeSide（arXiv 2606.04660，2026-06）以记忆–情绪–环境循环评测终身数字陪伴，模型跨会话追踪平均约 52%【搜索摘要】— [arXiv 2606.04660](https://arxiv.org/html/2606.04660)

### Inferences
- 【推断】LongMemEval_S 约 115k tokens 的规模已不足以区分方案，强模型 + 好压缩就能到 90%+。对"数月到数年"的 IM agent，应以 **MemoryAgentBench 的 Selective Forgetting（多跳冲突更新）** 和 **LongMemEval-V2 的 25M+ 规模与延迟约束** 作为回归测试的主轴，再自建"按真实时间线回放数月聊天"的私有评测。
- 【推断】temporal、knowledge-update、multi-hop 冲突是 Mem0 / Zep / MemoryOS 这类系统共同的弱项。2026 年高分方案（OM、Hindsight）都在写入时显式规范化日期（三日期 / 发生区间 + 提及时间），这是提升时间推理性价比最高的手段。
- 【推断】Letta 文件系统实验和 MemoryAgentBench "骨干决定 agentic memory" 两条结果指向同一结论：给强模型简单、熟悉的工具（grep/文件）+ 迭代检索，可能比复杂的记忆中间件更稳。

### Gaps
- MemBench（2025）、BEAM（"Beyond a Million Tokens"）、Letta Memory Benchmark 的具体数字本次没有核实。
- 没有找到覆盖"真实数月/数年 IM 聊天"的公开基准（LifeSide 接近，但细节未读）。
- 2026 年多数 LongMemEval 高分没有统一 judge 和 reader，也没有第三方复现。

## Q4. 2025–2026 综述及其分类法

### Takeaway
2025-12 的大型综述 *Memory in the Age of AI Agents*（47 位作者）提出 **Forms × Functions × Dynamics** 三轴，认为长/短期二分法已不够用。2026 年的综述进一步按表示基底（token / latent-KV / parametric）、控制策略和 write–manage–read 循环来组织，图式记忆有了专门的综述。

### Cited Findings
- *Memory in the Age of AI Agents: A Survey — Forms, Functions and Dynamics*（Yuyang Hu 等 47 人，arXiv 2512.13564，2025-12，v2 2026-01）— [arXiv 2512.13564](https://arxiv.org/abs/2512.13564v2)；[HF papers](https://huggingface.co/papers/2512.13564)
  - Forms：token-level（再分 flat / planar / hierarchical）、parametric、latent。
  - Functions：factual、experiential、working。作者明确认为 long/short-term 分类不足。
  - Dynamics：formation（抽取/编码）→ evolution → retrieval。
  - 把 agent memory 与 LLM 内部记忆（KV-cache 管理）、RAG、context engineering 区分开；前沿方向包括 RL 驱动的记忆、多模态记忆、多 agent 共享记忆。
- *Memory for Autonomous LLM Agents: Mechanisms, Evaluation, and Emerging Frontiers*（Pengfei Du，arXiv 2603.07670，2026-03）：write–manage–read 循环；三维分类为 temporal scope × representational substrate × control policy — [arXiv 2603.07670](https://arxiv.org/pdf/2603.07670v1)
- *Graph-based Agent Memory: Taxonomy, Techniques, and Applications*（Chang Yang 等 18 人，arXiv 2602.05665，2026-02）：短期/长期 × knowledge/experience × 非结构/结构 — [arXiv 2602.05665](https://www.arxiv.org/abs/2602.05665)
- *LLM Agent Memory: A Survey from a Unified Representation–Management Perspective*（Zhenheng Tang 等，Preprints.org，2026-03）：表示分 token-level / latent(KV) / parameter-level；管理操作分 construction / update / query — [Preprints](https://www.preprints.org/manuscript/202603.0359/v1)
- *From Storage to Experience*（Preprints.org，2026-01）：演化式分类 storage → reflection → experience — [Preprints](https://www.preprints.org/manuscript/202601.0618/v2)
- 系统层面的记忆类型划分示例 — [MemOS](https://arxiv.org/abs/2507.03724)；[Hindsight HTML](https://arxiv.org/html/2512.12818v1)
  - MemOS：plaintext / activation / parametric；
  - Hindsight：world / experience / observation / opinion，把"agent 自身经历"和"主观信念"单列。

### Inferences
- 【推断】对拟人 IM agent 可以采用两层分类。设计轴用 Functions（factual / experiential / working）；实现轴用 Forms（token 文本为主，KV/参数化作缓存或长期蒸馏）。在此之上，按认知心理学惯例补充"情感/关系"和"自我/身份"两个功能子类：主流综述没有把 affective 作为一级类别，但 Hindsight 的 Opinion、MemoryOS 的 Agent Traits 已在系统层面体现。

### Gaps
- 2512.13564 的完整章节（各类别代表系统列表、评测汇总）只读到摘要和二手综述，没有逐章核实。
- 没有找到专门针对"情感/身份记忆"的综述。

## Q5. 生产系统如何控成本：分层存储、混合库、上下文预算、剪枝策略

### Takeaway
控成本的手段主要有五类：
1. **少注入**：只注入策展过的核心记忆和档案，日志只建索引、按需检索；
2. **在线轻、离线重**：写入时 soft 插入，夜间批量巩固；
3. **按 token 预算装包** + 稳定前缀吃 prompt cache；
4. **容量上限 + 热度/FIFO 逐出**；
5. 检索走低成本通道（BM25 + 向量），cross-encoder 和 LLM 只用在小候选集上。

数量级参考：检索后上下文 1.6k–30k tokens，full-context 约 115k；单次检索 p95 0.2–0.8s。

### Cited Findings
- **Token 与延迟数量级**
  - Mem0：记忆约 7k tokens/对话（原文约 26k）；search p95 0.200s；total p95 1.44s，full-context 17.12s。Zep 图超过 600k tokens/对话（存储膨胀）。LangMem search p95 59.82s（不可用于在线）— [Mem0 HTML](https://arxiv.org/html/2504.19413v1)
  - Zep：检索后上下文 1.6k tokens，full-context 115k；延迟降约 90%；cross-encoder 最准但最贵 — [Zep HTML](https://arxiv.org/html/2501.13956v1)
- **压缩与 prompt cache**：Mastra OM 用稳定、只追加的观察前缀提高 prompt cache 命中；平均上下文约 30k；文本压缩 3–6×（作者引用的"缓存使成本降 4–10×"是供应商普遍说法，不是 OM 实测）— [Mastra OM](https://mastra.ai/research/observational-memory)
- **在线/离线分离**：LightMem 在线 soft 插入 + 离线批量更新；在线 token 降 106–117×，API 调用降 159–310×，运行时间快至 12.4× — [LightMem HTML](https://arxiv.org/html/2510.18866)
- **容量上限与逐出**：MemoryOS 的 STM 7 页、每段最多 200 页、KB 和 Traits 各 100 条 FIFO、MTM 逐出最低热度段、平均 3,874 tokens / 4.9 次调用 — [MemoryOS HTML](https://arxiv.org/html/2506.06326)
- **上下文压力阈值**：MemGPT 在 70% 发警告、100% flush、逐出约 50% 并递归摘要 — [MemGPT HTML](https://arxiv.org/html/2310.08560v2)
- **OpenClaw 的预算控制** — [OpenClaw Memory](https://docs.openclaw.ai/concepts/memory)；[OpenClaw Dreaming](https://docs.openclaw.ai/concepts/dreaming)
  - `MEMORY.md` 超出 bootstrap 预算时只截断注入副本；每日日志只建索引不注入；
  - 自动回忆 1 秒预算，超时跳过；
  - 晋升片段每条不超过 160 tokens；单次重写最多删 25%；
  - dreaming 以后台优先级运行，受 `maxConcurrent` 约束；
  - 支持 keyword-only（`fts-only`）降级模式。
- **存储限额**：Letta MemFS 每个仓库 5 MiB（Free）/ 50 MiB（Pro），按最新快照计、不计 git 历史 — [Letta docs](https://docs.letta.com/guides/agents/architectures/sleeptime)
- **单次写入成本**：A-MEM 每次记忆操作约 1.2k–2.5k tokens，低于 $0.0003；GPT-4o-mini 平均 5.4s — [A-MEM HTML](https://arxiv.org/html/2502.12110)
- **本地部署**：Hindsight 召回结果按调用方给的 token 预算装包；20B 配置面向单块消费级 GPU — [Hindsight HTML](https://arxiv.org/html/2512.12818v1)
- **准确率 vs 延迟**：LongMemEval-V2 中最准的方案也最慢（Codex 69.9% / 177s，RAG 42.8% / 0.1s）— [LongMemEval-V2](https://xiaowu0162.github.io/longmemeval-v2/)
- **算力优化**：ChatGPT Dreaming 为 Free 用户提供服务的算力降低约 5×【搜索摘要】— [OpenAI](https://openai.com/index/chatgpt-memory-dreaming/)
- **离线算力摊销**：Sleep-time compute 跨多个 query 分摊，单 query 成本降 2.5× — [arXiv 2504.13171](https://arxiv.org/abs/2504.13171)

### Inferences
- 【推断】参考预算：IM 回合的热路径 = 固定核心块（人设 + 用户档案 + pinned 关系事实，约 2–4k）+ 检索装包（约 1.5–5k，按预算截断）+ 最近消息尾部，其中前缀保持稳定以吃缓存。冷路径（抽取、合并、反思、社区刷新）全部放进夜间或空闲期批处理。分级检索：先 BM25 + 向量（ms 级），必要时再图扩展和 cross-encoder，只有复杂问题才走 agentic 迭代检索（数十秒级，参照 LongMemEval-V2）。
- 【推断】存储分三层：原始 episode 无损存冷库（廉价对象存储或 SQLite）；派生事实/观察存 SQL + 向量 + 全文索引；图关系按需维护。Zep 600k tokens/对话的膨胀提示，图派生物要控制粒度，并做周期性压实。

### Gaps
- 没有找到厂商公开的"每用户每月记忆成本"真实数字。
- 各系统的具体剪枝/TTL 策略（例如 Zep 是否删除旧 episode）文档未说明。

## Q6. 情感标注与身份级记忆（自我认知、对特定人的关系模型）

### Takeaway
情感记忆在 2025-10 至 2026-07 间出现了一批专门工作：
- DAM-LLM：对"对象–方面"的情感极性做 Bayesian 式加权更新，用熵剪枝；
- ZifaMem：情感标签嵌入分层记忆，情绪峰值触发晋升，pooled EI +11.4%；
- Sentipolis、LifeSide 等。

关键反直觉结论：**显式情感状态机没有带来可测增益**（ZifaMem），情感作为记忆上的标签和晋升信号比独立的情绪模块更有效。身份级记忆方面：Hindsight 把带置信度的 Opinion 和 disposition 参数分开；MemoryOS 维护 90 维 User Traits 和动态 Agent Traits；OpenClaw 和 Claude 把用户档案与普通事实分开存放。

### Cited Findings
- **DAM-LLM**（Lu & Li，BUPT，arXiv 2510.27418，2025-10-31）— [DAM-LLM HTML](https://arxiv.org/html/2510.27418v1)
  - **记忆单元**：object_id、object_type、aspect、sentiment_profile（正/负/中三分量）、熵 H、summary、reason；证据强度 S∈[0,3]，累计权重 W。
  - **更新**：C_new = (C·W + S·P)/(W+S)，W_new = W + S（Bayesian-inspired 加权平均）。
  - **熵**：H = −Σ p log₂ p。H < 0.8 视为成熟/稳定；H > 1.4 的新观察直接丢弃。压缩操作有 Update / Integrate（同对象不同 aspect 合并）/ Delete（高熵且低权重）；主 agent 的目标是最小化总熵。
  - **检索**：先按 object_type + aspect 精确过滤，再按 summary embedding cosine 取 top-5。
  - **结果**：500 轮内记忆量减少 63.7–70.6%，稳定在 130–140 个单元；约 10–15 次观察后收敛；GPT-4 judge 评分 Emotional Resonance 4.5 vs 3.8，Personalization 4.6 vs 3.5。
  - 基线只是一个未命名的"LLM"记忆系统，没有和 Mem0 / A-Mem 比较；数据由 GPT-4 合成（DABench：2,500 条观察序列、100 个会话 / 1,000 轮、500 对 query）。
- **ZifaMem**（Fang 等，ZifaCorp/USTC/FDU，arXiv 2607.17564，2026-07-20）— [alphaXiv 2607.17564](https://www.alphaxiv.org/abs/2607.17564)
  - **四层**：L1 逐字滚动缓冲；L2 会话结束时摘要；L3 情景记忆（importance ≥ 0.5 **或检测到情绪峰值**时晋升）；L4 巩固的用户模型（特质、稳定偏好、长期史）。
  - **检索打分**：S_total = 0.35·S_semantic + 0.30·S_strength + 0.35·S_importance；strength 每个用户回合按年龄、访问频次、重要度重算。
  - **情感通道**：旧版 E+E+E+ 用 VAD（valence/arousal/dominance）；DYAFFECT 用置信度自适应增益，但**没有显著优于"取最近 3 次情绪观察"的基线**。
  - **结果**（ALPSBENCH，4 个骨干 pooled）：A5 情商 +0.42（相对 raw history +11.4%，CI 6.3–17.1%）；Claude-Sonnet-4.6 +0.64；persona grounding 在 4 个骨干上全部提升（Claude 相对 +42%）；多轮情感上下文比单轮快照净偏好 +39%（探索性）；偏好遵循上与 Mem0 统计等价（±5 分内）。
  - **负面结果**：**显式情感状态机在 5 个指标上都没有可测增益**；在 Claude 上加 affect block 的效果可忽略（−0.020）；在 Gemini 上做上下文压缩会损害 fiction–reality 分辨（−0.100），作者因此建议**保留来源（provenance-preserving）的记忆**；作者同时强调记忆增强带来的隐私和操纵风险，主张给用户查看、更正、删除记忆的控制权。
- **其他情感工作【搜索摘要】** — [arXiv 2601.18027](https://arxiv.org/html/2601.18027)；[arXiv 2606.04660](https://arxiv.org/html/2606.04660)；[arXiv 2605.15812](https://arxiv.org/pdf/2605.15812)；[alphaXiv 2508.16629](https://www.alphaxiv.org/abs/2508.16629v1)
  - Sentipolis（2026-01）：把情绪作为持久状态，对抗社会模拟 agent 的 "emotional amnesia"；
  - LifeSide（2026-06）：跨会话追踪约 52%；
  - Cross-Temporal Emotional Modeling（2026-05）：指出缺少把长期情感积累与即时行为联系起来的模型；
  - Learn to Memorize（2025-08）：在情感陪伴任务上，学到的记忆策略会转向保留情绪信息。
- **身份 / 自我 / 关系模型**
  - Hindsight：Experience 网络记录 agent 自身行为（自我经历）；Opinion 网络存带 confidence 的信念，按 reinforce / weaken / contradict 以 α / 2α 更新；CARA 的 skepticism / literalism / empathy（1–5）+ bias-strength 塑造反思风格，相当于"性格参数"— [Hindsight HTML](https://arxiv.org/html/2512.12818v1)
  - MemoryOS：用户档案含 90 维 User Traits；Agent 档案分固定人设和动态 Agent Traits，由高热度话题段晋升驱动更新 — [MemoryOS HTML](https://arxiv.org/html/2506.06326)
  - Generative Agents：importance 的 1–10 分本质上是 poignancy（情感显著性）评分，也是反思触发的累计量 — [Generative Agents HTML](https://arxiv.org/html/2304.03442)
  - Mastra OM：用 🔴🟡🟢 优先级标注"当时什么重要" — [Mastra OM](https://mastra.ai/research/observational-memory)
  - OpenClaw：`USER.md` 存档案和偏好（写成指令，单独预算）；`DREAMS.md` 是 agent 的"梦境日记"（叙事化自我记录，不作为晋升来源）— [OpenClaw Memory](https://docs.openclaw.ai/concepts/memory)；[OpenClaw Dreaming](https://docs.openclaw.ai/concepts/dreaming)
  - Claude：健康、政治、宗教等敏感话题默认不存入记忆，可由用户开启 — [Claude Help](https://support.claude.com/en/articles/11817273-use-claude-s-chat-search-and-memory-to-build-on-previous-context)

### Inferences
- 【推断】针对"对特定人的关系模型"，可组合三种机制：
  - DAM-LLM 的 (对象, 方面) → 情感分布 + 熵：表示"我对 A 在 X 方面的态度"，带收敛与稳定判据；
  - Hindsight 的 Opinion + confidence 增减规则：表示"我对 A 的判断"；
  - ZifaMem 的"情绪峰值 → 情景记忆晋升"：保证重大情感事件不被遗忘。
- 【推断】情感状态本身（agent 当下心情）宜用轻量方式，例如最近 N 次情绪观察的滑动窗口，不建复杂状态机，因为 ZifaMem 实测状态机无增益。情感更适合作为记忆的元数据：参与检索打分（类似 importance）、决定晋升和遗忘。
- 【推断】自我认知 / 身份应拆成三块：(a) 不可变的核心人设（pinned）；(b) 可缓慢演化的 traits 和 opinions（带 confidence 与修订历史）；(c) agent 自身经历的 episodic 日志（Hindsight 的 Experience）。(b) 的修改只在离线巩固中进行，并设单次变化上限（类比 OpenClaw 的 25% loss cap），防止人格漂移。

### Gaps
- 没有找到在真实长期（数月）陪伴数据上验证情感记忆效果的公开研究；现有结果多为 GPT 合成数据或短期基准。
- 没有找到"agent 对多个不同人分别维护关系模型"（多用户社交图谱 + 每人的情感史）的系统性评测。
- Character.AI、Replika 等陪伴产品的记忆机制没有公开技术资料，本次未覆盖。
