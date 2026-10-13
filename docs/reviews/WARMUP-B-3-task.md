# WARMUP-B-3：PR #98 全量失败继续清（tokenbeep-impl → tokenbeep-codex-3；provider，≤60 轮，到 50 轮提交并推送已完成部分后回报）

接你的 WARMUP-B-2（impl/warmup-b @2840fa9；归因 `full-failure-attribution.tsv`）。本单只清两个最大的簇：
① 「完整响应 / 指标金样未适配」649 条（例 `test_10b2d_sizing_range_calendar.py::test_omitted_sizing_keys_match_1f5b8dc_bytes[red_streak_rsi-default]`）；
② 「新严格拒跑与旧 HTTP 成功期望冲突」728 条（例 `test_risk_quantity_hand_calculated[red_streak_default]`）。
做法：先取每簇 3 个样本找共同根因。如果根因是测试夹具 / 旧期望没跟上 WARMUP-B 的口径变化，就批量改夹具或用现成重算机制重算金样，不手改 JSON，不用 xfail。如果根因是实现把本不该拒跑的拒了，就改实现，并在回报里写清。
**不许**改变 51 个金样里的交易、净值、指标（B-2 已证零变化，保持住）；凡会改变交易的，停下来列进「待裁」。
其余簇（22 / 188 / 177）和两条待裁（`test_patconf2b1_patterns.py:441` 六组、`P6.py:157` 十响应入场变化）本单不碰，原样列在回报第 8 项。
快信号：每簇样本文件单跑；收尾全量一次，`> /tmp/warmup-b3/full.log 2>&1; echo "EXIT=$?"`，报 passed / failed 数，与 B-2 的 8682 / 1987 对比。PR 保持 draft。异模型 review：Claude Sonnet 5.5 增量一轮，追加原 review 文件。
回报 `/tmp/warmup-b3/REPORT.md`：首行 `WARMUP-B-3-DONE <provider 7 位 sha>`，八项 ≤25 行，末行 ACK；pane 只回首行。
