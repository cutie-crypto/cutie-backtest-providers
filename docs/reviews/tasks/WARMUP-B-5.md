# WARMUP-B-5：provider PR #98 按仓根口径续清（tokenbeep-impl → tokenbeep-codex-5；provider 仓，≤55 轮，到 45 轮提交并回报）

接 WARMUP-B-4（`/tmp/warmup-b4/REPORT.md`，分支 `impl/warmup-b` @e2aea60，PR #98 draft）。常备令 `/Users/tf/Documents/bitbucket/TokenBeep/.claude/rules/herdr-line-standing-orders.md`；provider 仓 `/Users/tf/Documents/bitbucket/cutie-backtest-providers`，`main` 只走 PR，不 force push。先用 `git worktree list` 找到占着 `impl/warmup-b` 的树，在那棵树里做；main 已前进到 0da4fc7（#96 / #100 / #101 已合），先 `git merge origin/main`，不要 rebase，以免改写已推送的历史。
## 统领 1013 裁定（口径以此为准）
1. **全量口径 = pin 门跑的那条仓根命令**：`cd <wt>/backtesting-py && python3 -m pytest tests -q -p no:cacheprovider`（与 PIN-17 的 10646、PIN-18 的 10681 / 10683 同一口径）。子目录跑法只当定向快信号用。
2. 只有子目录口径才激活的风控簇不计入本单失败数，记成遗留一行：文件:行 + 两种口径下各多少条，登 BT-PROVIDER。
3. WARMUP-B 退出 PIN-18，单独攒 PIN-19，不设日期。**ready 判据** = 目标两簇清零 + 相对 pin 基线新增失败为 0 + 51 份金样字节不变。
## 范围
接着清目标两簇（剩 437 / 614），路子与 B-3 / B-4 相同：共享夹具 / 金样随预热语义更新，不改断言意图；先 3 个样本单文件绿，再扩到整簇。金融结果（收益、交易数）有任何变化的一律停件、列清单，不更新金样。B-3 / B-4 遗留里的「待裁 / 停件」项一律不动。
## 交付
收尾跑一次仓根全量，记 passed / failed，与 pin 基线对比新增失败（nodeid 列表必须为空）、51 份金样的字节变化数。异模型 review：Claude Sonnet 5.5 一轮只读，追加到原 review。两簇清零且满足 ready 判据才转 ready，否则保持 draft。回报 `/tmp/warmup-b5/REPORT.md`：首行 `WARMUP-B-5-DONE <7 位 sha>`，八项 ≤25 行，末行 ACK；pane 里只回首行。
