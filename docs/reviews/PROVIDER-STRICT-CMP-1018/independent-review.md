# 异模型只读审查

模型：claude-opus-5-5；effort high；退出码 0。Codex 已核实并在 review.md 记录取舍。以下为原始输出，未作为远端或 advisor 验收。

PASS

结论：6 个核查点里没有发现阻断合并的问题。我只读了代码，没有跑任何命令。下面的测试数字（85 passed、248 passed、变异 6 failed）都来自 Codex 写的 review.md，我没有独立复跑，也没有打开 .runtime 下的日志。

**1. 默认兼容：通过**
- 三个 schema 默认值都是 True（`strategy_macro_events.py:43`、`:50`，`strategy_funding_reversal.py:40`）。
- 不传这个参数时取默认值：`strategy_macro_events.py:97`、`strategy_funding_reversal.py:113`。
- `_threshold_not_met` 在 inclusive=True 时用 `<`，和旧判据一致（`strategy_macro_events.py:114-115`）。
- 零 surprise 仍由 `delta == 0` 先挡掉（`:286`），严格模式和 `surprise_threshold=0` 的组合也有测试。
- H1 和 liquidation 不读这个参数，范围之外的行为没变。

**2. 严格边界和正负方向：通过**
- 比较前都先取绝对值（`:287`、`:296-297`），正负两侧对称；方向判定的代码没改。
- 测试的数值在等值点上算出来是精确的：
  - H2 用 Decimal(str(2.9)) 加减 0.1/0.09/0.11；
  - H3 的 `move_pct` 在 `:175-180` 用 Decimal 计算，(105-100)/100*100 正好是 5；
  - funding 是 0.0005×100 = 0.05 pct，和 Decimal(str(0.05)) 相等。
- 所以 "equal" 这一档确实测到了等号，不会被浮点误差蒙混过去。

**3. 校验和白名单：通过**
- HTTP 路径先用 `_validate_params_against_schema` 校验（`cutie_backtesting_provider.py:6599`、`:6619`），里面的 boolean 分支是原有代码（`:9075-9077`，用 `type is bool`，所以 0/1 会被拒）。然后才解析配置，最后才取数。
- 直接调用 `MacroConfig.parse`（`:100-101`）和 `FundingReversalConfig.parse`（`:113-115`）也会拒非法类型。
- 目录里的参数和白名单都从 schema 派生（`:7695`），测试断言了 `additionalProperties=False`。
- 仓库里没有需要同步的目录快照或固定 JSON。

**4. assumptions 是否诚实：通过，有一处轻微不对称**
- 三个工具都回显了 `threshold_inclusive` 的实际值（`:343` 的 `**config.values`，以及 `strategy_funding_reversal.py:228`、`:278`）。
- H2 的 threshold 文案会在 gte/gt 之间切换（`:361-363`），funding 的 `rate_rule` 也会切换（`:231-235`）。
- H3 的 assumptions 只有布尔值，没有 gte/gt 的规则文案（`strategy_macro_events.py:365`）。不算失实，但和另外两个工具不一致。
- H2/H3 的目录描述是通用句（`:7690`），没有写死“含等号”，不存在过期措辞。

**5. 测试和变异能证明什么：通过，有两处覆盖不完全**
- 54 个边界用例都走真实的 HTTP 和引擎，也覆盖了交易方向、跳过原因码和 assumptions。
- 变异只把严格模式改回了旧判据，打掉了 6 条“等值+严格”的用例。反过来那个方向（把含等号模式误改成严格）没有专门做变异，但“等值+默认/含等号”的 12 条用例会挡住它；这只是推断，没有实际跑过。
- 非法类型的 HTTP 用例里，schema 校验和 parse 都会报同一条消息，单删一层这个用例照样通过。它证明的是两层合起来能在取数前拒绝，单独删掉 HTTP 那层校验它察觉不到。这层是原有代码，影响低。
- 外部行情都用 mock，只能算本地证据，不能当远端、Pre 或服务端集成的证明。

**6. 有无范围外改动：通过**
- diff 只涉及三个 strategy/provider 文件、Q18 契约文档、新测试文件和 review 文档。
- funding 目录描述的改动属于本单范围。没有服务端（甲-2）、pin 或发布相关改动。

**发现的问题（都不阻断合并）**
- 低：严格模式下“刚好等于阈值”仍沿用 `*_below_threshold` 原因码，字面上不准确，但契约文档里已经说明（`docs/Q18-macro-event-contract.md` 新增小节）。
- 低：H3 的 assumptions 缺 gte/gt 规则文案（`strategy_macro_events.py:365`），可以在以后的版本补上，不影响这次合并。

合并前 advisor 亲读由统领另行组织，本审查不代签。
