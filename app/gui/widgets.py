# -*- coding: utf-8 -*-
"""通用界面控件与工具函数（v4 重构版）。

在 v3 的基础上补了四类东西：

* **卡片 / 字段布局**：``card`` / ``field`` / ``kv_grid``，让各页面排版一致；
* **单次全局滚轮绑定**：v3 每个可滚动页都 ``bind_all("<MouseWheel>")``，7 个页面就挂
  7 个回调且永不解除；v4 改为顶层窗口只绑一次；
* **对话框基类** ``Dialog``：统一标题/说明/按钮栏/居中/模态/关闭语义；
* **可搜索的对话框表格**（``SearchableTree``）与路径选择行（``PathRow``）。
"""
from __future__ import annotations

import datetime
import os
import traceback
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import theme


# --------------------------------------------------------------------------- #
# 可滚动页面
# --------------------------------------------------------------------------- #
class ScrollablePage(ttk.Frame):
    """带纵横滚动条的页面容器；把内容放进 ``self.body``。

    滚轮只在指针位于本页时才处理；绑定挂在顶层窗口上并且只绑一次（见
    :func:`install_wheel_binding`），避免 v3 那样每个页面挂一个全局回调。
    """

    def __init__(self, master, padding=(0, 0)):
        super().__init__(master, padding=padding)
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        bg = theme.WINDOW_BG
        self.canvas = tk.Canvas(self, highlightthickness=0, background=bg,
                                borderwidth=0)
        self.vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.hsb = ttk.Scrollbar(self, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=self.vsb.set,
                              xscrollcommand=self.hsb.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.vsb.grid(row=0, column=1, sticky="ns")
        self.hsb.grid(row=1, column=0, sticky="ew")
        self.body = ttk.Frame(self.canvas, style="TFrame")
        self._win = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.body.bind("<Configure>", self._on_body)
        self.canvas.bind("<Configure>", self._on_canvas)
        install_wheel_binding(self)

    @property
    def scrollable(self) -> bool:
        content = max(1, self.body.winfo_reqheight())
        visible = max(1, self.canvas.winfo_height())
        return content > visible + 2

    def _on_body(self, _event=None):
        try:
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))
            # 内容不超出可视区时不显示滚动条（减少视觉噪声）
            self.vsb.grid() if self.scrollable else self.vsb.grid_remove()
            need_h = self.body.winfo_reqwidth() > max(1, self.canvas.winfo_width()) + 2
            self.hsb.grid() if need_h else self.hsb.grid_remove()
        except tk.TclError:
            pass

    def _on_canvas(self, event):
        """宽度始终等于可视区。

        v3 这里取 ``max(可视宽, 内容请求宽)``：只要某一行文字过长就会撑出整页横向
        滚动条，右侧按钮也会被裁掉。v4 固定为可视宽度，长内容交给控件自己的滚动条
        （表格/文本框）或 wraplength 处理。
        """
        try:
            self.canvas.itemconfigure(self._win, width=max(200, int(event.width)))
        except tk.TclError:
            pass

    def scroll_to_top(self) -> None:
        try:
            self.canvas.yview_moveto(0.0)
        except tk.TclError:
            pass


#: 顶层 Tk 是否已经绑定过全局滚轮
_WHEEL_INSTALLED: set = set()


def install_wheel_binding(page: "ScrollablePage") -> None:
    """在页面所属顶层窗口上绑定一次滚轮回调（幂等）。"""
    try:
        top = page.winfo_toplevel()
    except tk.TclError:
        return
    key = str(top)
    if key in _WHEEL_INSTALLED:
        return
    _WHEEL_INSTALLED.add(key)

    def _on_wheel(event):
        try:
            w = top.winfo_containing(event.x_root, event.y_root)
        except tk.TclError:
            return
        # 找到指针所在的 ScrollablePage 祖先
        target = None
        cur = w
        while cur is not None:
            if isinstance(cur, ScrollablePage):
                target = cur
                break
            cur = getattr(cur, "master", None)
        if target is None:
            return
        # 内部自带滚动的控件不抢滚轮
        if w is not None and w.winfo_class() in ("Text", "Treeview", "Listbox"):
            return
        step = int(-event.delta / 120)
        if step == 0 and event.delta:
            step = -1 if event.delta > 0 else 1
        try:
            if event.state & 0x0001:      # Shift + 滚轮 = 横向
                target.canvas.xview_scroll(step, "units")
            else:
                target.canvas.yview_scroll(step, "units")
        except tk.TclError:
            pass

    try:
        top.bind_all("<MouseWheel>", _on_wheel, add="+")
    except tk.TclError:
        pass


