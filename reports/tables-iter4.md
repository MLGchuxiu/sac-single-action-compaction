# 迭代 4：混合状态 v4

## Aggregate

| Model | Regime | State | Runs | Pass | Mean score | Peak action ctx (avg/max) | Total input tok | Total output tok | Compactions | Requests | Wall s |
|---|---|---|---|---|---|---|---|---|---|---|---|
| deepseek-v4.1-flash | sac | v4 | 7 | 7/7 | 1.00 | 2,459/3,145 | 542,282 | 132,755 | 92 | 191 | 115.5 |
| qwen3.7-flash | sac | v4 | 7 | 3/7 | 0.54 | 2,225/2,844 | 1,554,761 | 405,621 | 225 | 452 | 732.6 |

## Per task

| Task | Model | Regime | State | Score | OK | Detail | Peak ctx | Input tok | Output tok | Comp | Req |
|---|---|---|---|---|---|---|---|---|---|---|---|
| code_edit | deepseek-v4.1-flash | sac | v4 | 1.00 | ✅ | all 4 regression tests pass | 2,643 | 117,140 | 23,649 | 22 | 45 |
| code_edit | qwen3.7-flash | sac | v4 | 1.00 | ✅ | all 4 regression tests pass | 1,937 | 199,965 | 56,502 | 32 | 64 |
| doc_qa_multi | deepseek-v4.1-flash | sac | v4 | 1.00 | ✅ | per-question: [True, True, True, True, True, True] | 2,402 | 38,984 | 6,665 | 8 | 17 |
| doc_qa_multi | qwen3.7-flash | sac | v4 | 0.00 | ❌ | no JSON answer object found | 2,358 | 291,270 | 48,919 | 32 | 64 |
| doc_read | deepseek-v4.1-flash | sac | v4 | 1.00 | ✅ | correct total 2171 | 2,299 | 18,761 | 3,718 | 4 | 9 |
| doc_read | qwen3.7-flash | sac | v4 | 0.00 | ❌ | expected 2171; answer had [] | 1,966 | 250,072 | 35,500 | 30 | 60 |
| long_output | deepseek-v4.1-flash | sac | v4 | 1.00 | ✅ | words=13827 headings=8/8 json=True json keys=['budgets', 'components', 'version'] | 3,145 | 184,867 | 60,004 | 28 | 57 |
| long_output | qwen3.7-flash | sac | v4 | 0.75 | ❌ | words=4718 headings=8/8 json=False  | 2,844 | 277,624 | 150,845 | 44 | 88 |
| multimodal | deepseek-v4.1-flash | sac | v4 | 1.00 | ✅ | correct total 526 | 1,693 | 4,405 | 801 | 1 | 3 |
| multimodal | qwen3.7-flash | sac | v4 | 1.00 | ✅ | correct total 526 | 1,629 | 4,259 | 1,165 | 1 | 3 |
| state_retention | deepseek-v4.1-flash | sac | v4 | 1.00 | ✅ | total 43357 and first code 4017 both correct | 2,074 | 16,512 | 3,378 | 4 | 9 |
| state_retention | qwen3.7-flash | sac | v4 | 1.00 | ✅ | total 43357 and first code 4017 both correct | 2,146 | 127,716 | 51,762 | 30 | 61 |
| swe_multifile | deepseek-v4.1-flash | sac | v4 | 1.00 | ✅ | all 8 regression tests pass | 2,958 | 161,613 | 34,540 | 25 | 51 |
| swe_multifile | qwen3.7-flash | sac | v4 | 0.00 | ❌ |     self.assertEqual(warehouse.available("A"), 6)
AssertionError: 2 != 6

================ | 2,692 | 403,855 | 60,928 | 56 | 112 |
