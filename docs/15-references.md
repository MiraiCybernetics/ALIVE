# 15 · 参考文献与调研依据

> 文档编号：ALIVE-DOC-15 · 最后更新：2026-11-06
> 本文列出设计中直接引用的来源，按子系统分组。完整的调研笔记，包括数字、机制细节、未核实项与缺口，见 `research_notes/拟人自主AI框架技术调研/` 下的七份笔记。
> 笔记中标注为"二手""检索摘要""厂商自报"的数字，在本设计中只作为量级参考。

## 调研笔记索引

| 笔记 | 覆盖 | 主要支撑文档 |
|---|---|---|
| `memory.md` | 记忆架构、巩固、基准、成本、情感与身份记忆 | 03 |
| `continual_learning.md` | 免训练学习、权重层学习、自修改系统、课程 | 06、13 |
| `affect_personality_drives.md` | 经典情感模型、LLM 时代情绪状态、人格漂移、驱力、风险 | 04、05 |
| `cognitive_architectures.md` | 经典认知架构、LLM 时代多进程运行时、空闲思考、失效模式 | 02、10 |
| `always_on_im_agents.md` | OpenClaw、Hermes Agent、Moltbook、主动性研究、文本轮次、网页注入 | 07、09 |
| `metacognition_eval_safety.md` | 元认知、评估方法、安全风险、法规、治理机制 | 08、09、11 |
| `chinese_community_projects.md` | 麦麦源码、中文开源项目、社区经验、国内监管 | 07、09 |
| `psychology_sociology_understanding.md` | 语用学、心智理论、预测加工、共情、关系科学、群体社会学、LLM 社会推理 | 17 |
| `language_production.md` | 言语产出、为听者设计、省略与片段、间接与表达规则、汉语语气词、LLM 说话偏差 | 18 |
| `dialogue_strategy.md` | 多重目标、对话分析、动机式访谈、LLM 策略偏差与规划 | 19 |
| `humanities_communication.md` | 交际民族志、框架与立足点、言语体裁、差序格局、人情与面子、面子协商、叙事、打趣、修辞、沉默、交往理性 | 20 |

## 认知核心（02）

- Park et al., *Generative Agents: Interactive Simulacra of Human Behavior*, 2023. https://arxiv.org/abs/2304.03442
- Altera, *Project Sid: Many-agent simulations toward AI civilization*（PIANO 架构）, 2024-10. https://arxiv.org/abs/2411.00114
- Liu et al., *Proactive Conversational Agents with Inner Thoughts*, CHI 2025. https://arxiv.org/abs/2501.00383
- Lin et al., *Sleep-time Compute: Beyond Inference Scaling at Test-time*, 2025-04. https://arxiv.org/abs/2504.13171
- Springdrift（可审计的持久运行时）, 2026. https://arxiv.org/abs/2604.04660

## 记忆（03）

- Packer et al., *MemGPT*, 2023. https://arxiv.org/abs/2310.08560
- Chhikara et al., *Mem0*, 2025-04. https://arxiv.org/abs/2504.19413
- Rasmussen et al., *Zep: A Temporal Knowledge Graph Architecture for Agent Memory*, 2025-01. https://arxiv.org/abs/2501.13956
- Xu et al., *A-MEM: Agentic Memory for LLM Agents*, 2025. https://arxiv.org/abs/2502.12110
- Gutiérrez et al., *From RAG to Memory: Non-Parametric Continual Learning for LLMs*（HippoRAG 2）, ICML 2025. https://arxiv.org/abs/2502.14802
- Kang et al., *MemoryOS*, 2025. https://arxiv.org/abs/2506.06326
- *LightMem*, 2025–2026. https://arxiv.org/abs/2510.18866
- *Hindsight*, 2025-12. https://arxiv.org/abs/2512.12818
- Mastra, *Observational Memory*, 2026-02. https://mastra.ai/research/observational-memory
- OpenClaw 记忆与 Dreaming 文档. https://docs.openclaw.ai/concepts/memory ；https://docs.openclaw.ai/concepts/dreaming
- Letta 休眠期代理文档. https://docs.letta.com/guides/agents/architectures/sleeptime
- Hu, Wang, McAuley, *MemoryAgentBench*, 2025–2026. https://arxiv.org/abs/2507.05257
- Wu et al., *LongMemEval-V2*, 2026-05. https://xiaowu0162.github.io/longmemeval-v2/
- Hu et al., *Memory in the Age of AI Agents: A Survey*, 2025-12. https://arxiv.org/abs/2512.13564
- Lu & Li, *DAM-LLM: Dynamic Affective Memory Management*, 2025-10. https://arxiv.org/abs/2510.27418
- Fang et al., *ZifaMem*, 2026-07. https://www.alphaxiv.org/abs/2607.17564

