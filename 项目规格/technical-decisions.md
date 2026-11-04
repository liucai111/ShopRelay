# 技术选型与运行约束

状态：规划基线。实际安装后把依赖精确版本、模型 revision、运行设备和性能测量写进锁文件与 `docs/runtime-baseline.md`；不能把这里的候选当成已验证环境。

## 确定的组件

| 职责 | 默认选型 | 取舍和故障方式 |
|---|---|---|
| 生成/路由/总结 | DeepSeek API，模型名配置，默认 `deepseek-flash` | provider 封装结构化输出、用量、超时；运行失败显式报错/转人工，不以假模型伪装线上成功 |
| 后端/工作流 | Python + FastAPI + LangGraph | Graph 只管可恢复的状态流；数据库事务由业务服务负责 |
| 中文稀疏检索 | jieba 分词 + BM25 | 当前在线默认；常规政策问题先用低成本稀疏检索，词典/停用词版本化 |
| 中文向量 | `BAAI/bge-small-zh-v1.5` | 当前仅按需 HYBRID_RERANK 使用；模型不可用时显式记录 BM25 降级 |
| 精排 | `BAAI/bge-reranker-base` | 当前仅按需 HYBRID_RERANK 使用；候选级 Cross-Encoder，不把词面 overlap 称为精排模型 |
| 高质量对照 | `BAAI/bge-m3` + `BAAI/bge-reranker-v2-m3` | 作为额外实验配置；M3 官方模型文档标称约 2.27 GB，先测机器加载/延迟再决定是否作为默认 |
| 向量索引 | Qdrant Python client 本地持久化路径；部署用 Qdrant 服务 | 同一抽象适配两种模式；不向公网映射未认证端口；索引与政策版本一起切换 |
| 业务数据库 | 开发 SQLite，部署 PostgreSQL + Alembic 迁移 | 使用唯一约束、条件更新与事务；并发确认以 PostgreSQL 集成测试验收 |
| 前端 | React + TypeScript | 复用参考项目的技术思路；页面范围仅对话、引用、轨迹、确认和状态 |
| MCP | 官方 Python SDK 2.x，目标协议 `2026-07-28`，候选锁定 `mcp==2.2.0` | 使用 v2 `Client`；不得混用 v1 `ClientSession.initialize()` 教程；旧 `2025-11-25` 连接做兼容测试，不承诺更早版本全兼容 |

选择轻量 BGE 作为默认是运行环境未知时的稳妥基线，不等于它一定优于 M3。**经用户于 2026-10-19 明确批准，M0 强制门槛以 BGE-small + bge-reranker-base 轻量链路的固定版本加载、复载及检索验证为准；BGE-M3 (`5617a9f61b028005a4858fdac845db406aefb181`) 与 bge-reranker-v2-m3 (`953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e`) 的下载、加载、同池质量/资源对照延期到独立的资源合格实验。**延期项保持 BLOCKED/DEFERRED 的事实记录，不视作 PASS；不得删除半成品缓存或改动固定 revision。若实验后决定切换默认，须另作记录在案的设计决定，不偷偷替换评测模型。向量维度与 collection 名随模型配置生成，不混存不同维度。

## M2-R 在线政策检索决策（2026-10-20）

- 在线主策略只有 `BM25` 与按需 `HYBRID_RERANK`。BM25 是默认；多政策方面显式触发增强；单方面 BM25 无候选时允许一次有界增强尝试。不得使用未经校准的统一 BM25 原始分数阈值。
- `HYBRID_RERANK` 复用已有 BM25、BGE-small、RRF 与 bge-reranker-base 实现/缓存。向量、RRF 等旧模式仍供历史评测及兼容读取，但不成为额外在线策略分支。
- 在线 MULTI 使用独立 `policy_aspect_planning` 结构化节点，只输出请求模式、锚定方面和用户明示事实；政策规则仅从本轮有效政策证据解释。每个方面独立检索、版本/时效核验、去重，之后进行一次有界答案生成，不运行历史 Self-RAG Grade/replan 循环。
- 单 Run 请求数、Token、费用、检索查询及墙钟限制继续执行；真实凭据不进入 GraphState、Trace 或日志。模型请求的 SDK 自动重试为 0。
- 旧 `MultiHopRetrievalService`、Grade、三跳测试及历史 Run 保留用于兼容/研究回放，但从 legacy 与 LangGraph 的在线 SINGLE/MULTI 入口退出，不作隐式失败回退。
- 新方案的工程正确性与 M2 质量放行分开判定。已有 48 条检索比较及答案质量/人工审核限制原样保留；本次架构采纳不构成质量阈值豁免或 M3 放行。

## 生命周期和预算

启动只建立连接、加载已构建索引与 Graph；知识入库、数据库迁移和 seed 是显式命令。模型预热和依赖检查分别报告 `ready/degraded/unavailable`，API 存活不等于具备检索能力。运行参数由 `.env.example` 给出，密钥只保留在服务端。

