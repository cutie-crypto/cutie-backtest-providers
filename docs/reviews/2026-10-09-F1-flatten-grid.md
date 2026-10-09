# F1 强平截止点网格校验

起点 `af9952d`；分支 `feat/1009-cx3-f1-flatten-grid`。范围截止点 `flatten_at` 切穿 K 线时，注册 HTTP 路由取数前返回 `INVALID_PARAMS`；两种区间模板共用此修复。F2 原有 `CalendarConfig.validate_grid` 已校验强平点，本次只补 15m 对齐回归用例，无运行时修改。

codex-3 定：按本次派单的明确修法执行强平点拒绝；F1 可选开仓窗口的强平端点与冻结区间端点使用同一 UTC K 线网格。泛用时间层延迟强平口径不在本修复范围。

独立手算：15m 网格 `23:47` 在桶内，拒绝；`23:45` 在桶边界，接受。F2 实际退出 `2026-01-01T23:45Z` = `1767311100`。新增 6 条（F1 两模板 × 拒/收 = 4，F2 拒/收 = 2）。

本地证据（无交易所、Pre 或生产证明）：
- 基线全量：3919 passed，EXIT=0，`/tmp/f1fix/baseline.log`。
- 定向：654 passed，EXIT=0，`/tmp/f1fix/target.log`。
- 首次全量发现两条 1h 测试仍配默认 23:45；失败证据 `/tmp/f1fix/full-before-grid-fixture-repair.log`。既有三处运行时验收（6b、3b、时间层门禁）显式指定 23:00 以匹配它们的 1h 输入；不改黄金或旧结果断言。
- 修复定向及收尾全量见 `/tmp/f1fix/repair-target.log` 与 `/tmp/f1fix/final-full.log`；收尾 3925 = 3919 + 6，EXIT=0。
- validator：29 passed，EXIT=0；本地 ASGI catalog/auth/实际 F2 引擎响应检查通过（31 工具），EXIT=0，`/tmp/f1fix/validator-{tests,smoke}.log`。
- 可编译变异：`python3 docs/reviews/f1_flatten_grid_mutation.py`，删掉强平点校验 ⇒ 2 failed / 2 passed，pytest EXIT=1；copy 还原并 cmp EXIT=0，驱动 EXIT=0，`/tmp/f1fix/mutation{.log,.json,-driver.log}`。

只推独立分支，不开 PR、不合 main。
