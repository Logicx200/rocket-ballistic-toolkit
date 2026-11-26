# 地形瓦片目录说明（运行时缓存，不随交付）

本目录用于存放 SRTM 1″（SRTMGL1）地形瓦片，**默认不随程序附送**（每片约 12–25 MB）。

程序的作用顺序：

1. **自动扫描**本机已有瓦片（历史工程里的 `data/dem/gz|hgt`），同盘时用硬链接导入，不额外占盘；
2. 若用户勾选「DEM 在线补全」且联网，则从 AWS Open Data（`elevation-tiles-prod/skadi`）按需下载；
3. 都拿不到时，自动回退到**内嵌粗 DEM**（`app/data/china_dem_0p02.npz`，0.02° ≈ 2 km），
   天空轨迹的地形遮挡角精度会下降（首次出现时刻误差可达数十秒），报告/日志会明确标注。

目录结构（运行时自动创建）：

```
data/dem/
├─ gz/           下载的 .hgt.gz 瓦片（保存用）
├─ hgt/          解压后的 .hgt 瓦片（25,934,402 字节 = 3601×3601 int16）
├─ cache/        观测点 3″ 局部缓存 <observer_id>.npz
├─ srtm_tiles.json   已下载瓦片清单
└─ dem_mode.json     最近一次运行的地形模式（srtm / coarse / none），供报告引用
```

在免安装版里，该目录位于 `%LOCALAPPDATA%\RocketKit\dem`（因为 exe 目录可能不可写）。

要用真实 SRTM 瓦片出正式结论：在界面「运行选项」勾选「DEM 在线补全」，
或把已有的 `gz/hgt` 瓦片直接拷进本目录后重算。
