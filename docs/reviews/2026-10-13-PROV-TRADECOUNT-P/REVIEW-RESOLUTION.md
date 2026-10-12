# Claude review 处理与验证口径

独立 reviewer：Claude CLI，无工具、完整 diff + 新测试 + ledger/组装器/runner/HTTP当前源文件静态审查。原文见 CLAUDE-REVIEW.md。结论 PASS：0阻断/高/中，1低级测试建议，1消费者目录核对项。reviewer 未执行测试，运行证据由实施线独立提供。

低级建议已采纳：增加仅部分卖出且没有lot清零的 fixture（权重 .2/.4/.3，期望字符串1）；原有跨lot清仓 fixture仍期望2。避免目录检查污染全局 TOOL_SPECS，传 deepcopy 后检查声明。
目录消费者核对已完成：connector仅检查metrics字符串数组、server保留目录结构，详见CONSUMER.md。严格三键的结果validateResultV4缺口保留，只读且未修。
手写金样只新增metrics.trade_count="2"；JSON删除此字段后与origin/main逐结构相等。无其他哈希或字节金样重生。
变异：删portfolio_result_v4.py的trade_count赋值，test_v4_outlet_closed_count_string[runner-2]指名红（KeyError trade_count，1 failed，EXIT=1）。cp还原，cmp EXIT=0；新测试10 passed EXIT=0。review修订后最终定向/全量见VALIDATION.md。
初次定向曾因旧helper返回tuple及旧金样失败，已修；两个初次日志共写产生混合，正式绿证据仅使用target-green.log/final-target.log。
初次全量为补review用例中断（EXIT=130），不作为验收；最终全量在修订后重跑。
