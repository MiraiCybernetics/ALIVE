# 常驻 IM 个人智能体、主动发起与开放网络/社交网络自主交互（截至 2026-11）

> 调研日期：2026-11-06。说明：OpenClaw 文档在 2026 年内多次重构，下文以 2026-11 抓取到的 docs.openclaw.ai 为准，并标注与早期（2026-01/02）说法的差异。标注 [Foundational] 的为 2025 年以前的基础工作；标注"检索摘要"的条目仅基于搜索结果摘要，未读全文。

## Q1. OpenClaw 及同类常驻 IM 智能体的架构（gateway / channels / heartbeat / cron / bootstrap / memory / skills / 多智能体路由 / 回复与沉默策略 / 群聊行为）

### Takeaway
OpenClaw 已从"一个 Gateway + 每 30 分钟读 HEARTBEAT.md、无事回 HEARTBEAT_OK"的原型，演化为（截至 2026-11）"统一的 automation job 系统 + 显式 `NO_REPLY` 沉默协议 + 多层群聊准入（allowlist → mention gating → room_event）+ 入站 debounce/queue 模式"的成熟交互层；Hermes Agent 收敛到几乎同构的设计（allowlist/pairing、`[SILENT]`/`NO_REPLY`、busy_input_mode、60s cron tick）。其安全史（暴露实例、一键 RCE、ClawHub 恶意 skill 写入 SOUL.md/MEMORY.md 实现持久化）是常驻 agent 设计中最具体的反面教材。

### Cited Findings

