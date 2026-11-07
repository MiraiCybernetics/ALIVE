# 16 · 实现说明（v0.1）

> 文档编号：ALIVE-DOC-16 · 状态：Draft v0.1 · 最后更新：2026-11-06
> 本文说明设计如何落到代码上：技术选型、目录与模块边界、并发模型、性能与兼容策略、开发约定。设计文档 00–15 是"做什么"，本文是"怎么做"。

> **术语说明（ADR-0005）**：本文中的"对话、会话、线程、回复"均按 22 号文档 §5 理解为"与某个实体、在某个场合的互动"；说话是行动选择的结果，不是对消息的应答；一切状态按实体组织，不按通道或会话。

## 1. 技术选型

| 项 | 选择 | 理由 |
|---|---|---|
| 语言 | Python ≥ 3.10（在 3.10 与 3.12 上测试） | AI 生态；Ubuntu 22.04 自带 3.10，兼容面广 |
| 并发 | 单进程 asyncio 事件循环 | 交互负载以 I/O 等待为主（LLM、IM 网络），协程足够；CPU 密集部分放线程池 |
| 存储 | SQLite（WAL）+ FTS5 三元组分词 | 零运维；单写线程组提交；中文全文检索 |
| 向量 | 内存 numpy 矩阵 + 本地哈希向量化（可换 fastembed / OpenAI 兼容接口） | DeepSeek 无向量接口；单个个体的记忆规模下暴力检索毫秒级 |
| HTTP | aiohttp（客户端、服务端、WebSocket） | 一个依赖覆盖 LLM 调用、Telegram、OneBot、内省面板 |
| 配置 | YAML | 人和 AI 都易读写 |
| 依赖 | aiohttp、numpy、pyyaml；可选 fastembed | 依赖最少，部署简单 |

## 2. 目录

```text
alive/
  core/types.py          共享数据类型、事件名 EV
  interfaces.py          子系统契约（ABC）。子系统之间只通过这里声明的方法和事件交互
  kernel.py              内核：运行时服务 + 按工厂构建子系统
  config.py              配置分层加载
  world/                 实体注册表（registry）与合并后的数据迁移（rekey，22 §3.4）
  testing.py             每个接口的内存假实现 + make_test_kernel
  runtime/               clock · store · bus · scheduler · llm · embed · texture（生活纹理）· http（内省面板）
  core/                  认知核心
    mind.py              心跳、睡眠、主动外联、回复与外联的撰写流程
    context.py           INV-1 上下文装配
    workspace.py         全局工作空间
    inner.py             意识流、情绪调节、阅读
    coherence.py         一致性过滤与说话自检
    understanding.py     理解引擎：个人基线、正在聊的话题、情境脚本、深读、推断检验（17）
    dialogue/            对话策略：交际情境、目标与伦理门、话步策略与学习、前瞻、主体性（19–21）
    schemas/zh.yaml      社会情境脚本库
    prompts.py           认知核心的全部提示词
  self_model/            身份文件、自我状态、成长日志、缓变区叙事（08）
  memory/                记忆（03）
  growth/                学习与睡眠流水线（06）
  affect/                情感（04）
  motivation/            需求、意图、主动性（05）
  governance/            治理（09）
  social/                交互层与关系（07）；channels/ 下是 cli、telegram、onebot、sim 适配器
  eval/                  生活模拟器、留出集与运行器、Codex 评审脚本（11）
personas/<name>/         个体模板（12 §5）
config/                  配置样例
tests/                   按子系统分目录；integration/ 为全系统端到端；eval/ 为防泄漏检查
eval_runs/               评测输出
```

每个子系统包在 `__init__.py` 中暴露 `build(kernel) -> 实例`，实例实现 `alive/interfaces.py` 中对应的抽象类。内核先启动运行时服务与生活纹理，再按 `gov → affect → motivation → memory → growth → selfm → social → mind` 的顺序启动子系统，逆序停止。

## 3. 并发模型

