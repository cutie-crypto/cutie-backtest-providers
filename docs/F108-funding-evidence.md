# F108 资金费证据（尚未接入回测响应）

`backtesting-py/funding_history.py` 读取 Binance USD-M USDT 永续公开资金费历史。
来源：https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Get-Funding-Rate-History

- 只支持同一交易场所，禁止拿 Binance 费率替代 OKX（provider 默认仍为 OKX）。
- 使用每次结算的 fundingRate × markPrice × 当时持仓量；多头正费率为支出，空头为收入。保留 Regular/Special。
- 区间单位毫秒，分页重叠最后时间戳去重，冲突/不前进/请求失败拒绝。manifest 保存来源、范围、获取时间与行内容 SHA256。
- 平台事件约定 `(entry, exit]`：同毫秒先结算资金费，再退出，再新入场；不是用户真实交易所成交顺序的证明。
- 公共接口穷尽是来源证据，无法证明交易所历史档案从无缺失；不伪装为交易账单。
- 八项本地测试通过。2026-09-09 一次只读实取 BTCUSDT `[1788877470520,1788963870520]` 返回3条，SHA256 `281ee58b30216897d3f5baaebff782538765c2bfd923984a935c7f45260a83a8`。
- 当前尚未更改 result.v2/hash/目录、默认交易场所或 `funding_rate_included=False`。需要独立兼容的风险结果契约、服务端校验及实际调用后才能宣称资金费已进入收益。

## 可选风险回放结果

`backtest.risk_policy` 可传 `{schema: "cutie.strategy_risk_policy.v1", direction: "long", leverage: 1}`。
provider 实际请求入口会在 `raw_report.strategy_risk_result` 返回独立 payload/hash，绑定完整 result.v2 的规范哈希、费用、方向杠杆、资金费证据及暂停规则。原 result.v2 与目录保持兼容；默认请求不增加风险结果。

回放只接受不重叠的单边完整交易，按净收益计数/归一化周期净值，第三亏预警，第五亏或峰值回撤20%停止该周期。不能用双向回测过滤掉另一方向后冒充单边。现货仅1倍多头，合约资金费仅Binance来源；未自动切换OKX。

测试：HTTP/资金费/风险回放26项通过；另以服务端真实风险核算模块对5组场景逐笔25个状态（净值、计数、暂停原因）对账一致。只是局部一致性证据，尚不等于所有判定器/SLTP全成交序列验收。

待接入：server 派单 risk_policy、独立验收及冻结成本、作者展示风险周期收益、周期启用/恢复；长回测风险证据可能超 server raw_report 256KiB上限，须明确处理尺寸，不能静默裁剪/漏验。运行时SLTP仍需同版本背书。部署尚未执行。
