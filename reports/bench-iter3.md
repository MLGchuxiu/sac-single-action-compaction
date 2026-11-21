# Iteration 3 (continuity)

## 汇总

| 基准 | 范式 | n | 平均分 | 满分率 | 峰值上下文(中位/最大) | 主 prompt token | 状态更新 token | 总 token | 主请求数 | 折叠次数 | 折叠错误 | 墙钟(中位, s) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| loong | fha | 5 | 0.8 | 4/5 | 27267 / 50894 | 331986 | 0 | 354746 | 4.2 | 0 | 0 | 41.4 |
| loong | sac | 5 | 0.933 | 4/5 | 24467 / 42918 | 259776 | 188887 | 462434 | 4.4 | 9 | 3 | 66.3 |
| swe | fha | 3 | 1.0 | 3/3 | 18048 / 20470 | 867650 | 0 | 883569 | 23.667 | 0 | 0 | 254.7 |
| swe | sac | 3 | 0.667 | 2/3 | 12951 / 16488 | 562868 | 566792 | 1187612 | 24.333 | 61 | 7 | 689.8 |
| tau2 | fha | 4 | 0.527 | 1/4 | 8382.5 / 8618 | 47802 | 0 | 76624 | 2.0 | 0 | 0 | 167.2 |
| tau2 | sac | 4 | 0.571 | 1/4 | 8387.5 / 8536 | 47711 | 0 | 94216 | 2.0 | 0 | 0 | 176.2 |

## 明细

| 基准 | 任务 | 范式 | 分 | 峰值 | 请求 | 折叠 | 折叠错误 | 状态token | 墙钟 | 备注 |
|---|---|---|---|---|---|---|---|---|---|---|
| loong | financial-L2-0 | fha | 1.0 | 11691 | 6 | 0 | 0 | 0 | 41.38 | normalised match |
| loong | financial-L2-0 | sac | 1.0 | 7223 | 4 | 1 | 1 | 7072 | 56.85 | normalised match |
| loong | financial-L2-5 | fha | 1.0 | 8400 | 3 | 0 | 0 | 0 | 18.5 | normalised match |
| loong | financial-L2-5 | sac | 1.0 | 8073 | 4 | 1 | 1 | 6652 | 47.35 | normalised match |
| loong | legal-L2-0 | fha | 0.0 | 27267 | 6 | 0 | 0 | 0 | 639.14 | {"predicted": [], "gold": ["6", "8"]} |
| loong | legal-L2-0 | sac | 0.667 | 24467 | 6 | 3 | 1 | 56488 | 546.6 | {"predicted": ["8"], "gold": ["6", "8"]} |
| loong | paper-L3-0 | fha | 1.0 | 50894 | 3 | 0 | 0 | 0 | 39.92 | {"Reference": 1.0, "Citation": 1.0} |
| loong | paper-L3-0 | sac | 1.0 | 42639 | 5 | 3 | 0 | 73924 | 213.02 | {"Reference": 1.0, "Citation": 1.0} |
| loong | paper-L3-1 | fha | 1.0 | 44919 | 3 | 0 | 0 | 0 | 55.88 | {"Reference": 1.0, "Citation": 1.0} |
| loong | paper-L3-1 | sac | 1.0 | 42918 | 3 | 1 | 0 | 44751 | 66.33 | {"Reference": 1.0, "Citation": 1.0} |
| swe | sympy__sympy-24066 | fha | 1.0 | 20470 | 30 | 0 | 0 | 0 | 259.28 | {"files": ["sympy/physics/units/unitsystem.py"], "gold_files": ["sympy/physics/u |
| swe | sympy__sympy-24066 | sac | 0.0 | 16488 | 31 | 25 | 5 | 251191 | 1798.86 | empty diff |
| swe | sympy__sympy-24213 | fha | 1.0 | 18048 | 21 | 0 | 0 | 0 | 254.69 | {"files": ["sympy/physics/units/unitsystem.py"], "gold_files": ["sympy/physics/u |
| swe | sympy__sympy-24213 | sac | 1.0 | 12951 | 21 | 18 | 1 | 168920 | 689.75 | {"files": ["sympy/physics/units/unitsystem.py"], "gold_files": ["sympy/physics/u |
| swe | sympy__sympy-24539 | fha | 1.0 | 11348 | 20 | 0 | 0 | 0 | 124.03 | {"files": ["sympy/polys/rings.py"], "gold_files": ["sympy/polys/rings.py"], "fil |
| swe | sympy__sympy-24539 | sac | 1.0 | 11833 | 21 | 18 | 1 | 146681 | 632.48 | {"files": ["sympy/polys/rings.py"], "gold_files": ["sympy/polys/rings.py"], "fil |
| tau2 | airline-1 | fha | 1.0 | 8253 | 2 | 0 | 0 | 0 | 47.46 | {"names": "2/2", "info_recall": 1.0, "predicted": ["get_user_details", "get_rese |
| tau2 | airline-1 | sac | 1.0 | 8261 | 2 | 0 | 0 | 0 | 76.92 | {"names": "2/2", "info_recall": 1.0, "predicted": ["get_user_details", "get_rese |
| tau2 | retail-29 | fha | 0.583 | 5598 | 2 | 0 | 0 | 0 | 339.15 | {"names": "5/6", "info_recall": 0.0, "predicted": ["find_user_id_by_name_zip", " |
| tau2 | retail-29 | sac | 0.583 | 8514 | 2 | 0 | 0 | 0 | 122.72 | {"names": "5/6", "info_recall": 0.0, "predicted": ["find_user_id_by_name_zip", " |
| tau2 | retail-36 | fha | 0.0 | 8512 | 2 | 0 | 0 | 0 | 218.51 | no JSON object in final answer |
| tau2 | retail-36 | sac | 0.7 | 5598 | 2 | 0 | 0 | 0 | 494.91 | {"names": "1/1", "info_recall": 0.0, "predicted": ["find_user_id_by_email", "get |
| tau2 | retail-37 | fha | 0.525 | 8618 | 2 | 0 | 0 | 0 | 115.9 | {"names": "3/4", "info_recall": 0.0, "predicted": ["find_user_id_by_email", "get |
| tau2 | retail-37 | sac | 0.0 | 8536 | 2 | 0 | 0 | 0 | 229.62 | no JSON object in final answer |