# --------------------------------------------------------------------------- #
# 小标签 / 布局 / 卡片
# --------------------------------------------------------------------------- #
def hint(master, text: str, wraplength: int = 0, style: str = "Hint.TLabel"):
    """小号灰字提示。"""
    lb = ttk.Label(master, text=text, style=style, justify="left")
    if wraplength:
        lb.configure(wraplength=wraplength)
    return lb


def section(master, text: str):
    return ttk.Label(master, text=text, style="Section.TLabel")


def page_title(master, title: str, subtitle: str = "") -> ttk.Frame:
    """页面标题 + 一句说明（每个步骤页顶部统一使用）。"""
    box = ttk.Frame(master)
    ttk.Label(box, text=title, style="PageTitle.TLabel").pack(anchor="w")
    if subtitle:
        ttk.Label(box, text=subtitle, style="Subtitle.TLabel",
                  wraplength=980, justify="left").pack(anchor="w", pady=(2, 0))
    return box


def badge(master, text: str, kind: str = "Badge") -> ttk.Label:
    """状态徽标（kind: Badge / BadgeOk / BadgeWarn / BadgeErr / BadgeMuted）。"""
    return ttk.Label(master, text=text, style="%s.TLabel" % kind)


class Card(ttk.Frame):
    """白底卡片：标题行（可带右侧操作区）+ 内容区 ``self.body``。"""

    def __init__(self, master, title: str = "", hint_text: str = "",
                 padding=(12, 10)):
        super().__init__(master, style="Card.TFrame")
        self.columnconfigure(0, weight=1)
        head = ttk.Frame(self, style="Panel.TFrame")
        head.grid(row=0, column=0, sticky="ew", padx=padding[0],
                  pady=(padding[1], 0))
        head.columnconfigure(1, weight=1)
        self.title_label = ttk.Label(head, text=title, style="CardTitle.TLabel")
        self.title_label.grid(row=0, column=0, sticky="w")
        self.action_bar = ttk.Frame(head, style="Panel.TFrame")
        self.action_bar.grid(row=0, column=2, sticky="e")
        if not title:
            self.title_label.grid_remove()
        self.body = ttk.Frame(self, style="Panel.TFrame")
        self.body.grid(row=1, column=0, sticky="nsew", padx=padding[0],
                       pady=(8, padding[1]))
        self.body.columnconfigure(1, weight=1)
        self.rowconfigure(1, weight=1)
        if hint_text:
            self.hint = ttk.Label(self, text=hint_text, style="CardHint.TLabel",
                                  justify="left", wraplength=980)
            self.hint.grid(row=2, column=0, sticky="ew", padx=padding[0],
                           pady=(0, padding[1]))
        else:
            self.hint = None

    def add_action(self, text: str, command: Callable, style: str = "Ghost.TButton"):
        btn = ttk.Button(self.action_bar, text=text, command=command, style=style)
        btn.pack(side="left", padx=(6, 0))
        return btn


def field(master, row: int, label: str, widget_factory, hint_text: str = "",
          label_width: int = 12, pady: int = 5, column: int = 0) -> ttk.Frame:
    """在 ``master`` 的网格里放一行"标签 + 控件 + 可选提示"。

    ``widget_factory`` 是 ``f(box) -> widget`` 的工厂：控件**必须**以传入的 box 为父容器
    （Tk 的 pack 不能把控件塞进"非父容器"的 frame 里——早期版本正是这么写的，
    结果输入框直接不显示）。
    """
    ttk.Label(master, text=label, style="Panel.TLabel", width=label_width,
              anchor="e").grid(row=row, column=column, sticky="e",
                               padx=(0, 10), pady=pady)
    box = ttk.Frame(master, style="Panel.TFrame")
    box.grid(row=row, column=column + 1, sticky="w", pady=pady)
    try:
        widget = widget_factory(box)
    except TypeError:
        widget = widget_factory
    if widget is not None:
        widget.pack(side="left")
    if hint_text:
        ttk.Label(box, text=hint_text, style="CardHint.TLabel").pack(
            side="left", padx=(10, 0))
    return box


