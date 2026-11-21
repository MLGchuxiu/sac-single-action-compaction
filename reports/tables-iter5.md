# 迭代 5：v5-v9（8 任务 × 2 模型）

## Aggregate

| Model | Regime | State | Runs | Pass | Mean score | Peak action ctx (avg/max) | Total input tok | Total output tok | Compactions | Requests | Wall s |
|---|---|---|---|---|---|---|---|---|---|---|---|
| deepseek-v4.1-flash | eocc | - | 1 | 1/1 | 1.00 | 4,389/4,389 | 33,677 | 2,015 | 0 | 11 | 19.8 |
| deepseek-v4.1-flash | fha | - | 1 | 1/1 | 1.00 | 4,237/4,237 | 24,249 | 1,946 | 0 | 8 | 16.2 |
| deepseek-v4.1-flash | sac | v5 | 8 | 7/8 | 0.88 | 2,488/3,325 | 1,032,313 | 280,899 | 205 | 415 | 216.4 |
| deepseek-v4.1-flash | sac | v6 | 8 | 8/8 | 1.00 | 3,641/6,290 | 540,440 | 124,990 | 71 | 162 | 100.8 |
| deepseek-v4.1-flash | sac | v7 | 8 | 7/8 | 0.97 | 3,745/7,933 | 449,521 | 116,969 | 65 | 148 | 94.9 |
| deepseek-v4.1-flash | sac | v8 | 8 | 8/8 | 1.00 | 4,375/8,832 | 562,524 | 117,991 | 71 | 159 | 101.8 |
| deepseek-v4.1-flash | sac | v9 | 8 | 8/8 | 1.00 | 3,642/5,758 | 457,559 | 108,670 | 70 | 160 | 93.0 |
| qwen3.7-flash | eocc | - | 1 | 1/1 | 1.00 | 2,881/2,881 | 20,642 | 1,260 | 0 | 10 | 16.2 |
| qwen3.7-flash | fha | - | 1 | 1/1 | 1.00 | 2,965/2,965 | 20,782 | 1,158 | 0 | 10 | 15.3 |
| qwen3.7-flash | sac | v5 | 8 | 5/8 | 0.66 | 2,057/2,366 | 1,030,118 | 266,210 | 241 | 485 | 390.6 |
| qwen3.7-flash | sac | v6 | 8 | 8/8 | 1.00 | 3,708/8,027 | 389,319 | 83,633 | 72 | 156 | 111.7 |
| qwen3.7-flash | sac | v7 | 8 | 8/8 | 1.00 | 3,608/6,197 | 464,390 | 104,595 | 84 | 180 | 131.1 |
| qwen3.7-flash | sac | v8 | 8 | 7/8 | 0.88 | 4,484/8,093 | 702,734 | 145,103 | 119 | 252 | 170.9 |
| qwen3.7-flash | sac | v9 | 8 | 7/8 | 0.90 | 4,324/11,602 | 336,951 | 68,750 | 66 | 146 | 107.5 |

## Per task

