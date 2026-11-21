# 迭代 1：论文模板 v1（FHA/EoCC/SAC 对照）

## Aggregate

| Model | Regime | State | Runs | Pass | Mean score | Peak action ctx (avg/max) | Total input tok | Total output tok | Compactions | Requests | Wall s |
|---|---|---|---|---|---|---|---|---|---|---|---|
| deepseek-v4.1-flash | eocc | - | 5 | 4/5 | 0.80 | 8,391/16,705 | 387,769 | 30,463 | 0 | 60 | 51.5 |
| deepseek-v4.1-flash | fha | - | 5 | 5/5 | 1.00 | 8,277/24,302 | 450,050 | 30,722 | 0 | 56 | 51.4 |
| deepseek-v4.1-flash | sac | v1 | 5 | 4/5 | 0.94 | 2,282/3,314 | 224,779 | 83,383 | 46 | 96 | 99.4 |
| qwen3.7-flash | eocc | - | 5 | 5/5 | 1.00 | 4,622/14,445 | 140,618 | 17,428 | 0 | 34 | 42.1 |
| qwen3.7-flash | fha | - | 5 | 4/5 | 0.83 | 23,582/109,509 | 2,024,260 | 130,409 | 0 | 50 | 206.2 |
| qwen3.7-flash | sac | v1 | 5 | 4/5 | 0.94 | 1,867/2,686 | 271,847 | 93,028 | 65 | 133 | 198.5 |

## Per task

| Task | Model | Regime | State | Score | OK | Detail | Peak ctx | Input tok | Output tok | Comp | Req |
|---|---|---|---|---|---|---|---|---|---|---|---|
| code_edit | deepseek-v4.1-flash | eocc | - | 1.00 | ✅ | all 4 regression tests pass | 3,105 | 13,754 | 2,552 | 0 | 6 |
| code_edit | deepseek-v4.1-flash | fha | - | 1.00 | ✅ | all 4 regression tests pass | 4,421 | 26,401 | 3,952 | 0 | 9 |
| code_edit | deepseek-v4.1-flash | sac | v1 | 1.00 | ✅ | all 4 regression tests pass | 2,535 | 34,661 | 11,617 | 8 | 17 |
| code_edit | qwen3.7-flash | eocc | - | 1.00 | ✅ | all 4 regression tests pass | 2,893 | 16,963 | 988 | 0 | 7 |
| code_edit | qwen3.7-flash | fha | - | 1.00 | ✅ | all 4 regression tests pass | 2,823 | 16,703 | 1,050 | 0 | 7 |
| code_edit | qwen3.7-flash | sac | v1 | 1.00 | ✅ | all 4 regression tests pass | 1,728 | 79,697 | 27,361 | 24 | 48 |
| doc_read | deepseek-v4.1-flash | eocc | - | 1.00 | ✅ | correct total 2171 | 6,913 | 40,393 | 1,789 | 0 | 9 |
| doc_read | deepseek-v4.1-flash | fha | - | 1.00 | ✅ | correct total 2171 | 6,191 | 30,369 | 1,727 | 0 | 8 |
| doc_read | deepseek-v4.1-flash | sac | v1 | 1.00 | ✅ | correct total 2171 | 2,143 | 12,190 | 3,480 | 3 | 7 |
| doc_read | qwen3.7-flash | eocc | - | 1.00 | ✅ | correct total 2171 | 2,097 | 6,616 | 847 | 0 | 4 |
| doc_read | qwen3.7-flash | fha | - | 1.00 | ✅ | correct total 2171 | 2,086 | 6,622 | 822 | 0 | 4 |
| doc_read | qwen3.7-flash | sac | v1 | 1.00 | ✅ | correct total 2171 | 1,707 | 42,402 | 10,367 | 10 | 21 |
| long_output | deepseek-v4.1-flash | eocc | - | 1.00 | ✅ | words=8348 headings=8/8 json=True json keys=['budgets', 'components', 'version'] | 16,705 | 166,306 | 15,588 | 0 | 16 |
| long_output | deepseek-v4.1-flash | fha | - | 1.00 | ✅ | words=11181 headings=8/8 json=True json keys=['budgets', 'components', 'version'] | 24,302 | 353,167 | 21,993 | 0 | 23 |
| long_output | deepseek-v4.1-flash | sac | v1 | 0.70 | ❌ | words=9563 headings=7/8 json=False  | 3,314 | 154,960 | 62,074 | 28 | 56 |
| long_output | qwen3.7-flash | eocc | - | 1.00 | ✅ | words=6365 headings=8/8 json=True json keys=['budgets', 'components', 'version'] | 14,445 | 99,029 | 13,267 | 0 | 11 |
| long_output | qwen3.7-flash | fha | - | 0.15 | ❌ | words=550 headings=3/8 json=False  | 109,509 | 1,985,039 | 126,373 | 0 | 28 |
| long_output | qwen3.7-flash | sac | v1 | 0.70 | ❌ | words=5339 headings=7/8 json=False  | 2,686 | 139,787 | 52,117 | 28 | 56 |
| multimodal | deepseek-v4.1-flash | eocc | - | 0.00 | ❌ | expected 526 | 13,218 | 159,466 | 9,212 | 0 | 24 |
| multimodal | deepseek-v4.1-flash | fha | - | 1.00 | ✅ | correct total 526 | 4,694 | 34,154 | 2,195 | 0 | 12 |
| multimodal | deepseek-v4.1-flash | sac | v1 | 1.00 | ✅ | correct total 526 | 1,873 | 14,609 | 4,342 | 4 | 9 |
| multimodal | qwen3.7-flash | eocc | - | 1.00 | ✅ | correct total 526 | 1,727 | 2,866 | 463 | 0 | 2 |
| multimodal | qwen3.7-flash | fha | - | 1.00 | ✅ | correct total 526 | 1,719 | 2,858 | 497 | 0 | 2 |
| multimodal | qwen3.7-flash | sac | v1 | 1.00 | ✅ | correct total 526 | 1,592 | 3,888 | 1,180 | 1 | 3 |
| state_retention | deepseek-v4.1-flash | eocc | - | 1.00 | ✅ | total 43357 and first code 4017 both correct | 2,015 | 7,850 | 1,322 | 0 | 5 |
| state_retention | deepseek-v4.1-flash | fha | - | 1.00 | ✅ | total 43357 and first code 4017 both correct | 1,777 | 5,959 | 855 | 0 | 4 |
| state_retention | deepseek-v4.1-flash | sac | v1 | 1.00 | ✅ | total 43357 and first code 4017 both correct | 1,547 | 8,359 | 1,870 | 3 | 7 |
| state_retention | qwen3.7-flash | eocc | - | 1.00 | ✅ | total 43357 and first code 4017 both correct | 1,948 | 15,144 | 1,863 | 0 | 10 |
| state_retention | qwen3.7-flash | fha | - | 1.00 | ✅ | total 43357 and first code 4017 both correct | 1,775 | 13,038 | 1,667 | 0 | 9 |
| state_retention | qwen3.7-flash | sac | v1 | 1.00 | ✅ | total 43357 and first code 4017 both correct | 1,620 | 6,073 | 2,003 | 2 | 5 |
