# 62-2a Provider compiler / kernel / capability 复核记录

日期：2026-07-15
分支：`codex/62-2a-provider-kernel`
基线：`8cfe18e5fd7d82c50f7f5079e4171b4e333352dd`

## 结论

本分支已实现 62-2a 的 Provider 本地基础：完整 StrategySpec v2/manifest 静态编译、Provider-local capability/hash、artifact execution wire 校验、历史 replay 与 future paper 共用的单一 `StrategyKernel.evaluate`、result.v2/trace/evidence 构造，以及 legacy 七个固定策略的兼容路由。

当前结论为 **Provider-local Source + Local tests Pass，等待同一独立 reviewer 复验，不自行宣布验收通过**。candidate `d8f6878101f6322ac129e3f91c4b01cfd9193fa2` 经 reviewer task `019f6622-1f0f-7d32-b13b-f1517d54c45c` 首次判定 NOT ACCEPT；两个 P1、一个 P2 的直接修复在复验中均通过，但修复 candidate `560b499085733c6bd6ff7c61448707a936019646` 又因 warmup 可绕过 365 天数据访问预算被同一 reviewer 判定 NOT ACCEPT。本次按 Owner 授权采用保守、fail-closed、最小且可回滚的收口并加入回归。Owner 决定的 transform 收窄仍保持不变。本分支没有部署 Provider、没有接触 Pre、Fan、RN 或真实中心数据，因此这些证据仍为 **Blocked / Cannot assess**，不能由本地通过替代；TokenBeep 共享 fixture、Connector 和 Server 也尚未同步，不能把本记录解释为跨仓合同已经对齐。

## 冻结合同与实现边界

- 只读对照 TokenBeep 的 `SPEC_策略工件与统一执行契约.md`、`IMPL_策略接入与不可变版本.md` 和共享 capability fixture；冻结 SPEC 未修改。
- Provider 本地 fixture 保留 `binance_futures` 等 62-2a exact-set，但 `data_transforms=[]`，新 SHA-256 为 `ab659c29d5feb6f0691a1ef1a1a7a0b9db71619279ac4a397bd9b6c2a0e5f00a`。该 hash 仅代表 Provider-local payload；TokenBeep 共享 fixture 仍需后续串行生成并同步其消费者。
- 只有 `local.strategy_spec_v2.compiler` 在 immutable lowercase revision 下出现并声明 capability；七个 legacy tool 不声明 artifact capability。revision 未锁定时 compiler tool 不进入 catalog。
- artifact-shaped 或 partial artifact 请求一律 fail-closed，不回退 fixed tool、ccxt 或默认策略。
- 当前 Provider capability 是 exact subset（binary `add`、`gt`、`crosses_above`）。编译器和 kernel 已实现 `add/mul/min/max` 变参 `>=2`、`sub/div/compare/cross` 固定二元、`abs` 固定一元；只有 capability 精确出现实际 `arg_types` 长度的签名时才可执行，不从二元签名推断三元签名。

## Owner 决策与本次增量

- Owner 决定：62-2a 暂不声明 `combine_first.v1`、`ffill_after_close.v1`、`flow_dilution_shifted.v1`；62-2b 真正实现并验证零前视语义后，才能升级 fixture/hash 恢复声明。
- `capability_payload()` 与 Provider fixture 都明确输出 `data_transforms=[]`；catalog producer、execution validator 和 evidence 继续复用同一 payload/hash，没有另一个隐式广告源。
- `data_requirements[*].allowed_transforms` 仍按冻结 manifest 结构投影到 `capability_requirements.data_transforms`。三项 canonical transform 名称可被 manifest parser 识别，但因不在 Provider capability 中，会在 `compile_strategy()` 的 capability membership 检查稳定返回 `ERR_STRATEGY_SPEC_UNSUPPORTED`；HTTP 路径同样在任何中心数据访问前失败关闭。
- empty transforms 的 exact-complete-stream artifact 继续通过；本次没有加入 transform dispatcher、synthetic frame builder、coverage transform 记录或任何 62-2b/62-2c 生产逻辑。

## 独立 reviewer NOT ACCEPT 修复

- P1，feature 90 天分块：中心 `/metrics` 使用 `start<=ts<=end` 闭区间；Provider 现在把本地 `[cursor, chunk_end)` 精确映射为远端 `[cursor, chunk_end-1]`，相邻分块不再重复边界点。90 天、90 天加一个日点、365 天分别验证 90/91/365 个精确点；真实重复或冲突 timestamp 仍返回 `ERR_STRATEGY_COVERAGE_INCOMPLETE`，没有用 dedupe 掩盖脏数据。
- P1，artifact 最大区间：`EXECUTION_MAX_RANGE_DAYS=365` 移到 execution validator，并由 catalog 同源导入。恰好 365 天允许进入数据路径；365 天加 1 秒和 366 天均在任何 fetch 前返回 `ERR_STRATEGY_SPEC_INVALID`。没有再保留“只广告、不执行”的第二套常量。
- P2，trusted execution params：`initial_state(validated.plan, params)` 前移到数据循环之前，后续 simulation 复用同一个 state。空 instrument rules、rules symbol mismatch、fee mismatch、slippage mismatch 均在任何中心抓数前失败关闭。
- P1，warmup 数据访问预算：复验确认 execution 仅 1 天但 feature warmup 400 天时，旧 validator 会通过并开始中心分块调用。按 Owner 的保守决定，本次不另发明产品级 warmup 数字，而是复用 `EXECUTION_MAX_RANGE_DAYS=365` 作为每个 requirement 的实际 adapter data window 上限；编译和 artifact hash 绑定完成后，统一计算 `max(0, start_at - warmup_bars * interval)` 到 `end_at` 的窗口，超限时在任何 K-line/feature adapter 前返回 `ERR_STRATEGY_SPEC_INVALID`，path 稳定锚定 `$.artifact_manifest.data_requirements[i].warmup_bars`。
- warmup 边界：总实际窗口恰好 365 天允许进入抓数，365 天加最小一个 interval step 在抓数前失败；365 天 execution 加任意正 warmup 保守失败，零 warmup 保持通过；冻结 SPEC 的 720h warmup 示例在合法短 execution 下仍可进入抓数。这是 62-2a 的保守预算收口，不代表未来合同不能显式扩展。
- 同类搜索：Provider 内只有 `_fetch_artifact_features` 使用 `/metrics` 分块；K-line 使用另一 endpoint/adapter，未复用该闭区间路径。所有 artifact adapter 都由同一个 execution validator 前置，逐 requirement 的预算检查会在任一数据循环开始前完成。

