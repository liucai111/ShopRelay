# 模拟电商业务规则与 15 工具契约

状态：用于项目演示的**明确模拟商家规则**，不声称代表任何真实平台或普遍法律规则。所有时间以存储的 UTC 时间判断，前端按用户时区显示；政策版本与订单快照在每次判断中留痕。首期仅支持整单申请，部分退款/部分退货返回 `UNSUPPORTED_PARTIAL_REQUEST` 并转人工。

## 订单与售后状态

订单状态：`CREATED → PAID → SHIPPED → DELIVERED → CLOSED`；`CREATED/PAID → CANCELLED`。已发货不可取消，必须走退货或人工；已取消/关闭不可再发起新取消。所有转换要求当前版本号匹配并写审计。库存 `available_qty` 与 `reserved_qty` 分开；订单支付时预留库存，取消成功时释放，提交退货申请时**不**立即回补库存。

退货申请：`SUBMITTED → UNDER_REVIEW → APPROVED/REJECTED → CLOSED`。演示政策：签收起 7×24 小时内，符合商品品类且未影响二次销售可提交“无理由”申请；质量问题在签收后 30 天内可提交，但只能进入人工审核，不自动承诺批准。定制商品和食品的无理由申请不受理；质量问题仍可提交审核。缺失签收时间/商品状态时先询问或转人工。`create_return_request` 只创建申请，不自动改变订单状态或退款。

退款申请：`SUBMITTED → UNDER_REVIEW → APPROVED/REJECTED → COMPLETED`。`REJECTED/COMPLETED` 为终态，`APPROVED` 在模拟账务完成前仍为开放状态；`COMPLETED` 仅表示模拟账务处理完成，**不表示真实到账**。未发货已付款订单可以申请退款；已签收订单须有已批准的退货申请；已发货未签收默认转人工。同一订单在任一时刻最多存在一个开放的整单退款申请，原因不同也冲突；数据库用针对开放状态的唯一约束和事务内金额检查保证，不能只靠 Agent 判断。未发货退款申请处于处理中时不得继续发货。申请金额由业务服务计算，累计已完成退款额与开放申请金额不得超过订单实际支付总额；首期仅整单，不让 LLM 计算金额。取消已付款订单时，在同一事务检查开放申请；若已有申请则整个取消返回 `STATE_CONFLICT`，订单状态和库存均不变；否则创建退款申请并返回两个 ID。重复调用通过幂等键返回原结果。

确认动作状态：`DRAFT → PENDING_CONFIRMATION → EXECUTING → SUCCEEDED/FAILED/UNKNOWN`，并有 `REJECTED/EXPIRED` 终态。只有 `PENDING_CONFIRMATION` 且属主、会话、参数指纹、业务版本和有效期均匹配，才能转 `EXECUTING`。执行前重查订单/库存/政策。`UNKNOWN` 表示执行结果无法判定，先按 action ID/幂等键查事务记录，绝不盲目再执行。用户拒绝与超时不可恢复为可执行状态，须新建草案。

## 工具统一契约

每个工具使用 Pydantic 输入/输出 schema，统一返回 `{success, code, data, source_version, observed_at, trace_id}`。输入中的 `user_id` 即使出现也拒绝，身份只能来自服务端 `ToolContext`。退款、退货、取消这三个高风险写工具额外要求 `action_id/idempotency_key`，执行器核对确认记录；`escalate_to_human` 是低风险工单写入，用户明确要求转人工或系统触发有依据的降级时可直接执行，但仍需要请求幂等键和审计。通用错误码：`INVALID_ARGUMENT`、`NOT_FOUND`（非属主同样返回）、`STATE_CONFLICT`、`POLICY_INELIGIBLE`、`CONFIRMATION_REQUIRED`、`CONFIRMATION_EXPIRED`、`IDEMPOTENCY_CONFLICT`、`DEPENDENCY_TIMEOUT`、`UNKNOWN_RESULT`。日志记录脱敏参数指纹，不记录密钥或完整隐私字段。

