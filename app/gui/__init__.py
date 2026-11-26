# -*- coding: utf-8 -*-
"""火箭弹道工具箱 v3.0 图形界面（Tkinter）。

模块划分
--------
* :mod:`app.gui.main_window` —— 主窗口、菜单/工具栏、分页调度、后台线程与队列
* :mod:`app.gui.actions`     —— 任务文件 / 工作区设置 / 环境自检等窗口动作
* :mod:`app.gui.tab_*`       —— 六个分页：任务信息 / 航警 / 观测点 / 运行选项 /
                               数据体检 / 运行与结果
* :mod:`app.gui.widgets`     —— 通用控件（表格、文本框、日志面板、对话框）
* :mod:`app.gui.theme`       —— 配色、字体与 ttk 样式
* :mod:`app.gui.help_text`   —— 使用说明与关于文本

入口：``from app.gui.main_window import launch; launch()``。
界面只调用 ``app.core``，不直接 import engine 脚本。
"""
