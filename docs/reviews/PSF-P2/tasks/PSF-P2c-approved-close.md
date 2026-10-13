# PSF-P2c 对照表批准后的条件收尾（统领 1013）

统领已批准三份海龟金样第22根按组止损当根成交、both第24根建空替换第51根，以及long_risk_layer 4→5组。

条件：fixture README写清两处机理：
1. both：第22根全平 → 第23根反向信号 → 第24根开盘100建空 → 原第51根那组不再出现。
2. long_risk_layer：附第22根OHLC与止损价，证明止损价落在当根区间内，且不是入场即平的幽灵单；若核到价不在区间内，停下回报，不转ready。

写好、review及全量收尾后，PR base用main（96/100已合main），转ready；回报/tmp/psf-p2c/REPORT.md，首行用新sha。本线沿用已有PR101，改base而不重复建PR，不合main、不pin、不部署。

## impl 追加收尾条件（ARM-C0-DS 之后执行）

1. fixture README 写清原生保护当根成交、只禁当根再入、次根可反向；单 TP 当根成交改变可用权益，后续数量重算（long_risk_layer 4→5）。
2. 附第 22 根 OHLC 和 long/short/both 共享止损 146/54；任何一组止损不在 [low, high] 内立即停回报、不转 ready。
3. PR101 base 改 main，rebase origin/main（#96/#100 已合 main @0e8b0ce）；python3 -m pytest tests -q -p no:cacheprovider > log 2>&1; echo EXIT=$?；全量绿转 ready，不合并（归 impl）。
4. /tmp/psf-p2c/REPORT.md 首行改 PSF-P2c-READY <sha>，末尾追加收尾结果。