## 情感与人格（04）

- Gebhard, *ALMA: A Layered Model of Affect*, AAMAS 2005. https://cs.huji.ac.il/course/2005/aisemin/articles2006/docs/pa1a3_29.pdf
- Becker-Asano & Wachsmuth, *WASABI*, JAAMAS 2010. https://becker-asano.de/index.php/research/wasabi/61-wasabi-architecture-overview
- Marsella & Gratch, *EMA: A process model of appraisal dynamics*, 2009. https://www.cs.uky.edu/~sgware/reading/papers/marsella2009ema.pdf
- Verduyn & Lavrijsen, *Which emotions last longest and why*, Motivation and Emotion 2015. https://ppw.kuleuven.be/okp/_pdf/Verduyn2015WELLA.pdf
- *Controlling Long-Horizon Behavior with Explicit State Dynamics*, 2026-01. https://arxiv.org/abs/2601.16087
- Anthropic, *Emotion concepts and their function in a large language model*, 2026-04. https://www.anthropic.com/research/emotion-concepts-function
- Anthropic, *Persona vectors*, 2025-08. https://www.anthropic.com/research/persona-vectors
- Lu et al., *The Assistant Axis*, 2026-01. https://arxiv.org/abs/2601.10387
- Li et al., *Measuring and Controlling Persona Drift in Language Model Dialogs*, 2024. https://arxiv.org/abs/2402.10962
- Serapio-García et al., *Personality traits in large language models*, Nature Machine Intelligence 2025. https://arxiv.org/abs/2307.00184
- De Freitas et al., *Emotional Manipulation by AI Companions*, HBS 2025. https://www.library.hbs.edu/working-knowledge/how-ai-chatbots-try-to-keep-you-from-walking-away
- Qiu et al., *EmoAgent*, EMNLP 2025. https://arxiv.org/abs/2504.09689

## 动机（05）

- Keramati & Gutkin, *Homeostatic reinforcement learning*, eLife 2014. https://elifesciences.org/articles/04811
- Wang et al., *Humanoid Agents*, EMNLP 2023 Demo. https://arxiv.org/abs/2310.05418
- Gaven et al., *MAGELLAN*, ICML 2025. https://arxiv.org/abs/2502.07709

## 学习与成长（06）

- Zhang et al., *Agentic Context Engineering*, ICLR 2026. https://arxiv.org/abs/2510.04618
- 第三方对 ACE 代码的审查. https://zby.github.io/commonplace/agent-memory-systems/reviews/ace/
- Suzgun et al., *Dynamic Cheatsheet*, 2025. https://arxiv.org/abs/2504.07952
- *ReasoningBank*, ICLR 2026. https://arxiv.org/abs/2509.25140
- *SkillsBench*, 2026. https://arxiv.org/abs/2602.12670
- Anthropic, *Equipping agents for the real world with Agent Skills*, 2025-10. https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills
- Lin et al., *Continual Learning via Sparse Memory Finetuning*, 2025-10. https://arxiv.org/abs/2510.15103
- Zweiger et al., *Self-Adapting Language Models (SEAL)*, 2025. https://arxiv.org/abs/2506.10943
- Shenfeld et al., *RL's Razor: Why Online RL Forgets Less*, ICLR 2026. https://arxiv.org/abs/2509.04259
- Thinking Machines, *On-Policy Distillation*, 2025-10. https://thinkingmachines.ai/blog/on-policy-distillation/
- *Denser ≠ Better*, 2026-07. https://huggingface.co/papers/2607.01763
- Google Research, *Introducing Nested Learning*, 2025-11. https://research.google/blog/introducing-nested-learning-a-new-ml-paradigm-for-continual-learning/
- Zhang et al., *Darwin Gödel Machine*, 2025. https://arxiv.org/abs/2505.22954 ；https://sakana.ai/dgm/
- Robeyns et al., *A Self-Improving Coding Agent (SICA)*, 2025. https://arxiv.org/abs/2504.15228
- Shao et al., *Your Agent May Misevolve*, ICLR 2026. https://arxiv.org/abs/2509.26354
- Faldor et al., *OMNI-EPIC*, ICLR 2025. https://arxiv.org/abs/2405.15568
- Unsloth 显存需求. https://docs.unsloth.ai/get-started/beginner-start-here/unsloth-requirements

