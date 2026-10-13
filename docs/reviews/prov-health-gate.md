# PROV-HEALTH-GATE 根因和测试修复草稿

2026-10-13 JST；基线 origin/main 1e0f285（PR #97），PSF 比较点 a440830。

## 结论和计时范围

归类为计时不稳：硬性 100ms 墙钟断言把 Python GIL 争抢和宿主机调度噪声当成回测阻塞。PR #97 的 runner 在 AnyIO worker 中运行；健康端点没有等待回测限流器。健康端点仍同步构造 ccxt exchange、检查报告目录，并读取短持有的中央统计锁，不能宣称任意负载下具有 100ms 延迟保证。

基线 tests/test_provider_health_concurrency.py:50-59 测一次 ASGI /health 请求，在另一个 worker 运行 0.5s Python 忙循环时测墙钟，不是 N 个请求总耗时。已先请求一次健康端点，并等 build 启动，不含首次依赖导入和 worker 启动；包含 exchange 每次构造、文件检查、GIL 等待和 OS 调度。ASGITransport 不包含真实网络栈，不是 socket、Pre 或部署验收。

## 10 + 3 次原用例复现

同一命令：/usr/bin/python3 -m pytest -q -s tests/test_provider_health_concurrency.py。未改原测试；/tmp/prov-health-gate/run1.log 至 run10.log、load1.log 至 load3.log 记录真实 EXIT，采样脚本和 JSON 同目录。成功样本保留原输出两位小数，失败样本取原 assertion 精度。

空载仅指本任务未追加并行测试；宿主机当时已有工作，起测 load averages 33.84/28.54/26.80，不是机器实际闲置。Python 3.9.6，pytest 8.4.2，AnyIO 4.9.0，httpx 0.28.1，FastAPI 0.128.8，Starlette 0.49.3，pandas 2.3.3，backtesting 0.6.5。

| 组 | 逐次健康耗时 ms | min / median / max ms | 失败 |
|---|---|---|---|
| 无追加负载 10 次 | 100.705, 14.690, 108.135, 106.998, 58.890, 81.410, 172.001, 108.700, 82.390, 70.680 | 14.690 / 91.548 / 172.001 | 5/10 |
| 追加并行测试 3 次 | 107.234, 92.470, 120.784 | 92.470 / 107.234 / 120.784 | 2/3 |

负载文件 tests/test_q17b_patconf_guards.py，3 次均 140 passed / EXIT=0；健康文件结束时另一进程仍在执行（OVERLAP_AT_PROBE_END=True）。原门红都由延迟断言触发，未观察到健康只能在 0.5s 回测结束后返回。

## GIL 控制实验

同进程健康实现：无 CPU 时 3.809–4.202ms；默认 GIL 切换间隔 5ms 时墙钟 85.814–100.872ms，但主线程 CPU 仅 5.053–5.287ms；只在诊断进程把间隔改为 1ms，墙钟降至 24.027–45.159ms、主线程 CPU 4.374–4.760ms。diagnostic.json 保存逐次值；诊断 finally 恢复间隔，草稿没有改运行时设置。此控制支持 GIL/调度解释，不证明外部机器负载是唯一变量。

## PSF 分支比较

健康测试和 conftest 字节相同；health、run_backtest、统计快照和 capability 函数 AST 相同（source-comparison.json）。PSF 改动涉及真实策略和结果披露，本用例替换 build 后立即抛错，未进入这些策略和成交路径。

| 配对 | main ms | PSF ms | EXIT main / PSF |
|---|---|---|---|
| 1 | 5.10 | 17.07 | 0 / 0 |
| 2 | 34.67 | 43.76 | 0 / 0 |
| 3 | 49.93 | 58.19 | 0 / 0 |

配对用独立、未改动的 baseline 与 psf worktree。最初配对批次末段与草稿编辑重叠，已整组丢弃重跑，以上只引用重跑 paired.json。不能凭此前 127.911 / 166.255ms 推断 PSF 引入阻塞，也不能把全部差异严格归为外部机器负载；可确认不存在本用例命中的 PSF 健康/线程调度代码差异。

## 草稿和验证

只改健康并发测试、README 和本证据文档。保留真正的 Python CPU 忙循环，新增不忙争抢 GIL 的 held-worker 场景；后台任务由测试释放，健康必须在释放前 HTTP 200 且 ok=true。五秒 watchdog 仅防回归挂死，延迟继续打印用于诊断，不把 100ms 改成更宽的性能门。README 同步描述实际验证边界。

首次修后 6 passed；两个变异恢复后各 6 passed；3 次追加并行测试下各 6 passed，负载文件各 140 passed，均 EXIT=0。变异一取消 to_thread，在事件循环内执行 runner：2 failed / EXIT=1；变异二健康等待回测 limiter：2 failed / EXIT=1。每个变异 finally 恢复生产文件，最终生产源码与基线无 diff。compileall、git diff --check EXIT=0。未重跑全量，未验证真实 socket 或部署性能。

同形搜索范围 backtesting-py/tests 和 freqtrade，未找到其他健康 100ms 硬门。没有修改 PSF、海龟或任何最终生产代码；原 impl/psf-p2 worktree 只读。

## PIN-17 建议

10-15 JST 建议带修 pin：实施线先审草稿并合入测试修复，在最终待 pin revision 上完成完整门和运行验收后再 pin。本任务只推草稿，不合 main、不 pin；这份证据不替代 PSF 其他未通过判据。
