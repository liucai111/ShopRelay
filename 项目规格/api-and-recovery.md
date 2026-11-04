# API、SSE、确认事务与故障恢复契约

状态：接口设计基线。实现前可调整字段名，但要同步 OpenAPI、前端类型和端到端测试。所有时间为 ISO 8601 UTC；所有响应带 `trace_id`。用户身份取自服务端认证上下文，任何请求体 `user_id` 都不构成授权。

## 主要接口

| 方法/路径 | 请求要点 | 成功响应要点 | 访问约束 |
|---|---|---|---|
| `POST /api/demo/login` | 演示账号与独立口令 | 服务端会话 Cookie、用户显示名 | 本地演示；公开部署换正式认证 |
| `POST /api/chat` | `session_id?`, `message`, `client_request_id` | `run_id,session_id,answer,citations,strategy,sub_strategy,pending_action?,trace_id` | 会话属主 |
| `POST /api/chat/stream` | 同上 | SSE 事件；断线后可用 `run_id` 查询 | 会话属主 |
| `GET /api/runs/{run_id}` | 无 | `status,answer?,error?,events_cursor,trace_id` | run 属主 |
| `GET /api/runs/{run_id}/events` | `after_seq?` 或 `Last-Event-ID` | 最多100条持久化事件及下一 cursor | run 属主 |
| `GET /api/sessions/{id}` | 无 | 消息、摘要版本、近期 run 与待确认操作 | 会话属主 |
| `GET /api/actions/{id}` | 无 | 草案、风险、过期时间、状态 | action 属主 |
| `POST /api/actions/{id}/confirm` | `idempotency_key`, `decision=approve/reject`, `expected_fingerprint` | `action_status,resource_id?,run_id` | action 属主；确认内容精确匹配 |
| `GET /api/orders/{id}`、`GET /api/returns/{id}`、`GET /api/refunds/{id}`、`GET /api/tickets/{id}` | 无 | 当前业务状态及更新时间 | 对应资源属主 |
| `GET /health/live`、`GET /health/ready` | 无 | 进程存活；DB/模型/检索/MCP 能力状态 | ready 不泄露凭据 |

聊天接口只允许服务端从认证 Cookie/令牌绑定用户；`session_id` 必须检查 owner。消息长度、引用数、top-k、上传内容和请求频率都有限额；只保留本项目需要的文本输入。管理/入库接口独立身份和权限，不暴露给普通买家。

## SSE 事件协议

每个事件包含 `event_id,run_id,session_id,trace_id,seq,timestamp,type,payload`。`seq` 对单个 run 单调递增，`event_id` 可供重连去重。类型为 `started`、`route_decided`、`agent_started`、`retrieval_hop`、`tool_started`、`tool_finished`、`action_pending`、`answer`、`done`、`error`。`retrieval_hop` 可显示第几跳及证据数量，不显示模型私有推理；`tool_finished` 仅显示脱敏摘要。最终 `answer` 在事实/引用校验后发送，`done` 表示最终状态已持久化。

客户端断线时服务端继续或按执行器策略终止，但最终 run 状态必须落库；前端用 `GET /api/runs/{run_id}` 获取最终结果和已持久化事件，不依赖 SSE 恰好送达。不能把“前端收到 token”当成业务写入成功的依据。同一 `client_request_id` 重试聊天要返回既有 run，不创建重复草案/工单。

M1.3 实现说明：普通 JSON 与 POST SSE 使用同一已 claim 的执行函数；SSE 仅订阅持久化事件。当前开发执行器由 FastAPI 应用管理的有界 worker pool 承载，120秒 lease 用于区分活跃与遗留任务；lease 丢失只归档为 `UNKNOWN_RESULT` 并生成 error 事件，不自动重放外部模型调用。该能力不等同于跨主机持久任务队列；worker 所在进程硬崩溃时，结果只能保持未知并等待人工/上层处理。SSE 客户端用 fetch 消费 POST 流；重连读取事件 GET，不依赖原生 EventSource 对 POST 的支持。

## 确认、并发与恢复

LangGraph 检查点存放可恢复的 Agent 状态；业务数据库是确认和写操作的事实来源。LangGraph 的 `interrupt` 只用于等待人工决定；恢复后节点可能重新执行，因此任何副作用都发生在独立的幂等业务服务中，节点不依赖“只执行一次”的假设。

