# -*- coding: utf-8 -*-
"""画面の配色(ライト/ダーク)。ttkの部品はスタイルで、地図(Canvas)は返す辞書の色で描く。"""
from tkinter import ttk

THEMES = {
    "light": dict(
        win="#f0f0f0", panel="#f0f0f0", fg="#1a1a1a", mute="#555555", field="#ffffff", btn="#e1e1e1", btn_hot="#d0d0d0",
        canvas="#ffffff", plot="#f4f4f4", outline="#888888", grid="#dddddd", axis="#555555", hint="#777777",
        legend_bg="#ffffff", legend_edge="#bbbbbb", sel="#0050ff", sel_text="#0030a0", label_bg="#ffffff",
        sash="#cccccc", select_bg="#0078d7", select_fg="#ffffff"),
    "dark": dict(
        win="#1e1f22", panel="#2b2d30", fg="#e6e6e6", mute="#a9adb4", field="#3a3d41", btn="#3a3d41", btn_hot="#4c5056",
        canvas="#1e1f22", plot="#2a2c30", outline="#6a6d73", grid="#44474c", axis="#b4b8be", hint="#9a9ea5",
        legend_bg="#2b2d30", legend_edge="#6a6d73", sel="#5aa0ff", sel_text="#a8d0ff", label_bg="#2b2d30",
        sash="#44474c", select_bg="#3d6fb5", select_fg="#ffffff"),
}
_default_theme = None


def apply(root, dark):
    """ttkのスタイルを切り替え、地図の描画に使う色の辞書を返す。"""
    global _default_theme
    st = ttk.Style(root)
    if _default_theme is None:
        _default_theme = st.theme_use()
    th = THEMES["dark" if dark else "light"]
    st.theme_use("clam" if dark else _default_theme)   # clam は色を変えやすい。ライトは元の見た目に戻す
    root.configure(bg=th["win"])
    # コンボボックスの候補リストはttkの外にあるので別に指定する
    root.option_add("*TCombobox*Listbox.background", th["field"])
    root.option_add("*TCombobox*Listbox.foreground", th["fg"])
    root.option_add("*TCombobox*Listbox.selectBackground", th["select_bg"])
    root.option_add("*TCombobox*Listbox.selectForeground", th["select_fg"])
    st.configure("Accent.TButton", font=("", 11, "bold"), padding=(10, 8))
    st.configure("Mute.TLabel", foreground=th["mute"])
    if dark:
        st.configure(".", background=th["panel"], foreground=th["fg"], fieldbackground=th["field"],
                     bordercolor=th["outline"], lightcolor=th["panel"], darkcolor=th["panel"],
                     troughcolor=th["win"], insertcolor=th["fg"], selectbackground=th["select_bg"],
                     selectforeground=th["select_fg"])
        for n in ("TFrame", "TLabelframe", "TPanedwindow"):
            st.configure(n, background=th["win"])
        st.configure("TLabelframe.Label", background=th["win"], foreground=th["fg"])
        st.configure("TLabel", background=th["win"], foreground=th["fg"])
        st.configure("Mute.TLabel", background=th["win"], foreground=th["mute"])
        st.configure("TCheckbutton", background=th["win"], foreground=th["fg"])
        st.map("TCheckbutton", background=[("active", th["win"])], indicatorcolor=[("selected", th["select_bg"])])
        st.configure("TButton", background=th["btn"], foreground=th["fg"], bordercolor=th["outline"])
        st.map("TButton", background=[("active", th["btn_hot"]), ("disabled", th["panel"])],
               foreground=[("disabled", th["mute"])])
        st.configure("TEntry", fieldbackground=th["field"], foreground=th["fg"])
        st.map("TEntry", fieldbackground=[("readonly", th["panel"])], foreground=[("readonly", th["fg"])])
        st.configure("TCombobox", fieldbackground=th["field"], background=th["btn"], foreground=th["fg"],
                     arrowcolor=th["fg"])
        st.map("TCombobox", fieldbackground=[("readonly", th["field"])], foreground=[("readonly", th["fg"])])
        st.configure("Treeview", background=th["field"], fieldbackground=th["field"], foreground=th["fg"])
        st.map("Treeview", background=[("selected", th["select_bg"])], foreground=[("selected", th["select_fg"])])
        st.configure("Treeview.Heading", background=th["btn"], foreground=th["fg"])
        st.configure("TScrollbar", background=th["btn"], troughcolor=th["win"], arrowcolor=th["fg"])
        st.configure("TScale", background=th["win"], troughcolor=th["btn"])
        st.configure("Sash", sashthickness=6)
    return th
