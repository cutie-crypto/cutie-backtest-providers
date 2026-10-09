# TIME-6a 本地验收证据

基线：`11e8cfbfccb68776eeb755aa3ad1296bf4034bce`，分支 `feat/1009-cx3-time-6a`。
原有 golden / fixture 未修改；无服务端、validator、出场逻辑或风控参数定义变更。

## 关闭态与接线

- 在基线、首个提交前运行 `capture_time_layer_11e8cfb.py`；脚本校验完整 SHA，测试不会重录 fixture。
- 账本三模板：RSI 分批 310 笔、grid 29 笔、DCA 28 笔；各比较逐笔、完整权益和 canonical result.v2 字节 SHA256，共 9 个交易证据指纹；另比较启动根、预热根及 assumptions/raw_report 指纹。
- 单仓复用原指纹函数及两份不可变 fixture：20 个方向用例 × 4 风险组合 × 2 预热状态 × 2 风险关闭参数状态 = 320 次比较；每次显式传全部时间缺省参数。
- 21 个运行时单仓模板，含 EMA pullback 空头共 22 个用例：启用窄时段 × legacy/3a 风控两模式 = 44 次实际 Backtest；每次有交易、全部入场在时段内、至少一次出场在时段外。
- 22 个用例 × 2 预热状态 = 44 次关闭态 HTTP 响应比较，时间上下文构造设为禁止调用；min_bars、预热根、assumptions、raw_report 与基线一致，交易无时间新键。
- 3 个 kernel_v3、3 个账本工具拒绝所有本批时间键（包括显式 false/缺省值）；非法时间参数在行情及预热请求前拒绝。
- HTTP 启用态验证开仓成交时间与 time_layer 披露；缺根在取预热前失败；补充请求隔离及末根无下一开盘禁止开仓。
- 纽约 2026-03-08 与 2026-11-01：6 个真实 UTC 判定全部通过。

## 五项变异自证

每项独立备份、修改并 py_compile，实际 pytest 退出 1 后按备份还原，重跑退出 0，再逐文件 cmp 退出 0；未用 git checkout。

| 变异 | 识别用例 | 改错后 | 还原后 |
| --- | --- | --- | --- |
| 决策使用本根开盘，不加周期 | test_entry_uses_close_boundary_and_exit_is_unrestricted | 2 failed | 2 passed |
| 出场也检查开仓门禁并跳过出场 | 同上 | 2 failed | 2 passed |
| 时区使用固定 UTC-5 | test_dst_real_utc | 2 failed, 4 passed | 6 passed |
| 关闭态也构建上下文并写空 time_layer | test_disabled_response_and_start_unchanged | 44 failed | 44 passed |
| 跨午夜星期按结束日 | test_overnight_weekday_belongs_to_session_start | 3 failed, 3 passed | 6 passed |

原始日志和备份：`/tmp/t6a/`；逐项真实退出与 cmp 记录：`mutations.log`；执行脚本：`mutations.py`。

## codex-3 定

- 缺根错误为 `TIME_DATA_GAP`，原因 `time_data_gap`：启用后全区间相邻开盘必须等于声明的固定周期，失败即停止回测。
- 支持正整数 m/h/d/w 固定周期；月周期等不可固定推算的周期报 `INVALID_PARAMS`，不沿用旧路径的猜测周期。
- 末根没有下一根开盘，不提交新开仓，防止 finalize_trades 把订单回填到本根开盘。
- 不改 6b/3b 的 max_holding_bars，也不声明后续批次时间键。与派单无范围偏离。

## 验证命令与结果

均在派单指定目录使用 python3，pytest 参数 `-q -p no:cacheprovider`，完整日志重定向后读取判据。

- 开工前 backtesting-py 全量：1799 passed, 29 warnings in 21.49s；EXIT=0（baseline.log）。
- 时间层两文件：603 passed, 2 warnings in 16.39s；EXIT=0（time.log）。
- validator 全量：29 passed in 0.48s；EXIT=0（validator.log）。

首轮收尾出现 `4 failed, 2398 passed`：既有测试要求目录 build 与模块导出 builder 对象相同，循环内包装改变了身份。
已改为 21 个单仓 builder 定义处统一装饰，目录继续引用原导出对象；未修改任何既有测试。相关四个测试文件 `84 passed`，EXIT=0；后续重跑五项变异及最终全量。

- 修正后 backtesting-py 收尾全量：2402 passed, 29 warnings in 38.79s；EXIT=0（all.log）。
- 数量核对：1799 + 603 = 2402；现存两个出场函数 AST 与基线相同，四份既有风险 / grid / DCA fixture 字节与基线相同。