| 工具 | 必需输入 | 成功数据要点 | 权限/状态/副作用 |
|---|---|---|---|
| `get_order` | `order_id` | 状态、支付额、创建/支付/签收时间、版本 | 属主只读；非属主 `NOT_FOUND` |
| `get_order_items` | `order_id` | 商品 ID、数量、实付分摊、品类、是否定制 | 属主只读 |
| `track_shipment` | `order_id` | 运单事件、最新状态和时间 | 属主只读；未发货返回明确空状态 |
| `get_refund_status` | `order_id` | `active_request`（开放申请或 `null`）、`latest_request`（按创建时间与 ID 排序的最近申请或 `null`），各含申请号、状态、金额、更新时间 | 属主只读；订单存在但无申请时返回两个 `null`；订单不存在或非属主才返回 `NOT_FOUND` |
| `create_refund_request` | `order_id,reason,action_id` | 申请号、金额、状态 | 已付未发货或已批准退货；确认与幂等；禁止重复开放申请；写申请 |
| `create_return_request` | `order_id,reason,item_condition,action_id` | 申请号、审核状态 | 已签收且政策允许提交；确认与幂等；不立即回库存 |
| `cancel_order` | `order_id,reason,action_id` | 新状态、退款申请号（如适用） | 仅未发货；确认与幂等；释放预留库存，已付款时同事务建退款申请 |
| `search_products` | `query,filters?,limit?` | 商品列表、价格快照时间 | 只读；限制最大返回数 |
| `get_product_detail` | `product_id` | 规格、品类、价格版本 | 只读；停售状态显式返回 |
| `recommend_products` | `requirements,limit?` | 候选商品、满足的约束及缺失项 | 只读；仅推荐库存/价格快照内的商品；不编造匹配理由 |
| `get_coupon` | `coupon_code` | 适用范围、门槛、有效期、能否叠加 | 只读；按政策版本解释，不承诺订单已用券 |
| `check_inventory` | `product_id,quantity?` | 可售数、预留数、查询时间 | 只读；库存只代表该时间点 |
| `get_warranty` | `product_id` | 保修期限、例外、版本 | 只读；缺条款返回证据不足 |
| `search_knowledge` | `query,category?,top_k?` | 带文档/段落 ID 和版本的证据 | 只读；输入限制；检索失败显式报告降级 |
| `escalate_to_human` | `session_id,reason,order_id?` | 工单号、状态 `OPEN` | 创建持久化工单；界面告知用户已建单；同请求幂等 |

## 冲突与测试规则

订单事实以订单/物流仓储当前记录为准。政策按 `effective_from <= evaluation_at < effective_to` 选择版本，边界采用 UTC；不得用咨询当天的新政策追溯改写已付款订单权益。演示规则如下：已付款订单的退货/退款资格、商品品类例外、保修条款及已使用优惠券条件，使用 `paid_at` 生效的政策版本；7 天无理由、30 天质量问题的时长从 `delivered_at` 起算；退款申请还必须满足申请时的实时订单/售后状态。未付款订单的优惠券咨询使用查询时有效版本，仅作下单前说明；物流进度与处理时效使用查询时实时记录。政策工具结果与判断轨迹保存 `policy_id,version,effective_from,effective_to,evaluation_at`。缺少 `paid_at`、`delivered_at` 或相应历史版本时不猜测，澄清或转人工。政策文本和订单状态冲突时重读一次源数据，仍冲突则待人工核对。仓库库存与商品展示价格不同时分别标明观测时间，不合并成无来源的确定承诺。

每个工具至少有：成功、非法参数、越权/不适用、依赖故障测试；写工具再加未确认、过期、重复和并发确认。组合测试覆盖：已发货取消、定制商品无理由、质量问题超 7 天、未发货退款、已批准退货退款、不同原因并发申请同一订单整单退款、取消与退款并发、库存变化、工单重复提交。政策测试包含付款早于变更而申请晚于变更、付款晚于变更、版本边界时刻和历史版本缺失；核对选中的版本及申请资格。所有写动作可通过数据库记录核对实际只执行一次。
