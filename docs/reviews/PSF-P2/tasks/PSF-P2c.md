# PSF-P2c：海龟按「保护单当根按触发价成交」口径（tokenbeep-impl → tokenbeep-codex-5；PSF-P2 收尾单，金样变化须统领批）

常备令 `/Users/tf/Documents/bitbucket/TokenBeep/.claude/rules/herdr-line-standing-orders.md`。≤45 轮，到 38 轮没做完就提交推送已完成部分并回报剩余。
**优先级**：`/tmp/psf-p2b3/REPORT.md` 一出现，先暂停本单，回去收 PSF-BOTTOM-GAP（衔接新 tip 后跑一次全量，转 ready），完成后再回到本单。

## 底稿
`/tmp/psf-p2/REPORT2.md` 第 12 行，以及上一轮被还原的候选（代码现为 30d6a07 版本）。
旧口径：第 22 根触发止损后排市价单，第 23 根开盘 140 才退出；`_turtle_sync` 在退出成交那根返回 True，挡掉了第 23 根的反向信号。
新口径：第 22 根按组止损 146 全平，只禁止第 22 根入场；第 23 根可以接反向信号，第 24 根开盘 100 建空组，原来第 51 根开盘 91 的空组被替换。第 21 根加仓会更新全组止损。
`turtle_both_golden.json` 入场 51/91 → 24/100（出场 6→6）。

## 做法
从 `origin/impl/psf-p2` 起分支 `impl/psf-p2c`（等 P2b-3 推了新 tip 后再衔接），单独 PR，base 用 impl/psf-p2，排在 #96、#100 之后。把海龟改到与七族同口径：保护单当根按触发价成交，跳空取开盘；组止损随加仓更新，各 unit 共享。金样一律用现成重算机制重算，不手改 JSON。
**对照表（必交，统领批之前 PR 保持 draft）**：每个变化的海龟金样一行，写「文件 · 用例 · 旧组数 / 新组数 · 入场根 旧→新 · 出场根 / 价 旧→新」。另外用一段 ≤5 行说明：资金更早释放后，后续组数为什么变了。
用例：补 2 条——组止损当根全平；退出根之后的下一根可以接反向信号。变异 1 条：恢复「下一根开盘退出」⇒ 两条红；cp 还原后回绿。
门：单文件快信号；全量只在收尾跑一次，`> /tmp/psf-p2c/full.log 2>&1; echo "EXIT=$?"`。相对 impl/psf-p2 基线，除海龟以外零变化。异模型 review：Claude Sonnet 5.5 一轮，追加到 provider `docs/reviews/` 的 PSF-P2 review 文件。

## 回报
`/tmp/psf-p2c/REPORT.md`：首行 `PSF-P2c-DONE <provider 7 位 sha>`，八项 ≤25 行，加对照表 ≤10 行，末行 ACK；pane 只回首行。