## 本地实现检查

- StrategySpec/manifest 顶层与嵌套 exact-key、排序、静态类型、hash/binding、data requirement 和 source material 校验。
- capability 顶层/嵌套 exact-key、16 KiB、数组上限、排序去重、静态类型与真实 operator arity 校验。
- Decimal128 precision 34 / ROUND_HALF_EVEN；integer safe range；next-open、tick/qty、long/short gap、same-bar priority、三种 sizing、双边 fee/slippage、time/signal/end-of-data 语义。
- closed FeatureFrame、rolling_sum、lag/cross 防前视；同一 source/interval 可绑定多个 feature，不互相覆盖。
- execution request 的 11-key exact-set、Snowflake string、artifact/spec/manifest/capability/revision/result-contract 绑定；Server 注入的 trusted `instrument_rules` exact object。
- result.v2 五键、trade 十键、coverage/trace/evidence 和各 canonical hash；中心数据声明不匹配、空流、gap、未覆盖完整对齐区间均失败关闭。
- validator 仅对白名单字段放行 7..64 lowercase `provider_revision` 与 64 lowercase `strategy_execution_capability_hash`；同形状值放在无关字段仍被 secret scan 拒绝。
- 已搜索 Provider repo 内 `capability_payload`、`capability_hash`、catalog `strategy_execution_capability*`、execution `expected_capability_hash`、fixture、focused tests 与 review 的全部使用点；producer 与 Provider-local fixture/hash 同步收窄，未发现第二套 transform capability producer。

## 验证证据

| 层级 | 结果 | 命令 / 证据 |
|---|---|---|
| Focused compiler/kernel/API | Pass | `python3 -m pytest -q backtesting-py/tests/test_strategy_kernel.py` → `53 passed`；在原 48 个 case 上新增 5 个 case，覆盖 1 天 execution + 400 天 feature warmup 的双 adapter 零调用、总实际窗口恰好 365 天与多一个 step、365 天 execution 的正 warmup 拒绝，以及 720h warmup + 合法短 execution；既有分块、参数前置拒绝、transform fail-closed、exact replay、paper tick、Decimal、exit/sizing、result.v2、trace/evidence 与 replay-frame conformance 同套通过 |
| Provider + validator + installer tests | Pass | `PYTHONPATH=validator python3 -m pytest -q backtesting-py/tests validator/tests scripts/tests --import-mode=importlib` → `174 passed` |
| Format | Pass | Black 对 `cutie_backtesting_provider.py` 的改动行 ranges 检查，对 `strategy_execution.py`、focused tests 全文件检查；isort `--check-only --profile black` 对三个改动 Python 文件通过 |
| Lint | Pass | flake8 对三个改动 Python 文件通过；Provider 仅忽略仓库既有 `E501,E741,F841`，新模块/tests 仅按 Black 口径忽略 `E501` |
| Diff hygiene | Pass | `git diff --check`、`git diff --cached --check`；提交后复跑 `8cfe18e..HEAD`、`d8f6878..HEAD` 与 `560b499..HEAD` |
| Pre / deployed Provider | Blocked | 未部署、未读取节点 revision、未调用真实 catalog/backtest |
| 中心数据 / transform | Cannot assess | 未调用真实 Binance Vision/中心 K 线/CoinGlass；无 Provider runtime evidence |
| Fan / RN | Cannot assess | 不在本分支执行范围，未接触设备 |

测试仅出现 FastAPI `on_event` deprecation、macOS LibreSSL/urllib3 三条既有 warning，不改变断言结果。

## 残余风险与后续边界

1. 62-2b 的 coverage 目录、真实 freshness/gap incident、`combine_first.v1`、`ffill_after_close.v1`、`flow_dilution_shifted.v1` 及零前视 fixtures 尚未实现。本分支只允许 exact complete streams；任何上述 transform 请求都在 capability 阶段 fail-closed，不能视为 62-2b 完成。
2. TokenBeep 共享 capability fixture、Connector 与 Server 仍需由编排器后续串行同步并分别验证；在此之前可能存在旧 hash/旧 transform 列表的跨仓不一致。本 Provider-local commit 不代表共享 fixture 已更新，也不代表跨仓验收完成。
3. `funding=included` 因 result.v2 无 funding ledger，按冻结合同返回 unsupported。
4. paper runner 未部署；本地 `paper_tick` 只证明与 replay 共用同一 `evaluate` 及 ledger，不证明 62-3 runtime。
5. 上线前仍需 immutable revision 下的真实 `/health`、`/catalog`、artifact request、Connector callback/readback 和 Pre 证据。
