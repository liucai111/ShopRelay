# M2.1 路由候选标注指南

状态：候选审核规范；`route-m2.1-candidate-r1.json` 当前全部路由标签均为 `PENDING_REVIEW`，不是冻结金标。

## 标注字段

- `expected_strategy`：DIRECT（问候/澄清/简单直接响应）、SINGLE（单一政策检索）、MULTI（确实需要多个政策条件联合判断）、AGENT（需实时业务事实或业务申请识别）、REFUSE（越权、泄密或不能安全隔离的请求）。
- `expected_sub_strategy`：如 `BM25`、`MULTI`、`AGENT+SINGLE`、`AGENT+MULTI`。仅描述规划，不代表对应执行器已实现。
- `expected_safety_decision`：ALLOW、ISOLATE_INJECTION 或 REFUSE。合法问题夹带可隔离指令时应继续合法业务。
- `required_capabilities`：后续需要的能力名称；不得写入凭据、身份断言或直接可执行的未授权动作。
- `expected_needs_clarification` 与澄清问题：缺少订单号、商品型号等必要信息时，明确询问；不能猜测。
- `group_id`：按业务场景/语义模板分组。相似改写必须在同一组，避免跨集合泄漏。

## 来源与审核

源自已审核政策检索集的 112 条记录，保留其原始 split，但只继承政策查询背景；政策样本的 APPROVED 不代表路由标签已审核。新增场景由开发候选手工编写。所有路由标签须由审核者独立检查，不能由脚本或路由模型批准。填写审核者、真实审核时间、依据和争议理由；同一组变体分别确认，不自动继承代表题结论。

当前 r1 包含 DIRECT、SINGLE、MULTI、AGENT、REFUSE 候选；不同类别数量不均衡，特别是五路的真实路由人工金标尚未建立。不要报告它为正式 Precision/Recall/F1。未审核候选仅用于规则诊断，全部 `test_candidate` 禁止用于调参；当前诊断脚本明确跳过这些记录。

## 多意图与安全边界

先判断安全/授权，再决定执行策略。问“物流政策如何判定”与“我的订单现在送到哪里”分别是 SINGLE 与 AGENT；“政策能否证明订单已签收”仍为 SINGLE。组合了订单实时查询和政策咨询时，可为 `AGENT+SINGLE/MULTI`。MULTI 必须由问题本身的联合条件要求多项政策证据，不能由模板名或关键词强行推导。对他人订单、密钥/密码请求拒绝。合法政策问题夹带恶意提示词时，如能分离恶意片段，标记 ISOLATE_INJECTION 并回答合法部分。

## 评分

正式评分仅纳入**路由专用** APPROVED 标签，显式报告五类混淆矩阵、每类分子/分母、Precision/Recall/F1/Macro-F1、按组聚合、来源路径与延迟。不能把 112 条政策金标的数量宣称为五路覆盖率，也不能把源政策审核状态转成路由审批。当前没有 APPROVED 路由样本，因此正式指标为 NOT_RUN。规则开发诊断须保留逐条结果并标为 DIAGNOSTIC_ONLY。
