# PROV-TRADECOUNT-P 任务书

provider 补 trade_count，为下次 pin。≤40轮；32轮未完成则提交推送完成部分、回报剩余后停。适用 TokenBeep/.claude/rules/herdr-line-standing-orders.md。
背景：server PROV-TRADECOUNT-S 已合 dev@1ae9c0394，v4 接受可选非负整数字符串 trade_count，旧三键不变。上单因 server 拒新键停止；provider PR#93 已合 main@30d6a07。
1. 从最新 origin/main 起新 worktree，分支 feat/prov-tradecount。
2. v4 共用组装 portfolio_result_v4.py:165 及轮动两工具、runner 补 ledger 闭仓笔数，序列化非负整数字符串；非v4已有int不动，缺的按现有类型补。先列文件:行清单。
3. 只补字段，不动信号、取数、其他指标；README补口径。
4. 每个补齐出口一条类型和值=fixture闭仓笔数断言，加全出口覆盖；删一处赋值指名红，cp还原。
5. 哈希/字节金样变化按现有方式重生，回报第7项说明只新增字段。
6. TokenBeep cutie-connector portfolio-result-v4.ts:165 只读核对是否固定三键、是否拒新键及调用方文件:行，不改该仓。
门：定向→pytest tests全量→py_compile；agy或Claude异模型review一轮，结论贴PR评论。推feat/prov-tradecount，PR到main，描述末尾加 🤖 Generated with [Claude Code](https://claude.com/claude-code)。不合、不pin。
中文提交，无Co-Authored-By；/tmp/prov-tradecount-p/REPORT.md 首行 PROV-TRADECOUNT-P-DONE <7位sha> PR#<号>，八项≤25行。
