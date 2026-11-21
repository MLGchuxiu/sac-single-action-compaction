# 迭代 2：刚性结构 v2

## Aggregate

| Model | Regime | State | Runs | Pass | Mean score | Peak action ctx (avg/max) | Total input tok | Total output tok | Compactions | Requests | Wall s |
|---|---|---|---|---|---|---|---|---|---|---|---|
| deepseek-v4.1-flash | sac | v2 | 7 | 7/7 | 1.00 | 2,248/3,460 | 623,822 | 189,463 | 149 | 304 | 172.8 |
| qwen3.7-flash | sac | v2 | 7 | 2/7 | 0.42 | 1,676/1,817 | 708,679 | 164,747 | 214 | 430 | 253.0 |

## Per task

| Task | Model | Regime | State | Score | OK | Detail | Peak ctx | Input tok | Output tok | Comp | Req |
|---|---|---|---|---|---|---|---|---|---|---|---|
| code_edit | deepseek-v4.1-flash | sac | v2 | 1.00 | ✅ | all 4 regression tests pass | 2,245 | 91,163 | 25,612 | 23 | 47 |
| code_edit | qwen3.7-flash | sac | v2 | 0.25 | ❌ | PASS test_cents
FAIL test_invoice_total: invoice_total -> (30.0, 6.0, 36.0)
FAIL test_roun | 1,633 | 106,832 | 21,944 | 32 | 64 |
| doc_qa_multi | deepseek-v4.1-flash | sac | v2 | 1.00 | ✅ | per-question: [True, True, True, True, True, True] | 2,107 | 39,068 | 8,812 | 11 | 23 |
| doc_qa_multi | qwen3.7-flash | sac | v2 | 1.00 | ✅ | per-question: [True, True, True, True, True, True] | 1,734 | 46,751 | 11,623 | 15 | 31 |
| doc_read | deepseek-v4.1-flash | sac | v2 | 1.00 | ✅ | correct total 2171 | 1,957 | 42,635 | 9,825 | 13 | 27 |
| doc_read | qwen3.7-flash | sac | v2 | 0.00 | ❌ | expected 2171; answer had [] | 1,689 | 128,876 | 23,834 | 30 | 60 |
| long_output | deepseek-v4.1-flash | sac | v2 | 1.00 | ✅ | words=11285 headings=8/8 json=True json keys=['budgets', 'components', 'version'] | 3,460 | 207,226 | 80,438 | 39 | 79 |
| long_output | qwen3.7-flash | sac | v2 | 0.70 | ❌ | words=8201 headings=7/8 json=False  | 1,694 | 132,135 | 37,457 | 44 | 88 |
| multimodal | deepseek-v4.1-flash | sac | v2 | 1.00 | ✅ | correct total 526 | 1,650 | 17,223 | 3,277 | 5 | 11 |
| multimodal | qwen3.7-flash | sac | v2 | 1.00 | ✅ | correct total 526 | 1,649 | 16,427 | 4,816 | 5 | 11 |
| state_retention | deepseek-v4.1-flash | sac | v2 | 1.00 | ✅ | total 43357 and first code 4017 both correct | 1,647 | 6,384 | 1,344 | 2 | 5 |
| state_retention | qwen3.7-flash | sac | v2 | 0.00 | ❌ | expected total 43357 and first code 4017 | 1,518 | 72,591 | 20,814 | 32 | 64 |
| swe_multifile | deepseek-v4.1-flash | sac | v2 | 1.00 | ✅ | all 8 regression tests pass | 2,667 | 220,123 | 60,155 | 56 | 112 |
| swe_multifile | qwen3.7-flash | sac | v2 | 0.00 | ❌ |     self.assertEqual(warehouse.available("A"), 6)
AssertionError: 2 != 6

================ | 1,817 | 205,067 | 44,259 | 56 | 112 |
