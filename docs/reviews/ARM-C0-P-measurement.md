# ARM-C0-P 小时报告容量粗量

2026-10-13 JST，head=aeef34a；最终全量10699 passed EXIT=0，main基线10683 passed EXIT=0。PR #102保持draft，未合并、未改上限、未开新单。

本地合成代理：完整整点序列、真实生产族/基础币BTC、默认清算percentile/30天=720根回看；真实provider HTTP路径。指标值以稳定USD基线加一次放量、价格一次下跌模拟少量成交；top为0.8基线加一次0.6入场，仅1条动作。不是生产指标/价格回放，top数值不证明频繁交易场景容量。时间窗口统一以ops首根2026-04-16 01:00Z + 4326h为末边界。4000/4326h另合成了生产首根之前的回看数据，故只衡量报告容量；真实4326根库默认30天只能支持最多3606小时判定窗口。

每档独立进程一次；在 `_bounded_template_response` 输入处记录实际raw_report UTF-8紧凑JSON字节，序列化选项与生产上限检查一致；没有绕过或调整上限。EXIT=0=HTTP provider success，EXIT=1=HTTP 200里的业务failed（不是进程崩溃）；测量总调度EXIT=0表示五次结果全部捕获成功。

| 模板 | 回测小时 | raw_report 字节 | EXIT | 结果 |
|---|---:|---:|---:|---|
| 清算 | 2000 | 750712 | 0 | success |
| 清算 | 3000 | 1125712 | 1 | failed |
| 清算 | 4000 | 1500712 | 1 | failed |
| 清算 | 4326 | 1622962 | 1 | failed |
| 多空比 | 4326 | 1003 | 0 | success |

清算超限时HTTP=200、result_status=failed、error_type=INVALID_PARAMS，error_message精确为 `Complete raw_report evidence exceeds callback limit (1048576 bytes); shorten the backtest range.`；limitations.reason缺失，limitations={}。provider清楚标明字段、字节上限和缩短范围建议，并非只有笼统失败；机读reason码没有提供。Web/RN如何渲染这条英文错误未实测，不冒充端侧直接证据。

这四档严格拟合 raw_report_bytes=375×小时数+712。已直接证明2000h通过、3000h起所测档失败；未追加二分执行。粗估最大2794h=116.42天（1048462字节），2795h=1048837字节首先超限，每天24根约增9000字节；实际阈值随值字符串长度、成交/跳过记录变动。

按ops首根2026-04-16 01:00Z，忽略回看、直接跑全部历史的容量模型约在2026-08-10 12:00Z（08-10 21:00 JST）撞上限；默认30天先扣720小时的真实可跑窗口模型约在2026-09-09 12:00Z（09-09 21:00 JST）撞上限。两者均已过去。当前真实最长判定窗口3606h，套模型约1352962字节，预计同样失败，未把这一估算写成实测。

裁定输入：①4326h清算不能过（直接证明），②超限有明确error_message但无独立reason码；按impl追加条件本批应留PIN-19，保持draft，待统领裁定。没有裁剪事件证据或创建「小时事件摘要」新单。

原始证据 `/tmp/arm-c0-p/measurements.json`、各档 `measure-<kind>-<hours>.json/.log`、脚本 `measure.py`、总调度 `measurements.log`。所有结果为本地合成代理，不代表生产当前实际执行结果。
