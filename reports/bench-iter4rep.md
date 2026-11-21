# Iteration 4 repeat validation (n=2)

## 汇总

| 基准 | 范式 | n | 平均分 | 满分率 | 峰值上下文(中位/最大) | 主 prompt token | 状态更新 token | 总 token | 主请求数 | 折叠次数 | 折叠错误 | 墙钟(中位, s) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| loong | fha | 10 | 0.84 | 8/10 | 36616.5 / 60865 | 890502 | 0 | 945472 | 4.7 | 0 | 0 | 37.6 |
| loong | sac | 10 | 0.79 | 7/10 | 15938.0 / 47055 | 526348 | 115864 | 683322 | 4.5 | 9 | 1 | 46.2 |
| tau2 | fha | 8 | 0.542 | 2/8 | 8526.0 / 8541 | 112598 | 0 | 192766 | 2.0 | 0 | 0 | 127.9 |
| tau2 | sac | 8 | 0.629 | 2/8 | 8527.5 / 8626 | 112779 | 0 | 184054 | 2.0 | 0 | 0 | 141.5 |

## 明细

| 基准 | 任务 | 范式 | 分 | 峰值 | 请求 | 折叠 | 折叠错误 | 状态token | 墙钟 | 备注 |
|---|---|---|---|---|---|---|---|---|---|---|
| loong | financial-L2-0 | fha | 1.0 | 8063 | 4 | 0 | 0 | 0 | 34.0 | normalised match |
| loong | financial-L2-0 | fha | 1.0 | 8198 | 6 | 0 | 0 | 0 | 59.88 | normalised match |
| loong | financial-L2-0 | sac | 1.0 | 32951 | 3 | 0 | 0 | 0 | 25.22 | normalised match |
| loong | financial-L2-0 | sac | 1.0 | 7395 | 4 | 1 | 0 | 8063 | 31.87 | normalised match |
| loong | financial-L2-5 | fha | 1.0 | 8505 | 4 | 0 | 0 | 0 | 27.64 | normalised match |
| loong | financial-L2-5 | fha | 1.0 | 8298 | 3 | 0 | 0 | 0 | 34.24 | normalised match |
| loong | financial-L2-5 | sac | 1.0 | 8264 | 3 | 0 | 0 | 0 | 9.93 | normalised match |
| loong | financial-L2-5 | sac | 1.0 | 13742 | 3 | 0 | 0 | 0 | 17.39 | normalised match |
| loong | legal-L2-0 | fha | 0.0 | 30305 | 9 | 0 | 0 | 0 | 315.96 | {"predicted": [], "gold": ["6", "8"]} |
| loong | legal-L2-0 | fha | 0.4 | 57125 | 11 | 0 | 0 | 0 | 438.83 | {"predicted": ["1", "2", "3", "4", "5", "6", "7", "8"], "gold": ["6", "8"]} |
| loong | legal-L2-0 | sac | 0.4 | 23297 | 9 | 3 | 0 | 43098 | 327.45 | {"predicted": ["1", "2", "3", "4", "5", "6", "7", "8"], "gold": ["6", "8"]} |
| loong | legal-L2-0 | sac | 0.0 | 18134 | 7 | 2 | 0 | 27638 | 351.11 | {"predicted": [], "gold": ["6", "8"]} |
| loong | paper-L3-0 | fha | 1.0 | 60865 | 3 | 0 | 0 | 0 | 34.36 | {"Reference": 1.0, "Citation": 1.0} |
| loong | paper-L3-0 | fha | 1.0 | 59317 | 3 | 0 | 0 | 0 | 56.97 | {"Reference": 1.0, "Citation": 1.0} |
| loong | paper-L3-0 | sac | 1.0 | 47055 | 3 | 0 | 0 | 0 | 35.24 | {"Reference": 1.0, "Citation": 1.0} |
| loong | paper-L3-0 | sac | 1.0 | 11349 | 6 | 2 | 0 | 24564 | 112.56 | {"Reference": 1.0, "Citation": 1.0} |
| loong | paper-L3-1 | fha | 1.0 | 42951 | 2 | 0 | 0 | 0 | 40.89 | {"Reference": 1.0, "Citation": 1.0} |
| loong | paper-L3-1 | fha | 1.0 | 42928 | 2 | 0 | 0 | 0 | 22.89 | {"Reference": 1.0, "Citation": 1.0} |
| loong | paper-L3-1 | sac | 1.0 | 42945 | 2 | 0 | 0 | 0 | 57.09 | {"Reference": 1.0, "Citation": 1.0} |
| loong | paper-L3-1 | sac | 0.5 | 11133 | 5 | 1 | 1 | 12501 | 58.81 | {"Reference": 0.0, "Citation": 1.0} |
| tau2 | airline-1 | fha | 1.0 | 8266 | 2 | 0 | 0 | 0 | 60.75 | {"names": "2/2", "info_recall": 1.0, "predicted": ["get_user_details", "get_rese |
| tau2 | airline-1 | fha | 1.0 | 8351 | 2 | 0 | 0 | 0 | 56.42 | {"names": "2/2", "info_recall": 1.0, "predicted": ["get_user_details", "get_rese |
| tau2 | airline-1 | sac | 1.0 | 8266 | 2 | 0 | 0 | 0 | 49.88 | {"names": "2/2", "info_recall": 1.0, "predicted": ["get_user_details", "get_rese |
| tau2 | airline-1 | sac | 1.0 | 8351 | 2 | 0 | 0 | 0 | 79.61 | {"names": "2/2", "info_recall": 1.0, "predicted": ["get_user_details", "get_rese |
| tau2 | retail-29 | fha | 0.583 | 8519 | 2 | 0 | 0 | 0 | 116.13 | {"names": "5/6", "info_recall": 0.0, "predicted": ["find_user_id_by_name_zip", " |
| tau2 | retail-29 | fha | 0.0 | 8527 | 2 | 0 | 0 | 0 | 232.01 | no JSON object in final answer |
| tau2 | retail-29 | sac | 0.583 | 8527 | 2 | 0 | 0 | 0 | 181.75 | {"names": "5/6", "info_recall": 0.0, "predicted": ["find_user_id_by_name_zip", " |
| tau2 | retail-29 | sac | 0.0 | 8530 | 2 | 0 | 0 | 0 | 217.4 | no JSON object in final answer |
| tau2 | retail-36 | fha | 0.7 | 8528 | 2 | 0 | 0 | 0 | 129.28 | {"names": "1/1", "info_recall": 0.0, "predicted": ["find_user_id_by_email", "get |
| tau2 | retail-36 | fha | 0.0 | 8525 | 2 | 0 | 0 | 0 | 237.85 | no JSON object in final answer |
| tau2 | retail-36 | sac | 0.7 | 8525 | 2 | 0 | 0 | 0 | 134.99 | {"names": "1/1", "info_recall": 0.0, "predicted": ["find_user_id_by_email", "get |
| tau2 | retail-36 | sac | 0.7 | 8528 | 2 | 0 | 0 | 0 | 199.49 | {"names": "1/1", "info_recall": 0.0, "predicted": ["find_user_id_by_email", "get |
| tau2 | retail-37 | fha | 0.525 | 8541 | 2 | 0 | 0 | 0 | 126.52 | {"names": "3/4", "info_recall": 0.0, "predicted": ["find_user_id_by_email", "get |
| tau2 | retail-37 | fha | 0.525 | 8541 | 2 | 0 | 0 | 0 | 205.25 | {"names": "3/4", "info_recall": 0.0, "predicted": ["find_user_id_by_email", "get |
| tau2 | retail-37 | sac | 0.525 | 8626 | 2 | 0 | 0 | 0 | 119.6 | {"names": "3/4", "info_recall": 0.0, "predicted": ["find_user_id_by_email", "get |
| tau2 | retail-37 | sac | 0.525 | 8626 | 2 | 0 | 0 | 0 | 147.95 | {"names": "3/4", "info_recall": 0.0, "predicted": ["find_user_id_by_email", "get |
