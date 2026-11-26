> 仅供公开信息分析与教学使用，非实际发射弹道。

# 给新 agent 的开场提示词（可直接粘贴）

## 提示词正文

```
你要完成一次「火箭上升段弹道预报 + 可视化」任务，工具箱在 <工作区>/：
  common/ scripts/ templates/ prompts/ docs/ + run_all.ps1
请严格按以下顺序工作：

1) 先读 README.md、prompts/02_执行SOP.md、prompts/05_数据契约.md，理解流水线与数据契约。
2) 用 prompts/01_任务提示词模板.md 把 {{...}} 换成本任务信息，形成 report/任务分解.md，
   只向用户核对“无法从公开信息确定”的项（发射时刻、航警原文、观测点坐标、是否需要反演/不确定度）。
3) 把 templates/config_cz2d.json 复制为 config.json，按 prompts/07_换箭型适配指南.md
   填写 mission / vehicle / guidance / notams / observers / grid。
   **任何公开参数都要在 sources/来源清单.md 记录 URL + 访问日期**，不确定就在配置里加 "confidence" 字段。
4) 依次运行（Windows PowerShell，先设置 PYTHONIOENCODING=utf-8 / MPLBACKEND=Agg）：
   python scripts/fetch_dem.py --config config.json      # 需要网络；已有 _pylibs/ 时先设 PYTHONPATH
   python scripts/invert.py  --config config.json        # 若配置里的参数已够好可 --quick 或跳过
   python scripts/export.py  --config config.json
   python scripts/window.py  --config config.json
   python scripts/plot_sky.py --config config.json --obs <观测点 id>
   python scripts/plot_sky_refstyle.py --config config.json --obs <观测点 id>
   python scripts/make_appearance_terrain_map.py --config config.json --obs <观测点 id>
   python scripts/plot_map.py --config config.json
   python scripts/mc.py --config config.json --n 250     # 可选
   python scripts/make_kml_refstyle.py --config config.json
   python scripts/qc.py --config config.json
5) 按 prompts/03_绘图风格规范.md 核对图件风格；按 prompts/06_自检与验收清单.md 过 DoD。
6) 写中文技术总报告 report/弹道仿真报告.md（≥12 章：任务/来源/箭型参数/模型/反演/验证/
   天空轨迹/全国地图/三维/不确定度/窗口/精度与局限/清单/来源），所有图内嵌、所有数值可追溯。
7) 最后向用户汇报：关键数字表（事件时刻/落点/入轨根数/观测点可见性）、产物清单、剩余不确定项。

铁律：
- 不编造数值；不修改其他任务工程目录；交付物中文命名；
- 观测点坐标必须用 GE 或多源核对后再算（用户给的有可能是笔误）；
- 地形遮挡/天空线用“稳健天空线”（人眼高 1.6 m + 忽略最近 200 m + 300 m 中值去噪）；
- KMZ 必须自包含且不要在 GE 里“另存到我的地点”；
- 沙箱内 multiprocessing 可能不可用，MC 一律串行；
- 任何不确定项先问用户，不要猜。
```

## 使用说明

- 若本次任务与 CZ-2D 类似（2 级、无助推器、陆上落区、南向 SSO），`templates/config_cz2d.json` 可直接当起点，只改 `mission / notams / observers` 与箭型参数。
- 若箭型不同（3 级、带助推器、海上落区），先读 `prompts/07_换箭型适配指南.md` 再填 `vehicle`。
- 若用户给了参考资料（旧任务工程、参考 KMZ/图片），把它们放到 `references/` 只读引用，**不要改动原工程**。