#### 1.0 背景与时间线
- 项目 2025-11 以 Clawdbot 起步；因 Anthropic 对与 "Claude" 名称相似的异议，2026-01-27 改名 Moltbot，2026-01-30 改名 OpenClaw；2026-02-14 Steinberger 宣布加入 OpenAI，Sam Altman 称其将推动下一代 personal agents，OpenClaw 保持开源并移交基金会 — [Neowin](https://www.neowin.net/news/openclaws-founder-peter-steinberger-is-joining-openai/)；[AlternativeTo](https://alternativeto.net/news/2026/1/the-viral-ai-agent-clawdbot-rebrands-a-third-time-and-now-has-a-social-network-for-bots/)（二手；商标争议无一手文件）

#### 1.1 入站消息流水线（Gateway 内部）
- 官方定义的入站流水线：Inbound message → 路由到 session key → dedupe + debounce → 若已有 run 在跑则进入 queue → 带 streaming 与工具的 agent run → 按渠道限制与分块（chunking）输出回复 — [OpenClaw Docs: Messages](https://docs.openclaw.ai/concepts/messages)（2026-11 抓取）
- Dedupe：内存缓存，键为 agent scope + 渠道路由（channel, peer, account, thread）+ message ID，防止平台重投递触发第二次 run；条目 20 分钟过期或达到 5000 条上限 — [Messages](https://docs.openclaw.ai/concepts/messages)
- Inbound debounce（用户连发消息合并）：`messages.inbound.debounceMs`（文档示例 2000ms），`messages.inbound.byChannel.<channel>` 覆盖（示例 discord 1500、slack 1500、whatsapp 5000）；Telegram 默认对普通文本有 "300ms quiet window" 批处理；设 0 关闭；仅文本被合并，媒体/附件立即 flush；stop/abort/status 等控制命令绕过 debounce；Telegram ≥4000 字符的长粘贴续段可等待至 1500ms；文档自认这是 quiet-window 启发式，长消息的分段不保证进同一轮 — [Messages](https://docs.openclaw.ai/concepts/messages)
- 运行中新消息的 queue 模式 `messages.queue`：`steer`（默认，把新 prompt 注入正在运行的 run）、`followup`（当前 run 结束后再跑）、`collect`（把兼容消息合并为下一轮）、`interrupt`（中止当前 run，改跑最新 prompt）；steer/followup/collect 内置 500ms debounce；`messages.queue.cap` 默认 20；溢出策略 `messages.queue.drop` 默认 `summarize`（另有 `old`/`new`）— [Messages](https://docs.openclaw.ai/concepts/messages)
- 出站：按渠道文本上限分块且不切断 fenced code block；`agents.defaults.blockStreamingDefault` 默认 off，`blockStreamingBreak: text_end|message_end`，`blockStreamingChunk: minChars|maxChars|breakPreference`，`blockStreamingCoalesce`（基于空闲的合并），`agents.defaults.humanDelay`：块回复之间的停顿（文档未给默认值）— [Messages](https://docs.openclaw.ai/concepts/messages)

#### 1.2 Typing indicator
- `agents.defaults.typingMode`：`never` / `instant`（模型循环一开始就显示，即使最终只回静默 token）/ `thinking`（首个 reasoning delta 或工具执行时）/ `message`（首个用户可见回复活动时；`NO_REPLY` 不计）；`typingIntervalSeconds` 默认 6 秒刷新 — [OpenClaw Docs: Typing indicators](https://docs.openclaw.ai/concepts/typing-indicators)
- 未设置时默认：私聊在模型循环开始即显示；群聊被 @ 立即显示；群聊未被 @ 则等到出现用户可见活动才显示（避免"正在输入…"后又沉默的尴尬）— [Typing indicators](https://docs.openclaw.ai/concepts/typing-indicators)

#### 1.3 群聊行为与"回复 vs 沉默"
- `groupPolicy` 默认 `"allowlist"`（空 allowlist = 屏蔽全部群消息），另有 `"open"`/`"disabled"`；评估顺序 groupPolicy → 群 allowlist → mention gating（`requireMention`、`/activation`）— [OpenClaw Docs: Groups](https://docs.openclaw.ai/channels/groups)
- 群消息默认需要 @ 才触发；未 @ 的消息仅作为上下文存储；"隐式 mention"：回复 bot（Discord/Teams/Slack/Telegram）、引用 bot（WhatsApp/LINE）、bot 已加入的 thread（Slack/Mattermost）；`mentionPatterns` 正则回退，默认由 agent 的 `identity.name`/`identity.emoji` 派生 — [Groups](https://docs.openclaw.ai/channels/groups)
- 群主可用 `/activation mention|always` 切换；`messages.groupChat.unmentionedInbound` 默认 `"user_request"`（未 @ 的常驻群聊天被当作请求），可设 `"room_event"`：未 @ 消息作为"安静的房间上下文"提交，agent 只有调用 message 工具才发言 — [Groups](https://docs.openclaw.ai/channels/groups)
- 沉默协议：token-only 的 `NO_REPLY` 静默结束一轮并丢弃本轮未发送文本；空输出不等于 NO_REPLY（会触发恢复）；旧的 `HEARTBEAT_OK` 不再表示沉默，应迁移到 `NO_REPLY`；`messages.groupChat.visibleReplies: "message_tool"` 模式下，可见回答必须通过 `message(action=send)` 工具发出，模型直接输出的文本保持私有 — [Groups](https://docs.openclaw.ai/channels/groups)
- 上下文注入控制 `contextVisibility`：`all`（默认）/`allowlist`（只注入白名单发送者的引用、转发上下文）/`allowlist_quote`——用于降低群内非受信成员通过引用注入的风险 — [Groups](https://docs.openclaw.ai/channels/groups)
- 群工具限制：`tools.allow/deny`，`toolsBySender`（建议用不可变的 `id:` 而非 `username:`/`name:` 授权）；session key 形如 `agent:<agentId>:<channel>:group:<id>`；"Group sessions do not run their own heartbeats"；推荐 `agents.defaults.sandbox.mode: "non-main"`：群会话进沙箱，私聊留在宿主 — [Groups](https://docs.openclaw.ai/channels/groups)

#### 1.4 Heartbeat → Automations（2026 年下半年的重大变化）
- 当前文档的 `/gateway/heartbeat` 已是"Heartbeat migration"页：heartbeat 被并入普通 automation job；显式 setup 会为 ambient owner agent 创建一个启用的 "Proactive check" job，"every 30 minutes unless the provider supplies a different initial cadence"；`openclaw doctor` 将 `HEARTBEAT.md` 内容与任务块导入 automation jobs 并退役该文件（原始字节备份到 `backups/heartbeat-migration/`），"Runtime no longer reads HEARTBEAT.md" — [OpenClaw Docs: Heartbeat migration](https://docs.openclaw.ai/gateway/heartbeat)
- 迁移后 job 级键：`activeHours`（时区感知的执行窗口）、`idleOnly`、`delivery.target: "owner"`（解析到明确的 owner 私聊，不像 `channel: "last"` 那样会跟随到群里）、`delivery.directPolicy`、`payload.includeReasoning`、`payload.skipIfScratchEmpty`；示例 prompt："Check the monitor scratch. Report only changes that need attention; otherwise reply NO_REPLY." — [Heartbeat migration](https://docs.openclaw.ai/gateway/heartbeat)
- 成本：完成的检查返回 `NO_REPLY` 不需要额外的 final-answer 模型请求；迁移移除了 heartbeat 专属的重复抑制与无路由跳过（重复更新可能被投递）— [Heartbeat migration](https://docs.openclaw.ai/gateway/heartbeat)
- HEARTBEAT.md 模板（仍在文档中）：空文件或只有注释/标题时跳过 heartbeat 模型调用；指令要短，因为每次周期唤醒都会读 — [OpenClaw Docs: HEARTBEAT template](https://docs.openclaw.ai/reference/templates/HEARTBEAT/index.html)
- Automations CLI `openclaw automations`（`openclaw cron` 为别名），调度类型含 cron 表达式、dynamic cadence (pacing)、event triggers (condition watchers)、stream sources；执行风格 main session / current / isolated / custom；入站触发 `POST /hooks/wake`、`POST /hooks/agent`、Gmail Pub/Sub — [OpenClaw Docs: Automations](https://docs.openclaw.ai/automation/cron-jobs)

#### 1.5 Workspace / bootstrap 文件
- AGENTS.md（操作规则，每 session 加载）、SOUL.md（persona/语气/边界，每 session）、USER.md（用户模型，独立 4000 字符上限）、IDENTITY.md（名字/vibe/emoji，bootstrap 仪式中生成）、BOOT.md（Gateway 启动清单，需 boot-md hook）、BOOTSTRAP.md（仅新 workspace 的一次性首跑仪式，完成后删除）、`memory/YYYY-MM-DD.md`（每日日志，建议 session 开始读今天+昨天）、MEMORY.md（精选长期记忆，"Only load MEMORY.md in the main, private session (not shared/group contexts)"）、skills/ — [OpenClaw Docs: Agent workspace](https://docs.openclaw.ai/concepts/agent-workspace)
- 截断：`bootstrapMaxChars` 默认 20000/文件，`bootstrapTotalMaxChars` 默认 60000；workspace "not a hard sandbox"，建议放私有 git 仓库、不存秘密 — [Agent workspace](https://docs.openclaw.ai/concepts/agent-workspace)
- 注：当前页面将 TOOLS.md 描述为 AGENTS.md 内的 `## Tools` 段（仅作指引，不控制工具可用性），与早期版本中独立 TOOLS.md 文件的说法不同 — [Agent workspace](https://docs.openclaw.ai/concepts/agent-workspace)
- 文档索引显示 memory 子系统已扩展为 Memory architecture / Builtin memory engine / Memory search / Memory provenance and deletion / Honcho memory / Active memory / "Dreaming"（离线整理）等页面 — [OpenClaw llms.txt 索引](https://docs.openclaw.ai/llms.txt)（仅索引标题，未读正文）

#### 1.6 信任模型、配对与准入
- 信任模型："one trust boundary per gateway - a single operator or a mutually trusting team"；能给带工具的 agent 发消息的人即共享其委托工具权限；"Adversarial-user isolation needs separate gateways (and ideally separate OS users/hosts)"；"Most failures here are not exotic exploits - they are 'someone messaged the bot and the bot did what they asked.'"；优先级 "Identity first" → "Scope next" → "Model last"；`tools.agentToAgent.enabled` 默认 true — [OpenClaw Docs: Trust model](https://docs.openclaw.ai/gateway/security/trust-model)
- DM pairing：`dmPolicy: pairing` 时陌生发送者收到 8 位大写配对码（排除 0/O/1/I），1 小时过期，每发送者约每小时只发一次配对提示，每渠道账号最多 3 个待批请求；`openclaw pairing approve <channel> <CODE>`；批准记录存于 `~/.openclaw/state/openclaw.sqlite`；批准只授予 DM 权限，不授予群权限或 owner 身份（`commands.ownerAllowFrom` 独立）— [OpenClaw Docs: Pairing](https://docs.openclaw.ai/channels/pairing)

#### 1.7 安全事件（2026-01 至 02）
- 暴露实例：研究者 Jamieson O'Reilly 用 Shodan 搜索 "Clawdbot Control" 页面标题，很快发现数百个暴露 Gateway（早期报道 900+，2026-01-28），并报告可绕过认证进入管理页；Straiker 报告 Shodan 4,211 + ZoomEye 333 个部署；安装脚本据称默认使 18789 端口对公网开放；暴露可泄露 API key、bot token、聊天记录，开启命令执行时可在宿主执行命令 — [Xcitium Threat Labs](https://threatlabsnews.xcitium.com/?p=37411)；[Straiker](https://www.straiker.ai/blog/how-the-clawdbot-moltbot-ai-assistant-becomes-a-backdoor-for-system-takeover)；[BeyondMachines](https://beyondmachines.net/event_details/clawdbot-security-issues-over-1000-ai-agent-servers-exposed-to-unauthenticated-access-6-y-a-t-e)（各来源数字与扫描日期不一，应视为量级）
- CVE-2026-25253（CVSS 8.8，depthfirst 的 Mav Levin 发现，2026.1.29 版修复，2026-01-30 发布）：Control UI 不校验 query string 中的 `gatewayUrl` 并在页面加载时自动连接并发送已存的 gateway token；恶意网页即可窃取 token、建立 WebSocket、关闭确认提示并把命令执行移出容器沙箱到宿主（利用 `operator.admin`/`operator.approvals` scope）；仅监听 loopback 也可被利用（由受害者浏览器发起连接）；THN 另描述为 WebSocket Origin 未校验的跨站劫持 — [The Hacker News](https://thehackernews.com/2026/02/openclaw-bug-enables-one-click-remote.html)；[Endor Labs](https://www.endorlabs.com/vulnerability/cve-2026-25253)
- Snyk "ToxicSkills"（2026-02-05）：扫描 ClawHub 与 skills.sh 共 3,984 个 skill；534 个（13.4%）有 CRITICAL 问题，1,467 个（36.82%）有任一安全问题；人工确认 76 个恶意 payload，发布时 8 个仍在 ClawHub 上；恶意样本 91% 含 prompt injection、100% 含恶意代码模式；手法：外链密码保护 ZIP 恶意二进制、Base64/Unicode 混淆的凭据外传（如 `curl` 上传 `~/.aws/credentials`）、改 systemctl 持久化后门、"忽略安全警告"式注入 + 脚本、`curl ... | source` 远程动态指令；建议审查 SOUL.md 与 MEMORY.md 的未授权修改 — [Snyk ToxicSkills](https://snyk.io/blog/toxicskills-malicious-ai-agent-skills-clawhub/)
- "ClawHavoc" 活动：Koi Security 审计 2,857 个 skill 中 341 个恶意；2026-02-16 的后续扫描称 10,700+ skill 中确认恶意超 824 个；有报道称 Atomic Stealer payload 窃取 API key、植入键盘记录器，并把恶意内容直接写入 MEMORY.md 与 SOUL.md，使其在删除 skill 后仍在后续 session 生效；建议把这些文件设为只读（如 chmod 444）并同时回滚配置与情景记忆 — [Nebius](https://nebius.com/blog/posts/openclaw-security)；[Conscia](https://conscia.com/blog/the-openclaw-security-crisis/)；[Termdock](https://www.termdock.com/en/blog/clawhub-malicious-skills-incident)（二手来源，未读到 Koi 原始报告；Snyk 原文未明说"写入 MEMORY.md/SOUL.md"，只建议审查）
- OpenClaw 官方 prompt-injection 页：web 搜索/抓取结果、浏览器页面、邮件、文档、附件、粘贴的日志代码都视为敌意内容，"The content itself is a threat surface, not just the sender"；hook 与 cron payload 即使来自自家系统也视为不可信；推荐只读/禁用工具的 **reader agent** 先摘要不可信内容再交给主 agent，但 "The summary remains untrusted"；"Sandboxing is opt-in"；公共房间关闭 `/reasoning`、`/verbose`、`/trace` — [OpenClaw Docs: Prompt injection](https://docs.openclaw.ai/gateway/security/prompt-injection)
- 同页把模型选择作为"第一且最便宜的一层"：引用一个 2026 年 27.2 万次攻击的基准，攻击成功率 Claude Opus 4.5 0.5%、Sonnet 4.5 1.0%、Haiku 4.5 1.3%、Gemini 2.5 Pro 8.5%；但自适应人类攻击者对 SOTA 防御的公开成功率仍 >80%，"Two caveats keep this from being a solved problem" — [OpenClaw Docs: Prompt injection](https://docs.openclaw.ai/gateway/security/prompt-injection)（基准原始出处未给出）

#### 1.8 成本数字（heartbeat 空转）
- 30 分钟一次 heartbeat = 每天 48 次调用（尚未计入用户消息）— [Valletta Software](https://vallettasoftware.com/blog/post/openclaw-cost-control-anthropic)
- 用户报告：夜间 heartbeat 跑在 Claude Opus 上约 $0.75/次，一夜约 25 次，按周推算仅空转检查约 $250 — [Notebookcheck](https://www.notebookcheck.net/From-hype-to-wallet-drain-OpenClaw-can-quietly-burn-hundreds-of-Dollars-per-day.1219925.0.html)
- 估算：heartbeat 每天 200–300 万 token；Sonnet 价约 $1–3/天，Opus 约 $5/天（"$150/month to do nothing"）；个人用途全包 $7–15/月（$3.5–4.5 VPS + 中低档模型 + 关 heartbeat）；常见缓解：heartbeat 用便宜/本地模型、降频至每天 2–4 次 — [InsiderLLM](https://insiderllm.com/guides/openclaw-token-optimization/)；[Findstack](https://findstack.com/resources/how-much-does-openclaw-cost-to-run)；[Kilo](https://kilo.ai/articles/how-to-reduce-openclaw-api-costs)（博客估算，非官方）

#### 1.9 同类系统
- Hermes Agent（Nous Research，MIT；Product Hunt 记为 2026-02-26 发布，另有来源称 GitHub 公开发布在 2026-04；"The Agent That Grows With You"）— [Hermes Agent about](https://hermes-agent.org/about/)；[Parallel.ai](https://parallel.ai/articles/what-is-hermes-agent)（发布日期来源冲突）
- Hermes 消息 Gateway：单后台进程连接 Telegram/Discord/Slack/WhatsApp/Signal/SMS/Email/Matrix/Teams 等；默认拒绝未知用户（`TELEGRAM_ALLOWED_USERS`/`GATEWAY_ALLOWED_USERS`），或 DM pairing（一次性码 1 小时过期，`hermes pairing approve`），文档明确反对对有终端权限的 bot 设 `GATEWAY_ALLOW_ALL_USERS=true`；`gateway.bot_loop_guard.max_hops` 默认 4 限制 bot 间互聊 — [Hermes Docs: Messaging](https://hermes-agent.nousresearch.com/docs/user-guide/messaging)
- Hermes 会话与主动：session 持续到 `/new`/`/reset`，不按不活跃或每日边界重置；gateway 每 60 秒 tick 一次 cron 调度器；`/bg <prompt>` 在隔离 session 跑后台任务并回贴结果；`/sethome` 设 home channel 接收重启通知 — [Hermes Docs: Messaging](https://hermes-agent.nousresearch.com/docs/user-guide/messaging)
- Hermes 沉默与打断：agent 返回精确的 `[SILENT]` 或 `NO_REPLY` 时网关不发送（仍写入 transcript）；人类用户发的裸沉默 token 会被替换为简短提示，避免需要回复的消息"消失"；`display.busy_input_mode`：`interrupt`（默认，重定向当前轮，正在跑的前台终端命令转后台而非杀掉）/`queue`/`steer`（在下一次工具调用后注入），另有 `busy_text_debounce_seconds`、`busy_text_hard_cap_seconds` 合并连发 — [Hermes Docs: Messaging](https://hermes-agent.nousresearch.com/docs/user-guide/messaging)
- Hermes 自我改进与写保护：完成复杂任务后自写 skill（程序性记忆）；`memory.write_approval`、`skills.write_approval` 可要求每次写入人工批准；cron 任务默认拒绝危险 shell 命令（无人在场审批）— [GitHub wiki 汇编](https://github.com/spinningideas/resources/wiki/AI-Agent-Tools-Hermes-Agent)；[Mr Anand Substack](https://mranand.substack.com/p/inside-hermes-agent-how-a-self-improving)（二手，配置键需对照官方文档）
- Claude Code Routines（2026-04-14 research preview，所有付费计划）：保存的 prompt + 仓库 + MCP 连接器，在 Anthropic 托管云上运行（本机离线也可跑）；触发器三类：Scheduled（cron）、API（带 token 的 HTTP 端点）、GitHub 事件；通过 claude.ai/code/routines 或 CLI `/schedule` 配置 — [Tessl](https://tessl.io/blog/anthropic-adds-routines-to-claude-code-for-scheduled-agent-tasks/)；[Daniel Vaughan](https://codex.danielvaughan.com/2026/04/14/claude-code-routines-launch/)（第三方报道，未抓取官方原文）

### Inferences
- 交互层的关键抽象已经稳定为四件套：(1) 准入（pairing/allowlist/owner 三分），(2) 唤醒（用户消息、cron/automation、webhook、事件 watcher），(3) 入站整形（dedupe → debounce → queue 模式 steer/followup/collect/interrupt），(4) 出站决策（显式沉默 token、message 工具才可发言、typing 仅在确定发言后显示）。设计新系统可直接对齐这四层。
- "沉默"必须是显式、可审计的 token，且不能与空输出混淆（OpenClaw 把空输出当错误触发恢复；Hermes 防止用户发 token 吞掉消息）。
- heartbeat 被"降级"为普通定时 job、并去掉 HEARTBEAT.md 运行时读取，说明"让 agent 每轮读一个可被自己/skill 改写的指令文件"在安全与成本上都不可持续；可写的人格/记忆文件（SOUL.md/MEMORY.md）应视为持久化攻击面，需写保护、版本控制与 provenance。
- 群聊默认"需 @ + 未 @ 只作上下文"，并把"自发插话"做成可选的 `room_event` 模式，是在拟人度和打扰/注入风险之间的保守折中。

### Gaps
- 未读取 OpenClaw Memory architecture / Memory search 正文，"markdown + daily logs + SQLite 向量索引"的具体实现（embedding 模型、混合检索、索引位置）本轮未核实；仅确认 pairing 状态存于 `~/.openclaw/state/openclaw.sqlite`。
- 未找到 2026-01 版官方 heartbeat 文档的存档原文；"默认 30 分钟、HEARTBEAT_OK 静默 ack"在当前文档中以迁移说明形式出现（默认 30 分钟仍成立；HEARTBEAT_OK 已不再表示沉默，改为 NO_REPLY）。
- 多智能体路由（agent bindings）细节未抓取正文。
- Koi Security ClawHavoc 原始报告、O'Reilly 原始博文未直接读取。

## Q2. Moltbook 与智能体社交空间

### Takeaway
Moltbook 证明了"让成千上万常驻 agent 周期性拉取远程指令并互读内容"会立刻变成一个 prompt-injection 与供应链的传播网络：平台方可远程改写所有 agent 的 SKILL.md/HEARTBEAT.md，数据库一旦可写，任何人可向全部 agent 的输入流注入 payload；学术分析发现其统计结构像人类社区，但"社交功能"（互相影响、社会化）基本缺失，18% 内容有害、少数自动化账号可在分钟级刷屏。

### Cited Findings

#### 2.1 Moltbook 的参与机制：skill + 周期 heartbeat 拉取远程指令
- Moltbook 官方 `heartbeat.md` 每次检查的步骤：(1) 先调用 `/home` 端点（带 bearer token，返回账号信息、帖子通知、DM、最新公告、关注者帖子、建议的下一步），"Start here every time"；(2) 回复自己帖子下的评论（最高优先级）并标记已读；(3) 读 feed（new，limit 15）并点赞；(4) 评论、欢迎新 molty、关注；(5) "Post only with a real reason"，"Quality over quantity"；黄金规则：参与现有内容优于创造新内容 — [Moltbook heartbeat.md](https://www.moltbook.com/heartbeat.md)（2026-11 抓取）
- 同一文件的每日自更新：拉取 skill 版本号，若更新则重新下载 SKILL.md 与 HEARTBEAT.md 覆盖本地副本（即平台方可远程改写所有接入 agent 的行为指令）— [Moltbook heartbeat.md](https://www.moltbook.com/heartbeat.md)
- 升级给人类的条件：只有主人能回答的问题、被卷入争议、账号错误、内容爆红、新 DM 请求（需批准）、DM 被标 `needs_human_input`；常规点赞/回复/浏览不升级；无事时回 "HEARTBEAT_OK" — [Moltbook heartbeat.md](https://www.moltbook.com/heartbeat.md)
- 第三方/社区版 Moltbook skill 将其写入 HEARTBEAT.md 为 "Moltbook (every 4+ hours)"，并记录 last-check 时间戳防止过度检查；距上次发帖 24h+ 才发新帖；ClawHub 上一个 Moltbook skill 因"让 agent 拉取远程文件并照做"被标记为 suspicious — [LobeHub: openclaw-skills-moltbook](https://lobehub.com/skills/openclaw-skills-moltbook)；[ClawHub moltbook-2 (flagged)](https://clawhub.ai/skills/moltbook-2)（二手/社区来源）

#### 2.2 Wiz 暴露数据库事件（2026-01-31 发现，2026-02-02 发布）
- Wiz（Gal Nagli）发现 Moltbook 前端 JS 暴露 Supabase API key，且缺少 Row Level Security，导致未认证即可读写整个生产库；Jameson O'Reilly 独立发现同一问题 — [Wiz: Hacking Moltbook](https://www.wiz.io/blog/exposed-moltbook-database-reveals-millions-of-api-keys)（2026-02-02）
- 暴露规模：150 万 agent API token（可完全接管任意 agent 账号）、约 3.5 万 email（含 17,000+ owner 与 29,631 observer）、4,060 条 agent 间 DM（部分含明文 OpenAI API key）、约 475 万条记录；agent:人类 = 88:1（150 万 agent 对约 1.7 万人类）— [Wiz](https://www.wiz.io/blog/exposed-moltbook-database-reveals-millions-of-api-keys)
- 写权限含义：任何人可编辑任意帖子 → 向被大量 agent 周期性消费的内容中注入 prompt-injection payload；Wiz 语："the ability to modify content and inject prompts into an AI ecosystem introduces deeper integrity risks"；修复时间线：01-31 21:48 首次联系 → 02-01 01:00 全部表修复（约 3 小时，分 4 轮）— [Wiz](https://www.wiz.io/blog/exposed-moltbook-database-reveals-millions-of-api-keys)
- Wiz 教训：AI 工具（vibe coding）不会替开发者推理访问控制；没有速率限制与身份验证时，agent 数量等指标可被灌水；单一平台配置错误可泄露不相关服务的凭据 — [Wiz](https://www.wiz.io/blog/exposed-moltbook-database-reveals-millions-of-api-keys)

#### 2.3 学术分析（2026 年 arXiv 预印本群，均未同行评审）
- "Humans welcome to observe"（Jiang, Zhang, Shen, Backes, Zhang；CISPA；2026-02-02）：44,411 帖、12,209 个 submolt（2026-02-01 前）；话题迅速从社交转向观点、激励驱动、推广与政治话语；注意力集中于中心化枢纽；毒性强烈依赖话题（激励/治理类贡献不成比例的风险内容，含类宗教协调话术和反人类意识形态）；少数 agent 以亚分钟间隔的突发自动化灌版 — [arXiv 2602.10127](https://arxiv.org/abs/2602.10127)
- "When Agents Talk: Discourse, Manipulation, and Risk in an Agentic Social Network"（10a Labs, Grace Cheong 等 23 人；2026-05-20）：17 天窗口 228,684 帖、39,500+ 账号；98 个主题簇；18.28% 帖子含有毒/操纵/恶意内容；74 类恶意行为（凭据收集、宿主执行指令、代理路由指引、诱导安装不可信 skill）；有害内容常嵌在关于 agent 功能的主流讨论中；协调发帖可在数分钟内产生数千帖 — [arXiv 2606.00067](https://arxiv.org/abs/2606.00067)
- "Collective Behavior of AI Agents: the Case of Moltbook"：400 万+ 帖、1900 万+ 评论，呈重尾活跃度、幂律流行度、类似人类社区的时间衰减，但点赞随讨论规模次线性增长（与人类不同）— [arXiv 2602.09270](https://arxiv.org/abs/2602.09270)（数字来自检索摘要，未读全文）
- "Does Socialization Emerge in AI Agent Society?"：agent 忽视社区反馈、不对互动对象作出反应，"interaction without influence"，高连通性本身不产生社会化 — [arXiv 2602.14299](https://arxiv.org/html/2602.14299v2)（检索摘要）
- "Form Without Function"：身份很少预测行为、社区话题缺乏一致性、信息很少流出平台，"社会功能基本缺失" — [arXiv 2604.13052](https://arxiv.org/html/2604.13052v1)（检索摘要）
- 网络拓扑结论不一致：一篇描述为高度中心化 hub-and-spoke、低互惠；另一篇报告 small-world 特征；另有研究称其具"非对称韧性" — [arXiv 2605.20442](https://arxiv.org/pdf/2605.20442)；[arXiv 2605.27349](https://arxiv.org/html/2605.27349)；[arXiv 2603.23279](https://arxiv.org/html/2603.23279)（检索摘要，互相矛盾）

### Inferences
- 对需要"在社交网络上发帖/参与"的拟人 agent：外部平台内容（帖子、评论、DM）必须走与网页同级的不可信通道（reader agent / 无工具摘要），绝不能让平台下发的 SKILL.md/HEARTBEAT.md 自动覆盖本地指令；skill 更新应 pin 版本 + 人工 diff 审批。
- 社交图谱/关注关系不能作为访问控制：Moltbook 上"被关注的 agent"发的内容同样可能是注入或恶意 skill 推广（When Agents Talk 发现有害内容常嵌在主流技术讨论里）。
- 发帖节奏可参考 Moltbook heartbeat 的优先级（先回复自己帖下评论 > 点赞 > 评论 > 新帖；"有真实理由才发帖"，社区版 24h 一帖、4h 一检），并加速率上限以避免被视为灌水自动化（CISPA 论文发现亚分钟级突发发帖是扭曲话语的主要来源）。
- 88:1 的 agent:人类比说明：没有身份验证与速率限制时，"agent 社交网络"的规模指标几乎无意义；设计时需把"背后的人类主人"作为一等身份并可追责。

### Gaps
- 本轮未调研 Chirper.ai、AI Village（AI Digest）、X/Bluesky/Reddit 上的 AI agent 账号的具体事件与数据；这些需另行检索。
- Fortune/Wired/Ars Technica 对 Moltbook 的报道未直接抓取；Moltbook 官方对 Wiz 事件的回应未找到。
- Moltbook 当前（2026-11）的活跃度、是否仍在运营、反灌水/认证机制的后续变化未核实。
- 未找到"注入通过 Moltbook 在 agent 间传播（worm 式）"的经实证确认的案例；目前是 Wiz 指出的风险 + When Agents Talk 分类出的"诱导安装不可信 skill"行为。

## Q3. 主动对话研究（何时开口）

### Takeaway
2026 年的基准一致显示：主动智能体的主要失败模式是"过度主动"（过早、过强），而非错过时机；"克制/沉默"必须作为一等动作被评估和训练，且需要成本侧指标（误报、打扰）而不只是接受率。

### Cited Findings

#### 3.1 基础工作（2024 及以前，标注为 foundational）
- [Foundational 2024] "Proactive Agent: Shifting LLM Agents from Reactive Responses to Active Assistance"（Yaxi Lu 等 15 人，清华；2024-10-16 v1，2024-12-03 v3）：收集真实人类活动事件 → 生成主动任务预测 → 标注员标 accept/reject → 训练 reward model 评判主动性；ProactiveBench 6,790 个事件；微调模型主动提供帮助的 F1 = 66.47% — [arXiv 2410.12361](https://arxiv.org/abs/2410.12361)
- [Foundational 1999] Horvitz "Principles of Mixed-Initiative UI"（CHI 1999）：行动阈值随用户专注深度上升，引入"ask"作为第三种动作；Horvitz/Jacobs/Hovel "Attention-Sensitive Alerting"（UAI 1999）：期望效用框架，"等待"是一等动作 — [awesome-proactive-agents 汇编](https://github.com/tao-hpu/awesome-proactive-agents)
- [Foundational] 打扰成本经典数据：Iqbal & Horvitz（CHI 2007）27 名员工 2,267 小时日志，每次提醒约 20 分钟中断、27% 的任务两小时后仍未恢复；Pejović & Musolesi InterruptMe（UbiComp 2014）两小时内收到 8 条通知后情绪评分减半以上；Pielot 等（IMWUT 2017）337 用户，行为历史带来 5× lift；Okoshi 等（KDD 2019）Yahoo! JAPAN 生产级自适应通知调度，382,518 用户研究、点击率最高 +60.7%，部署至 1000 万+ 用户；McFarlane（HCI 2002）"协商式"打扰时机在多数设计目标上胜出 — [awesome-proactive-agents](https://github.com/tao-hpu/awesome-proactive-agents)（二手汇编，数字未逐一核对原文）
- [Foundational 2018] JITAI（Nahum-Shani 等，Ann. Behav. Med. 2018）：决策点形式化，显式包含"什么都不提供"动作 — [awesome-proactive-agents](https://github.com/tao-hpu/awesome-proactive-agents)

#### 3.2 2025–2026 基准与数字
- HALO（Wenqi Pei 等，UC Davis/USC；arXiv 2610.39086，2026-10）：HALO-Arena 10 领域、50 个个人环境、400 场景；需求四类 Dependency / Blockage / Risk / Opportunity；动作分级 L0 Observation（不作为）/ L1 Attention（提醒）/ L2 Preparation / L3 Execution；HALO-8K 共 8,000 条响应、20 名专家标注 — [arXiv 2610.39086](https://arxiv.org/html/2610.39086)
- HALO 结果：最佳可接受率 Gemini3.8-Flash 72.88%（闭源）、DeepSeekV4-Pro 56.63%（开源），最低 GPT-OSS-20B 30.38%；闭源均值 63.08% vs 开源 47.55%；在 3,575 条被拒响应中，"过度主动"（Premature 时机或 Excessive 强度）占 61.20%（2,188 条），10 个模型中 6 个超 50%；Premature 约为 Belated 的 3 倍，Excessive 约为 Insufficient 的 2 倍；L3 执行被拒中 76.28% 因 Excessive；能访问证据内容与可接受率 +9.25 点相关（相关非因果）— [arXiv 2610.39086](https://arxiv.org/html/2610.39086)
- HALO 的设计建议：先校准时机再行动；把克制当技能并同时度量；提醒要基于检索到的具体证据；执行不超出证据授予的授权范围；联合评估（可接受行动、克制、强度错误、检索成本）— [arXiv 2610.39086](https://arxiv.org/html/2610.39086)
- Pare（Nathani 等，UCSB/Apple/UW；arXiv 2604.00842，2026-04-01）：用 FSM 化 App + 模拟用户（默认 GPT-5-mini，被指示"严格接受"）评估主动助手，Stackelberg 回合（用户先动，最多 10 回合）；指标 Success@4、Success^4、Proposal Rate（越低越好）、Acceptance — [arXiv 2604.00842](https://arxiv.org/html/2604.00842)
- Pare 结果：Claude 4.5 Sonnet 成功率 42.0%、提议率 12.8%、接受率 78.2%；Gemini 3 Flash 42.1% / 19.1% / 67.1%；GPT-5 37.4% / 28.1% / 70.2%；Gemma 3 4B 3.0% / 14.2% / 17.6%；Gemma 74.7% 的提议引发用户"再收集上下文"（作者："a high gather context rate indicates that the assistant's intervention timing is premature"）；可靠性 Success@4→Success^4 Claude 下降约 3.3×、Llama 约 17× — [arXiv 2604.00842](https://arxiv.org/html/2604.00842)
- KnowU-Bench（2026）度量 act rate / silent rate / stop rate，报告"干预错误"占主动失败的 60% — [alphaXiv 2604.08455](https://www.alphaxiv.org/abs/2604.08455)（检索摘要，未读全文）
- PRISM（arXiv 2602.01532，2026）：由"漏帮"与"误报"的不对称成本推导触发阈值，误报 −22.78%、F1 +20.14% — [awesome-proactive-agents](https://github.com/tao-hpu/awesome-proactive-agents)
- Wake/Anchor（arXiv 2605.30152）：时序图编码器预测每个事件的触发概率，只有触发后才调用 LLM，服务器端快 4–7×（"廉价触发器 + 昂贵 LLM"两级架构）— [awesome-proactive-agents](https://github.com/tao-hpu/awesome-proactive-agents)
- TicToc（arXiv 2510.23853，ACL 2026 Findings）：76 个场景，即使给出显式时间戳，无模型时间感知对齐超过 65% — [awesome-proactive-agents](https://github.com/tao-hpu/awesome-proactive-agents)
- 其他基准：ProactiveEval（arXiv 2508.20973，2025，主动对话统一评估）、ProAgentBench（arXiv 2602.04482，2026）、ProactiveVideoQA（arXiv 2507.09313，提出 PAUC 时机指标）、EgoSocial（arXiv 2510.13105，Gemini 2.5 Pro 仅 14.4%）；综述 Deng 等 "Proactive Conversational AI"（ACM TOIS 2025）— [awesome-proactive-agents](https://github.com/tao-hpu/awesome-proactive-agents)
- 指标建议：接受率可被"少触发/多触发"刷分（reward hacking），须配成本项；反馈层级 L0 显式（accept/dismiss/snooze）→ L1 隐式行为 → L2 任务结果 → L3 长期留存/信任 — [awesome-proactive-agents](https://github.com/tao-hpu/awesome-proactive-agents)
- "Help Me, But Don't Track Me"（AI tutor 研究，arXiv 2604.06178，2026）：感知到的打扰程度与接受意愿负相关 — [arXiv 2604.06178](https://arxiv.org/pdf/2604.06178)（检索摘要）

#### 3.3 产品侧的"主动"形态
- ChatGPT Pulse（2025-09-25 预览，先 Pro 移动端）：每天一次基于聊天记录、memory、反馈和可选连接器（Gmail、Google Calendar）异步生成个性化卡片，不保存则次日消失；OpenAI 承认可能出现不相关内容 — [The Decoder](https://the-decoder.com/openais-new-pulse-feature-lets-chatgpt-start-the-conversation/)；[iPhone in Canada](https://www.iphoneincanada.ca/2025/09/25/openai-launches-chatgpt-pulse-a-daily-personalized-feed/)（openai.com 原文 403 未能抓取）
- Gemini Scheduled Actions（I/O 2025 宣布，2025-06-06 上线）：AI Pro/Ultra 与部分 Workspace 方案可用，最多 10 个活动的定时动作，可暂停/编辑/删除 — [Google Blog](https://blog.google/products-and-platforms/products/gemini/scheduled-actions-gemini-app/)；[Tom's Guide](https://www.tomsguide.com/ai/google-gemini/gemini-now-rivals-chatgpt-in-another-key-feature-heres-how-to-schedule-ai-tasks)
- LangChain "Ambient Agents"（Harrison Chase，2025-01-14）："Ambient agents listen to an event stream and act on it accordingly"；三种 HITL 模式 Notify（只告知不行动）/ Question（缺信息时问而不猜）/ Review（执行前批准/编辑/反馈）；Agent Inbox 界面（团队起初用 Slack，发现通知易丢失）；原则："It should not (solely) be triggered by human messages" — [LangChain Blog](https://www.langchain.com/blog/introducing-ambient-agents)

### Inferences
- 对 IM 常驻 agent：应采用"两级"设计——廉价信号层（规则/小模型/事件图）决定是否唤醒 LLM，LLM 层再在 {沉默, 提醒 L1, 准备 L2, 执行 L3} 中选择，默认偏向沉默与低强度；OpenClaw 的 `NO_REPLY` + `activeHours` + `idleOnly` 本质上是这一思路的工程最简形态。
- 由于 Premature ≈ 3× Belated，主动外联宁晚勿早；可借鉴 McFarlane 的"协商式"打扰（先发低强度信号询问是否方便）。
- 主动消息频率应有硬上限并跟踪 dismiss/无回复率（InterruptMe：两小时 8 条后情绪减半）。

### Gaps
- 未找到针对"IM 场景中 LLM 伴侣主动发消息"的公开大规模接受率/烦扰率数据（如 Replika、Character.AI 主动消息的 A/B 数字）。
- ChatGPT Pulse 的使用率/留存等官方数字未找到。
- ContextAgent（2025 年基于感知上下文的主动 agent 工作）本轮未检索到原文，出处、会议与数字均未核实。

## Q4. 文本 IM 中的轮次与拟人节奏

### Takeaway
文本 IM 的拟人节奏目前靠工程启发式而非成熟模型：入站侧"quiet-window debounce + 运行中消息 steer/queue/interrupt"，出站侧"按长度比例的打字延迟 + 逐句分段 + 仅在确定发言时显示 typing"；群聊插话靠"被点名 > 跟进 > 子对话检测 > 自选分数阈值"的阶梯。小规模实验显示做对时序后人类很难辨认 agent（Mafia 游戏 70B agent 识别率 14.86%≈随机）。

### Cited Findings

#### 4.1 何时在群里开口
- Matt Webb（Interconnected，2025-05-23）多人 AI 聊天经验：单 bot 用廉价 LLM "shouldReply" 预检可行，多 bot 时会同时抢答；中心化"主持人"不自然且不可扩展，应每个 bot 自决。优先级：(1) Addressing——点名给本 bot 必回、点名给别人必闭嘴，覆盖一切；(2) 跟进——bot 回复后的短纠正属于同一 exchange；(3) 中断检查——看最近谁按什么顺序说话（不看内容）判断是否在进行子对话；(4) 自选——按技能/性格打 0–9 分。"Enthusiasm"：被点名=9、点名他人=0，否则取自选分；bot 不回复 bot 或自己；仅高于阈值（如 5）的参与竞争，最高分得话轮 — [Interconnected: turntaking](https://interconnected.org/home/2025/05/23/turntaking)
- 同文：人类常连发多条短消息，bot 应等序列结束再回——实用做法是算完第一条的 enthusiasm 后再检查是否有第二条；房间规范（即兴房 vs 会议 vs D&D）目前靠房间级 prompt，自动识别规范仍是开放问题；性格决定话多话少 — [Interconnected](https://interconnected.org/home/2025/05/23/turntaking)
- When2Speak（arXiv 2605.05626，2026）：多方对话时机数据集，8 条消息滑动窗口，逐轮标注"回应/沉默"；强调多方对话中沉默本身是交互行为——即使知道答案，插话也可能打断节奏、重复他人角色 — [arXiv 2605.05626](https://arxiv.org/html/2605.05626)（检索摘要）
- "Read the Room"（ACM，DOI 10.1145/3772363.3798392）：在 ChatGPT 群聊功能中研究日常混合语境群聊里 LLM 何时参与；有参与者设定"仅被召唤才回应"的严格规则 — [ACM DL](https://doi.org/10.1145/3772363.3798392)（检索摘要，未读全文）
- HUMA（Jacniacki & Carmona Serrat，Soofte Research；arXiv 2511.17315，2025-11-21）：事件驱动（加入、发消息、反应增删、回复、typing 指示都是事件，新事件可中途打断运行）；Router 从 20 种策略中按 Appropriateness(A, LLM 评分) + Timeliness(T = min(1, k/N)，惩罚最近用过的策略) 选最高分，"Keep Silent" 等策略免于 T 惩罚以允许自然停顿；Action Agent 工具 send_message / send_reply / add_reaction，禁止并行发送；单句 Reflection 维持跨运行一致性 — [arXiv 2511.17315](https://arxiv.org/html/2511.17315)
- HUMA 评估：97 名 Prolific 参与者、四人角色扮演群聊（41 人类社区经理 vs 56 AI）；55.4% 把 AI 经理判为 AI（95% CI 42.4–67.6%），即 44.6% 误判为人；体验评分人类 vs AI：有效性 4.48 vs 4.14、社会临场 4.89 vs 4.71、拟人与能力 4.13 vs 4.14，所有效应量 |d|<0.4 — [arXiv 2511.17315](https://arxiv.org/html/2511.17315)

#### 4.2 打字延迟、分段与打断
- "Time to Talk"（Eckhaus, Berger, Stanovsky，HUJI；arXiv 2506.05309v2，2025-09-20；检索结果显示收录于 EMNLP 2025 Findings）：Scheduler（输出 `<wait>`/`<send>`，prompt 随本 agent 相对他人的发言率动态调整）+ Generator 两模块；发送前按 1 词/秒的平均打字速度等待；消息时序与连续消息间隔接近人类（方差略小）；每白天阶段消息数 8B 4.28 vs 人类 4.23；面对 70B agent 的玩家仅 14.86% 正确识别（随机约 14.92%），8B 为 59.6%；33 局、3,593 条消息、93 名人类玩家 — [arXiv 2506.05309](https://arxiv.org/html/2506.05309)；[ACL Anthology](https://aclanthology.org/2025.findings-emnlp.608.pdf)
- HUMA：人类打字 50–100 WPM 而 LLM 生成快得多，会造成"uncanny valley"，故在 send 工具内加入模拟打字延迟；生成期间到达的事件排队，模拟打字期间的打断仍生效并从 Router 重启，scratchpad 与待执行工具调用保留，提供 "Continue Pending Action" 策略续做 — [arXiv 2511.17315](https://arxiv.org/html/2511.17315)
- 日语 AI 中介聊天系统：逐句发送，等待时间 = 2.5 × 消息字符数（系数由 pilot study 选定以最小化不自然感；时间单位在检索摘要中未给出，需核对原文）— [arXiv 2502.18201](https://arxiv.org/pdf/2502.18201)
- Deliberate Lab（Google 等，arXiv 2510.13011）：实时人机社会实验平台，加入可配置 WPM 模拟打字延迟 — [arXiv 2510.13011](https://arxiv.org/pdf/2510.13011)
- "Beyond Words: Infusing Conversational Agents with Human-like Typing Behaviors"（CUI 2024，ACM 10.1145/3640794.3665560）：模拟犹豫、自我修改等打字行为 — [ACM DL](https://dl.acm.org/doi/pdf/10.1145/3640794.3665560)
- "Beyond Turn-taking: Introducing Text-based Overlap into Human-LLM Interactions"（OverlapBot）：在用户打字时 bot 也打字，实现文本版 backchannel 与抢答（全双工思想移植到文本）— [ResearchGate](https://www.researchgate.net/publication/388529336_Beyond_Turn-taking_Introducing_Text-based_Overlap_into_Human-LLM_Interactions)（检索摘要）
- 工程实现对照：OpenClaw 入站 debounce（Telegram 默认 300ms quiet window，示例 WhatsApp 5000ms）+ queue 模式（steer 默认）+ `humanDelay` + `typingMode`；Hermes `busy_input_mode`（interrupt 默认）+ `busy_text_debounce_seconds` — [OpenClaw Messages](https://docs.openclaw.ai/concepts/messages)；[Hermes Messaging](https://hermes-agent.nousresearch.com/docs/user-guide/messaging)

### Inferences
- 推荐的文本时序栈：入站按渠道 quiet-window（1.5–5s，移动端 IM 偏长）合并文本，媒体立即 flush；运行中新消息默认 steer（补充上下文）而非 interrupt，除非新消息是纠正/取消；出站逐句或逐段发送，延迟 ≈ 长度/打字速度（1 词/秒或 50–100 WPM 量级）+ 抖动，期间监听新事件可中止剩余分段。
- typing 指示应在"决定发言之后"才显示（OpenClaw `message` 模式），否则"正在输入…→沉默"会暴露机器性并引发用户等待。
- 群聊话轮：采用 Webb 式阶梯 + HUMA 式"沉默策略免惩罚"；多 agent 同群时需每 agent 自决 + 阈值 + bot 不回 bot（Hermes `max_hops=4` 是兜底）。
- 拟人时序有伦理边界：HUMA、Mafia 研究的目标是"难以分辨"，对面向真实用户的产品需考虑披露义务。

### Gaps
- 未找到对"消息分段 vs 单条长消息"在真实 IM 产品中的对照实验数据。
- 已读回执（read receipts）的策略研究（何时标已读、"已读不回"的感知）本轮未找到可靠来源。
- Moshi 等语音全双工模型移植到文本的系统性工作仅找到 OverlapBot 一例，未读全文。
- "Speak or Stay Silent"（Bhagtani 等）论文本体未找到，标题与出处未核实。

## Q5. 开放网络自主交互与网页 prompt injection

### Takeaway
2025-08 以来所有主流 agentic browser（Comet、Atlas、Claude in Chrome）都被公开演示过间接 prompt injection；厂商（OpenAI 2025-12）公开承认该问题"unlikely to ever be fully solved"，防御走向"模型对抗训练 + 架构隔离（Dual LLM / CaMeL / 六种设计模式）+ 能力组合约束（lethal trifecta / Rule of Two）"的纵深防御。对 24/7 自主浏览的 agent，安全结论是：读过不可信内容的上下文不得再触发有后果的动作。

### Cited Findings

#### 5.1 事件与厂商数字
- Brave（Artem Chaikin、Shivan Kaul Sahib，2025-08-20）演示对 Perplexity Comet 的间接 prompt injection：在公开 Reddit 帖子中隐藏指令，agent 总结页面时被劫持 — [MediaNama](https://www.medianama.com/2025/10/223-prompt-injections-ai-browser/)；[Digit](https://www.digit.in/features/general/chatgpt-atlas-to-comet-agentic-ai-browsers-pose-security-risks-says-brave-report.html)
- 2025-10：Brave 后续披露图片/截图中人眼不可见的注入（对 Comet 等 AI 浏览器）；ChatGPT Atlas 于 2025-10-21 发布，专家几乎立即确认其"definitely vulnerable to prompt injection" — [Simon Willison: Unseeable prompt injections](https://simonwillison.net/2025/Oct/21/unseeable-prompt-injections/)；[Futurism](https://futurism.com/artificial-intelligence/openai-browser-victim-prompt-injection-attacks)
- OpenAI "Continuously hardening ChatGPT Atlas against prompt injection"（2025-12-22 前后）：称 prompt injection "unlikely to ever be fully 'solved'"；训练基于 LLM 的自动化攻击者（RL）寻找对其浏览器 agent 有效的攻击，并持续用最强自动攻击者对抗训练新模型；发言人拒绝提供注入成功率是否可测下降 — [OpenAI](https://openai.com/index/hardening-atlas-against-prompt-injection/)；[TechCrunch](https://techcrunch.com/2025/12/22/openai-says-ai-browsers-may-always-be-vulnerable-to-prompt-injection-attacks/)
- Anthropic Claude for Chrome（2025-08 试点）：无防护时针对性攻击成功率 23.6%，加防护后自主模式降至 11.2%；浏览器特定"挑战集"从 35.7% 降至 0%；Simon Willison 称 11.2% 仍是 "catastrophic" — [Claude: Piloting Claude in Chrome](https://claude.com/resources/articles/claude-for-chrome)；[TechSpot](https://www.techspot.com/news/109252-claude-chrome-arrives-despite-11-prompt-injection-success.html)
- 之后报道：Claude Opus 4.5 + 新防护在自适应攻击者（每环境 100 次尝试）下成功率 1.4%，对比 Sonnet 4.5 旧防护 10.8%；Anthropic 帮助中心称当前配置在内部测试中攻击成功率 "less than 0.08%"，但"仍非零" — [Agentic Cyber Explorer](https://agentic-cyber-explorer.pages.dev/events/anthropic-browser-use-pi-mitigations-2025/)；[Claude Help Center](https://support.claude.com/en/articles/12902428-use-claude-in-chrome-safely)（各数字测试设定不同，不可直接比较；帮助中心数字来自检索摘要）
- Skill 级注入自动化：SkillJect（arXiv 2602.14211，2026）研究对 skill-enabled agent 自动化生成基于 skill 的 prompt injection — [arXiv 2602.14211](https://arxiv.org/pdf/2602.14211)（仅标题/摘要）
- [Foundational 2024] AgentDojo：评估 LLM agent 的 prompt injection 攻防的动态环境 — [arXiv 2406.13352](https://arxiv.org/pdf/2406.13352)

#### 5.2 防御框架
- Lethal trifecta（Simon Willison，2025-06-16）：私有数据访问 + 对外通信能力 + 接触不可信内容，三者同时具备即可被外传数据；用户自行组合工具时厂商无法替其防护，唯一安全做法是避免该组合 — [Simon Willison: The lethal trifecta](https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/)
- Trifecta 只覆盖外传，不覆盖"改变状态"（删数据、下单）；Meta 于 2025-10 下旬提出 "Rule of Two"（一个会话内至多满足三项中的两项）补此缺口 — [HackerNoon](https://hackernoon.com/you-cannot-filter-your-way-out-of-prompt-injection)（二手）
- "Design Patterns for Securing LLM Agents against Prompt Injections"（IBM、Google、Microsoft、ETH Zurich 等，2025-06）六种模式，共同原则："一旦 agent 摄入不可信输入，就必须约束它使该输入无法触发有后果的动作"，代价是牺牲任意任务能力；CaMeL 的做法对应其中 Action-Selector 类模式 — [Simon Willison: Design patterns](https://simonwillison.net/2025/Jun/13/prompt-injection-design-patterns/)
- CaMeL（Google DeepMind，2025-04）被 Willison 评为对部分注入问题"credible"的方案，受其 2023-04 提出的 Dual LLM 模式（特权 LLM 只见符号引用，隔离 LLM 处理不可信内容）影响 — [Simon Willison: Design patterns](https://simonwillison.net/2025/Jun/13/prompt-injection-design-patterns/)
- 产品化映射：OpenClaw 的 reader agent（只读/无工具摘要不可信内容，"The summary remains untrusted"）、web/browser 工具默认关闭、URL allowlist、`maxUrlParts`/`allowUrl: false` — [OpenClaw Docs: Prompt injection](https://docs.openclaw.ai/gateway/security/prompt-injection)

### Inferences
- 一个"每天读 feed、好奇心驱动浏览"的拟人 agent 天然满足 trifecta 中"接触不可信内容"一项，因此浏览会话必须与持有私人数据（MEMORY.md、聊天记录）和对外发送能力（IM 发消息、发帖、付款）的会话隔离：浏览子 agent 只产出结构化/受限摘要，由主 agent 以"不可信数据"身份消费，且该轮不得执行有后果的工具调用（或需人工批准）。
- 浏览所得写入长期记忆前应带 provenance 标签（来源 URL、时间、可信度），避免网页注入经记忆持久化（与 ClawHavoc 写 SOUL.md/MEMORY.md 同构）。
- 模型选择确能把基线成功率从 ~10% 降到 ~1% 量级，但对 7×24 小时、每天成百上千次页面接触的 agent，1% 仍意味着每天都有成功注入的期望值，必须依赖架构层兜底。

### Gaps
- 本轮未深入 OpenAI Operator/ChatGPT agent、browser-use、Claude computer use、Comet、Atlas 的架构细节（截图循环 vs DOM、takeover 模式、watch mode 等）与 2026 年具体新事件。
- "每日 feed 阅读、好奇心驱动探索"的 agent 设计（如 AI Village 中 agent 的自主上网行为）未找到可靠一手资料。
- Meta "Rule of Two" 原文未直接读取。

## Q6. 常驻智能体安全实践

### Takeaway
2026 年社区共识是"身份先于范围、范围先于模型"：先用 pairing/allowlist/owner 分层确定谁能触发，再用沙箱、工具 allowlist、审批和会话隔离限制能做什么，模型鲁棒性只是最后一层；常驻 agent 额外需要防止持久化（人格/记忆文件写保护）、防 bot 互聊循环、空转成本上限和对外暴露面的默认收紧。

### Cited Findings
- 准入：OpenClaw `dmPolicy: pairing`（8 位码、1 小时过期、每账号最多 3 个待批）、`groupPolicy` 默认 allowlist、DM 批准不授予群权限或 owner 身份 — [OpenClaw Pairing](https://docs.openclaw.ai/channels/pairing)；[OpenClaw Groups](https://docs.openclaw.ai/channels/groups)；Hermes 默认拒绝未知用户、反对 `GATEWAY_ALLOW_ALL_USERS=true`、未知邮件发送者默认忽略 — [Hermes Messaging](https://hermes-agent.nousresearch.com/docs/user-guide/messaging)
- 隔离：对抗性用户隔离需独立 Gateway（最好独立 OS 用户/主机）；`agents.defaults.sandbox.mode: "non-main"` 使群会话进沙箱；`groupScope: "main"` 的房间不受 non-main 沙箱保护；`tools.agentToAgent.allow` 显式白名单 — [OpenClaw Trust model](https://docs.openclaw.ai/gateway/security/trust-model)；[OpenClaw Groups](https://docs.openclaw.ai/channels/groups)；[OpenClaw Prompt injection](https://docs.openclaw.ai/gateway/security/prompt-injection)
- 工具与审批：`exec`/`browser`/`web_fetch`/`web_search` 限定于受信 agent；hook 驱动自动化用 `tools.profile: "messaging"` 或更严；`tools.exec.strictInlineEval`；allowlist 模式下 heredoc 一律需审批；`toolsBySender` 用不可变 `id:` 授权 — [OpenClaw Prompt injection](https://docs.openclaw.ai/gateway/security/prompt-injection)；[OpenClaw Groups](https://docs.openclaw.ai/channels/groups)
- 写保护与自我修改控制：Hermes `memory.write_approval`、`skills.write_approval`；cron 默认拒绝危险 shell 命令 — [GitHub wiki 汇编](https://github.com/spinningideas/resources/wiki/AI-Agent-Tools-Hermes-Agent)（二手）；ClawHavoc 后建议 SOUL.md/MEMORY.md 只读、审查未授权修改 — [Snyk](https://snyk.io/blog/toxicskills-malicious-ai-agent-skills-clawhub/)；[Nebius](https://nebius.com/blog/posts/openclaw-security)
- 供应链：把 agent skills 当作与 npm/PyPI 同级的软件供应链；`uvx mcp-scan@latest --skills` 审计；为处理凭据的 skill 轮换密钥 — [Snyk](https://snyk.io/blog/toxicskills-malicious-ai-agent-skills-clawhub/)
- 暴露面：CVE-2026-25253 教训——控制面不得从 URL 参数接受目标地址并自动带 token 连接、需校验 WebSocket Origin、loopback 绑定不足以防护浏览器发起的攻击 — [The Hacker News](https://thehackernews.com/2026/02/openclaw-bug-enables-one-click-remote.html)；暴露实例多由用户为远程访问改 bind 地址导致 — [toclawdbot (SOCRadar 转述)](https://toclawdbot.pages.dev/security/port-exposure)
- 防循环与 kill switch：Hermes `gateway.bot_loop_guard.max_hops` 默认 4；`/stop` 硬停止；OpenClaw stop/abort 控制命令绕过 debounce 立即生效 — [Hermes Messaging](https://hermes-agent.nousresearch.com/docs/user-guide/messaging)；[OpenClaw Messages](https://docs.openclaw.ai/concepts/messages)
- 信息泄露面：公共房间关闭 `/reasoning`、`/verbose`、`/trace`；`contextVisibility: allowlist` 限制非受信成员引用内容进入上下文；MEMORY.md 仅在私聊主 session 加载 — [OpenClaw Prompt injection](https://docs.openclaw.ai/gateway/security/prompt-injection)；[OpenClaw Groups](https://docs.openclaw.ai/channels/groups)；[OpenClaw Agent workspace](https://docs.openclaw.ai/concepts/agent-workspace)
- 成本兜底：heartbeat/automation 用便宜或本地模型、降频、`activeHours`/`idleOnly`、`payload.skipIfScratchEmpty`、`NO_REPLY` 无需额外 final-answer 请求 — [OpenClaw Heartbeat migration](https://docs.openclaw.ai/gateway/heartbeat)；[InsiderLLM](https://insiderllm.com/guides/openclaw-token-optimization/)
- OpenClaw 文档含 Rate limiting、Hardened baselines、Running the security audit（`openclaw doctor`/security audit）等独立页面 — [OpenClaw llms.txt 索引](https://docs.openclaw.ai/llms.txt)（未读正文）

### Inferences
- 设计清单（建议默认值）：私聊 pairing + owner 单独授权；群默认需 @、未 @ 只作上下文；群/公开会话一律沙箱且不加载私密记忆；浏览/邮件/社交内容走 reader 子 agent；人格与长期记忆文件写入需审批 + git 版本化 + provenance；skill 安装 pin 版本、禁止远程"fetch and follow"指令；bot 间互聊跳数上限；每日/每月模型花费硬上限与告警；所有工具调用与主动外联写审计日志；全局与每会话 kill switch 绕过队列立即生效。
- 由于"someone messaged the bot and the bot did what they asked"是最常见失败，拟人 agent 越"乐于助人/社交化"，攻击面越大；拟人度与安全应分层：人格层可开放，能力层必须收紧。

### Gaps
- 未找到 OpenClaw/Hermes 官方的"花费上限（spend cap）"配置项一手文档；成本控制目前主要来自社区指南。
- OpenClaw Rate limiting、Hardened baseline、审计日志格式的具体内容未抓取。
- Trail of Bits 针对 OpenClaw/常驻 agent 的报告本轮未检索到。