| 关注点 | 做法 |
|---|---|
| 多个会话同时进行 | 每个会话线程一个协程 actor，自带缓冲与防抖；彼此不阻塞 |
| 一致的自我（INV-3） | 自我状态由 self_model 单一写入；认知调用读取快照 |
| 深思并发上限 | 模型路由按档位的信号量：快速档 8、中档 4、深思档 2（可配置） |
| 外部限流 | 全局令牌桶限制每秒请求数；429/5xx/超时指数退避重试；连续失败熔断 |
| 数据库 | 单写线程组提交（2 ms 窗口、每批 ≤ 256 条，每条写入用保存点隔离失败）；读走 4 个读连接线程池 |
| 事件分发 | 每个订阅者独立有界队列与工作协程；慢订阅者丢弃最旧的未投递事件，不阻塞发布者（事件已落盘，不丢日志） |
| CPU 密集 | 向量检索、批量向量化在线程池执行（numpy 释放 GIL） |
| 后台任务 | 全部经调度器命名、受监督；崩溃记录为 rt.error，可配置自动重启与退避 |
| 时间 | 一切经 Clock：真实时钟、加速时钟（模拟器）、手动时钟（单元测试，离散事件推进） |

## 4. 性能策略

- **提示缓存**：INV-1 的固定前缀（宪法、身份摘要、行为手册全局条目）在每次调用中保持字节级稳定，可变内容放在后面。DeepSeek 等服务对相同前缀自动缓存，命中部分价格约为未命中的五分之一。
- **分层算力**：插话判断先走零成本规则与统计门控；快速档关闭推理（实测约 0.7 秒）；只有决定回复才走中档。
- **批处理与防抖**：记忆抽取、表达学习按会话防抖批量执行；数据库组提交。
- **预算**：治理层的路由钩子按每日、每小时预算降档或拒绝；成本账本按档位与用途归集。

## 5. 兼容性

- Python 3.10–3.12；不使用 3.11 才有的 TaskGroup、tomllib。
- SQLite ≥ 3.34（FTS5 三元组分词）。Ubuntu 22.04 自带 3.37。
- 任何 OpenAI 兼容的 chat/completions 服务都可以作为模型来源：配置 base_url、api_key 与各档模型名即可。推理强度与关闭推理的参数对不支持的服务会被忽略。
- IM：Telegram Bot API；OneBot v11 正向 WebSocket（NapCat、Lagrange 等 QQ 实现）；命令行；模拟通道。
- 数据目录必须位于 Linux 原生文件系统。WSL 下不要放在 /mnt/c 或 /mnt/d，否则 SQLite 的 WAL 锁不可靠。

## 6. 开发约定

1. 子系统之间只通过 `kernel.<名字>` 上的接口方法和总线事件交互，不导入别的子系统的内部模块。
2. 不在事件循环中做阻塞 I/O。数据库走 `kernel.db`，网络走 aiohttp，时间走 `kernel.clock`。
3. 每个包用 `register_schema("<包名>", 版本, [DDL...])` 注册自己的表，表名以包前缀开头（mem_、grow_、aff_、mot_、gov_、soc_、self_）。
4. 默认参数写在使用它的代码旁边，并允许从 `kernel.cfg.section("<包名>")` 覆盖；人格相关参数来自 `personas/<name>/`。
5. 提示词集中放在各包的 `prompts.py`，便于迭代和 AI 修改；默认中文。
6. LLM 调用一律带 `purpose`（如 `mem.extract`），走 `kernel.llm.json(...)` 或 `kernel.llm.complete(...)`。
7. 单元测试用 `alive.testing.make_test_kernel(tmp_path, real=[...])`：被测子系统用真实实现，其余用假实现；模型用 `MockProvider` 按 purpose 前缀返回固定结果；时间用 `ManualClock`。调用真实模型的测试标记 `@pytest.mark.live`。
8. 运行测试：

```bash
# 在 Windows Git Bash 中
MSYS_NO_PATHCONV=1 wsl.exe -d Ubuntu-22.04 -- bash /mnt/d/Projects/Gits/ALIVE/scripts/wsl-run.sh python -m pytest -q
# 在 Linux 中
scripts/wsl-run.sh python -m pytest -q -m "not live"
scripts/wsl-run.sh py310 python -m pytest -q -m "not live"   # 兼容性
```

## 7. 运行

