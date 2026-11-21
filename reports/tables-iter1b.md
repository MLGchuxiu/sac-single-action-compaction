# 迭代 1b：v1 在加大步数预算后的复测

## Aggregate

| Model | Regime | State | Runs | Pass | Mean score | Peak action ctx (avg/max) | Total input tok | Total output tok | Compactions | Requests | Wall s |
|---|---|---|---|---|---|---|---|---|---|---|---|
| deepseek-v4.1-flash | sac | v1 | 5 | 4/5 | 0.80 | 2,714/3,352 | 713,016 | 234,221 | 141 | 284 | 264.9 |
| qwen3.7-flash | sac | v1 | 5 | 3/5 | 0.65 | 1,906/2,066 | 612,996 | 197,157 | 152 | 306 | 365.0 |

## Per task

| Task | Model | Regime | State | Score | OK | Detail | Peak ctx | Input tok | Output tok | Comp | Req |
|---|---|---|---|---|---|---|---|---|---|---|---|
| code_edit | deepseek-v4.1-flash | sac | v1 | 1.00 | ✅ | all 4 regression tests pass | 2,483 | 128,445 | 38,159 | 32 | 64 |
| code_edit | qwen3.7-flash | sac | v1 | 0.25 | ❌ | PASS test_cents
FAIL test_invoice_total: invoice_total -> (30.0, 6.0, 36.0)
FAIL test_roun | 1,884 | 140,202 | 53,797 | 32 | 64 |
| doc_qa_multi | deepseek-v4.1-flash | sac | v1 | 1.00 | ✅ | per-question: [True, True, True, True, True, True] | 2,141 | 11,028 | 2,981 | 2 | 5 |
| doc_qa_multi | qwen3.7-flash | sac | v1 | 1.00 | ✅ | per-question: [True, True, True, True, True, True] | 1,944 | 36,289 | 11,043 | 12 | 25 |
| doc_read | deepseek-v4.1-flash | sac | v1 | 1.00 | ✅ | correct total 2171 | 2,369 | 28,437 | 8,509 | 7 | 15 |
| doc_read | qwen3.7-flash | sac | v1 | 1.00 | ✅ | correct total 2171 | 1,787 | 28,814 | 8,446 | 8 | 17 |
| long_output | deepseek-v4.1-flash | sac | v1 | 1.00 | ✅ | words=11559 headings=8/8 json=True json keys=['budgets', 'components', 'version'] | 3,352 | 207,537 | 78,622 | 44 | 88 |
| long_output | qwen3.7-flash | sac | v1 | 1.00 | ✅ | words=7554 headings=8/8 json=True json keys=['budgets', 'components', 'version'] | 1,851 | 147,025 | 58,009 | 44 | 88 |
| swe_multifile | deepseek-v4.1-flash | sac | v1 | 0.00 | ❌ | lf.assertEqual(warehouse.available("A"), 6)
AssertionError: 2 != 6

====================== | 3,227 | 337,569 | 105,950 | 56 | 112 |
| swe_multifile | qwen3.7-flash | sac | v1 | 0.00 | ❌ |     self.assertEqual(warehouse.available("A"), 6)
AssertionError: 2 != 6

================ | 2,066 | 260,666 | 65,862 | 56 | 112 |