def kv_grid(master, items: Sequence[Tuple[str, str]], columns: int = 2,
            wraplength: int = 300) -> ttk.Frame:
    """只读的"标签：值"网格（用于摘要区）。"""
    box = ttk.Frame(master, style="Panel.TFrame")
    for i, (k, v) in enumerate(items):
        r, c = divmod(i, columns)
        cell = ttk.Frame(box, style="Panel.TFrame")
        cell.grid(row=r, column=c, sticky="nw", padx=(0, 18), pady=3)
        ttk.Label(cell, text=k, style="SummaryKey.TLabel").pack(anchor="w")
        ttk.Label(cell, text=v or "—", style="SummaryVal.TLabel",
                  wraplength=wraplength, justify="left").pack(anchor="w")
    return box


class PathRow(ttk.Frame):
    """路径输入 + 浏览按钮。"""

    def __init__(self, master, var: tk.StringVar, kind: str = "file",
                 title: str = "选择文件", filetypes=None, width: int = 56):
        super().__init__(master, style="Panel.TFrame")
        self.var = var
        self.kind = kind
        self.title = title
        self.filetypes = filetypes
        ttk.Entry(self, textvariable=var, width=width).pack(side="left", fill="x",
                                                           expand=True)
        ttk.Button(self, text="浏览…", style="Small.TButton",
                   command=self._browse).pack(side="left", padx=(6, 0))

    def _browse(self) -> None:
        if self.kind == "dir":
            path = filedialog.askdirectory(parent=self, title=self.title,
                                           initialdir=self.var.get() or None)
        else:
            path = filedialog.askopenfilename(parent=self, title=self.title,
                                              filetypes=self.filetypes or
                                              [("所有文件", "*.*")])
        if path:
            self.var.set(path)


# --------------------------------------------------------------------------- #
# 表格
# --------------------------------------------------------------------------- #
def configure_tree_tags(tree: ttk.Treeview) -> None:
    """配置数据体检的来源配色与低置信度红底、结果状态色。"""
    for src, color in theme.SOURCE_COLORS.items():
        tree.tag_configure("src_" + src, foreground=color)
        tree.tag_configure("src_%s_low" % src, foreground=color,
                           background=theme.LOW_ROW_BG)
    tree.tag_configure("ok", foreground=theme.OK)
    tree.tag_configure("fail", foreground=theme.ERR)
    tree.tag_configure("warn", foreground=theme.WARN)
    tree.tag_configure("muted", foreground=theme.FG_MUTED)
    tree.tag_configure("skip", foreground=theme.WARN)
    tree.tag_configure("na", foreground=theme.FG_FAINT)
    tree.tag_configure("alt", background=theme.PANEL_ALT)
    tree.tag_configure("group", background=theme.HEAD_BG, font=theme.font(
        theme.BASE_SIZE, True))


def make_tree(parent, columns: Sequence[Sequence], height: int = 8,
              selectmode: str = "browse", show: str = "headings"):
    """创建带滚动条的表格。

    Parameters
    ----------
    columns : [(key, 标题, 宽度, 对齐)]，宽度单位像素。
    返回 ``(容器 Frame, Treeview)``；容器用 grid/pack 放置即可。
    """
    container = ttk.Frame(parent, style="Panel.TFrame")
    tree = ttk.Treeview(container, columns=[c[0] for c in columns], show=show,
                        height=height, selectmode=selectmode)
    for key, heading, width, anchor in columns:
        tree.heading(key, text=heading)
        tree.column(key, width=int(width), anchor=anchor, stretch=True, minwidth=40)
    vsb = ttk.Scrollbar(container, orient="vertical", command=tree.yview)
    hsb = ttk.Scrollbar(container, orient="horizontal", command=tree.xview)
    tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
    tree.grid(row=0, column=0, sticky="nsew")
    vsb.grid(row=0, column=1, sticky="ns")
    hsb.grid(row=1, column=0, sticky="ew")
    container.rowconfigure(0, weight=1)
    container.columnconfigure(0, weight=1)
    configure_tree_tags(tree)
    return container, tree


def clear_tree(tree: ttk.Treeview) -> None:
    for iid in tree.get_children():
        tree.delete(iid)


def selected_index(tree: ttk.Treeview) -> Optional[int]:
    """当前选中行在父列表中的序号（无选中返回 None）。"""
    sel = tree.selection()
    if not sel:
        return None
    return tree.index(sel[0])


