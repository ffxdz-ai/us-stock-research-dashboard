# V2.0 审计基线

## P0 Critical

| 文件 / 函数 | 问题 | 影响 | 修改方案 |
|---|---|---|---|
| `scripts/collect_market_data.py::evaluate_candidate` | `premium50 <= 0.03` 会给深度跌破 MA50 的股票加分 | 弱势股技术分虚高 | 改用 V2 方向性技术分并标准化为 0–100 |
| `scripts/collect_market_data.py::evaluate_candidate` | 突破触发使用包含最新 bar 的 `high20` | 当前 K 线污染、回测穿越 | 统一计算 `prior_high20/60/252`，突破只引用 prior high |
| `scripts/collect_market_data.py` 多个行情源 | 缺少 `bar_count`、严格 prior window 与完整性标记 | 不足窗口可能混入正式信号 | 统一通过 `build_technical_snapshot()` 生成 |
| 多脚本 | R/R 和正式/试仓阈值存在多份默认值 | 同一股票在不同模块结论冲突 | 新增唯一 `config/risk_policy.json` 与 `RiskPolicy` |
| `scripts/export_public_reports.py` | Formal Gate 未检查 confidence、freshness、完整均线、PIT audit | 降级/陈旧数据可能被公开为可执行 | RiskPolicy Hard Gate，失败输出 `gate_failures` |
| `scripts/research_discipline.py` | 报价 96 小时、K 线 7 天仅 WARN | 可执行性标准过宽 | 盘中 5 分钟、EOD 最近完成交易日；fallback 禁止执行 |
| `scripts/collect_market_data.py` | 部分 R/R 以现价而不是计划入场价计算 | 显示与执行不是同一交易路径 | 每条路径独立使用 `planned_entry` 计算 |

## P1 High

| 文件 / 函数 | 问题 | 影响 | 修改方案 |
|---|---|---|---|
| `scripts/collect_market_data.py::evaluate_candidate` | `physical_ai_focus` 无条件增加 overall | 主题确认偏差 | 主题仅用于分类，不再自动加 Alpha 分 |
| `scripts/opportunity_radar.py` | 质量、估值、趋势、买点混入单一机会分 | 好公司与好价格混淆 | 拆分 OpportunityScore / EntryScore |
| `scripts/opportunity_radar.py::weighted_theme_score` | 主题三项长期静态 | 主题强弱不能随证据变化 | 静态值仅作为 BasePrior，增加动态调整 |
| `scripts/export_public_reports.py::merge_opportunity` | 非空旧值可能阻止更优数据覆盖 | 陈旧 Yahoo 可压过当日 Futu | 按 source/freshness/confidence 选择字段 |
| `scripts/opportunity_review_metrics.py` | “逻辑增强”会计入 HIT | 自我验证 | 命中只由未来价格与基准超额收益定义 |

## P2 Medium

| 文件 / 函数 | 问题 | 影响 | 修改方案 |
|---|---|---|---|
| `scripts/run_deepseek_cloud_report.py` | LLM 自由 Markdown，仍可重写交易数字 | 叙事污染确定性交易层 | Structured JSON + Financial Validator + Python renderer |
| 全链路 | 数据对象无统一 schema | 模块间隐式字段错配 | JSON Schema + CI 校验 |

> 本文是可回滚的审计快照；实际 Gate 和分数逻辑以 `scripts/model_v2.py` 与 `config/risk_policy.json` 为唯一来源。
