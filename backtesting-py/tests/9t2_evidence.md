# 9-T2：晨星、红三兵、十字星反转、内包线突破 provider 证据

基线 `9f4146592811df2cb024698c5bfb8358701e91ff`，来自 `origin/feat/1009-cx2-9t1-engulf-pin`；工作树 `cx2-p1a`，交付分支 `feat/1009-cx2-9t2-star-soldiers`，不跟踪上游。只推本分支，不开 PR、不改 main。

## 工具与参数

以下 id 均以 `local.backtesting_py.` 开头，四条一引擎、多 id，避免复制冻结出场、跳空取消和时间门禁行为。

| id | 专属参数（默认 / 范围） | 止损锚点 |
|---|---|---|
| morning_star | 无额外位置过滤 | 第2根最低价 |
| three_white_soldiers | position_filter=true / boolean | 第1根最低价 |
| bullish_doji_reversal | position_filter=true / boolean，候选根 RSI14<30 | 十字星最低价 |
| inside_bar_breakout | breakout_window=3 / 严格整数1–20；trend_filter=false / boolean | 母线最低价 |

共用：direction=long（唯一允许，spot/futures 的 short/both 均取数前 INVALID_PARAMS）；reward_r=2（number，0.1–20）；exchange 沿用环境默认/okx；现有仓位百分比/固定名义额、leverage=1、risk/time 关闭默认、持仓到期和时间门禁。模板拥有出场，外置止损/止盈与动态风控出场参数继续互斥。

## codex-2 定

1. 四个独立 id 复用现有 candle 引擎。晨星/三兵在最后一根收盘确认；十字星只认紧接的下一根收盘严格超过十字星最高价，确认失败不能之后补确认；内包线在内包根之后 N 根收盘内严格突破母线最高价，边界第 N 根有效、第 N+1 根失效。全部在确认后的下一根开盘市价入场。
2. 统一止损缓冲0.1%，冻结锚点×0.999；目标=实际引擎入场价+(入场价−冻结止损)×reward_r。High/Low 触发后下一开盘市价出场，同根顺序止损→持仓/时间到期→止盈→形态信号；已有平仓单时返回。assumptions 披露触发价不是成交保证。
3. 入场开盘≤冻结止损则取消，不建仓；raw_report.candle_pattern 记录原因、信号/入场索引、价格及累计次数。result.v2 trades/metrics/manifest 键集保持不变。
4. 红三兵沿用9-P严格口径：包括第1根在内，每根开盘落在前根实体内，收盘严格超过前根收盘；默认位置过滤为第1根开盘<20根前收盘。十字星位置过滤只读候选根 RSI14，复用当前 provider 的 Wilder ewm RSI，不能换成确认根 RSI。
5. 内包线新形态替换尚未突破的旧形态；突破消耗候选，即使趋势/交易时段过滤挡住入场，也不延迟再开。可选趋势过滤=突破收盘>EMA20，adjust=False，满20根才就绪，默认关闭。预热和主区间共同形成因果确认事实，持仓和交易时段不延长候选寿命。
6. 晨星第2根只按“实体≤首根实体×30%”，移除额外“通用小实体”限制；镜像共用识别函数同步遵循相同相对实体条件，但本批不注册做空工具。范围内搜索只有这一处重复限制。
7. 主区间 min_bars：晨星23；三兵开过滤23/关过滤4；十字星22；内包线3，开趋势过滤21。每个 builder 挂 @_with_time_config；末根没有下一开盘不排新入场。

## 独立手算与关态指纹

