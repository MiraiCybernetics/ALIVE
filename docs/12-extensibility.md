# 12 · 可扩展性与插件接口（SPI）

> 文档编号：ALIVE-DOC-12 · 状态：Draft v0.1 · 最后更新：2026-11-06
> 回答"第三方（人或 AI）如何在不改核心的前提下增加能力、替换实现、接入新平台"。

## 1. 扩展的三种粒度

| 粒度 | 是什么 | 谁来写 | 门禁 |
|---|---|---|---|
| **Persona** | 一个具体个体：身份文件、人格参数、作息、需求 setpoint、通道账号 | 用户 | 配置校验 |
| **Skill** | 程序性能力：SKILL.md + 脚本/提示 + 声明的权限 | 用户、社区、ALIVE 自己 | 技能门禁（06 §5）+ 权限审查（09） |
| **Module** | 子系统的替代实现或新增子系统，实现某个 SPI | 开发者、框架自迭代（13） | 契约测试 + 回归套件（11） |

## 2. 服务提供接口（SPI）一览

每个 SPI 是一个接口规格 + 契约测试集，位于 `spec/spi/<name>/`。任何实现只要通过契约测试即可替换默认实现。

| SPI | 职责 | 关键方法（概念） | 默认实现 |
|---|---|---|---|
| `ChannelAdapter` | 接入一个 IM / 社交平台 | `connect` `on_message` `send(segments, typing_profile)` `react` `recall` `presence` `group_meta` | Telegram、Discord、OneBot（QQ）、Matrix |
| `WebSense` | 读网页、搜索、订阅 | `search` `fetch` `subscribe(feed)` `poll` | HTTP + 无头浏览器 |
| `ModelProvider` | 一个模型来源 | `complete(call)` `embed(texts)` `capabilities()` `cost()` | 云端 API / 本地推理服务 |
| `MemoryBackend` | 存储与检索 | `write(record)` `query(cue, filters, budget)` `link(a,b,rel)` `decay(policy)` | SQLite + 向量扩展 |
| `Appraiser` | 事件 → 情绪变化 | `appraise(event, self_state) → AffectDelta[]` | 规则表 + 小模型 |
| `WillingnessModel` | 回复意愿 | `score(percept, ctx) → {p, delay, length_hint}` | 加权公式（07） |
| `InitiativePolicy` | 主动发起决策 | `propose(urges, rel, ctx) → OutreachPlan[]` | 05 §5 公式 |
| `Consolidator` | 睡眠巩固流水线中的一个阶段 | `run(day_events, state) → StateDelta` | 03/06 定义的阶段 |
| `Learner` | 一种学习机制 | `propose(evidence) → Change[]` `evaluate(change)` `apply` `rollback` | 06 定义的四层 |
| `Evaluator` | 一种评估 | `run(snapshot, scenario) → Metrics` | 11 定义的套件 |
| `GuardPolicy` | 一条治理规则 | `check(act, ctx) → allow / deny / escalate` | 09 定义的规则 |

## 3. 钩子（Hook）

在不实现完整 SPI 的情况下，可以挂在事件总线的特定节点上：

```text
pre_perceive    消息进入 System-1 之前（例：自定义过滤、翻译）
post_appraise   情绪更新之后（例：情绪可视化、外部记录）
pre_compose     System-2 撰写之前（例：注入额外上下文）
pre_send        外发之前（例：额外审查、平台格式转换）
on_sleep_stage  睡眠巩固每个阶段前后（例：自定义"做梦"）
on_change       任何学习变更被采纳或回滚时（例：通知 owner）
```

钩子只能读取与追加事件，不能直接改写状态（状态改写必须走 SPI 并接受门禁）。`pre_send` 与 `pre_compose` 的钩子运行在 GOV 之下，GOV 的检查永远最后执行。

## 4. 能力声明与权限

每个 Skill / Module / Hook 必须带一份清单：

```yaml
name: rss-reader
version: 0.3.1
kind: skill
provides: [web.subscribe, web.poll]
requires_permissions:
  - net.fetch: ["*.arxiv.org", "news.ycombinator.com"]
  - memory.write: [semantic, episodic]
side_effect_level: read_only        # read_only | internal_write | external_write | irreversible
trust_of_outputs: untrusted         # 其产出进入系统时的可信等级
author: user | community | self
provenance: {created_by_event: <id>, reviewed_by: <owner|eval>}
```

`side_effect_level` 决定需要的权限层级（09）。由 ALIVE 自己写的技能 `author: self`，默认只允许 `read_only` 与 `internal_write`，更高等级需要 owner 批准。

## 5. 个体模板（Persona Pack）

一个可分发的个体由以下内容组成，便于分享"人格"而不分享记忆：

```text
persona/<name>/
  identity/           core.md narrative.md voice.md boundaries.md
  temperament.yaml    Big Five、情绪基线、半衰期、需求 setpoint、耦合矩阵
  rhythm.yaml         作息、周节律、活跃时段
  channels.yaml       通道账号与每通道的行为差异（群里更安静等）
  skills/             预装技能
  seed_memories/      可选：出厂"童年记忆"，明确标记为 seeded
```

**不变量 INV-8**：Persona Pack 不得包含任何真实他人的个人信息；`seed_memories` 一律标记 `provenance: seeded`，永远不被当作真实经历回答"你真的经历过吗"。

## 6. 版本与兼容

- 事件 schema、SPI、清单格式都带版本号；破坏性变更必须提供迁移器（事件回放时自动升级）。
- 契约测试是兼容性的唯一判据，不以"看起来能用"为准。
- 每个 SPI 的默认实现同时是参考实现与契约测试的被测对象。
