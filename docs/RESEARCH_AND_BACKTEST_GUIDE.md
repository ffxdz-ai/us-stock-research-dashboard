# 研究与前向仿真指南

## 1. 运行离线仿真

准备一个 JSON market pack，至少包含：

```json
{
  "as_of": "2026-10-01T20:00:00Z",
  "source_version": "historical-snapshot-v1",
  "plans": [],
  "bars_by_symbol": {},
  "benchmarks": {
    "SPY": [],
    "QQQ": [],
    "industry_etf": [],
    "equal_weight_universe": [],
    "simple_trend": []
  }
}
```

然后运行：

```powershell
python scripts/run_forward_simulation.py `
  --market-pack tests/fixtures/simulation_market_pack.json `
  --ledger data/simulation_ledger.sqlite `
  --output data/latest_simulation_metrics.json
```

命令只读离线输入，不下单、不通知。成本、TIF、整股和成交量假设来自 `config/simulation_policy.json`。

## 2. 成交假设

- 收盘后形成的信号最早使用下一交易日价格。
- 回调策略使用买入限价近似；突破策略使用买入停止触发近似。
- 日线触及只能代表成交近似，并受成交量参与率、现金、整股、点差、滑点与手续费限制。
- 跳空穿越止损使用开盘可成交价近似；同根同时触发止损/止盈时按止损优先。
- 未触达订单在 TIF 到期后记为 `expired`，不产生交易损益。
- 可选 `partial_target_price` 与 `partial_exit_fraction` 会生成 `partially_exited`；未配置时不虚构分批止盈。
- 分红作为现金调整，拆股调整数量和价格；退市使用明确退市价或保守终值。数据没有公司行动字段时必须显示限制。

## 3. 指标定义

- 总收益：组合净值末值相对初始现金。
- 最大回撤：`equity / running_peak - 1` 的最小值，以负百分比展示。
- MAE/MFE：实际成交后的持有路径相对成交价；有 high/low 时标为日线高低粒度，只有 close 时标为 close proxy。
- 夏普：连续日组合收益减同频无风险收益，再按 252 年化；没有提供无风险序列时明确记录零日无风险假设。
- 信息比率：同日组合收益与指定主基准收益差；日期/长度不齐时为 null。
- 年化收益/波动：至少 20 个日收益样本才输出，否则给出 `insufficient_*` 原因。
- 换手率：成交名义金额合计除以平均组合净值；现金利用率和总暴露从每日净值表计算。
- 胜率、期望和利润因子只使用已闭合交易。未成交、未成熟、基准缺失各自计数，不能自动算失败。

## 4. 模型冻结与时点纪律

1. 确定 `model_version`、`policy_version`、`simulation_policy_version`、策略参数和冻结时间。
2. 将已经查看并用于调参的历史区间标记为开发样本。
3. 每条输入保存 `period_end / published_at / available_at / ingested_at / source / source_version / content_hash`。
4. 只允许 `available_at <= signal_time` 的数据进入信号。日期级 SEC 公告默认下一交易日可用。
5. 预测修正必须先统一预测期；FY1 滚动到新财年时记为不可比较，而不是上修。
6. walk-forward 分割剔除未来标签跨过边界的样本，并设置 embargo；测试集不参与调参。
7. 任何参数变化都提升版本并重新冻结，不能覆盖旧信号或旧快照。

## 5. 如何判断样本是否足够

不要用“运行满几天”作为充分条件。至少同时检查：

- 每个策略的已触发、已成交、已闭合和到期未成交数量；
- 7/30/60/90 日或策略定义期限是否真正成熟；
- 牛市、熊市、震荡、流动性收紧和行业降温等状态是否覆盖；
- 同主题、同日期和重叠持有期造成的相关性，区块重采样后的有效独立样本；
- SPY、QQQ、行业 ETF、等权股票池和简单趋势基线是否同日完整；
- 收益、回撤、胜率、Rank IC 的置信区间是否仍过宽；
- 成本和成交假设变化后结论是否稳定。

条件不足时状态必须保持“样本不足/未验证”，不能显示“验证有效”。

## 6. 真实数据接入要求

- 历史逐日 OHLCV 与拆股/分红/退市/停牌/代码变更；执行验证最好有更细粒度报价和买卖盘。
- 历史股票池和当时分类，避免用今天热门股回填过去。
- SEC 精确受理时间、公司 IR 原文、FRED/ALFRED vintage、历史分析师预期快照。
- 与组合日期严格对齐的 SPY、QQQ、行业 ETF、等权股票池及趋势基线净值。
- 私有前向账本的耐久存储；真实账户只在本地私有层使用，绝不进入公开站点或 Git 历史。

