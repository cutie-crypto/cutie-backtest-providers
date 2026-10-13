（provider PR #98 续做，pin 窗 10-16，≤55 轮，到 45 轮提交推送并回报剩余）
（tokenbeep-impl → tokenbeep-codex-3；从 codex-2 队列改派。常备令 `/Users/tf/Documents/bitbucket/TokenBeep/.claude/rules/herdr-line-standing-orders.md`）
接 provider `impl/warmup-b` @e2b1565（draft PR #98）。上一单回报 `/tmp/warmup-b/REPORT.md`，逐条失败归因在 `/tmp/warmup-b/full-failure-attribution.tsv`、`full-comparison.json`；任务书原件 `/tmp/warmup-b/WARMUP-B.md`。先 fetch，rebase 到最新 origin/main（无冲突就 rebase；有冲突就停下列出来）。
本单只做一件：全量门清到「本单引入失败 = 0」。
- 先把 2895 failed / 6 errors 按根因聚类（大概率是旧 HTTP 模拟 / 完整响应金样没带新的 warmup 字段），每类写一行「根因 · 条数 · 代表用例」。
- 是口径随显式预热变化的：用现成重算机制重算金样，硬编码断言按新口径改，不手改 JSON。
- **入场根、笔数、方向任一变化就停在该文件**，列出「文件 · 用例 · 旧 / 新」，不硬改。
- 基线（origin/main 同一命令）就红的归「基线红」，不修。
- 上一单遗留的「高周期完整冻结复放」「1M 非固定网格」两件不在本单，写进遗留。
门：单文件快信号；全量只在收尾跑一次，`cmd > /tmp/warmup-b2/full.log 2>&1; echo "EXIT=$?"`，不接管道。全绿才把 PR #98 转 ready，否则保持 draft。返修超过 100 行，过一轮 Claude Sonnet 5.5 增量 review，追加到原 review 文件。不合 main，不 pin。
回报 `/tmp/warmup-b2/REPORT.md`：首行 `WARMUP-B-2-DONE <provider 7 位 sha>`，八项 ≤25 行，聚类表 ≤8 行，末行 ACK；pane 只回首行。