| Task | Model | Regime | State | Score | OK | Detail | Peak ctx | Input tok | Output tok | Comp | Req |
|---|---|---|---|---|---|---|---|---|---|---|---|
| code_edit | deepseek-v4.1-flash | sac | v5 | 1.00 | ✅ | all 4 regression tests pass | 2,639 | 153,969 | 44,103 | 32 | 64 |
| code_edit | deepseek-v4.1-flash | sac | v6 | 1.00 | ✅ | all 4 regression tests pass | 3,157 | 39,535 | 8,736 | 6 | 15 |
| code_edit | deepseek-v4.1-flash | sac | v7 | 1.00 | ✅ | all 4 regression tests pass | 3,166 | 91,133 | 23,411 | 16 | 34 |
| code_edit | deepseek-v4.1-flash | sac | v8 | 1.00 | ✅ | all 4 regression tests pass | 3,625 | 35,269 | 7,741 | 5 | 12 |
| code_edit | deepseek-v4.1-flash | sac | v9 | 1.00 | ✅ | all 4 regression tests pass | 3,026 | 95,997 | 22,062 | 18 | 39 |
| code_edit | qwen3.7-flash | sac | v5 | 0.25 | ❌ | PASS test_cents
FAIL test_invoice_total: invoice_total -> (30.0, 6.0, 36.0)
FAIL test_roun | 2,003 | 155,039 | 45,110 | 32 | 64 |
| code_edit | qwen3.7-flash | sac | v6 | 1.00 | ✅ | all 4 regression tests pass | 2,941 | 32,987 | 6,755 | 8 | 18 |
| code_edit | qwen3.7-flash | sac | v7 | 1.00 | ✅ | all 4 regression tests pass | 2,628 | 26,145 | 5,213 | 6 | 13 |
| code_edit | qwen3.7-flash | sac | v8 | 1.00 | ✅ | all 4 regression tests pass | 3,871 | 73,521 | 15,305 | 15 | 31 |
| code_edit | qwen3.7-flash | sac | v9 | 1.00 | ✅ | all 4 regression tests pass | 2,869 | 22,656 | 5,154 | 3 | 10 |
| doc_qa_multi | deepseek-v4.1-flash | sac | v5 | 1.00 | ✅ | per-question: [True, True, True, True, True, True] | 2,522 | 112,737 | 26,911 | 25 | 51 |
| doc_qa_multi | deepseek-v4.1-flash | sac | v6 | 1.00 | ✅ | per-question: [True, True, True, True, True, True] | 3,436 | 29,946 | 4,785 | 4 | 10 |
| doc_qa_multi | deepseek-v4.1-flash | sac | v7 | 1.00 | ✅ | per-question: [True, True, True, True, True, True] | 3,577 | 26,557 | 4,564 | 4 | 10 |
| doc_qa_multi | deepseek-v4.1-flash | sac | v8 | 1.00 | ✅ | per-question: [True, True, True, True, True, True] | 6,589 | 43,087 | 5,702 | 5 | 12 |
| doc_qa_multi | deepseek-v4.1-flash | sac | v9 | 1.00 | ✅ | per-question: [True, True, True, True, True, True] | 4,171 | 17,154 | 2,558 | 2 | 6 |
| doc_qa_multi | qwen3.7-flash | sac | v5 | 1.00 | ✅ | per-question: [True, True, True, True, True, True] | 2,108 | 44,375 | 11,554 | 11 | 23 |
| doc_qa_multi | qwen3.7-flash | sac | v6 | 1.00 | ✅ | per-question: [True, True, True, True, True, True] | 2,381 | 20,943 | 4,051 | 5 | 11 |
| doc_qa_multi | qwen3.7-flash | sac | v7 | 1.00 | ✅ | per-question: [True, True, True, True, True, True] | 2,605 | 13,439 | 3,373 | 3 | 7 |
| doc_qa_multi | qwen3.7-flash | sac | v8 | 1.00 | ✅ | per-question: [True, True, True, True, True, True] | 5,499 | 74,200 | 13,734 | 13 | 27 |
| doc_qa_multi | qwen3.7-flash | sac | v9 | 1.00 | ✅ | per-question: [True, True, True, True, True, True] | 4,922 | 54,421 | 10,435 | 10 | 21 |
| doc_read | deepseek-v4.1-flash | sac | v5 | 1.00 | ✅ | correct total 2171 | 2,205 | 21,044 | 5,434 | 5 | 11 |
| doc_read | deepseek-v4.1-flash | sac | v6 | 1.00 | ✅ | correct total 2171 | 4,023 | 24,234 | 4,350 | 3 | 8 |
| doc_read | deepseek-v4.1-flash | sac | v7 | 1.00 | ✅ | correct total 2171 | 2,855 | 26,414 | 4,336 | 5 | 12 |
| doc_read | deepseek-v4.1-flash | sac | v8 | 1.00 | ✅ | correct total 2171 | 3,037 | 48,988 | 6,495 | 9 | 20 |
| doc_read | deepseek-v4.1-flash | sac | v9 | 1.00 | ✅ | correct total 2171 | 2,505 | 27,083 | 4,722 | 5 | 12 |
| doc_read | qwen3.7-flash | sac | v5 | 0.00 | ❌ | expected 2171; answer had [] | 2,046 | 170,394 | 36,229 | 30 | 60 |
| doc_read | qwen3.7-flash | sac | v6 | 1.00 | ✅ | correct total 2171 | 8,027 | 33,863 | 3,843 | 3 | 8 |
| doc_read | qwen3.7-flash | sac | v7 | 1.00 | ✅ | correct total 2171 | 4,603 | 32,249 | 4,926 | 4 | 10 |
| doc_read | qwen3.7-flash | sac | v8 | 1.00 | ✅ | correct total 2171 | 8,093 | 41,904 | 5,539 | 5 | 11 |
| doc_read | qwen3.7-flash | sac | v9 | 1.00 | ✅ | correct total 2171 | 11,602 | 39,202 | 5,413 | 4 | 10 |
| long_output | deepseek-v4.1-flash | sac | v5 | 1.00 | ✅ | words=10528 headings=8/8 json=True json keys=['budgets', 'components', 'version'] | 2,845 | 192,932 | 65,188 | 37 | 75 |
| long_output | deepseek-v4.1-flash | sac | v6 | 1.00 | ✅ | words=12298 headings=8/8 json=True json keys=['budgets', 'components', 'version'] | 6,290 | 200,589 | 58,045 | 24 | 51 |
| long_output | deepseek-v4.1-flash | sac | v7 | 0.75 | ❌ | words=14153 headings=8/8 json=False json keys=['error', 'hint', 'limit_pps', 'observed_pps | 7,933 | 153,202 | 48,055 | 15 | 33 |
| long_output | deepseek-v4.1-flash | sac | v8 | 1.00 | ✅ | words=15772 headings=8/8 json=True json keys=['budgets', 'components', 'version'] | 8,832 | 237,606 | 63,622 | 23 | 48 |
| long_output | deepseek-v4.1-flash | sac | v9 | 1.00 | ✅ | words=10966 headings=8/8 json=True json keys=['budgets', 'components', 'version'] | 5,758 | 151,662 | 44,907 | 17 | 36 |
| long_output | qwen3.7-flash | sac | v5 | 1.00 | ✅ | words=9240 headings=8/8 json=True json keys=['budgets', 'components', 'version'] | 2,001 | 168,153 | 58,092 | 44 | 88 |
| long_output | qwen3.7-flash | sac | v6 | 1.00 | ✅ | words=11775 headings=8/8 json=True json keys=['budgets', 'components', 'version'] | 4,418 | 105,038 | 27,575 | 14 | 30 |
| long_output | qwen3.7-flash | sac | v7 | 1.00 | ✅ | words=11960 headings=8/8 json=True json keys=['budgets', 'components', 'version'] | 6,197 | 92,733 | 24,733 | 10 | 23 |
| long_output | qwen3.7-flash | sac | v8 | 1.00 | ✅ | words=7437 headings=8/8 json=True json keys=['budgets', 'components', 'version'] | 4,604 | 115,563 | 25,665 | 16 | 36 |
| long_output | qwen3.7-flash | sac | v9 | 0.20 | ❌ | words=885 headings=4/8 json=False  | 3,298 | 9,305 | 3,075 | 1 | 4 |
| million_repo | deepseek-v4.1-flash | eocc | - | 1.00 | ✅ | settlement tests pass | 4,389 | 33,677 | 2,015 | 0 | 11 |
| million_repo | deepseek-v4.1-flash | fha | - | 1.00 | ✅ | settlement tests pass | 4,237 | 24,249 | 1,946 | 0 | 8 |
| million_repo | deepseek-v4.1-flash | sac | v5 | 1.00 | ✅ | settlement tests pass | 2,330 | 162,267 | 38,457 | 40 | 80 |
| million_repo | deepseek-v4.1-flash | sac | v6 | 1.00 | ✅ | settlement tests pass | 2,829 | 37,698 | 7,613 | 7 | 16 |
| million_repo | deepseek-v4.1-flash | sac | v7 | 1.00 | ✅ | settlement tests pass | 2,993 | 72,134 | 17,747 | 13 | 28 |
| million_repo | deepseek-v4.1-flash | sac | v8 | 1.00 | ✅ | settlement tests pass | 3,226 | 41,699 | 7,188 | 7 | 16 |
| million_repo | deepseek-v4.1-flash | sac | v9 | 1.00 | ✅ | settlement tests pass | 3,728 | 67,601 | 16,073 | 12 | 26 |
| million_repo | qwen3.7-flash | eocc | - | 1.00 | ✅ | settlement tests pass | 2,881 | 20,642 | 1,260 | 0 | 10 |
| million_repo | qwen3.7-flash | fha | - | 1.00 | ✅ | settlement tests pass | 2,965 | 20,782 | 1,158 | 0 | 10 |
| million_repo | qwen3.7-flash | sac | v5 | 1.00 | ✅ | settlement tests pass | 2,090 | 144,712 | 36,399 | 40 | 80 |
| million_repo | qwen3.7-flash | sac | v6 | 1.00 | ✅ | settlement tests pass | 3,022 | 54,213 | 10,256 | 13 | 27 |
| million_repo | qwen3.7-flash | sac | v7 | 1.00 | ✅ | settlement tests pass | 3,214 | 61,792 | 11,037 | 14 | 29 |
| million_repo | qwen3.7-flash | sac | v8 | 1.00 | ✅ | settlement tests pass | 3,651 | 47,141 | 8,097 | 9 | 19 |
| million_repo | qwen3.7-flash | sac | v9 | 1.00 | ✅ | settlement tests pass | 3,260 | 83,859 | 16,848 | 19 | 39 |
| multimodal | deepseek-v4.1-flash | sac | v5 | 1.00 | ✅ | correct total 526 | 1,856 | 11,607 | 1,951 | 3 | 7 |
| multimodal | deepseek-v4.1-flash | sac | v6 | 1.00 | ✅ | correct total 526 | 2,802 | 21,807 | 3,228 | 3 | 9 |
| multimodal | deepseek-v4.1-flash | sac | v7 | 1.00 | ✅ | correct total 526 | 2,593 | 17,326 | 3,514 | 3 | 8 |
| multimodal | deepseek-v4.1-flash | sac | v8 | 1.00 | ✅ | correct total 526 | 2,832 | 38,103 | 5,290 | 7 | 17 |
| multimodal | deepseek-v4.1-flash | sac | v9 | 1.00 | ✅ | correct total 526 | 2,870 | 29,749 | 5,263 | 5 | 13 |
| multimodal | qwen3.7-flash | sac | v5 | 1.00 | ✅ | correct total 526 | 1,911 | 50,117 | 13,427 | 14 | 29 |
| multimodal | qwen3.7-flash | sac | v6 | 1.00 | ✅ | correct total 526 | 2,292 | 11,511 | 2,375 | 2 | 6 |
| multimodal | qwen3.7-flash | sac | v7 | 1.00 | ✅ | correct total 526 | 2,898 | 5,892 | 2,963 | 1 | 3 |
| multimodal | qwen3.7-flash | sac | v8 | 1.00 | ✅ | correct total 526 | 2,914 | 5,969 | 1,661 | 1 | 3 |
| multimodal | qwen3.7-flash | sac | v9 | 1.00 | ✅ | correct total 526 | 2,533 | 9,914 | 2,397 | 2 | 5 |
| state_retention | deepseek-v4.1-flash | sac | v5 | 1.00 | ✅ | total 43357 and first code 4017 both correct | 2,181 | 27,249 | 7,321 | 7 | 15 |
| state_retention | deepseek-v4.1-flash | sac | v6 | 1.00 | ✅ | total 43357 and first code 4017 both correct | 2,190 | 11,089 | 2,001 | 2 | 6 |
| state_retention | deepseek-v4.1-flash | sac | v7 | 1.00 | ✅ | total 43357 and first code 4017 both correct | 2,562 | 18,521 | 4,770 | 3 | 9 |
| state_retention | deepseek-v4.1-flash | sac | v8 | 1.00 | ✅ | total 43357 and first code 4017 both correct | 2,587 | 12,674 | 2,856 | 2 | 6 |
| state_retention | deepseek-v4.1-flash | sac | v9 | 1.00 | ✅ | total 43357 and first code 4017 both correct | 2,604 | 33,017 | 5,961 | 7 | 18 |
| state_retention | qwen3.7-flash | sac | v5 | 1.00 | ✅ | total 43357 and first code 4017 both correct | 1,933 | 45,059 | 11,776 | 14 | 29 |
| state_retention | qwen3.7-flash | sac | v6 | 1.00 | ✅ | total 43357 and first code 4017 both correct | 2,610 | 37,045 | 9,438 | 9 | 19 |
| state_retention | qwen3.7-flash | sac | v7 | 1.00 | ✅ | total 43357 and first code 4017 both correct | 2,395 | 68,654 | 17,478 | 18 | 38 |
| state_retention | qwen3.7-flash | sac | v8 | 1.00 | ✅ | total 43357 and first code 4017 both correct | 1,987 | 27,292 | 5,181 | 8 | 17 |
| state_retention | qwen3.7-flash | sac | v9 | 1.00 | ✅ | total 43357 and first code 4017 both correct | 2,027 | 25,855 | 5,159 | 7 | 16 |
| swe_multifile | deepseek-v4.1-flash | sac | v5 | 0.00 | ❌ | lf.assertEqual(warehouse.available("A"), 6)
AssertionError: 2 != 6

====================== | 3,325 | 350,508 | 91,534 | 56 | 112 |
| swe_multifile | deepseek-v4.1-flash | sac | v6 | 1.00 | ✅ | all 8 regression tests pass | 4,401 | 175,542 | 36,232 | 22 | 47 |
| swe_multifile | deepseek-v4.1-flash | sac | v7 | 1.00 | ✅ | all 8 regression tests pass | 4,282 | 44,234 | 10,572 | 6 | 14 |
| swe_multifile | deepseek-v4.1-flash | sac | v8 | 1.00 | ✅ | all 8 regression tests pass | 4,270 | 105,098 | 19,097 | 13 | 28 |
| swe_multifile | deepseek-v4.1-flash | sac | v9 | 1.00 | ✅ | all 8 regression tests pass | 4,474 | 35,296 | 7,124 | 4 | 10 |
| swe_multifile | qwen3.7-flash | sac | v5 | 0.00 | ❌ |     self.assertEqual(warehouse.available("A"), 6)
AssertionError: 2 != 6

================ | 2,366 | 252,269 | 53,623 | 56 | 112 |
| swe_multifile | qwen3.7-flash | sac | v6 | 1.00 | ✅ | all 8 regression tests pass | 3,977 | 93,719 | 19,340 | 18 | 37 |
| swe_multifile | qwen3.7-flash | sac | v7 | 1.00 | ✅ | all 8 regression tests pass | 4,321 | 163,486 | 34,872 | 28 | 57 |
| swe_multifile | qwen3.7-flash | sac | v8 | 0.00 | ❌ | test suite failed to import: agent removed inventory.orders.total (corrected post-hoc) | 5,256 | 317,144 | 69,921 | 52 | 108 |
| swe_multifile | qwen3.7-flash | sac | v9 | 1.00 | ✅ | all 8 regression tests pass | 4,077 | 91,739 | 20,269 | 20 | 41 |
