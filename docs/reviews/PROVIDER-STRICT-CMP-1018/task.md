# 甲-1 PROVIDER-STRICT-CMP-1018 派单

来源：tokenbeep-orch → tokenbeep-codex-2，2026-10-18。
背景：走查 #14，用户写「高于 / 低于」（严格），回测按「不小于 / 不大于」（含等于）执行。advisor 裁甲方案：provider 加严格比较参数，默认保持含等号（旧行为不变），服务端（甲-2，另一条线做）在用户写严格比较时传严格参数。

仓库 `/Users/tf/Documents/bitbucket/cutie-backtest-providers`，从最新 origin/main 起独立 worktree 与分支 `fix/strict-threshold-cmp-1018`；main 必须走 PR，不直推、不 force push、不 --no-verify。

现状点（origin/main 18e5205）：backtesting-py/strategy_macro_events.py:278 一带 `abs(delta) < surprise_threshold` 则跳过（偏差 ≥ 阈值入场）；同文件 move_threshold_pct 的 `abs(move) < ...` 同形。检索本文件与 provider 其它用户阈值含等号比较、对应模板句式写「不小于 / 不大于」的点，逐处列出再决定；只改 #14 阈值判据。

参数 `threshold_inclusive`（bool，默认 True），False 时等于阈值不入场（surprise / funding 用 abs(delta) <= 阈值跳过，move 同理）。参数校验、schema、参数白名单同步加入；不传等于 True。

新增 tests/test_strict_threshold_cmp_1018.py，至少覆盖默认相等入场、False 相等不入场、大于阈值两种都入场、非法类型拒绝。回归 tests/test_q18_macro_events.py 与 test_psf_3_macro_watching.py。快信号 pytest 单文件，输出重定向文件并记录 EXIT。

变异：新判据改回 < 后指名红例，cp 备份还原后回绿，写进 PR 描述。执行/回测口径单需要异模型审及 advisor 亲读。

完成后推分支、开中文标题/描述 PR 到 main，不加署名、不合并。60 轮未完成则提交推送已完成部分、回报剩余项后停。回报 ≤10 行，首行 PROVIDER-STRICT-DONE 加 7 位 sha，包含 PR 号/链接、改动文件:行、用例条数/EXIT、变异、偏离、遗留。