## 社交与交互（07）

- OpenClaw 消息与自动化文档. https://docs.openclaw.ai/concepts/messages
- Hermes Agent 消息文档. https://hermes-agent.nousresearch.com/docs/user-guide/messaging
- MaiBot 源码：轮次调度、回复概率、动态门控、防抖. https://github.com/Mai-with-u/MaiBot
- Pei et al., *HALO*, 2026-10. https://arxiv.org/abs/2610.39086
- Nathani et al., *Pare*, 2026-04. https://arxiv.org/abs/2604.00842
- Lu et al., *Proactive Agent / ProactiveBench*, 2024. https://arxiv.org/abs/2410.12361
- Matt Webb, *Turn-taking for multiplayer AI chat*, 2025-05. https://interconnected.org/home/2025/05/23/turntaking
- *HUMA*, 2025-11. https://arxiv.org/abs/2511.17315
- Eckhaus et al., *Time to Talk*, EMNLP 2025 Findings. https://arxiv.org/abs/2506.05309
- LangChain, *Introducing Ambient Agents*, 2025-01. https://www.langchain.com/blog/introducing-ambient-agents
- Wiz 关于 Moltbook 数据库暴露的报告与 *When Agents Talk*, 2026. https://arxiv.org/abs/2606.00067
- 掘金《让 QQ AI 机器人"可控地插话"》, 2026-06. https://juejin.cn/post/7655654223931179023

## 自我模型与元认知（08）

- Anthropic, *Emergent introspective awareness in large language models*, 2025-10. https://transformer-circuits.pub/2025/introspection/index.html
- *SimpleToM*, ICLR 2026. https://arxiv.org/abs/2410.13648
- *PersonaMem*. https://arxiv.org/abs/2504.14225

## 治理与安全（09）

- 国家网信办等，《人工智能拟人化互动服务管理暂行办法》，2026-04-10 公布，2026-07-15 施行. https://www.cac.gov.cn/2026-04/10/c_1777558395078289.htm
- 同上，征求意见稿，2025-12-27. https://www.cac.gov.cn/2025-12/27/c_1768571207311996.htm
- California SB 243（Companion Chatbots）解读. https://skadden.com/insights/publications/2025/10/new-california-companion-chatbot-law
- EU AI Act 第 50 条透明度义务自 2026-08-02 起适用的解读. https://www.goodwinlaw.com/en/insights/publications/2026/08/alerts-technology-dpc-eu-ai-act-transparency-obligations-now-in-force
- Anthropic, *Agentic Misalignment*, 2025-06. https://www.anthropic.com/research/agentic-misalignment
- Anthropic, *Claude's constitution*, 2026-01. https://www.anthropic.com/constitution
- Simon Willison, *The lethal trifecta for AI agents*, 2025-06. https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/
- Debenedetti et al., *Defeating Prompt Injections by Design (CaMeL)*, 2025. https://arxiv.org/abs/2503.18813
- Snyk, *ToxicSkills*, 2026-02. https://snyk.io/blog/toxicskills-malicious-ai-agent-skills-clawhub/
- OWASP GenAI Security Project. https://genai.owasp.org/

## 理解与共鸣（17）

