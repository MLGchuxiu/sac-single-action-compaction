# Iteration 2 (cost)

## 汇总

| 基准 | 范式 | n | 平均分 | 满分率 | 峰值上下文(中位/最大) | 主 prompt token | 状态更新 token | 总 token | 主请求数 | 折叠次数 | 折叠错误 | 墙钟(中位, s) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| loong | fha | 5 | 0.4 | 2/5 | 8906 / 34881 | 135880 | 0 | 154215 | 2.4 | 0 | 0 | 29.4 |
| loong | sac | 5 | 0.48 | 2/5 | 6156 / 6581 | 85674 | 111321 | 199849 | 3.0 | 9 | 3 | 61.1 |
| swe | fha | 3 | 1.0 | 3/3 | 15332 / 29414 | 1107068 | 0 | 1126242 | 23.333 | 0 | 0 | 183.6 |
| swe | sac | 3 | 1.0 | 3/3 | 7366 / 7382 | 998821 | 1218040 | 2256279 | 48.667 | 144 | 0 | 2005.0 |
| tau2 | fha | 4 | 0.321 | 0/4 | 8530.0 / 8605 | 56318 | 0 | 112044 | 2.0 | 0 | 0 | 206.7 |
| tau2 | sac | 4 | 0.381 | 1/4 | 6750.0 / 6854 | 197884 | 314419 | 553223 | 7.75 | 27 | 0 | 412.5 |

## 明细

| 基准 | 任务 | 范式 | 分 | 峰值 | 请求 | 折叠 | 折叠错误 | 状态token | 墙钟 | 备注 |
|---|---|---|---|---|---|---|---|---|---|---|
| loong | financial-L2-0 | fha | 1.0 | 8906 | 5 | 0 | 0 | 0 | 72.98 | normalised match |
| loong | financial-L2-0 | sac | 1.0 | 6581 | 5 | 3 | 1 | 22962 | 100.96 | normalised match |
| loong | financial-L2-5 | fha | 1.0 | 34881 | 2 | 0 | 0 | 0 | 29.39 | normalised match |
| loong | financial-L2-5 | sac | 1.0 | 6156 | 3 | 1 | 1 | 9534 | 61.07 | normalised match |
| loong | legal-L2-0 | fha | 0.0 | 26335 | 5 | 0 | 0 | 0 | 230.8 | empty answer |
| loong | legal-L2-0 | sac | 0.4 | 6546 | 7 | 5 | 1 | 78825 | 206.58 | {"predicted": ["1", "2", "3", "4", "5", "6", "7", "8"], "gold": ["6", "8"]} |
| loong | paper-L3-0 | fha | 0.0 | 0 | 0 | 0 | 0 | 0 | 16.86 | no JSON object in final answer |
| loong | paper-L3-0 | sac | 0.0 | 0 | 0 | 0 | 0 | 0 | 15.17 | no JSON object in final answer |
| loong | paper-L3-1 | fha | 0.0 | 0 | 0 | 0 | 0 | 0 | 16.86 | no JSON object in final answer |
| loong | paper-L3-1 | sac | 0.0 | 0 | 0 | 0 | 0 | 0 | 17.0 | no JSON object in final answer |
| swe | sympy__sympy-24066 | fha | 1.0 | 29414 | 32 | 0 | 0 | 0 | 332.65 | {"files": ["sympy/physics/units/unitsystem.py"], "gold_files": ["sympy/physics/u |
| swe | sympy__sympy-24066 | sac | 1.0 | 7366 | 43 | 43 | 0 | 409449 | 2005.03 | {"files": ["sympy/physics/units/unitsystem.py"], "gold_files": ["sympy/physics/u |
| swe | sympy__sympy-24213 | fha | 1.0 | 15332 | 20 | 0 | 0 | 0 | 154.82 | {"files": ["sympy/physics/units/unitsystem.py"], "gold_files": ["sympy/physics/u |
| swe | sympy__sympy-24213 | sac | 1.0 | 7382 | 37 | 36 | 0 | 282745 | 1324.26 | {"files": ["sympy/physics/units/unitsystem.py"], "gold_files": ["sympy/physics/u |
| swe | sympy__sympy-24539 | fha | 1.0 | 12470 | 18 | 0 | 0 | 0 | 183.57 | {"files": ["sympy/polys/rings.py"], "gold_files": ["sympy/polys/rings.py"], "fil |
| swe | sympy__sympy-24539 | sac | 1.0 | 7244 | 66 | 65 | 0 | 525846 | 2019.32 | {"files": ["sympy/polys/rings.py"], "gold_files": ["sympy/polys/rings.py"], "fil |
| tau2 | airline-1 | fha | 0.0 | 8261 | 2 | 0 | 0 | 0 | 275.06 | no JSON object in final answer |
| tau2 | airline-1 | sac | 1.0 | 6629 | 8 | 7 | 0 | 65793 | 243.85 | {"names": "2/2", "info_recall": 1.0, "predicted": ["get_user_details", "get_rese |
| tau2 | retail-29 | fha | 0.583 | 8521 | 2 | 0 | 0 | 0 | 109.83 | {"names": "5/6", "info_recall": 0.0, "predicted": ["find_user_id_by_name_zip", " |
| tau2 | retail-29 | sac | 0.0 | 6797 | 8 | 7 | 0 | 85692 | 578.64 | no JSON object in final answer |
| tau2 | retail-36 | fha | 0.7 | 8605 | 2 | 0 | 0 | 0 | 192.06 | {"names": "1/1", "info_recall": 0.0, "predicted": ["find_user_id_by_email", "get |
| tau2 | retail-36 | sac | 0.0 | 6703 | 8 | 7 | 0 | 95309 | 550.29 | no JSON object in final answer |
| tau2 | retail-37 | fha | 0.0 | 8539 | 2 | 0 | 0 | 0 | 221.24 | no JSON object in final answer |
| tau2 | retail-37 | sac | 0.525 | 6854 | 7 | 6 | 0 | 67625 | 274.66 | {"names": "3/4", "info_recall": 0.0, "predicted": ["find_user_id_by_email", "get |
