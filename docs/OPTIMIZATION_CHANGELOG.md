# 选股系统优化变更记录

分支：`codex/stock-system-optimization`  
模型版本：`2.1.0`  
风险政策版本：`2.1.0`  
仿真政策版本：`simulation-1.0.0`

本次修改只完成本地代码、离线测试和文档交付；没有部署、推送、发送飞书消息或连接真实账户下单。

## 问题核查表

| 问题 | 核查结果 | 修复位置 | 状态 |
| --- | --- | --- | --- |
| 缺失因子按剩余权重归一后出现虚高分 | 用 25% 覆盖率合成样例复现 | `scripts/model_v2.py::factor_snapshot`、`evaluate_plan_qualification`；`config/risk_policy.json` | 已修复并离线验证 |
| 必需估值/盈利修正缺失仍可能进入正式路径 | 用高分但缺估值样例复现 | `config/model_v2.json` 的策略必需因子；`scripts/model_v2.py` | 已修复并离线验证 |
| 合法数值 0 被 `or` 当缺失 | 边界样例复现 | `scripts/model_v2.py::first_number` 及导出/雷达传播 | 已修复并离线验证 |
| EOD 研究新鲜度与即时执行新鲜度混用 | 三天旧券商报价样例复现 | `scripts/model_v2.py::assess_price_freshness`；Worker、网页、飞书 | 已修复并跨语言验证 |
| 最大回撤实际是“相对信号价最低收益” | 原函数口径确认 | `scripts/opportunity_review_metrics.py`、`scripts/performance_metrics.py` | 已改名并实现真正峰谷回撤 |
| 横截面 20 日收益离散度被称为组合夏普 | 原聚合口径确认 | `scripts/opportunity_review_metrics.py::build_payload` | 已移除错误命名；无净值时返回 null 与原因 |
| 计划合格、到价、报价有效、组合许可混成一个布尔值 | 数据链确认 | `scripts/model_v2.py`、`scripts/export_public_reports.py`、Worker/飞书 | 已拆分并统一最终合取逻辑 |
| 旧报告只保留每只股票单一结果，无法重建事件 | 数据结构确认 | `scripts/simulation_ledger.py`、`scripts/simulation_engine.py` | 已提供不可变快照、信号和事件账本 |
| 回调未触发也可能被观察收益当作交易收益 | 仿真假设缺失 | `scripts/simulation_engine.py` | 已实现下一交易时段、TIF、未成交无损益 |
| 目标价可能只是历史高点加价 | 规则确认 | `scripts/strategy_rules.py` | 新策略要求目标方法、证据和日期；旧策略保留作对照 |
| 公开候选默认拥有组合许可 | 隐私/权限语义确认 | `scripts/portfolio_risk.py`、公开导出 | 改为 `pending_local_review`，必须本地私有复核 |
| 真实策略是否有效、是否优于基准 | 当前没有足够冻结后成熟样本 | 前向仿真接口及验证文档 | 待真实数据和前向样本确认 |
| GitHub Actions 跨日 SQLite 是否有耐久私有存储 | 仓库未配置私有对象存储/制品恢复通道 | `docs/MIGRATION_AND_ROLLBACK.md` | 外部基础设施待配置；未伪装为已完成 |

## P0：确定性错误与公共契约

- `factor_snapshot()` 输出因子原值、覆盖权重、必需因子、缺失原因、质量维度、评分完整度和版本。覆盖率低于暂定 0.80 时只用于研究，不产生正式资格。
- 行情输出拆为 `research_data_valid`、`execution_quote_valid`、`execution_allowed`、`quote_age_seconds`、`market_session`、`reason_codes`。实现美股周末、主要假日、提前收盘和 DST 处理。
- `maximum_drawdown()` 使用净值运行峰值；MAE/MFE 标识 `daily_high_low` 或 `daily_close_proxy`；夏普与信息比率没有有效时间序列时返回 null 和原因。
- 新增 `signal_id`、`plan_id`、`strategy_id`、`policy_version`、`model_version`、`data_snapshot_id`。报告生成时的静态执行状态不再阻止 Worker 用新鲜实时报价重新判定。

## P1：模拟交易与绩效

- SQLite WAL 账本保存内容哈希快照、不可变信号、幂等状态事件、成交和组合净值。
- 仿真支持回调限价、突破触发、下一交易时段、订单有效期、共享现金、整股、成交量参与率、手续费、点差、滑点、停牌、拆股、股息和退市。
- 同根同时触发止盈止损按止损优先并记录 `intrabar_ambiguous`；跳空止损按开盘可成交近似加成本。
- 状态包含 `observed / qualified / waiting / order_pending / filled / partially_exited / closed / expired / rejected`；分批止盈为显式可选参数。
- 输出收益、回撤、波动、夏普、信息比率、交易统计、持有期、换手、现金利用率、最大总暴露和基准可用性。样本不足绝不强行年化。

## P2：可验证规则与时点纪律

- `scripts/strategy_rules.py` 提供分离的回调/突破构造器、ATR 检查、成本后 R/R 和目标证据要求。
- 行业解析不再把未知公司默认为工业；存储、芯片设计、半导体设备、软件平台、工业自动化分开。
- `scripts/point_in_time.py` 保存期间、发布时间、可用时间、摄取时间、来源、版本和哈希；日期级 SEC 数据采用保守次日可用规则。
- FY 预测期切换不再算作盈利修正；walk-forward 分割剔除跨边界标签并支持 embargo。

## P3：组合风险与使用界面

- 风险数量取允许损失、现金、单票上限的最小值，并检查主题、行业、总止损风险、财报、相关性未知和组合回撤。
- 默认阈值集中于 `config/simulation_policy.json`，全部标为 provisional，未宣称经过历史优化。
- 股票卡显示计划资格、当前执行、因子覆盖、缺失字段、研究/执行行情状态、组合复核、订单类型、目标依据、成本后 R/R 和版本。
- `crowding_score` 在前端明确显示为“价格过热代理”，不冒充真实持仓拥挤度。

## 兼容性

- 保留旧字段并增加新字段；旧 `reports.json` 与新 `index.json` 加载方式不变。
- 旧 `max_drawdown` 消费者仍能读取数值，但新输出同时提供真实定义及旧口径字段 `min_close_return_from_signal`。
- 旧候选缺少因子覆盖、组合许可或研究有效性时会安全降级为研究展示/待复核，不会默认放行。
- 旧策略不被历史回写；新策略和新信号由版本字段区分。

