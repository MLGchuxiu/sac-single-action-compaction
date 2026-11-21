# Iteration 0 (baseline)

## 汇总

| 基准 | 范式 | n | 平均分 | 满分率 | 峰值上下文(中位/最大) | 主 prompt token | 状态更新 token | 总 token | 主请求数 | 折叠次数 | 折叠错误 | 墙钟(中位, s) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| loong | fha | 5 | 0.8 | 4/5 | 29273 / 42923 | 259467 | 0 | 282310 | 3.8 | 0 | 0 | 64.1 |
| loong | sac | 5 | 0.933 | 4/5 | 6670 / 42638 | 182082 | 217703 | 406876 | 4.8 | 15 | 3 | 156.2 |
| swe | fha | 3 | 1.0 | 3/3 | 14068 / 39301 | 1656933 | 0 | 1688505 | 30.0 | 0 | 0 | 249.4 |
| swe | sac | 3 | 1.0 | 3/3 | 8802 / 12948 | 1111738 | 1231700 | 2387362 | 53.667 | 148 | 10 | 1192.5 |
| tau2 | fha | 4 | 0.527 | 1/4 | 8527.5 / 8544 | 56353 | 0 | 92943 | 2.0 | 0 | 0 | 123.8 |
| tau2 | sac | 4 | 0.556 | 1/4 | 9367.0 / 10423 | 144404 | 142126 | 342194 | 5.25 | 13 | 4 | 295.4 |

## 明细

| 基准 | 任务 | 范式 | 分 | 峰值 | 请求 | 折叠 | 折叠错误 | 状态token | 墙钟 | 备注 |
|---|---|---|---|---|---|---|---|---|---|---|
| loong | financial-L2-0 | fha | 1.0 | 8178 | 4 | 0 | 0 | 0 | 80.99 | normalised match |
| loong | financial-L2-0 | sac | 1.0 | 6571 | 5 | 2 | 1 | 16008 | 156.15 | normalised match |
| loong | financial-L2-5 | fha | 1.0 | 35141 | 3 | 0 | 0 | 0 | 12.4 | normalised match |
| loong | financial-L2-5 | sac | 1.0 | 6440 | 5 | 3 | 1 | 23683 | 91.86 | normalised match |
| loong | legal-L2-0 | fha | 0.0 | 29273 | 5 | 0 | 0 | 0 | 655.96 | {"predicted": [], "gold": ["6", "8"]} |
| loong | legal-L2-0 | sac | 0.667 | 6670 | 6 | 5 | 0 | 60117 | 257.79 | {"predicted": ["8"], "gold": ["6", "8"]} |
| loong | paper-L3-0 | fha | 1.0 | 17958 | 5 | 0 | 0 | 0 | 64.08 | {"Reference": 1.0, "Citation": 1.0} |
| loong | paper-L3-0 | sac | 1.0 | 42638 | 3 | 1 | 1 | 53235 | 148.79 | {"Reference": 1.0, "Citation": 1.0} |
| loong | paper-L3-1 | fha | 1.0 | 42923 | 2 | 0 | 0 | 0 | 35.91 | {"Reference": 1.0, "Citation": 1.0} |
| loong | paper-L3-1 | sac | 1.0 | 6961 | 5 | 4 | 0 | 64660 | 223.61 | {"Reference": 1.0, "Citation": 1.0} |
| swe | sympy__sympy-24066 | fha | 1.0 | 39301 | 50 | 0 | 0 | 0 | 623.09 | {"files": ["sympy/physics/units/unitsystem.py"], "gold_files": ["sympy/physics/u |
| swe | sympy__sympy-24066 | sac | 1.0 | 12948 | 37 | 31 | 5 | 276649 | 1150.58 | {"files": ["sympy/physics/units/unitsystem.py"], "gold_files": ["sympy/physics/u |
| swe | sympy__sympy-24213 | fha | 1.0 | 13019 | 18 | 0 | 0 | 0 | 131.38 | {"files": ["sympy/physics/units/unitsystem.py"], "gold_files": ["sympy/physics/u |
| swe | sympy__sympy-24213 | sac | 1.0 | 8090 | 77 | 75 | 1 | 599253 | 1524.09 | {"files": ["sympy/physics/units/unitsystem.py"], "gold_files": ["sympy/physics/u |
| swe | sympy__sympy-24539 | fha | 1.0 | 14068 | 22 | 0 | 0 | 0 | 249.39 | {"files": ["sympy/polys/rings.py"], "gold_files": ["sympy/polys/rings.py"], "fil |
| swe | sympy__sympy-24539 | sac | 1.0 | 8802 | 47 | 42 | 4 | 355798 | 1192.5 | {"files": ["sympy/polys/rings.py"], "gold_files": ["sympy/polys/rings.py"], "fil |
| tau2 | airline-1 | fha | 1.0 | 8354 | 2 | 0 | 0 | 0 | 54.36 | {"names": "2/2", "info_recall": 1.0, "predicted": ["get_user_details", "get_rese |
| tau2 | airline-1 | sac | 1.0 | 8545 | 8 | 6 | 1 | 68724 | 414.65 | {"names": "2/2", "info_recall": 1.0, "predicted": ["get_user_details", "get_rese |
| tau2 | retail-29 | fha | 0.583 | 8527 | 2 | 0 | 0 | 0 | 72.33 | {"names": "5/6", "info_recall": 0.0, "predicted": ["find_user_id_by_name_zip", " |
| tau2 | retail-29 | sac | 0.0 | 9599 | 6 | 4 | 1 | 42163 | 359.0 | no JSON object in final answer |
| tau2 | retail-36 | fha | 0.0 | 8528 | 2 | 0 | 0 | 0 | 254.3 | no JSON object in final answer |
| tau2 | retail-36 | sac | 0.7 | 9135 | 3 | 1 | 1 | 10565 | 213.44 | {"names": "1/1", "info_recall": 0.0, "predicted": ["find_user_id_by_email", "get |
| tau2 | retail-37 | fha | 0.525 | 8544 | 2 | 0 | 0 | 0 | 175.29 | {"names": "3/4", "info_recall": 0.0, "predicted": ["find_user_id_by_email", "get |
| tau2 | retail-37 | sac | 0.525 | 10423 | 4 | 2 | 1 | 20674 | 231.74 | {"names": "3/4", "info_recall": 0.0, "predicted": ["find_user_id_by_email", "get |
