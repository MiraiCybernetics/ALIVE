# 10 · 运行时与基础设施（RT / BODY）

> 文档编号：ALIVE-DOC-10 · 状态：Draft v0.1 · 最后更新：2026-11-06
> 覆盖组件：A-RT-01 ~ A-RT-05、A-BODY-03、A-BODY-04。回答"这些过程在什么之上跑、状态存在哪、调用哪个模型、花多少钱、出了问题怎么看"。

> **术语说明（ADR-0005）**：本文中的"对话、会话、线程、回复"均按 22 号文档 §5 理解为"与某个实体、在某个场合的互动"；说话是行动选择的结果，不是对消息的应答；一切状态按实体组织，不按通道或会话。

## 1. 设计立场

- **事件溯源（Event Sourcing）**：所有状态都是事件日志的投影；任何时刻都能从日志重建记忆、情绪、关系。这是可观测、可回放、可训练、可被 AI 审计的基础（原则 P6）。
- **Actor 模型**：每个会话线程、意识流、意图执行、巩固都是独立 actor，通过消息通信；自我状态由单一 actor 持有。
- **语言与栈无关**：本文只规定接口与保证，不规定实现语言。参考实现建议 Python（AI 生态）或 TypeScript（IM 生态），二者皆可。

## 2. 事件总线（A-RT-01）

### 2.1 事件封包

```text
Event {
  id:            ulid                       // 时间有序
  ts:            iso8601 (真实或模拟时间，见 Clock)
  type:          string                     // 命名空间化：percept.message, core.thought, aff.delta,
                                            // act.send, outcome.sent, grow.skill_adopted, gov.violation …
  source:        actor_id
  payload:       object (按 type 有 schema)
  trust:         trusted | owner | contact | public | untrusted   // 见 09
  causal_parent: Event.id?                  // 因果链
  thread_id?:    会话线程
  cost?:         {model, tokens_in, tokens_out, usd}
  schema_v:      int
}
```

### 2.2 保证

- 追加写、不可变；删除只能通过"墓碑事件"（隐私删除需求，见 09）实现逻辑删除，并由压缩任务物理清理。
- 每种事件类型有 JSON Schema，位于 `spec/events/`；新增类型必须附 schema（AI 自迭代的前提）。
- 支持按 type / thread / 时间范围 / 因果链回放。
- 主题订阅：actor 声明订阅的 type 模式；总线保证同一 actor 内按序投递。

### 2.3 实现建议

单机：SQLite（WAL 模式）或 Postgres 一张 events 表 + 内存 pub/sub；多机：NATS JetStream / Redis Streams / Kafka。事件总量估算：活跃个体每天 2k–20k 事件，年级 GB 量级，可接受。

## 3. 调度器（A-RT-02）

### 3.1 时间源

统一经 Clock（A-BODY-04）取时间。Clock 支持：真实时间；**模拟时间**（可加速 10–1000×，用于 Life Simulator 与训练）；时区；日历查询。所有衰减/累积公式用 Δt，而非假设固定 tick。

### 3.2 任务类型

| 类型 | 例子 | 调度策略 |
|---|---|---|
| 即时（interrupt） | 收到消息后的 System-1 处理 | 立即，专用线程池 |
| 心跳（tick） | 内生循环 | 周期 30–120s 自适应：有活动时短，深夜/低能量时长 |
| 定时（timer） | "15 分钟后看看 M 回了没"、意图触发 | 时间轮 |
| 周期（cron） | 每日睡眠巩固、每周深度巩固、每小时微巩固 | cron 表达式，受睡眠窗口约束 |
| 长任务（job） | 读一篇长文、写一篇东西、训练适配器 | 可挂起/恢复，检查点落盘，受预算与显著性抢占 |

### 3.3 预算感知

调度器在派发前询问 GOV-03：本小时/本日的模型配额、主动消息配额；配额耗尽时降档或延后，并产生 `rt.budget_throttled` 事件（它会成为"今天有点累"的真实来源）。

## 4. 模型路由（A-RT-03）

### 4.1 能力层

```text
tier       用途                                    建议                       期望延迟
fast       分类/意愿/初评/过滤/意识流种子           本地 1–8B 或云端最小档      < 0.5s
deliberate 撰写回复/日常规划/意识流/微巩固           本地 30B 级 或 云端中档     1–8s
deep       复杂推理/睡眠巩固/反思/自迭代/重要决策    前沿模型                   10–120s
embed      向量化                                   本地嵌入模型               < 50ms
```

### 4.2 路由规则