默认单轮预算上限建议：`MAX_MODEL_REQUESTS=12`、`MAX_RETRIEVAL_HOPS=3`、`MAX_RETRIEVAL_QUERIES=6`（每跳最多 2 条不同查询）、`MAX_DOMAIN_TOOL_CALLS=8`、`MAX_AGENT_HANDOFFS=4`。领域工具预算计入订单、商品、售后、工单等业务调用，也计入主控显式调用一次 `search_knowledge`；该工具内部的 BM25/向量查询和三跳改写仅计入检索预算，不重复计入领域工具预算。直接由政策 Agent 发起的检索同样只计入检索预算。重试计入对应预算；同时设置单轮墙钟时限和 Token 上限，由 M0 实测后固定配置，避免预算拆分后出现无界流程。以输入/输出 Token 的实际 usage 与价格快照估算费用。配置 `MAX_ESTIMATED_COST_USD_PER_CHAT=0.10`、`MAX_EVAL_COST_USD=20` 作为初始保护值，M0 用真实样本校准；预算触发时返回部分已证实信息与人工选项，不假装任务成功。费用估算不是供应商账单保证值，未知用量单独记 `unknown`。

DeepSeek 的推理模式按工作流节点显式配置，不继承服务端默认值。初始待测配置：路由、简单参数解析与记忆压缩用 non-thinking；复杂规划和证据充分性判断用 thinking；答案生成按简单/复合任务分别试验。M0 用同一冻结样本比较准确率、P95 延迟和费用后锁定每类节点的模型、`thinking.type` 与可用的 `reasoning_effort`；以后消融须保持这些设置一致。每次模型调用记录节点名、实际模型名、thinking 模式、effort、usage、延迟和配置版本；不记录私有推理内容。thinking 模式下不能假定 temperature 等采样参数生效，工具调用还需验证 `reasoning_content` 的接口处理。

## 首次实施的验证门槛

1. DeepSeek：实际调用模型，分别验证 thinking/non-thinking 的 JSON schema、工具调用、流式事件、usage 字段和错误路径；记录各节点锁定配置、可用模型名与价格访问日期。
2. BGE：M0 强制验证轻量配置（BGE-small 与 bge-reranker-base）的加载、批量向量化/精排及重启复载；高质量 M3 配置已按用户批准延期，不属于本次 M0 放行阻塞，仍须在独立实验完成其加载、对照及资源测量后才能声称已验证。
3. Qdrant：本地持久化重启不丢数据，部署服务端与同一检索接口结果一致；删除文档后无旧 point。
4. MCP：用锁定 SDK 连接 stdio 与 Streamable HTTP 两个独立服务端，并对 `2025-11-25` 服务端进行一次显式兼容用例。
5. 数据库：PostgreSQL 上执行并发确认、幂等键冲突、事务回滚测试；SQLite 只作为开发便捷模式。

## M0 / M6 验收边界（2026-10-19）

- M0 主里程碑退出条件以《完整交付方案》第 10 节 M0 行为准：可重复 seed/ingest、首批不可变原始评测记录、单 Agent + BM25 政策问答纵向链路跑通。
- M0 技术验证还需满足本文件“首次实施的验证门槛”中未延期项目以及 `docs/probes/m0-acceptance-closeout-20261019.json` 与后续版本化放行矩阵列出的证据。M3 两种大型模型对照按上述用户批准例外延期；不得将诊断探针描述为尚未实现的生产 Router/Planner。
- 冻结集的隔日盲审、冻结、最终指标和 M6 故障注入/消融/部署验收，属于正式测试集与 M6 独立验收流程，不是 M1 API/会话开发的前置条件。approved dev/validation 上的 M0 预备检索记录不能替代 M6 冻结集结果。测试集必须在开始任何 test_candidate 评分前完成隔日独立盲审与冻结。
- M0 当前 DeepSeek 基线只执行一个受限 `policy_answer` 请求；跨节点请求数、检索跳数、工具数、总 Token 和聊天费用预算账本，须在 M1/M2 的多节点状态流启用前实现并验证，不能把 M0 单次请求记录表述为已完成全聊天硬预算拦截。

官方依据：[DeepSeek 模型和价格](https://api-docs.deepseek.com/quick_start/pricing/)、[DeepSeek Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/)、[BGE-M3 模型说明](https://github.com/FlagOpen/FlagEmbedding/blob/master/docs/source/bge/bge_m3.rst)、[BGE 模型列表](https://github.com/FlagOpen/FlagEmbedding/blob/master/README_zh.md)、[Qdrant 本地模式](https://qdrant.tech/documentation/tutorials-basics/generate-embeddings-fastembed/)、[MCP Python SDK 发布](https://github.com/modelcontextprotocol/python-sdk/releases)与[迁移说明](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/whats-new.md)。
