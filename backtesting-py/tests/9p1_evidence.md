# 9-P 第一段：纯函数基础件与本地证据

基线 `5b8320a87116eaa0fd36c36ae277289bb502b8e8`，工作树 `cx2-p1a`，分支 `feat/1009-cx2-9p-swing`。
仅新增两个库模块、两个测试文件和本证据文档。没有 provider 接线、目录/schema 参数、模板、风控或成交改动；9-R / 9-T 的两项钱路径裁定仍由后续批次处理。

## 对外接口

- `strategy_swing_points.py:32`：`swing_points(high, low, *, n=5) -> SwingSeries`。结果为不可变 highs/lows 元组，按确认根索引；无事件为 `None`。`SwingPoint` 保存极值根 index、确认根 confirmed_at、极值 price，坐标属于输入序列（包含调用者自行提供的预热段）。
- `strategy_swing_points.py:55`：`confirmed_points(series, *, as_of, kind, count=3) -> tuple[SwingPoint, ...]`。必须提供截至根；返回最新 count 个已确认点，保持时间升序，点数不足不会补未来点。
- `strategy_candle_patterns.py:84`：`candle_geometry(open, high, low, close, *, lookback=20) -> Geometry`。实体、全长、上下影线及此前 lookback 根均值；不足完整历史的均值为 `None`。
- `strategy_candle_patterns.py:104`：`candle_patterns(kind, geometry, *, config=None, indicators=None) -> PatternSeries`。kind 为 engulfing / pin_bar / star / soldiers / doji / inside_bar，支持镜像方向。返回原始形态、带位置过滤的候选、候选原始极点及大小实体标志。
- `PatternConfig` 保留需求默认：1.5/0.5 大小实体、1.2 吞没、0.5% 位置容差、Pin Bar 2倍长影线/15%短影线/1⁄3实体位置、晨星30%、三兵30%、十字星10%与0.8倍全长。
- `indicators` 接收对齐的 ema20 / ema60 / bb_lower / bb_upper / rsi 数组；`None`/NaN 为未就绪，不能满足过滤。指标的计算是否因果仍是未来调用者的责任。本库只读取当前指标槽。

## codex-2 定

1. 波段点严格比较左右各 N 根，同价不成立；第 j 根极值只在 j+N 根输出，首部及未确认尾部不回填。`as_of` 同时截断确认槽并检查点的确认时间。
2. N、lookback 是严格整数 1–500（bool、float 不接收）。输入长度、有限正价格与 OHLC 顺序不合法均报 `INVALID_PARAMS`；空序列允许计算，没有可供访问的截至根。
3. 大小实体、全长均值及位置极值基准排除当前根，默认回看20根。已知零均值按原公式比较，不混同缺历史；零均值下零实体可同时满足大小标志，但不由此自动产生方向形态。
4. 吞没位置与锚点取两根整体极点；Pin Bar 的新低/新高严格比较，EMA/布林触及要求线值位于当根 [Low, High]。三兵三个开盘和收盘均与各自前一根比较，形态开始前趋势看 first_open 与20根前 close。
5. 星线第二根同时满足通用小实体与首根实体30%条件。Pin Bar 不接受零实体；Pin Bar/十字星不接受零全长。候选锚点依次为吞没整体极点、Pin本根、星线第二根、三兵第一根、十字星本根、内包母线。
6. 十字星仍需下一根确认，内包线仍需之后限时突破；这两个状态机留给9-T。返回候选并非下单指令，锚点不加0.1%缓冲，不计算2R。

## 验证

均在指定工作树运行，pytest 使用 `python3 -m pytest ... -q -p no:cacheprovider`，输出重定向至 `/tmp/9p1/<名>.log`，随后打印并保留实际退出码，无测试输出管道。

| 门 | 工作目录 / 参数 | 结果 |
|---|---|---|
| 基线全量 | backtesting-py / 无路径参数 | 3587 passed，EXIT=0，baseline.log |
| 定向 | backtesting-py / tests/test_strategy_swing_points.py tests/test_strategy_candle_patterns.py | 123 passed，EXIT=0，focused.log |
| 收尾全量 | backtesting-py / 无路径参数 | 3710 passed = 3587 基线 + 123 新增，EXIT=0，final.log |
| validator | validator / 无路径参数 | 29 passed，EXIT=0，validator.log |

用例期望独立手算。包括 N=1/500、左右平局、确认延迟、修改未来 N 根及追加后缀不改已确认前缀、截至根访问、六类形态镜像、位置过滤、大小实体阈值与零均值、0.5%容差边界和缺历史。
复核零均值时新增手算用例先红（`zero-mean-red.log`，1 failed，EXIT=1），修复额外的正均值门槛，并检索其他均值判断后重跑全部变异和收尾全量。

## 七条可编译变异

`/tmp/9p1/mutations.py` 使用复制备份，每项独立改源码、py_compile、定向测试、复制还原、再次 py_compile/定向测试、系统 cmp；不用 `git checkout --`，还原后强制编译以排除同时间戳缓存干扰。

| 变异 | 编译 / 改错测试 / 还原编译 / 还原测试 / cmp |
|---|---|
| 右侧未走完即确认 | 0 / 1 / 0 / 0 / 0 |
| 平局严格比较翻转 | 0 / 1 / 0 / 0 / 0 |
| 均值窗口含当前根 | 0 / 1 / 0 / 0 / 0 |
| 访问器越过截至根读未来 | 0 / 1 / 0 / 0 / 0 |
| 吞没忽略实体倍数 | 0 / 1 / 0 / 0 / 0 |
| Pin Bar 忽略实体位置 | 0 / 1 / 0 / 0 / 0 |
| 三兵忽略第一根与前根比较 | 0 / 1 / 0 / 0 / 0 |

每次还原后123条全绿。详细记录见 `/tmp/9p1/mutations.log`，每项红/还原日志独立留存，最终源码哈希见 `/tmp/9p1/restoration.sha256`。
这些是本地纯函数与既有全量回归证据，不代表新模板接线、实盘、舰队或服务端交付验收。