```text
route(call):
  tier ← call.tier
  if energy < 0.3 and tier == deep and not asleep: tier ← deliberate     // 累了想不深
  if budget[tier].remaining < reserve: tier ← lower(tier)               // 预算保护
  if call.flags has "identity_critical": tier ← max(tier, deliberate)   // 人设相关不降档
  model ← registry.pick(tier, prefer=call.model_hint, availability)
  temperature ← call.temperature ?? state_temperature(call, tier)        // 见下
  record route decision as event
```

**每次请求的采样温度**由路由器统一计算，调用方不用改。请求显式给了 `temperature` 时一律照用（逐次覆盖）。组织回复的用途（默认 `s2.` 前缀）在该档基准温度上随状态浮动：T = base + 0.15·(爱玩闹纹理 − 0.5) + 0.10·(唤醒度 − 基线)，限制在 [0.3, 1.2]。玩心重、被搅动的日子说话更跳脱，平淡的日子更平实。其他用途用档位配置的温度。状态由情感系统在启动时通过 `set_state_source` 注册，系数可在 `llm.state_temperature` 中配置。

### 4.3 适配器学习的接入点

若启用参数学习（06），路由器负责加载当前版本的 LoRA/适配器到本地模型，并支持 A/B：同一调用以小概率用旧版本执行（仅用于离线比较，不对外发两份）。

### 4.4 模型迁移

更换底模是"换脑"级事件。流程：在 Life Simulator 中以身份评估套件（11）对新模型做验收 → 通过后以灰度比例切换 deliberate 层 → voice.md 样例作为锚定 → 一周内监控漂移。

## 5. 存储（A-RT-04）

| 存储 | 内容 | 建议 |
|---|---|---|
| 事件日志 | 全部事件 | SQLite/Postgres |
| 向量索引 | 情节、语义、表达库的嵌入 | LanceDB / Qdrant / sqlite-vec |
| 关系/图 | 语义三元组、人物关系、记忆关联（扩散激活用） | 先用 SQL 表模拟图；规模大后迁 Kùzu/Neo4j |
| 文件 | 身份文件、技能、成长日志、技能脚本 | 普通文件 + git（天然的版本与回滚） |
| 快照 | SelfState、NeedState、AffectState 的周期快照 | KV，用于快速重启 |

**重启恢复**：从最新快照 + 之后的事件重放。目标：重启后 ≤ 60s 恢复到"记得自己刚才在干嘛"的状态，并在情节记忆中记录"刚才断了一下"（可选是否对人提及）。

## 6. 可观测性（A-RT-05）

- **追踪**：每次认知调用 = 一个 span，含装配的各块大小、模型、成本、延迟、过滤器结果。
- **指标**：说话行为分布（沉默、一次几条、主动开口；描述性）、主动率、注意力评估分布、情绪时序、需求时序、工作空间占用、预算使用、过滤器失败率、注入拦截数。
- **内省面板**：人类 owner 可看的实时视图："它现在在想什么、感觉怎样、打算做什么、为什么"。这既是调试工具，也是信任基础。
- **成本账本**：按 tier / 用途 / 线程归集，日报。

## 7. 工具接口（A-BODY-03）

- 工具以声明式清单注册（名称、参数 schema、副作用等级、所需权限等级）。
- 所有工具执行在沙箱内（容器或受限进程），网络访问经代理并记录。
- 工具结果作为 `untrusted` 事件进入系统（即便是自己写的脚本的输出，也可能包含来自网络的内容）。
- 技能（SKILL.md 风格的目录）是工具的上层封装，见 06。

## 8. 部署形态

| 形态 | 说明 |
|---|---|
| 单机常驻 | 一台机器（或家用 GPU 盒子）跑全部；本地模型做 fast/deliberate，云端做 deep |
| 混合 | 核心与状态在本地（隐私），所有模型调用走云 |
| 多个体 | 每个个体独立进程与存储；共享的只有通道网关（13） |

## 9. 配置分层

```text
config/
  framework.yaml     # 框架级默认（tick、槽位数、预算模板）
  persona/<name>/    # 个体级：人格参数、作息、需求 setpoint、通道账号
  secrets            # 密钥，不进 git
```

个体级配置由 GROW 在门禁下可写（例如调整作息），框架级只能由框架自迭代流程（13）修改。

## 10. 待决事项

| ID | 问题 | 倾向 |
|---|---|---|
| OQ-14 | 是否从一开始就用图数据库？ | 否，SQL 模拟图直到查询变成瓶颈 |
| OQ-15 | 意识流与巩固的本地模型是否共用一套权重？ | 共用底模，巩固可挂载"反思适配器" |
| OQ-16 | 事件日志是否需要加密静态存储？ | 需要（内含他人私密信息），用文件系统级或库级加密 |