- 晨星首根实体10；第2根实体1；第2根前20均值=(19×1+10)/20=1.45；通用小实体阈值0.725。1位于(0.725,3]，原通用限制漏识别；第三收盘106>首根中点105，修复后入场26根、开盘110。
- 三兵实体3/4/4，上影均0.5≤自身实体×0.3；前一根实体[98,100]包住首兵开盘98，之后开盘100/103分别落在前兵实体内。四个“开盘离开前实体”差例必须无交易。
- 十字星实体0.5、全长7；0.5≤0.7且全长不低于前20均值×0.8。前根下降序列使候选 RSI=0；确认收盘等于最高价104不成立，104.001成立。另一个用例让确认根 RSI>60，候选根 RSI=0仍应成立，不切换指标权威。
- 内包线母线高108/低97，内包根高107/低98，随后收盘109突破。默认第3根有效，第4根作废；窗口1/4/20边界及新母线替换单独手算。
- 四组指定锚点97→止损96.903；实际入场110→R距离13.097；2R目标136.194、3R目标149.291。围绕止损/目标两侧取数，成交独立取下一根开盘。跳空等于或低于96.903均取消。
- 新工具没有历史交易基线。`fixtures/capture_9t2_candle_off.py` 手动绕过时间装饰器、不给 risk/time 键，冻结4模板×预热开/关共8组指纹，每组真实1笔成交。装饰器绕过与默认关态响应的8组指纹完全一致；显式关态默认再比较 trades、权益、result.v2 canonical、assumptions/raw_report。测试只读金样，旧金样不动。
- 213条新增专属用例；运行时枚举增加76条：时间兼容16、时间持仓4、风控特性16、杠杆40。互斥模板对外置出场明确断言 INVALID_PARAMS；持仓检查使用真实形态行情，不跳过枚举。

## 验证门

命令使用 `python3 -m pytest ... -q -p no:cacheprovider > /tmp/9t2/<名>.log 2>&1` 后 `echo "EXIT=$?"`，不接测试输出管道。

| 门 | 路径 / cwd | 结果 |
|---|---|---|
| 基线全量 | 根目录 / backtesting-py/tests | 3826 passed，EXIT=0，baseline.log |
| 定向（最终） | 新/旧形态、识别库、时间兼容/6b、风控3b、杠杆共7文件 | 1733 passed，EXIT=0，targeted-final.log |
| 收尾全量 | 根目录 / backtesting-py/tests | 4115 passed = 3826基线 + 213新增专属 + 76枚举新增，EXIT=0，final.log |
| validator | validator目录 / tests | 29 passed，EXIT=0，validator-local.log |

早期 `initial.log` 的4条持仓用例把 max_holding_bars=1 的成交期望错写为下一根之后，改为2使到期与止损/止盈同时触发；其余失败为尚未建立的金样。首次金样采集把十字星的长前缀构造成上涨至候选，导致无交易；修正独立下降行情，不改RSI过滤实现。`targeted.log` 最终920通过，追加5条边界用例和风控/时间/杠杆定向后1733通过。

首次从仓库根运行 validator（validator.log）导入本机 site-packages 的旧包，4 failed/25 passed，EXIT=1；核到 `/Users/tf/Library/Python/3.9/lib/python/site-packages/cutie_backtest_provider_validator`，转仓内 validator 目录后29通过。失败记录保留，不将旧包失败冒充仓内产品问题。

## 七条可编译变异

`/tmp/9t2/mutate.py` 复制两份源码到 `/tmp/9t2/backup/`，每项精确替换→py_compile→定向用例→复制还原→系统cmp；不用 git checkout。各项编译EXIT=0、错码用例EXIT=1、还原cmp=0，脚本EXIT=0。还原后1733条定向及全量门另行执行。

| 变异 | 被拦截行为 | 日志 |
|---|---|---|
| doji_same_bar | 十字星当根入场不等确认 | mutation_doji_same_bar.log |
| inside_no_expiry | 内包线过期仍突破入场 | mutation_inside_no_expiry.log |
| star_generic_small | 晨星恢复通用小实体限制 | mutation_star_generic_small.log |
| soldier_no_inside | 三兵开盘不在前实体也放行 | mutation_soldier_no_inside.log |
| reward_fixed_two | R写死2，忽略3R | mutation_reward_fixed_two.log |
| gap_guard_removed | 跳空错侧仍建仓 | mutation_gap_guard_removed.log |
| stop_priority_removed | 止损与到期同时触发却让到期先出 | mutation_stop_priority_removed.log |

源码还原SHA256：
- strategy_pattern_template.py：`5f41674a989a142aeb52594616cb8e5a293f23de7bd122039ac87dcde445b1d0`
- strategy_candle_patterns.py：`a4d9c1d40edd48e9f40b35b31b31d33985344d2e43af82fce09e96d894865f3f`

本材料证明本地 provider 注册/真实Backtest成交、假取数HTTP契约及回归；不代表服务端注册、舰队pin、Pre/生产部署或真实行情端到端验收。分主题中文提交，禁 Co-Authored-By / Claude-Session；交付后停止本单。
