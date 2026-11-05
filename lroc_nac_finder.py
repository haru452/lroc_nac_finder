#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LROC NAC「同じ場所を似た照明でたくさん撮った場所」探索ツール (地図つき v2)

使い方:  python lroc_nac_finder.py   (Python 3.8以上、追加インストール不要)
  1. 欄に条件を入れて [検索]
  2. 結果は 順位表 と 地図(ヒートマップ。東が右・北が上) に出る。濃いほど撮影回数が多い
  3. 地図: クリックでそのセルを選択 / セルの中をクリック・ドラッグで点・四角を置き、そこを写す画像に絞る / [全体表示] で戻す
  4. [GeoJSON/KML書き出し] でQuickMapなどの地図に重ねるファイルを作れる
  ※ 太陽の向き: 「東=右(朝)」「西=左(夕)」(北が上の地図で見たとき)
  ※ データ元: NASA PDS Geosciences Node の ODE REST (画像の中心座標・入射角などのメタデータ)
"""
import tkinter as tk
from tkinter import ttk, messagebox
import threading, queue, json, math, time, os
import concurrent.futures
import moon_basemap as bp   # 月面の背景地図 (Pillow が無ければ背景なしで動く)
import ui_theme as ut      # ライト/ダークの配色
import place_names as pn   # 地名(静かの海など)
from nac_data import (SETTINGS_PATH, STOPS, PALETTES, CACHE_PATH, CACHE_INC, CACHE_EMI, WORKERS,
                      discover_types, analyse, rng, rng_m, db_open, chunks_for, fetch_chunk, db_load,
                      uses_res, done_chunks, db_save_chunk)
from map_view import MapView
from results import ResultActions
from point_filter import PointFilter
from peek_view import PeekView


# ---------------------------------------------------------------- GUI
class App(MapView, ResultActions, PointFilter, PeekView):
    FIELDS = [   # 3つずつ1行に並ぶ。1行に同じ項目の 最小・最大・差 をまとめる
        ("入射角 最小", "incmin", ""), ("入射角 最大", "incmax", ""), ("入射角の差(°以内)", "incdiff", "5"),
        ("エミッション角 最小", "emimin", ""), ("エミッション角 最大", "emimax", ""), ("エミッション角の差(°以内)", "emidiff", ""),
        ("緯度 最小", "latmin", ""), ("緯度 最大", "latmax", ""), ("グリッド(°)", "grid", "0.5"),
        ("経度 最小", "lonmin", ""), ("経度 最大", "lonmax", ""), ("最小撮影回数", "minobs", "3"),
        ("ピクセルスケール 最小(m)", "resmin", ""), ("ピクセルスケール 最大(m)", "resmax", ""),
    ]
    OPTIONAL = {"latmin": -90.0, "latmax": 90.0, "lonmin": 0.0, "lonmax": 360.0, "emidiff": math.inf,
                "resmin": 0.0, "resmax": math.inf,
                "incmin": CACHE_INC[0], "incmax": CACHE_INC[1], "emimin": CACHE_EMI[0], "emimax": CACHE_EMI[1]}
    ML, MR, MT, MB = 46, 14, 22, 24  # 地図の余白

    def __init__(self, root):
        self.root = root
        root.title("LROC NAC 同条件多重撮影ファインダー")
        root.geometry("1300x950")
        try:   # ウィンドウとタスクバーのアイコン(無くても動く)
            root.iconbitmap(os.path.join(os.path.dirname(os.path.abspath(__file__)), "lroc_nac_finder.ico"))
        except tk.TclError:
            pass
        try:
            with open(SETTINGS_PATH, encoding="utf-8") as f:
                dark0 = bool(json.load(f).get("dark"))
        except (OSError, ValueError):
            dark0 = False
        self.dark = tk.BooleanVar(value=dark0)
        self.th = ut.apply(root, dark0)     # 部品より先に配色を決める
        self.q = queue.Queue()
        self.rows = []
        self.cellmap = {}
        self.stop = False
        self.view = None
        self.full = None
        self.sel = None
        self.drag = None
        self.init_points()

        top = ttk.Frame(root)
        top.pack(fill="x", padx=8, pady=4)
        frm = ttk.LabelFrame(top, text="条件(ここに入力)")
        frm.pack(side="left", fill="y")
        self.v, self.hints = {}, []
        for i, (label, key, default) in enumerate(self.FIELDS):
            r, col = divmod(i, 3)   # 3列にして条件欄の高さを抑える
            ttk.Label(frm, text=label).grid(row=r, column=col * 2, sticky="e", padx=4, pady=1)
            self.v[key] = tk.StringVar(value=default)
            ent = ttk.Entry(frm, textvariable=self.v[key], width=9)
            ent.grid(row=r, column=col * 2 + 1, sticky="w", padx=4)
            if key in self.OPTIONAL:
                self.add_hint(ent, self.v[key])
        r = (len(self.FIELDS) + 2) // 3
        ttk.Label(frm, text="太陽の向き").grid(row=r, column=0, sticky="e", padx=4, pady=3)
        self.side = tk.StringVar(value="両方")
        ttk.Combobox(frm, textvariable=self.side, width=10, state="readonly",
                     values=["両方", "東(右・朝)", "西(左・夕)"]).grid(row=r, column=1, sticky="w", padx=4)
        ttk.Label(frm, style="Mute.TLabel", justify="left", text=(
            "空欄 = 制限なし(緯度・経度は月全体、入射角は40〜80°、エミッション角は0〜10°、エミッション角の差とピクセルスケールは問わない)\n"
            "差 = セル内の画像を1枚ずつ基準にして、そこから何度以内の画像を同じ条件とみなすか")
        ).grid(row=r + 1, column=0, columnspan=6, sticky="w", padx=4, pady=(2, 0))
        ttk.Label(frm, text="画像の種類コード").grid(row=r, column=2, sticky="e", padx=4)
        self.pt = tk.StringVar(value="自動")
        self.ptbox = ttk.Combobox(frm, textvariable=self.pt, width=12, values=["自動"])
        self.ptbox.grid(row=r, column=3, sticky="w", padx=4)

        # 操作ボタンは3つのまとまりに分ける: 検索 / 地図の表示 / 書き出し
        bar = ttk.Frame(top)
        bar.pack(side="left", fill="y", padx=10)
        sbox = ttk.LabelFrame(bar, text="検索")
        sbox.pack(side="left", fill="y")
        eq = ttk.Frame(sbox)
        eq.pack(fill="x", padx=4, pady=(4, 0))
        self.equator = tk.BooleanVar(value=False)
        ttk.Checkbutton(eq, text="赤道付近だけ 緯度±", variable=self.equator).pack(side="left")
        self.eqw = tk.StringVar(value="10")
        ttk.Entry(eq, textvariable=self.eqw, width=4).pack(side="left")
        ttk.Label(eq, text="°").pack(side="left")
        self.btn = ttk.Button(sbox, text="検索", style="Accent.TButton", command=self.start)
        self.btn.pack(fill="x", padx=4, pady=(4, 2))
        ttk.Button(sbox, text="中止", command=lambda: setattr(self, "stop", True)).pack(fill="x", padx=4, pady=(1, 4))

        mbox = ttk.LabelFrame(bar, text="地図の表示")
        mbox.pack(side="left", fill="y", padx=(6, 0))
        zr = ttk.Frame(mbox)
        zr.pack(fill="x", padx=4, pady=(4, 2))
        ttk.Button(zr, text="＋", width=3, command=lambda: self.zoom_by(0.7)).pack(side="left")
        ttk.Button(zr, text="－", width=3, command=lambda: self.zoom_by(1 / 0.7)).pack(side="left", padx=2)
        ttk.Button(zr, text="全体表示に戻す", command=self.reset_view).pack(side="left", fill="x", expand=True)
        self.bg_on = tk.BooleanVar(value=bp.OK)
        cb = ttk.Checkbutton(mbox, text="月面の写真を下に敷く", variable=self.bg_on, command=self.draw)
        cb.pack(fill="x", padx=4)
        if not bp.OK:
            cb.state(["disabled"])
        ttk.Label(mbox, text="ヒートマップの濃さ", style="Mute.TLabel").pack(anchor="w", padx=4)
        self.alpha = tk.DoubleVar(value=0.6)
        ttk.Scale(mbox, from_=0.2, to=1.0, variable=self.alpha, command=lambda _v: self.draw()).pack(fill="x", padx=4)
        ttk.Label(mbox, text="配色", style="Mute.TLabel").pack(anchor="w", padx=4)
        self.palette = tk.StringVar(value=next(iter(PALETTES)))
        pcb = ttk.Combobox(mbox, textvariable=self.palette, state="readonly", values=list(PALETTES), width=18)
        pcb.pack(fill="x", padx=4, pady=(0, 4))
        pcb.bind("<<ComboboxSelected>>", self.on_palette)
        ttk.Checkbutton(mbox, text="ダークモード", variable=self.dark, command=self.on_theme).pack(
            fill="x", padx=4, pady=(0, 4))
        self.bg_cache = None      # (条件, 合成した画像) 選択だけ変えたときに作り直さないため
        self.bg_asked = None      # 最後に取得を頼んだタイル(同じ頼みを繰り返さない)

        bar2 = ttk.LabelFrame(top, text="保存・書き出し")
        bar2.pack(side="left", fill="y")
        ttk.Button(bar2, text="選択セルの座標をコピー", command=self.copy_cell).pack(fill="x", padx=4, pady=(4, 1))
        ttk.Button(bar2, text="選択セルの画像一覧をCSV保存", command=self.save_cell).pack(fill="x", padx=4, pady=1)
        ttk.Button(bar2, text="順位表をCSV保存", command=self.save_all).pack(fill="x", padx=4, pady=1)
        ttk.Button(bar2, text="GeoJSON書き出し(QuickMap用)", command=lambda: self.export("geojson")).pack(fill="x", padx=4, pady=1)
        ttk.Button(bar2, text="KML書き出し", command=lambda: self.export("kml")).pack(fill="x", padx=4, pady=(1, 4))

        self.top = top
        row = ttk.Frame(root)
        row.pack(fill="x", padx=8)
        self.tbtn = ttk.Button(row, text="▲ 条件パネルを隠す(地図を広げる)", command=self.toggle_top)
        self.tbtn.pack(side="left", pady=2)
        self.status = tk.StringVar(value="条件を入れて[検索]を押してください")
        ttk.Label(row, textvariable=self.status).pack(side="left", fill="x", padx=10)
        self.row = row
        # 地名: 表示のオン/オフと、地名を選んでその場所へ移動
        self.names_on = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text="地名を表示", variable=self.names_on, command=self.draw).pack(side="right", padx=(6, 0))
        self.jump = tk.StringVar(value="")
        jcb = ttk.Combobox(row, textvariable=self.jump, state="readonly", width=14, values=[p[0] for p in pn.PLACES])
        jcb.pack(side="right")
        jcb.bind("<<ComboboxSelected>>", self.on_jump)
        ttk.Label(row, text="地名へ移動").pack(side="right", padx=(10, 4))
        self.hover = tk.StringVar(value="")
        ttk.Label(root, textvariable=self.hover, style="Mute.TLabel").pack(fill="x", padx=8)
        # 選んだセルの撮影ID(M13xxxxxxxx)。コピーできる読み取り専用欄
        self.idvar = tk.StringVar(value="")
        idrow = ttk.Frame(root)
        idrow.pack(fill="x", padx=8)
        ttk.Button(idrow, text="点・四角を消す", command=self.clear_points).pack(side="right", padx=(6, 0))
        ttk.Button(idrow, text="IDをコピー", command=self.copy_ids).pack(side="right", padx=(6, 0))
        ttk.Button(idrow, text="セル内の全IDの画像ページを開く(ダウンロード)", command=self.open_all_products).pack(side="right", padx=(6, 0))
        ttk.Entry(idrow, textvariable=self.idvar, state="readonly").pack(side="left", fill="x", expand=True)
        # 操作ヒント。通常モードと重なり表示モード(Ctrl)で中身を切り替える(peek_view.py の HINTS)
        self.hint_var = tk.StringVar()
        ttk.Label(root, style="Mute.TLabel", wraplength=1200, textvariable=self.hint_var).pack(fill="x", padx=8, pady=(2, 0))
        self.init_peek()

        pw = ttk.PanedWindow(root, orient="vertical")
        pw.pack(fill="both", expand=True, padx=8, pady=4)
        self.cv = tk.Canvas(pw, bg=self.th["canvas"], height=560, highlightthickness=1, highlightbackground=self.th["outline"])
        pw.add(self.cv, weight=6)
        cols = ("rank", "lat", "lon", "side", "inc", "emi", "res", "obs", "n", "total")
        heads = ("順位", "緯度(中心)", "東経(中心)", "太陽の向き", "入射角", "エミッション角", "ピクセルスケール", "撮影回数", "画像枚数", "セル内の全枚数")
        tf = ttk.Frame(pw)
        pw.add(tf, weight=1)
        self.tree = ttk.Treeview(tf, columns=cols, show="headings", height=5)
        for c_, h in zip(cols, heads):
            self.tree.heading(c_, text=h, command=lambda k=c_: self.sort_tree(k))   # 見出しクリックで並べ替え
            self.tree.column(c_, width={"rank": 55, "total": 130, "inc": 110, "emi": 110, "res": 120}.get(c_, 95), anchor="center")
        sb = ttk.Scrollbar(tf, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self.on_tree)

        self.cv.bind("<Configure>", lambda e: self.on_resize())
        self.cv.bind("<Double-Button-1>", self.on_double)
        self.cv.bind("<Enter>", lambda e: self.cv.focus_set())   # 地図の上ではキーで操作できる
        for k in ("<plus>", "<equal>", "<KP_Add>"):
            self.cv.bind(k, lambda e: self.zoom_by(0.7))
        for k in ("<minus>", "<KP_Subtract>"):
            self.cv.bind(k, lambda e: self.zoom_by(1 / 0.7))
        for k, d in (("<Left>", (-.2, 0)), ("<Right>", (.2, 0)), ("<Up>", (0, .2)), ("<Down>", (0, -.2))):
            self.cv.bind(k, lambda e, d=d: self.pan_by(*d))
        self.cv.bind("<Home>", lambda e: self.reset_view())
        for k in ("<KeyPress-Control_L>", "<KeyPress-Control_R>"):     # Ctrlを押すたびに、画像の重なり表示を出す・消す
            self.cv.bind(k, self.peek_toggle)
        for k in ("<KeyRelease-Control_L>", "<KeyRelease-Control_R>", "<FocusOut>"):
            self.cv.bind(k, self.ctrl_up)
        for k in ("<Key-1>", "<KP_1>"):   # 重なり表示モード: 選んだ画像の入手ページを開く
            self.cv.bind(k, self.peek_open)
        for k in ("<Key-2>", "<KP_2>"):   # 重なり表示モード: ヒートマップ
            self.cv.bind(k, self.heat_toggle)
        for k in ("<Delete>", "<BackSpace>"):
            self.cv.bind(k, self.clear_points)
        self.cv.bind("<ButtonPress-1>", self.on_press)
        self.cv.bind("<B1-Motion>", self.on_drag)
        self.cv.bind("<ButtonRelease-1>", self.on_release)
        self.cv.bind("<Motion>", self.on_move)
        self.cv.bind("<MouseWheel>", self.on_wheel)           # ホイールで拡大・縮小
        self.cv.bind("<ButtonPress-3>", self.on_rpress)       # 右ドラッグで移動 / 動かさず右クリックで全体表示
        self.cv.bind("<B3-Motion>", self.on_rdrag)
        self.cv.bind("<ButtonRelease-3>", self.on_rrelease)
        self.cv.bind("<ButtonPress-2>", self.on_rpress)       # ホイールを押しながら動かしても移動(クリックだけでは何もしない)
        self.cv.bind("<B2-Motion>", self.on_rdrag)
        self.cv.bind("<ButtonRelease-2>", lambda e: setattr(self, "pan", None))
        self.load_settings()
        root.protocol("WM_DELETE_WINDOW", self.on_close)   # 閉じるときに条件と表示の設定を保存する
        root.after(150, self.poll)
        threading.Thread(target=self.load_types, daemon=True).start()

    def load_types(self):
        try:
            pts = discover_types()
            if pts:
                self.q.put(("types", pts))
        except Exception as e:  # noqa
            self.q.put(("status", "画像の種類コードを自動取得できませんでした(検索時に再試行します): %s" % e))

    def conds(self):
        c = {}
        try:
            for _, key, _d in self.FIELDS:
                s = self.v[key].get().strip()
                if s == "" and key in self.OPTIONAL:
                    c[key] = self.OPTIONAL[key]
                else:
                    c[key] = float(s)
        except ValueError:
            raise ValueError("数値でない欄があります(空欄にできるのは、緯度・経度・角度とピクセルスケールの最小最大・エミッション角の差の欄です)")
        c["minobs"] = int(c["minobs"])
        if self.equator.get():   # 赤道付近だけ: 緯度の欄より優先して ±幅 に絞る
            try:
                w = float(self.eqw.get())
            except ValueError:
                raise ValueError("赤道付近の幅(°)は数字で入れてください")
            if not 0 < w <= 90:
                raise ValueError("赤道付近の幅は0より大きく90以下にしてください")
            c["latmin"], c["latmax"] = -w, w
        c["side"] = self.side.get()
        if c["incmin"] < CACHE_INC[0] or c["incmax"] > CACHE_INC[1] or c["emimin"] < CACHE_EMI[0] or c["emimax"] > CACHE_EMI[1]:
            raise ValueError("保存するデータの範囲は 入射角%g〜%g°・エミッション角%g〜%g° です。この範囲内で指定してください"
                             % (CACHE_INC + CACHE_EMI))
        if c["incdiff"] < 0 or c["emidiff"] < 0:
            raise ValueError("角度の差は0以上にしてください")
        if c["grid"] <= 0:
            raise ValueError("グリッドは0より大きくしてください")
        if c["resmin"] < 0:
            raise ValueError("ピクセルスケールは0以上にしてください")
        if (c["latmin"] >= c["latmax"] or c["lonmin"] >= c["lonmax"] or c["incmin"] > c["incmax"]
                or c["emimin"] > c["emimax"] or c["resmin"] > c["resmax"]):
            raise ValueError("最小は最大より小さくしてください")
        return c

    def get_pt(self, v):
        """v は画面の種類コード欄の値(Tkの変数は裏のスレッドから触らないので、呼び出し側で読んで渡す)"""
        v = v.strip()
        if v and v != "自動":
            return v
        pts = discover_types()
        if not pts:
            raise RuntimeError("LROC NAC用のデータ種類コードが見つかりません")
        self.q.put(("types", pts))
        return pts[0]

    def start(self):
        try:
            c = self.conds()
        except ValueError as e:
            messagebox.showerror("入力エラー", str(e)); return
        self.save_settings()   # 検索した条件は、その時点で保存しておく
        self.stop = False
        self.searching = True
        self.btn.state(["disabled"]); self.btn.configure(text="検索中…")
        self.status.set("検索中…")
        self.tree.delete(*self.tree.get_children())
        self.rows, self.cellmap, self.sel = [], {}, None
        self.pts, self.pt_row = [], None
        self.full = (c["lonmin"], c["lonmax"], c["latmin"], c["latmax"])
        self.set_view(*self.full)
        threading.Thread(target=self.work, args=(c, self.pt.get()), daemon=True).start()

    def work(self, c, ptval):
        try:
            db = db_open()
            need = chunks_for(c)
            done = done_chunks(db, c)
            todo = [k for k in need if "%d,%d" % k not in done]
            if todo:
                pt = self.get_pt(ptval)
                what = "ピクセルスケールの情報が無い区画を取り直します(初回のみ)" if uses_res(c) else "初回のみ取得します"
                self.q.put(("status", "%s: %d / %d 区画が未保存(保存先 %s)" % (what, len(todo), len(need), CACHE_PATH)))
                t0, finished = time.time(), 0
                with concurrent.futures.ThreadPoolExecutor(WORKERS) as ex:
                    futs = {ex.submit(fetch_chunk, la, lo, pt, lambda: self.stop): (la, lo) for la, lo in todo}
                    for fu in concurrent.futures.as_completed(futs):
                        la, lo = futs[fu]
                        rows = fu.result()
                        if rows is None:    # 中止された区画は保存しない(次回やり直し)
                            continue
                        db_save_chunk(db, la, lo, rows)
                        finished += 1
                        el = time.time() - t0
                        eta = el / finished * (len(todo) - finished)
                        self.q.put(("status", "取得中… %d / %d 区画 保存済み(最新: 緯度%d〜 経度%d〜 %d枚) 残り約%d分。中止しても続きから再開できます"
                                    % (finished + len(need) - len(todo), len(need), la, lo, len(rows), eta / 60 + 0.5)))
                        if self.stop:
                            for f in futs: f.cancel()
            done = done_chunks(db, c)
            missing = [k for k in need if "%d,%d" % k not in done]
            self.q.put(("status", "検索中… 保存データから条件に合う画像を集計しています"))
            recs = db_load(db, c)
            db.close()
            rows = analyse(recs, c)
            msg = "完了: 条件一致 %d 枚 → %d 箇所 (保存データから検索)" % (len(recs), len(rows))
            if missing:
                msg = "中止しました(%d / %d 区画のみ)。途中までの結果: %s" % (len(need) - len(missing), len(need), msg)
            self.q.put(("rows", rows)); self.q.put(("status", msg))
        except Exception as e:  # noqa
            self.q.put(("status", "エラー: %s" % e))
        finally:
            self.q.put(("done", None))

    def poll(self):
        redraw, tile_bad = False, 0
        try:
            while True:
                k, v = self.q.get_nowait()
                if k == "status": self.status.set(v)
                elif k == "done":
                    self.searching = False
                    self.btn.state(["!disabled"]); self.btn.configure(text="検索")
                    self.draw()
                elif k == "tile":   # 月面タイルが届いた: まとめて1回だけ描き直す
                    redraw = True
                    tile_bad += not v
                elif k == "feet": self.on_feet(v)
                elif k == "types":
                    self.ptbox.configure(values=["自動"] + list(v))
                elif k == "rows":
                    self.rows = v
                    self.cellmap = {}
                    for r in v:
                        key = (r["iy"], r["ix"])
                        if key not in self.cellmap or r["obs"] > self.cellmap[key]["obs"]:
                            self.cellmap[key] = r
                    for i, r in enumerate(v[:2000], 1):
                        self.tree.insert("", "end", iid=str(i - 1), values=(
                            i, "%.2f" % r["lat"], "%.2f" % r["lon"], r["side"],
                            rng(r["inc0"], r["inc1"]), rng(r["emi0"], r["emi1"]), rng_m(r["res0"], r["res1"]),
                            r["obs"], r["n"], r["total"]))
                    if v:   # 経度に引き伸ばされないよう、1位のセルを中心に縦横比どおりの範囲で見せる
                        self.fit_view(v[0]["lon"])
                    else:
                        self.draw()
        except queue.Empty:
            pass
        if tile_bad:
            self.status.set("月面写真のタイルを %d 枚取得できませんでした(ネット接続を確認してください)" % tile_bad)
        if redraw and self.view:
            self.bg_asked = None
            self.draw()
        self.root.after(150, self.poll)

    def toggle_top(self):
        """条件・ボタンの帯を隠して地図を広げる / 元に戻す。"""
        if self.top.winfo_manager():
            self.top.pack_forget()
            self.tbtn.config(text="▼ 条件パネルを表示")
        else:
            self.top.pack(fill="x", padx=8, pady=4, before=self.row)
            self.tbtn.config(text="▲ 条件パネルを隠す(地図を広げる)")

    def add_hint(self, ent, var):
        """空欄の入力欄に、薄い文字で「制限なし」と出す。入力中や値があるときは消す。"""
        lab = tk.Label(ent, text="制限なし", bg=self.th["field"], fg=self.th["hint"], font=("", 8), bd=0, padx=0, pady=0)
        lab.bind("<Button-1>", lambda _e: ent.focus_set())   # 文字の上を押しても入力できる
        self.hints.append(lab)
        def update(*_):
            if var.get() == "" and self.root.focus_get() is not ent:
                lab.place(x=4, rely=0.5, anchor="w")
            else:
                lab.place_forget()
        var.trace_add("write", update)
        ent.bind("<FocusIn>", update, add="+")
        ent.bind("<FocusOut>", lambda _e: ent.after_idle(update), add="+")
        ent.after_idle(update)

    def on_theme(self):
        self.th = ut.apply(self.root, self.dark.get())
        for lab in self.hints:
            lab.configure(bg=self.th["field"], fg=self.th["hint"])
        self.cv.configure(bg=self.th["canvas"], highlightbackground=self.th["outline"])
        self.save_settings()
        self.draw()

    def settings_vars(self):
        """保存する項目: 検索の条件と、地図(ヒートマップ)の表示の設定。"""
        d = {"field_" + k: v for k, v in self.v.items()}
        d.update(side=self.side, pt=self.pt, equator=self.equator, eqw=self.eqw, dark=self.dark,
                 bg_on=self.bg_on, alpha=self.alpha, palette=self.palette, names_on=self.names_on)
        return d

    def load_settings(self):
        """前回の条件と表示の設定を戻す。読めない項目はとばして初期値のままにする。"""
        try:
            with open(SETTINGS_PATH, encoding="utf-8") as f:
                saved = json.load(f)
        except (OSError, ValueError):
            return
        for k, var in self.settings_vars().items():
            if k not in saved or k == "dark":   # ダークモードは起動の最初に反映済み
                continue
            try:
                if k == "palette" and saved[k] not in PALETTES:
                    continue
                if k == "bg_on" and not bp.OK:
                    continue
                var.set(saved[k])
            except (tk.TclError, TypeError, ValueError):
                pass
        STOPS[:] = PALETTES[self.palette.get()]

    def save_settings(self):
        try:
            d = {k: var.get() for k, var in self.settings_vars().items()}
            with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=1)
        except (OSError, tk.TclError):
            pass

    def on_close(self):
        self.save_settings()
        self.root.destroy()

    def on_palette(self, _e=None):
        STOPS[:] = PALETTES[self.palette.get()]
        self.draw()

    def sort_tree(self, col):
        """順位表を、押した列で並べ替える(もう一度押すと逆順)。行の中身(順位の番号)は変えない。"""
        rev = getattr(self, "_sort", (None, False))
        desc = not rev[1] if rev[0] == col else (col in ("obs", "n", "total"))   # 回数や枚数は多い順から
        def key(i):
            v = self.tree.set(i, col)
            try:
                return (0, float(v.rstrip("°").split("〜")[0]))
            except ValueError:
                return (1, v)
        items = sorted(self.tree.get_children(""), key=key, reverse=desc)
        for pos, i in enumerate(items):
            self.tree.move(i, "", pos)
        self._sort = (col, desc)



if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
