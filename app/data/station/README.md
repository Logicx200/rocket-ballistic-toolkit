# 空间站内置轨道数据（CZ-2F 交会模块）

本目录由 `engine/common/station.py` 读取；`station.json` 是索引，`*.dat` 是 CCSDS OEM
精密星历。更新数据时**只改本目录**，不要在代码里写死日期/路径。

## 文件

| 文件 | 内容 | 来源 |
|---|---|---|
| `station.json` | 空间站元数据、OEM/TLE 索引、CZ-2F 默认交会参数 | 人工维护 |
| `CSS_OEM_20261120003641_0001.dat` | 天宫空间站 OEM 精密星历（EME2000/UTC，4 min 步长，2026-11-20 ~ 2026-11-27） | 中国载人航天工程办公室（CMSE）公开发布，BACC 生成 |

`station.json` 中 `oem.sha256` 用于核对文件完整性；替换 OEM 时同步更新 `file`、
`start_utc`、`stop_utc`、`sha256`、`sources`。

## 数据优先级（`station.resolve`）

1. 用户导入 OEM（`config.station.oem_path`）；
2. 用户 TLE（`config.station.tle`）；
3. 本目录 OEM（仅当任务时刻在 `start_utc ~ stop_utc` 内）；
4. 本目录 TLE（历元 ±45 天内；超过 20 天会警告沿迹误差）；
5. 在线拉取（CMSE 公告 ZIP → OEM；CelesTrak TLE），仅 `--allow-network` 时；
6. 失败 → 中文提示，要求用户导入。

## 更新 OEM

1. 在 <https://www.cmse.gov.cn/gfgg/zgkjzgdcs/> 下载最新「空间站轨道根数」ZIP；
2. 解压得到 `.dat/.oem`，检查头部 `REF_FRAME=EME2000`、`TIME_SYSTEM=UTC`；
3. 复制到本目录，计算 SHA256；
4. 更新 `station.json` 的 `oem` 字段（文件名/覆盖区间/SHA256/来源 URL/访问日期）。

## 注意

- OEM 是官方精密星历，优先使用；TLE 仅回退，数天后沿迹相位误差可达数度，
  会直接影响交会窗口与对接时刻。
- 数据仅供公开信息分析与教学，非实际飞行程序。