class SearchableTree(ttk.Frame):
    """搜索框 + 表格（城市库/轨道库/箭型库选择器共用）。"""

    def __init__(self, master, columns: Sequence[Sequence], height: int = 14,
                 selectmode: str = "browse", search_hint: str = "搜索"):
        super().__init__(master, style="Panel.TFrame")
        self.rowconfigure(1, weight=1)
        self.columnconfigure(0, weight=1)
        top = ttk.Frame(self, style="Panel.TFrame")
        top.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ttk.Label(top, text=search_hint, style="Panel.TLabel").pack(side="left")
        self.search_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.search_var, width=26).pack(
            side="left", padx=(6, 0))
        self.search_var.trace_add("write", lambda *a: self.refresh())
        self.count_label = ttk.Label(top, text="", style="CardHint.TLabel")
        self.count_label.pack(side="left", padx=(12, 0))
        container, self.tree = make_tree(self, columns, height=height,
                                         selectmode=selectmode)
        container.grid(row=1, column=0, sticky="nsew")
        self._rows: List[Dict[str, Any]] = []
        self._text_of: Callable[[Dict[str, Any]], str] = lambda r: " ".join(
            str(v) for v in r.values())
        self._render: Callable[[Dict[str, Any]], Tuple] = lambda r: tuple(
            r.values())

    def set_data(self, rows: List[Dict[str, Any]],
                 render: Callable[[Dict[str, Any]], Tuple],
                 text_of: Optional[Callable[[Dict[str, Any]], str]] = None) -> None:
        self._rows = list(rows)
        self._render = render
        if text_of:
            self._text_of = text_of
        self.refresh()

    def refresh(self) -> None:
        q = self.search_var.get().strip().lower()
        clear_tree(self.tree)
        shown = 0
        for row in self._rows:
            if q and q not in self._text_of(row).lower():
                continue
            vals = self._render(row)
            self.tree.insert("", "end", iid=str(id(row)), values=vals)
            shown += 1
        self.count_label.configure(text="共 %d 项，匹配 %d 项" % (len(self._rows), shown))

    def selected(self) -> Optional[Dict[str, Any]]:
        sel = self.tree.selection()
        if not sel:
            return None
        target = sel[0]
        for row in self._rows:
            if str(id(row)) == target:
                return row
        return None


# --------------------------------------------------------------------------- #
# 文本区
# --------------------------------------------------------------------------- #
class TextBox(ttk.Frame):
    """带滚动条的 Text；可设为只读。"""

    def __init__(self, master, height: int = 6, width: int = 60, wrap: str = "word",
                 readonly: bool = False, font=None):
        super().__init__(master)
        self.text = tk.Text(self, height=height, width=width, wrap=wrap,
                            relief="solid", borderwidth=1, undo=not readonly,
                            background=theme.FIELD_BG, foreground=theme.FG,
                            insertbackground=theme.FG, padx=8, pady=6,
                            selectbackground=theme.SELECT_BG,
                            font=font or theme.font())
        vsb = ttk.Scrollbar(self, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=vsb.set)
        self.text.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        if readonly:
            self.text.configure(state="disabled")

    # ---- 常用操作 ----------------------------------------------------- #
    def set_readonly(self, flag: bool = True) -> None:
        self.text.configure(state="disabled" if flag else "normal")

    def is_readonly(self) -> bool:
        return str(self.text.cget("state")) == "disabled"

    def set_text(self, content: str) -> None:
        ro = self.is_readonly()
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", content or "")
        if ro:
            self.text.configure(state="disabled")

    def get_text(self) -> str:
        return self.text.get("1.0", "end-1c")

    def clear(self) -> None:
        self.set_text("")