- Zhang et al., *MetaMind: Modeling Human Social Thoughts with Metacognitive Multi-Agent Systems*, NeurIPS 2025. https://arxiv.org/abs/2505.18943
- Kim et al., *Hypothesis-Driven Theory-of-Mind Reasoning for Large Language Models*（thought-tracing）, COLM 2025. https://arxiv.org/abs/2502.11881
- *DRInQ: Evaluating Conversational Implicature with Controlled Context Variation*, ACL 2026. https://arxiv.org/abs/2605.24267
- Liu et al., *The Illusion of Empathy: How AI Chatbots Shape Conversation Perception*, AAAI 2025. https://ojs.aaai.org/index.php/AAAI/article/view/33569
- Telari, Gabbiadini & Riva, 2026，关于自我暴露、被回应感与亲近感的实验（报道）. https://www.psypost.org/psychologists-pinpoint-the-conversational-mechanisms-that-help-humans-bond-with-ai/
- Kalman et al., *Are you still waiting for an answer? The Chronemics of Asynchronous Written CMC*, JCMC 2007. https://cris.haifa.ac.il/en/publications/are-you-still-waiting-for-an-answer-the-chronemics-of-asynchronou/
- 经典理论：Grice 1975；Sperber & Wilson 1986；Clark 1996；Watzlawick 1967；Brown & Levinson 1987；Hall 1976；Walther 1992；Baker, Saxe & Tenenbaum 2009；Reis & Shaver 1988；Cutrona 1990；Altman & Taylor 1973；Goffman 1959；Bell 1984；Collins 2004；Ickes 1997。

## 语言产出、对话策略与言语交际（18–20）

- Levelt, *Speaking: From Intention to Articulation*, 1989；Vygotsky, *Thought and Language*, 1934；Clark & Wilkes-Gibbs 1986；Brennan & Clark 1996；Pickering & Garrod 2004；Giles 的交际顺应理论；Ginzburg 的 KoS 框架；Pinker, Nowak & Lee 2008；Ekman & Friesen 1969；Keysar 关于说话者自我中心偏差的研究。
- Shaikh et al., *Grounding Gaps in Language Model Generations*, NAACL 2024. https://aclanthology.org/2024.naacl-long.348
- Kandra, Demberg & Koller, *LLMs syntactically adapt their language use to their conversational partner*, ACL 2025. https://arxiv.org/abs/2503.07457
- Jones & Bergen, *Large Language Models Pass the Turing Test*, 2025. https://arxiv.org/abs/2503.23674
- 信息均匀分布与片段产出，Frontiers in Psychology 2021. https://www.frontiersin.org/articles/10.3389/fpsyg.2021.661087/full
- Deng et al., *Plug-and-Play Policy Planner for LLM Powered Dialogue Agents*, ICLR 2024. https://arxiv.org/abs/2311.00262
- Yu, Chen & Yu, *Prompt-Based Monte-Carlo Tree Search for Goal-oriented Dialogue Policy Planning*, EMNLP 2023. https://aclanthology.org/2023.emnlp-main.439
- Kang et al., *Can Large Language Models be Good Emotional Supporter? Mitigating Preference Bias on Emotional Support Conversation*, ACL 2024. https://arxiv.org/abs/2402.13211
- Deng et al., *Prompting and Evaluating LLMs for Proactive Dialogues*（ProCoT）, 2023. https://arxiv.org/abs/2305.13626
- 人文社科经典：Hymes 1962/1972；Gumperz 1982；Bateson 1955；Goffman 1955, 1959, 1974, 1981；Bakhtin 1952–53；费孝通《乡土中国》；黄光国 1987；Ting-Toomey 1985；Brown & Levinson 1987；Sacks, Schegloff & Jefferson 1974；Labov & Waletzky 1967；Keltner et al. 1998, 2001；Aristotle《修辞学》；Burke 1950；Basso 1970；Habermas 1981；Dillard 1990；Clark & Delia 1979；Miller & Rollnick 的动机式访谈。

## 评估（11）

- Park et al., *Generative Agent Simulations of 1,000 People*, 2024. https://arxiv.org/abs/2411.10109
- Samuel et al., *PersonaGym*, Findings of EMNLP 2025. https://arxiv.org/abs/2407.18416
- EQ-Bench 3. https://eqbench.com/
- Zhou et al., *SOTOPIA*, ICLR 2024. https://arxiv.org/abs/2310.11667
- Anthropic, *Petri*（开源审计工具）, 2025. https://alignment.anthropic.com/2025/petri
- OpenAI 与 MIT Media Lab 关于情感使用与福祉的研究, 2025-03（报道）. https://engadget.com/ai/joint-studies-from-openai-and-mit-found-links-between-loneliness-and-chatgpt-use-193537421.html

> 说明：以上链接均来自调研笔记。Anthropic 宪法、Agentic Misalignment、Assistant Axis 等 Anthropic 发布物的链接也收录在 `metacognition_eval_safety.md` 中。
