# Iteration 4 (batching)

## 汇总

| 基准 | 范式 | n | 平均分 | 满分率 | 峰值上下文(中位/最大) | 主 prompt token | 状态更新 token | 总 token | 主请求数 | 折叠次数 | 折叠错误 | 墙钟(中位, s) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| loong | fha | 5 | 0.933 | 4/5 | 34883 / 52636 | 398883 | 0 | 416649 | 3.6 | 0 | 0 | 18.2 |
| loong | sac | 5 | 0.933 | 4/5 | 32929 / 49988 | 374711 | 99211 | 488414 | 4.4 | 4 | 1 | 71.2 |
| swe | fha | 3 | 1.0 | 3/3 | 20498 / 28294 | 1206953 | 0 | 1228394 | 26.333 | 0 | 0 | 225.6 |
| swe | sac | 3 | 1.0 | 3/3 | 9223 / 13293 | 499934 | 265405 | 791201 | 22.333 | 29 | 0 | 363.4 |
| tau2 | fha | 4 | 0.571 | 1/4 | 8558.0 / 48937 | 772994 | 0 | 835012 | 8.25 | 0 | 0 | 158.9 |
| tau2 | sac | 4 | 0.702 | 1/4 | 8530.5 / 8605 | 56319 | 0 | 96814 | 2.0 | 0 | 0 | 159.4 |

## 明细

| 基准 | 任务 | 范式 | 分 | 峰值 | 请求 | 折叠 | 折叠错误 | 状态token | 墙钟 | 备注 |
|---|---|---|---|---|---|---|---|---|---|---|
| loong | financial-L2-0 | fha | 1.0 | 32945 | 2 | 0 | 0 | 0 | 9.73 | normalised match |
| loong | financial-L2-0 | sac | 1.0 | 32929 | 2 | 0 | 0 | 0 | 33.1 | normalised match |
| loong | financial-L2-5 | fha | 1.0 | 34883 | 2 | 0 | 0 | 0 | 6.49 | normalised match |
| loong | financial-L2-5 | sac | 1.0 | 8379 | 4 | 0 | 1 | 0 | 71.2 | normalised match |
| loong | legal-L2-0 | fha | 0.667 | 26640 | 8 | 0 | 0 | 0 | 198.83 | {"predicted": ["8"], "gold": ["6", "8"]} |
| loong | legal-L2-0 | sac | 0.667 | 18424 | 9 | 3 | 0 | 47370 | 243.19 | {"predicted": ["8"], "gold": ["6", "8"]} |
| loong | paper-L3-0 | fha | 1.0 | 52636 | 4 | 0 | 0 | 0 | 51.32 | {"Reference": 1.0, "Citation": 1.0} |
| loong | paper-L3-0 | sac | 1.0 | 49988 | 4 | 1 | 0 | 51841 | 93.42 | {"Reference": 1.0, "Citation": 1.0} |
| loong | paper-L3-1 | fha | 1.0 | 42923 | 2 | 0 | 0 | 0 | 18.16 | {"Reference": 1.0, "Citation": 1.0} |
| loong | paper-L3-1 | sac | 1.0 | 44990 | 3 | 0 | 0 | 0 | 33.6 | {"Reference": 1.0, "Citation": 1.0} |
| swe | sympy__sympy-24066 | fha | 1.0 | 28294 | 39 | 0 | 0 | 0 | 488.33 | {"files": ["sympy/physics/units/unitsystem.py"], "gold_files": ["sympy/physics/u |
| swe | sympy__sympy-24066 | sac | 1.0 | 13293 | 25 | 11 | 0 | 110644 | 637.18 | {"files": ["sympy/physics/units/unitsystem.py"], "gold_files": ["sympy/physics/u |
| swe | sympy__sympy-24213 | fha | 1.0 | 20498 | 25 | 0 | 0 | 0 | 225.6 | {"files": ["sympy/physics/units/unitsystem.py"], "gold_files": ["sympy/physics/u |
| swe | sympy__sympy-24213 | sac | 1.0 | 9223 | 23 | 10 | 0 | 90158 | 363.35 | {"files": ["sympy/physics/units/unitsystem.py"], "gold_files": ["sympy/physics/u |
| swe | sympy__sympy-24539 | fha | 1.0 | 11378 | 15 | 0 | 0 | 0 | 142.49 | {"files": ["sympy/polys/rings.py"], "gold_files": ["sympy/polys/rings.py"], "fil |
| swe | sympy__sympy-24539 | sac | 1.0 | 7903 | 19 | 8 | 0 | 64603 | 239.54 | {"files": ["sympy/polys/rings.py"], "gold_files": ["sympy/polys/rings.py"], "fil |
| tau2 | airline-1 | fha | 1.0 | 8261 | 2 | 0 | 0 | 0 | 71.48 | {"names": "2/2", "info_recall": 1.0, "predicted": ["get_user_details", "get_rese |
| tau2 | airline-1 | sac | 1.0 | 8261 | 2 | 0 | 0 | 0 | 50.59 | {"names": "2/2", "info_recall": 1.0, "predicted": ["get_user_details", "get_rese |
| tau2 | retail-29 | fha | 0.583 | 8596 | 2 | 0 | 0 | 0 | 156.86 | {"names": "5/6", "info_recall": 0.0, "predicted": ["find_user_id_by_name_zip", " |
| tau2 | retail-29 | sac | 0.583 | 8522 | 2 | 0 | 0 | 0 | 156.3 | {"names": "5/6", "info_recall": 0.0, "predicted": ["find_user_id_by_name_zip", " |
| tau2 | retail-36 | fha | 0.7 | 8520 | 2 | 0 | 0 | 0 | 161.02 | {"names": "1/1", "info_recall": 0.0, "predicted": ["find_user_id_by_email", "get |
| tau2 | retail-36 | sac | 0.7 | 8605 | 2 | 0 | 0 | 0 | 162.44 | {"names": "1/1", "info_recall": 0.0, "predicted": ["find_user_id_by_email", "get |
| tau2 | retail-37 | fha | 0.0 | 48937 | 27 | 0 | 0 | 0 | 566.4 | no JSON object in final answer |
| tau2 | retail-37 | sac | 0.525 | 8539 | 2 | 0 | 0 | 0 | 181.94 | {"names": "3/4", "info_recall": 0.0, "predicted": ["find_user_id_by_email", "get |