class LogPanel(ttk.Frame):
    """运行日志：按等级着色、自动滚屏、可暂停。"""

    MAX_LINES = 6000

    def __init__(self, master, height: int = 14):
        super().__init__(master)
        self.text = tk.Text(self, height=height, wrap="none", state="disabled",
                            relief="solid", borderwidth=1,
                            background="#fbfcfd", foreground=theme.FG,
                            font=theme.font(theme.SMALL_SIZE), padx=8, pady=6,
                            selectbackground=theme.SELECT_BG)
        vsb = ttk.Scrollbar(self, orient="vertical", command=self.text.yview)
        hsb = ttk.Scrollbar(self, orient="horizontal", command=self.text.xview)
        self.text.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.text.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

        self.text.tag_configure("info", foreground=theme.FG)
        self.text.tag_configure("log", foreground=theme.LEVEL_COLORS["log"])
        self.text.tag_configure("warn", foreground=theme.WARN)
        self.text.tag_configure("error", foreground=theme.ERR)
        self.text.tag_configure("ok", foreground=theme.OK)
        self.text.tag_configure("step", foreground=theme.ACCENT,
                                font=theme.font(theme.SMALL_SIZE, True),
                                spacing1=4, spacing3=2)
        self.text.tag_configure("time", foreground=theme.FG_FAINT)
        self.paused = tk.BooleanVar(value=False)

    def append(self, level: str, message: str) -> None:
        level = level if level in ("info", "log", "warn", "error", "step", "ok") else "log"
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        self.text.configure(state="normal")
        self.text.insert("end", "%s " % stamp, ("time",))
        self.text.insert("end", "%s\n" % (message or ""), (level,))
        lines = int(self.text.index("end-1c").split(".")[0])
        if lines > self.MAX_LINES:
            self.text.delete("1.0", "%d.0" % (lines - self.MAX_LINES + 500))
        self.text.configure(state="disabled")
        if not self.paused.get():
            self.text.see("end")

    def clear(self) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")

    def scroll_to_end(self) -> None:
        self.text.see("end")


# --------------------------------------------------------------------------- #
# 数值解析 / 格式化
# --------------------------------------------------------------------------- #
def parse_optional_float(text, label: str, minimum=None, maximum=None) -> Optional[float]:
    """解析可空浮点数；空返回 None，非法抛 ValueError（中文原因）。"""
    s = str(text or "").strip().replace("，", ",").replace("。", ".")
    if not s:
        return None
    try:
        v = float(s)
    except (TypeError, ValueError):
        raise ValueError("「%s」需要填写数字，当前为「%s」" % (label, s)) from None
    if minimum is not None and v < minimum:
        raise ValueError("「%s」不能小于 %s（当前 %s）" % (label, minimum, v))
    if maximum is not None and v > maximum:
        raise ValueError("「%s」不能大于 %s（当前 %s）" % (label, maximum, v))
    return v


def parse_int(text, label: str, default: int = 0, minimum=None, maximum=None) -> int:
    s = str(text or "").strip()
    if not s:
        return int(default)
    try:
        v = int(float(s))
    except (TypeError, ValueError):
        raise ValueError("「%s」需要填写整数，当前为「%s」" % (label, s)) from None
    if minimum is not None and v < minimum:
        raise ValueError("「%s」不能小于 %s（当前 %s）" % (label, minimum, v))
    if maximum is not None and v > maximum:
        raise ValueError("「%s」不能大于 %s（当前 %s）" % (label, maximum, v))
    return v


def fmt_num(v, nd: int = 6) -> str:
    """数值 → 紧凑字符串（None → 空）。"""
    if v is None:
        return ""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if abs(f - round(f)) < 1e-9:
        return "%d" % int(round(f))
    txt = ("%." + str(nd) + "f") % f
    if "." in txt:
        txt = txt.rstrip("0").rstrip(".")
    return txt or "0"


