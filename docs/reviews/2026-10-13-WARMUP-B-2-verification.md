# WARMUP-B-2 验证与停止边界

接管已授权的codex-2补丁；fetch确认main=678b4e1，rebase已无冲突完成，原e2b1565与562c627的patch-id一致。主checkout未改。

- 原2895 failed/6 errors：初始8类候选见/tmp/warmup-b2/root-clusters.tsv；最新main同命令10646 passed/EXIT=0。旧健康门红由main PR99收口，本单不修基线问题。
- 收尾命令（仓根）：python3 -m pytest -q backtesting-py/tests --junitxml=/tmp/warmup-b2/full.xml > /tmp/warmup-b2/full.log 2>&1; echo "EXIT=$?"。接管前中断日志另存full-aborted-codex2.log，不冒充完成全量。
- 51个金样、235组响应对main核对：入场根/价格/笔数/方向变化0；trades全字段、equity_curve、metrics、data_manifest变化0。读取原JSON与现成post/snapshot/canonical返回做比较，通过才写出；未手改JSON。
- 单文件最终快信号：10b2a=30、10b2b=41、10b2c=51、pliq1=171、event0=57、scenarios=4、template=32、explicit=22、macro=171，均passed/EXIT=0。2d整文件27 passed/4 failed，red_streak_rsi旧冷路径待做。
- 冻结空种子捷径绕过校验变异：test_zero_budget_frozen_seed_replays_before_history_shortcut 1 failed/EXIT=1；cp还原/cmp相同，explicit全文件22 passed/EXIT=0。继承零预算删除分支变异在mutation-zero.log另存。
- patconf2b1的6组、P6的10个响应出现入场变化，停止文件并cp撤回本次文件自身尝试；不修改判据与金样。详细旧/新在blocked-patconf-entry-identities.json、p6-entry-changes.json。
- Sonnet5.5有效增量review追加原2026-10-13-WARMUP-B-review.md；源编译及diff --check EXIT=0。
- 高周期完整冻结复放、1M非固定网格待做，明确不在本单。无main合入，无pin/部署/实盘行情取证。
- 全量仍在运行，本检查点只推已完成部分；未达到新增失败0，不转ready。