```bash
# Linux（或 WSL）中，仓库根目录
cp config/alive.example.yaml config/alive.yaml          # 按需修改：通道、owner、预算
scripts/wsl-run.sh python -m alive check -c config/alive.yaml   # 检查模型连通与存储
scripts/wsl-run.sh python -m alive chat  -c config/alive.yaml   # 在终端里和个体聊天
scripts/wsl-run.sh python -m alive run   -c config/alive.yaml   # 常驻运行（通道来自配置），内省面板 http://127.0.0.1:8765
scripts/wsl-run.sh python -m alive sim --days 1 --scale 60      # 加速生活模拟并输出报告
```

留出评估（ADR-0004）：

```bash
# 生成（Linux）
scripts/wsl-run.sh python -m alive.eval.heldout --judge none --out eval_runs/run.json
scripts/wsl-run.sh python -m alive.eval.heldout --set alive/eval/heldout_zh_v2.yaml --judge none --out eval_runs/run2.json
# 评审（安装了 Codex CLI 的机器，这里是 Windows）
python alive/eval/judge_codex.py eval_runs/run.json --parallel 4
```

## 8. 相对设计的调整

实现中对设计的调整都记录在 ADR 中，见 `docs/adr/`，主要是 ADR-0002 至 ADR-0007。

- ADR-0005 / 22 号文档：以实体为中心、以行动为中心。`alive/world/` 是实体注册表；`alive/world/rekey.py` 负责合并实体时的数据迁移，各子系统的 `rekey_entity` 负责切换内存缓存；所有不由新消息触发的开口都走 `Mind.speak`。
- ADR-0006 / 05 §5.6：行为不靠配额。`alive/core/initiative.py` 用依赖状态的随机过程决定何时主动、一次几条、何时让出话头；`Mind._autonomy_go` 是自主行动的风险率。治理只保留安全与合规底线和故障保险丝。
- ADR-0007 / 07 §3：像人的随机性。
  - 共用原语在 `alive/runtime/chance.py`：对数正态、计数分布、softmax、Gumbel 采样、每日抽样、时间重标度的 `HazardClock`。
  - "什么时候看"由 `alive/social/air.py` 决定，"看了以后怎么对待"由 `alive/social/decision.py` 决定，所有空间共用一个模型。
  - 防抖与打字节奏按发言者节奏、按天抽样。
  - 隐私按信息来源的受众过滤（`alive/world/audience.py`，INV-34）。
  - 对话状态按人存储（`PersonDialogue`）。
  - 作息每晚抽样。
  - 测试默认注入 `MedianRandom`：伯努利走概率大的一边，时长取中位数，时间重标度事件在中位时间发生；检验随机性本身时用固定种子的真实随机数。
- 评测：`alive/eval/heldout.py --repeat N` 做重复采样；`alive/eval/judge_codex.py` 调用 Windows 本机 Codex 评审；留出集 v1–v3 见 `alive/eval/heldout_zh_v*.yaml`，v3 由 Codex 盲写；模拟器里的人会拆句、不逐条回、会主动开口（11 §4.4）。

## 9. 已知缺口（v0.1）

- 面对他人观点时的主体性曾长期偏弱；21 §2.5 的"感受与判断分层"之后的效果见 `docs/eval-log.md`。
- 隐私按来源受众过滤已覆盖记忆、关系事实、跨场合上下文、工作区、意图与梦（INV-34）；梦的种子（kv）还没有受众标注。
- 主动倾向过程的参数（λ0、τ 等）还是先验值，尚未由成长系统根据接受度在线校准。
- 一句话没说完的判断看句末形式（逗号、连接词、单独的 @）；等多久按这个人的拆句停顿抽样（PersonBaseline.gap_median）。
- 读空气的先验来自公开研究与推断。中文场景下的回复中位数、拆句频率、群里被 @ 后的回复率都没有找到可靠数据，需要用个体自己的日志重新拟合。
- 表达库的"至少见过 2 次"仍是一个固定的学习门槛。
- 默认的哈希向量化语义较弱；安装 fastembed 后可切换。
- Telegram 只支持长轮询；OneBot 只支持正向 WebSocket、不支持表情回应。
- 参数层学习只导出训练样本，不实际训练。
- 未成年人识别与群聊披露策略尚未实现。
