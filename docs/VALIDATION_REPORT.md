# 优化验证报告

验证日期：2026-10-03  
分支：`codex/stock-system-optimization`

## 基线

修改前本地基线为 58 个 Python 测试和 22 个 Node 测试通过。基线通过只说明原有确定性测试正常，不代表策略盈利或数据供应商可用。

## 本地执行结果

以下命令不访问交易接口、不发送通知：

```powershell
python -m unittest discover -s tests -p 'test_*.py'
node --test tests/live_quotes.test.js cloudflare-worker/test/execution-contract.test.js cloudflare-worker/test/worker.test.js
python scripts/run_forward_simulation.py --market-pack tests/fixtures/simulation_market_pack.json --ledger data/validation_simulation.sqlite --output data/validation_simulation_metrics.json
python -m compileall -q scripts tests
python scripts/validate_public_archive.py docs/data/reports.json
```

最终结果：

- Python：83 通过，0 失败，0 跳过。
- Node/前端/Worker：24 通过，0 失败，0 跳过。
- T01–T20 风险场景均有离线固定 fixture 覆盖；T19 使用同一 JSON fixture 验证 Python 与 Worker 语义。
- 合成 market pack：1 个信号、1 个合格计划、1 个触发、2 个成交事件、1 笔闭合交易、3 个净值点；SQLite 对账通过。
- 合成样本只有 2 个日收益，因此夏普、信息比率、年化收益和年化波动返回 null 及 `insufficient_*` 原因，这是预期结果。
- `docs/data/reports.json` 的公开档案结构校验通过；现有 `docs/data/index.json` 是 2.1.0 之前生成的旧快照，严格新门槛校验会拒绝其旧 `execution_allowed` 和缺失因子覆盖字段。未覆盖历史快照，需由下一次新版完整报告任务生成新索引。

## 关键证据

- 25% 因子覆盖率与缺失估值均不能取得正式资格；合法零值保留。
- 三天旧报价不能即时执行；周末最新周五收盘只可用于研究；未来时间戳、假日、DST 和提前收盘边界已测试。
- 100→150→110 的最大回撤为 -26.6667%；单调上涨为 0%；100→90→95 为 -10%。
- 收盘形成信号不在同一根 K 线成交；回调不触达时到期且无损益；跳空止损可损失超过 1R；同根双触达按止损优先。
- 重复记录不会重复创建信号/事件；提高固定成本不会提高同一成交序列的净收益。
- 拆股、股息、停牌、退市和可选分批止盈在结果中可审计。
- Worker 只在计划、研究、因子、实时常规盘报价、实时 R/R 和本地组合许可全部通过时放行。
- 公共摘要不含现金、持仓、账户、成本基础或 API Key。

## 合成验证与真实验证的边界

本报告中的成交、收益和净值全部来自 `tests/fixtures/simulation_market_pack.json`，仅证明工程规则和会计链条可重复，不是策略历史业绩，也不支持“提高胜率”或“可盈利”的结论。

本次没有运行：

- Futu、FMP、Finnhub、Alpha Vantage、SEC、FRED、CNINFO、HKEX 的真实联网集成测试；
- 真实历史逐日回放、ALFRED vintage、历史分析师预期快照；
- SPY、QQQ、行业 ETF、历史股票池等权和趋势基线的真实对比；
- 真实飞书消息、Cloudflare 发布、GitHub Pages 发布、订单或账户读取。

缺少凭据时没有把 mock 成功报告成真实连接成功。供应商集成测试应在私有环境单独运行，并将无凭据记为 skip，而不是失败或伪成功。

## 仍不能得出的结论

- 因子权重、0.80 覆盖门槛、3:1 成本后 R/R 或风险预算不是已验证最优参数。
- 尚不能评价策略胜率、超额收益、最大可承受回撤或适合的仓位。
- 样本是否充分取决于冻结后信号数量、成熟持有期、不同市场状态覆盖、重叠与相关性后的有效独立样本、基准完整度及置信区间；不能用固定运行天数代替这些条件。
