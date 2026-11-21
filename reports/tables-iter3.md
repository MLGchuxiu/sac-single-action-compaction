# 迭代 3：刚性结构 + 动作摘要 v3

## Aggregate

| Model | Regime | State | Runs | Pass | Mean score | Peak action ctx (avg/max) | Total input tok | Total output tok | Compactions | Requests | Wall s |
|---|---|---|---|---|---|---|---|---|---|---|---|
| deepseek-v4.1-flash | sac | v3 | 7 | 6/7 | 0.96 | 2,246/3,268 | 725,497 | 215,046 | 153 | 311 | 183.6 |
| qwen3.7-flash | sac | v3 | 7 | 4/7 | 0.72 | 1,674/1,913 | 684,465 | 182,871 | 160 | 324 | 250.2 |

## Per task

| Task | Model | Regime | State | Score | OK | Detail | Peak ctx | Input tok | Output tok | Comp | Req |
|---|---|---|---|---|---|---|---|---|---|---|---|
| code_edit | deepseek-v4.1-flash | sac | v3 | 1.00 | ✅ | all 4 regression tests pass | 2,424 | 136,654 | 36,991 | 30 | 61 |
| code_edit | qwen3.7-flash | sac | v3 | 0.25 | ❌ | PASS test_cents
FAIL test_invoice_total: invoice_total -> (30.0, 6.0, 36.0)
FAIL test_roun | 1,781 | 141,951 | 36,474 | 32 | 64 |
| doc_qa_multi | deepseek-v4.1-flash | sac | v3 | 1.00 | ✅ | per-question: [True, True, True, True, True, True] | 2,067 | 30,187 | 6,138 | 7 | 15 |
| doc_qa_multi | qwen3.7-flash | sac | v3 | 1.00 | ✅ | per-question: [True, True, True, True, True, True] | 1,688 | 47,019 | 6,550 | 9 | 19 |
| doc_read | deepseek-v4.1-flash | sac | v3 | 1.00 | ✅ | correct total 2171 | 1,926 | 43,667 | 9,195 | 12 | 25 |
| doc_read | qwen3.7-flash | sac | v3 | 1.00 | ✅ | correct total 2171 | 1,561 | 15,467 | 3,393 | 5 | 11 |
| long_output | deepseek-v4.1-flash | sac | v3 | 0.75 | ❌ | words=16929 headings=8/8 json=False  | 3,268 | 232,717 | 83,547 | 44 | 88 |
| long_output | qwen3.7-flash | sac | v3 | 0.45 | ❌ | words=3067 headings=5/8 json=False  | 1,687 | 149,245 | 44,463 | 44 | 88 |
| multimodal | deepseek-v4.1-flash | sac | v3 | 1.00 | ✅ | correct total 526 | 1,491 | 4,015 | 583 | 1 | 3 |
| multimodal | qwen3.7-flash | sac | v3 | 1.00 | ✅ | correct total 526 | 1,620 | 20,013 | 4,761 | 6 | 13 |
| state_retention | deepseek-v4.1-flash | sac | v3 | 1.00 | ✅ | total 43357 and first code 4017 both correct | 1,836 | 10,217 | 2,622 | 3 | 7 |
| state_retention | qwen3.7-flash | sac | v3 | 1.00 | ✅ | total 43357 and first code 4017 both correct | 1,467 | 19,478 | 5,262 | 8 | 17 |
| swe_multifile | deepseek-v4.1-flash | sac | v3 | 1.00 | ✅ | all 8 regression tests pass | 2,708 | 268,040 | 75,970 | 56 | 112 |
| swe_multifile | qwen3.7-flash | sac | v3 | 0.38 | ❌ | ering element 0:
'd'
'b'

- ['d', 'a']
?   ^    ^

+ ['b', 'c']
?   ^    ^


============= | 1,913 | 291,292 | 81,968 | 56 | 112 |