def fmt_seconds(sec) -> str:
    try:
        s = float(sec or 0.0)
    except (TypeError, ValueError):
        return "-"
    if s < 60:
        return "%.1f s" % s
    return "%d 分 %.0f 秒" % (int(s // 60), s % 60)


def fmt_size(n: float) -> str:
    try:
        v = float(n or 0)
    except (TypeError, ValueError):
        return "-"
    if v >= 1048576:
        return "%.1f MB" % (v / 1048576.0)
    if v >= 1024:
        return "%.0f KB" % (v / 1024.0)
    return "%d B" % int(v)


# --------------------------------------------------------------------------- #
# 错误处理 / 对话框
# --------------------------------------------------------------------------- #
def explain_error(exc: BaseException) -> str:
    """把异常翻译成给用户看的中文短语。"""
    msg = str(exc).strip()
    if isinstance(exc, ValueError) and msg:
        return msg
    if isinstance(exc, (FileNotFoundError, PermissionError, OSError)) and msg:
        return msg
    name = type(exc).__name__
    return "%s：%s" % (name, msg or "（无详细信息）")


def write_error_log(context: str, exc: BaseException) -> str:
    """把异常写进可写根的 gui_error.log，返回日志路径（失败返回空串）。"""
    try:
        from app.core import paths as P
        path = P.runtime_base() / "gui_error.log"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n[%s] %s\n" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                     context))
            fh.write("".join(traceback.format_exception(type(exc), exc,
                                                        exc.__traceback__)))
        return str(path)
    except Exception:  # noqa: BLE001 - 日志失败不能影响界面
        return ""


def center_window(win: tk.Toplevel, parent: Optional[tk.Misc] = None,
                  width: Optional[int] = None, height: Optional[int] = None) -> None:
    """把顶层窗口摆到父窗口中间（或屏幕中间）。"""
    win.update_idletasks()
    w = int(width or win.winfo_width())
    h = int(height or win.winfo_height())
    if parent is not None:
        try:
            px, py = parent.winfo_rootx(), parent.winfo_rooty()
            pw, ph = parent.winfo_width(), parent.winfo_height()
            x = px + max(0, (pw - w) // 2)
            y = py + max(0, (ph - h) // 3)
        except tk.TclError:
            x = y = 60
    else:
        x = max(0, (win.winfo_screenwidth() - w) // 2)
        y = max(0, (win.winfo_screenheight() - h) // 3)
    # 不要超出屏幕
    try:
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        x = max(0, min(x, sw - w - 8))
        y = max(0, min(y, sh - h - 40))
    except tk.TclError:
        pass
    win.geometry("%dx%d+%d+%d" % (w, h, x, y))


def safe_grab(win) -> None:
    """延后设置模态 grab（窗口尚未映射时不会抛异常）。"""
    try:
        win.grab_set()
    except Exception:  # noqa: BLE001
        pass


class Dialog(tk.Toplevel):
    """模态对话框基类：标题 + 说明 + 内容 + 按钮栏。

    子类在 ``build_body(parent)`` 里构建内容，并调用 ``self.set_result(...)`` 后
    ``self.close()``；调用方用 ``self.result`` 取结果（``wait_window`` 已阻塞）。
    """

    def __init__(self, parent, title: str, subtitle: str = "",
                 width: int = 760, height: int = 560, resizable: bool = True):
        super().__init__(parent)
        self.result: Any = None
        self.title(title)
        self.configure(background=theme.WINDOW_BG)
        self.transient(parent)
        self.resizable(resizable, resizable)
        self._parent = parent
        outer = ttk.Frame(self, padding=(14, 12))
        outer.pack(fill="both", expand=True)
        outer.rowconfigure(1 if subtitle else 0, weight=1)
        outer.columnconfigure(0, weight=1)
        if subtitle:
            ttk.Label(outer, text=subtitle, style="Subtitle.TLabel",
                      wraplength=max(320, width - 60), justify="left").grid(
                row=0, column=0, sticky="ew", pady=(0, 10))
        self.body = ttk.Frame(outer)
        self.body.grid(row=1 if subtitle else 0, column=0, sticky="nsew")
        self.body.rowconfigure(0, weight=1)
        self.body.columnconfigure(0, weight=1)
        self.button_bar = ttk.Frame(outer)
        self.button_bar.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        self.build_body(self.body)
        self._build_buttons()
        center_window(self, parent, width, height)
        self.protocol("WM_DELETE_WINDOW", self.close)
        try:
            self.after(60, safe_grab, self)
        except tk.TclError:
            pass
        try:
            self.wait_window(self)
        except tk.TclError:
            pass

    # ---- 供子类重写 ----
    def build_body(self, parent: ttk.Frame) -> None:  # pragma: no cover - 抽象
        raise NotImplementedError

    def _build_buttons(self) -> None:
        ttk.Button(self.button_bar, text="取消", command=self.close).pack(side="right")
        ttk.Button(self.button_bar, text="确定", style="Accent.TButton",
                   command=self.close).pack(side="right", padx=(0, 8))

    # ---- 工具 ----
    def close(self) -> None:
        try:
            self.grab_release()
        except Exception:  # noqa: BLE001
            pass
        self.destroy()


def open_text_dialog(parent, title: str, content: str,
                     width: int = 820, height: int = 640) -> tk.Toplevel:
    """只读文本对话框（环境自检 / 关于）。"""
    win = tk.Toplevel(parent)
    win.title(title)
    win.transient(parent)
    win.configure(background=theme.WINDOW_BG)
    body = ttk.Frame(win, padding=(12, 12))
    body.pack(fill="both", expand=True)
    box = TextBox(body, height=20, wrap="word", readonly=True)
    box.pack(fill="both", expand=True)
    box.set_text(content)
    bar = ttk.Frame(body)
    bar.pack(fill="x", pady=(10, 0))
    ttk.Label(bar, text="提示：可用鼠标选中后 Ctrl+C 复制。",
              style="Hint.TLabel").pack(side="left")
    ttk.Button(bar, text="关闭", command=win.destroy).pack(side="right")
    center_window(win, parent, width, height)

    def _grab():
        try:
            win.grab_set()
        except tk.TclError:
            pass

    win.after(60, _grab)
    return win


def open_path_ex(path) -> "tuple[bool, str]":
    """用系统程序打开文件/目录，返回 ``(是否成功, 说明)``。"""
    p = str(path or "")
    if not p:
        return False, "路径为空"
    try:
        from app.core import paths as P
        return P.open_path(p)
    except Exception as exc:  # noqa: BLE001
        return False, "打开失败：%s" % explain_error(exc)


def open_path(path) -> bool:
    """用系统默认程序打开文件/目录；失败返回 False（兼容旧调用）。"""
    ok, _msg = open_path_ex(path)
    return ok


# --------------------------------------------------------------------------- #
# 经纬度格式：十进制 ⇄ 度分秒
# --------------------------------------------------------------------------- #
def coord_format() -> str:
    """当前坐标格式：``dec``（十进制度）或 ``dms``（度分秒）。"""
    try:
        from app.core import paths as P
        return P.coord_format()
    except Exception:  # noqa: BLE001
        return "dec"


def set_coord_format(fmt: str) -> None:
    try:
        from app.core import paths as P
        P.save_coord_format(fmt)
    except Exception:  # noqa: BLE001
        pass


def coord_format_label(fmt=None) -> str:
    return "度分秒" if str(fmt or coord_format()).lower() in (
        "dms", "度分秒", "dm", "deg-min-sec") else "十进制度"


def format_coord(value, axis: str = "lat", fmt=None) -> str:
    """把十进制度格式化成当前（或指定）格式的文本。"""
    from app.core.notam_parse import format_one_coord
    return format_one_coord(value, axis, fmt or coord_format())


def parse_coord(text, label: str, axis: str = "lat"):
    """解析单个纬度/经度输入（十进制或度分秒均可）；空返回 None，非法抛 ValueError。"""
    from app.core.notam_parse import parse_one_coord
    try:
        return parse_one_coord(text, axis)
    except ValueError as exc:
        raise ValueError("「%s」%s" % (label, exc)) from None


# --------------------------------------------------------------------------- #
# 消息框
# --------------------------------------------------------------------------- #
def show_error(parent, title: str, message: str) -> None:
    try:
        messagebox.showerror(title, message, parent=parent)
    except tk.TclError:
        pass


def show_info(parent, title: str, message: str) -> None:
    try:
        messagebox.showinfo(title, message, parent=parent)
    except tk.TclError:
        pass


def show_warning(parent, title: str, message: str) -> None:
    try:
        messagebox.showwarning(title, message, parent=parent)
    except tk.TclError:
        pass


def ask_yes_no(parent, title: str, message: str) -> bool:
    try:
        return bool(messagebox.askyesno(title, message, parent=parent))
    except tk.TclError:
        return False


class WorkdirCancelled(Exception):
    """用户在工作区冲突提示里选择了“取消”（不开始计算）。"""


def ask_workdir_conflict(parent, workdir: str) -> str:
    """工作区已有成果时询问：返回 ``"overwrite"`` / ``"new"`` / ``"cancel"``。"""
    try:
        ans = messagebox.askyesnocancel(
            "工作区已有成果",
            "该工作区已有上一次计算的成果：\n%s\n\n"
            "「是」覆盖：复用该目录（本次生成的同类文件会覆盖/清理旧文件）\n"
            "「否」新建：自动新建副本（目录名加 _2 后缀），旧成果保留\n"
            "「取消」：不开始计算" % workdir,
            parent=parent)
    except tk.TclError:
        return "new"
    if ans is None:
        return "cancel"
    return "overwrite" if ans else "new"
