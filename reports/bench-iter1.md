# Iteration 1 (robustness)

## 汇总

| 基准 | 范式 | n | 平均分 | 满分率 | 峰值上下文(中位/最大) | 主 prompt token | 状态更新 token | 总 token | 主请求数 | 折叠次数 | 折叠错误 | 墙钟(中位, s) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| loong | fha | 5 | 0.8 | 4/5 | 26807 / 50112 | 322256 | 0 | 347225 | 4.4 | 0 | 0 | 36.2 |
| loong | sac | 5 | 0.5 | 2/5 | 6504 / 6909 | 143685 | 221870 | 371410 | 4.6 | 16 | 1 | 128.9 |
| swe | fha | 3 | 1.0 | 3/3 | 18674 / 33342 | 1367495 | 0 | 1392507 | 30.0 | 0 | 0 | 204.3 |
| swe | sac | 3 | 1.0 | 3/3 | 7161 / 7534 | 855257 | 1002242 | 1887367 | 41.667 | 120 | 4 | 960.4 |
| tau2 | fha | 4 | 0.702 | 1/4 | 8522.5 / 8539 | 56240 | 0 | 92175 | 2.0 | 0 | 0 | 128.3 |
| tau2 | sac | 4 | 0.527 | 1/4 | 6909.5 / 9714 | 130778 | 165042 | 337979 | 5.0 | 15 | 1 | 272.9 |

## 明细

| 基准 | 任务 | 范式 | 分 | 峰值 | 请求 | 折叠 | 折叠错误 | 状态token | 墙钟 | 备注 |
|---|---|---|---|---|---|---|---|---|---|---|
| loong | financial-L2-0 | fha | 1.0 | 11549 | 5 | 0 | 0 | 0 | 40.89 | normalised match |
| loong | financial-L2-0 | sac | 1.0 | 6340 | 4 | 2 | 0 | 19462 | 67.47 | normalised match |
| loong | financial-L2-5 | fha | 1.0 | 9336 | 5 | 0 | 0 | 0 | 21.03 | normalised match |
| loong | financial-L2-5 | sac | 1.0 | 6256 | 4 | 2 | 1 | 14493 | 47.92 | normalised match |
| loong | legal-L2-0 | fha | 0.0 | 26807 | 7 | 0 | 0 | 0 | 259.2 | {"predicted": [], "gold": ["6", "8"]} |
| loong | legal-L2-0 | sac | 0.0 | 6692 | 5 | 4 | 0 | 43085 | 128.91 | {"predicted": [], "gold": ["6", "8"]} |
| loong | paper-L3-0 | fha | 1.0 | 50112 | 3 | 0 | 0 | 0 | 36.24 | {"Reference": 1.0, "Citation": 1.0} |
| loong | paper-L3-0 | sac | 0.5 | 6504 | 4 | 3 | 0 | 63537 | 155.88 | {"Reference": 0.0, "Citation": 1.0} |
| loong | paper-L3-1 | fha | 1.0 | 42923 | 2 | 0 | 0 | 0 | 27.86 | {"Reference": 1.0, "Citation": 1.0} |
| loong | paper-L3-1 | sac | 0.0 | 6909 | 6 | 5 | 0 | 81293 | 162.12 | no JSON object in final answer |
| swe | sympy__sympy-24066 | fha | 1.0 | 33342 | 39 | 0 | 0 | 0 | 366.58 | {"files": ["sympy/physics/units/unitsystem.py"], "gold_files": ["sympy/physics/u |
| swe | sympy__sympy-24066 | sac | 1.0 | 7534 | 40 | 37 | 3 | 335555 | 1220.4 | {"files": ["sympy/physics/units/unitsystem.py"], "gold_files": ["sympy/physics/u |
| swe | sympy__sympy-24213 | fha | 1.0 | 16118 | 25 | 0 | 0 | 0 | 194.37 | {"files": ["sympy/physics/units/unitsystem.py"], "gold_files": ["sympy/physics/u |
| swe | sympy__sympy-24213 | sac | 1.0 | 7161 | 39 | 39 | 0 | 317582 | 960.36 | {"files": ["sympy/physics/units/unitsystem.py"], "gold_files": ["sympy/physics/u |
| swe | sympy__sympy-24539 | fha | 1.0 | 18674 | 26 | 0 | 0 | 0 | 204.33 | {"files": ["sympy/polys/rings.py"], "gold_files": ["sympy/polys/rings.py"], "fil |
| swe | sympy__sympy-24539 | sac | 1.0 | 6935 | 46 | 44 | 1 | 349105 | 915.5 | {"files": ["sympy/polys/rings.py"], "gold_files": ["sympy/polys/rings.py"], "fil |
| tau2 | airline-1 | fha | 1.0 | 8264 | 2 | 0 | 0 | 0 | 22.03 | {"names": "2/2", "info_recall": 1.0, "predicted": ["get_user_details", "get_rese |
| tau2 | airline-1 | sac | 1.0 | 6378 | 3 | 2 | 0 | 17796 | 66.14 | {"names": "2/2", "info_recall": 1.0, "predicted": ["get_user_details", "get_rese |
| tau2 | retail-29 | fha | 0.583 | 8522 | 2 | 0 | 0 | 0 | 143.7 | {"names": "5/6", "info_recall": 0.0, "predicted": ["find_user_id_by_name_zip", " |
| tau2 | retail-29 | sac | 0.583 | 6885 | 4 | 3 | 0 | 31227 | 152.28 | {"names": "5/6", "info_recall": 0.0, "predicted": ["find_user_id_by_name_zip", " |
| tau2 | retail-36 | fha | 0.7 | 8523 | 2 | 0 | 0 | 0 | 112.91 | {"names": "1/1", "info_recall": 0.0, "predicted": ["find_user_id_by_email", "get |
| tau2 | retail-36 | sac | 0.0 | 6934 | 4 | 3 | 0 | 39496 | 393.43 | no JSON object in final answer |
| tau2 | retail-37 | fha | 0.525 | 8539 | 2 | 0 | 0 | 0 | 170.35 | {"names": "3/4", "info_recall": 0.0, "predicted": ["find_user_id_by_email", "get |
| tau2 | retail-37 | sac | 0.525 | 9714 | 9 | 7 | 1 | 76523 | 454.47 | {"names": "3/4", "info_recall": 0.0, "predicted": ["find_user_id_by_email", "get |