`pending_actions` 包含 `action_id,user_id,session_id,tool_name,args_json,args_hash,source_order_version,policy_version,status,expires_at,idempotency_key,resource_id,created_at,updated_at`；MCP 动作还保存内部 `(server_id,tool_name)` 与发现目录版本，对外别名不能充当授权标识。状态为 `DRAFT/PENDING_CONFIRMATION/EXECUTING/SUCCEEDED/FAILED/UNKNOWN/REJECTED/EXPIRED`。确认在数据库事务中通过条件更新（状态仍为 `PENDING_CONFIRMATION` 且未到期）抢占执行权；数据库对 `(user_id,idempotency_key)` 建唯一约束，对开放退款申请按 `order_id` 建不含 `reason` 的业务唯一约束。并发输家读取赢家结果；相同键不同参数返回 `IDEMPOTENCY_CONFLICT`。

执行业务前重新读取订单/库存/政策版本；若与草案不兼容，记录 `FAILED:STATE_CONFLICT` 并要求新草案。执行与幂等记录在同一事务提交；外部远程写入若响应丢失，状态为 `UNKNOWN`，以 action ID 查询远程结果或人工处理，不盲目重放。MCP 写入的风险级别由本地 `(server_id,tool_name)` 策略表判定；服务端的 `readOnlyHint` 等声明只作展示信息，目录变化或别名冲突时须重新授权。重复确认 `SUCCEEDED` 返回原 `resource_id`；`FAILED/EXPIRED/REJECTED` 不自动重新执行。过期清理任务只改变未执行的待确认记录。

## 错误与降级

统一错误体 `{code,message,trace_id,retryable,details?}`；至少区分 `UNAUTHENTICATED,FORBIDDEN,NOT_FOUND,INVALID_ARGUMENT,STATE_CONFLICT,CONFIRMATION_REQUIRED,CONFIRMATION_EXPIRED,IDEMPOTENCY_CONFLICT,MODEL_UNAVAILABLE,RETRIEVAL_UNAVAILABLE,MCP_UNAVAILABLE,BUDGET_EXCEEDED,UNKNOWN_RESULT`。客户端只根据 `code/retryable` 决定提示和重试，不分析自然语言。权限拒绝不可由其他工具兜底；单检索路故障时可降级并标明；两路均失败或模型故障时明确显示不可完成。

只读调用可有限重试，需指数退避与抖动并记录每次尝试；写入调用只有幂等查询后才能决定是否重试。健康检查单列 `llm,bm25,vector,reranker,db,mcp_servers` 的 `ready/degraded/unavailable`。日志、trace 和 SSE 均脱敏，不能暴露密钥、完整身份证/手机号或他人订单内容。

### M2-R 政策检索与 Run 遥测

当前线上 `SINGLE` 默认执行 BM25；`MULTI` 先用一个有界结构化节点识别政策方面，再逐方面执行有限检索（多方面默认使用 HYBRID_RERANK），最后生成一次答案。BM25 无候选时最多对该方面做一次增强尝试。检索查询数、模型请求、Token、估算费用和墙钟共享既有每 Run 限额；每个请求的实际策略、触发原因、配置版本/哈希、知识快照、有效时间、证据 ID、延迟、降级原因和模型 usage 写入 `Run/Trace/SSE`，LangGraph 模式继续写入同一 Checkpoint。缺证时返回有明确原因的澄清状态，不做隐式旧 Self-RAG 回退。

旧 Run 仍按保存的 `execution_version`、策略字段及 `self_rag` 结果读取；历史 Self-RAG Hop/Grade 是历史执行数据，不能与 M2-R 的 aspect 检索记录混为一谈。恢复/幂等重放只读取既有终态和事件，不再请求模型。

## 部署与恢复演练

开发环境：SQLite + Qdrant 本地模式 + 本地 BGE 权重 + 两个演示 MCP 服务端。MCP 的本地 Streamable HTTP 服务默认仅监听 `127.0.0.1`，检查 `Host`/`Origin` 白名单；远程服务经 HTTPS 与认证接入，禁止把用户凭据转发到未信任来源。部署环境：PostgreSQL + Qdrant 服务 + 受控 API/Web 入口；数据库和 Qdrant 不映射公网。启动前运行迁移、索引构建和显式 seed；启动过程不自动覆盖已有业务数据。配置、模型 revision、MCP SDK 精确版本和协议兼容范围写入锁文件与运行报告。备份 PostgreSQL 与知识库版本；从备份恢复后重建索引，并抽样核验引用与待确认操作状态。

必要故障演练：SSE 断线后查 run；确认响应丢失后重试；两个客户端并发确认；确认等待期间订单发货；MCP 服务重启/工具目录变化；通过真实 MCP 协议调用受控模拟写工具，验证未确认拒绝、属主隔离、重复确认只写一次，并让服务端谎称 `readOnlyHint=true` 验证本地风险策略仍要求确认；Embedding 或 reranker 加载失败；三跳全部不足；数据库事务中断。每个演练必须能依据 trace 与数据库记录判断实际业务结果。
