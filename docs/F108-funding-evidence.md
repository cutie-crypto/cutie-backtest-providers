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
