#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LROC NAC「同じ場所を似た照明でたくさん撮った場所」探索ツール (地図つき v2)

使い方:  python lroc_nac_finder.py   (Python 3.8以上、追加インストール不要)
  1. 欄に条件を入れて [検索]
  2. 結果は 順位表 と 地図(ヒートマップ。東が右・北が上) に出る。濃いほど撮影回数が多い
  3. 地図: ドラッグで拡大 / クリックでそのセルを選択 / [全体表示] で戻す
  4. [GeoJSON/KML書き出し] でQuickMapなどの地図に重ねるファイルを作れる
  ※ 太陽の向き: 「東=右(朝)」「西=左(夕)」(北が上の地図で見たとき)
  ※ データ元: NASA PDS Geosciences Node の ODE REST (画像の中心座標・入射角などのメタデータ)
"""
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import threading, queue, json, csv, math, time, collections, os, sqlite3
import concurrent.futures
import urllib.request, urllib.parse, io

ODE = "https://oderest.rsl.wustl.edu/live2/"
PAGE = 1000
STOPS = [(255, 255, 178), (254, 204, 92), (253, 141, 60), (240, 59, 32), (189, 0, 38)]


# ---------------------------------------------------------------- データ取得
def flat(d, out=None):
    """入れ子のJSONを小文字キーの平らな辞書にする(最初に出た値を採用)。"""
    if out is None:
        out = {}
    if isinstance(d, dict):
        for k, v in d.items():
            if isinstance(v, (dict, list)):
                flat(v, out)
            else:
                out.setdefault(str(k).lower(), v)
    elif isinstance(d, list):
        for v in d:
            flat(v, out)
    return out


def num(rec, *names):
    for n in names:
        v = rec.get(n)
        if v in (None, ""):
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            pass
    return None


def fetch(params, retries=3):
    url = ODE + "?" + urllib.parse.urlencode(params)
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "lroc-nac-finder/2.0"})
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:  # noqa
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError("ODEに接続できません: %s" % last)


def check(j):
    res = j.get("ODEResults", {})
    if str(res.get("Status", "")).upper() == "ERROR":
        raise RuntimeError("ODEエラー: %s" % res.get("Error", res))
    return res


def products_of(j):
    p = check(j).get("Products", {})
    p = p.get("Product", []) if isinstance(p, dict) else []
    return [p] if isinstance(p, dict) else p


def discover_types():
    """LRO/LROC の NAC 用データ種類コードを ODE から取得(EDR優先)。"""
    j = fetch(dict(query="iipt", target="moon", output="JSON"))
    sets = check(j).get("IIPTSets", {}).get("IIPTSet", [])
    if isinstance(sets, dict):
        sets = [sets]
    pts = [s.get("PT", "") for s in sets
           if s.get("IHID") == "LRO" and s.get("IID") == "LROC" and "NAC" in str(s.get("PT", "")).upper()]
    pts = sorted(set(p for p in pts if p), key=lambda p: (0 if p.upper().startswith("EDR") else 1, p))
    return pts


def base_params(c, pt, with_angles=True):
    p = dict(query="product", results="m", output="JSON", target="moon",
             ihid="LRO", iid="LROC", pt=pt,
             minlat=c["latmin"], maxlat=c["latmax"],
             westernlon=c["lonmin"], easternlon=c["lonmax"])
    if with_angles:
        # パラメータ名は ODE REST Interface Manual V2.1.6 の Table 7 による
        p["mininangle"] = c["incmin"]
        p["maxinangle"] = c["incmax"]
        p["minemangle"] = c["emimin"]
        p["maxemangle"] = c["emimax"]
    return p


def index_solar(prod, emi, inc, pha):
    """ODE_notes の "Index Record" 行(LROC索引表のCSV)から太陽直下点の (方位角, 経度) を取り出す。
    索引表の並びは ... 放射角, 入射角, 位相角, 北方位角, 太陽方位角, 太陽直下点緯度, 太陽直下点経度 ...
    なので、放射角・入射角・位相角の3つが並ぶ場所を数値で探して、その後ろを読む。"""
    notes = prod.get("ODE_notes", {})
    notes = notes.get("ODE_note", []) if isinstance(notes, dict) else []
    if isinstance(notes, str):
        notes = [notes]
    for line in notes:
        if not str(line).startswith('"'):
            continue
        vals = []
        for s in next(csv.reader(io.StringIO(line), skipinitialspace=True), []):
            try:
                vals.append(float(s))
            except ValueError:
                vals.append(None)
        for j in range(len(vals) - 6):
            a, b, c_ = vals[j:j + 3]
            if None in (a, b, c_) or None in (emi, inc, pha):
                continue
            if abs(a - emi) < 0.006 and abs(b - inc) < 0.006 and abs(c_ - pha) < 0.006:
                return vals[j + 4], vals[j + 6]
    return None, None


def parse_record(prod):
    f = flat(prod)
    pid = (f.get("pdsid") or f.get("product_name") or f.get("productid") or f.get("ode_id") or "?")
    pid = str(pid).split(".")[0].upper()   # 例: M1404462373LE.IMG → M1404462373LE
    lat = num(f, "center_latitude", "centerlatitude")
    lon = num(f, "center_longitude", "centerlongitude")
    inc = num(f, "incidence_angle", "incidenceangle")
    emi = num(f, "emission_angle", "emissionangle")
    pha = num(f, "phase_angle", "phaseangle")
    # 注意: ODEの Solar_longitude は季節(Ls)で、太陽直下点の経度ではない
    ssl = num(f, "sub_solar_longitude", "subsolarlongitude")
    saz = num(f, "sub_solar_azimuth", "subsolarazimuth")
    if ssl is None and saz is None and isinstance(prod, dict):
        saz, ssl = index_solar(prod, emi, inc, pha)
    if None in (lat, lon, inc):
        return None
    lon %= 360.0
    side = None
    if ssl is not None:
        d = ((ssl % 360.0) - lon + 180.0) % 360.0 - 180.0
        side = "東(右・朝)" if d > 0 else "西(左・夕)"
    # 太陽方位角(saz)は画像の向き基準で東西どちらでも170°前後になるため、東西判定には使わない
    return dict(id=str(pid), lat=lat, lon=lon, inc=inc, emi=emi, side=side or "不明",
                time=str(f.get("observation_time") or f.get("start_time") or f.get("product_creation_time") or ""))


# ---------------------------------------------------------------- 集計
def analyse(recs, c):
    g, w = c["grid"], c["incwidth"]
    cells = collections.defaultdict(lambda: collections.defaultdict(list))
    for r in recs:
        if c["side"] != "両方" and not r["side"].startswith(c["side"][0]):
            continue
        key = (math.floor(r["lat"] / g), math.floor(r["lon"] / g), r["side"])
        cells[key][math.floor(r["inc"] / w)].append(r)
    rows = []
    for (iy, ix, side), bins in cells.items():
        b, items = max(bins.items(), key=lambda kv: len({i["id"][:-2] for i in kv[1]}))
        obs = len({i["id"][:-2] for i in items})
        if obs < c["minobs"]:
            continue
        rows.append(dict(iy=iy, ix=ix, g=g, lat=(iy + .5) * g, lon=(ix + .5) * g, side=side,
                         inc0=b * w, inc1=(b + 1) * w, obs=obs, n=len(items),
                         total=sum(len(v) for v in bins.values()), items=items))
    rows.sort(key=lambda r: (-r["obs"], -r["n"]))
    return rows


# ---------------------------------------------------------------- ローカル保存(キャッシュ)
# 一度ODEから取得した画像情報はPC内のSQLiteに保存し、2回目以降はネットを使わずに検索する。
# 取得する範囲は下の固定範囲(横から光が当たる画像を探す用途向け)。範囲外の条件では検索できない。
CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lroc_nac_cache.sqlite")
CACHE_INC = (40.0, 80.0)
CACHE_EMI = (0.0, 10.0)
LAT_STEP, LON_STEP = 30, 10     # 区画の大きさ(度)
WORKERS = 4                      # 同時に取得する数


def db_open():
    db = sqlite3.connect(CACHE_PATH)
    db.execute("CREATE TABLE IF NOT EXISTS recs(id TEXT PRIMARY KEY, lat REAL, lon REAL, inc REAL, emi REAL, side TEXT, t TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS done(chunk TEXT PRIMARY KEY, n INTEGER)")
    db.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
    # 取得範囲や種類コードが変わったら古い保存は使わない
    sig = "%s|%s|%s" % (CACHE_INC, CACHE_EMI, "v2")
    row = db.execute("SELECT v FROM meta WHERE k='sig'").fetchone()
    if row and row[0] != sig:
        db.execute("DELETE FROM recs"); db.execute("DELETE FROM done")
    db.execute("INSERT OR REPLACE INTO meta VALUES('sig',?)", (sig,))
    db.commit()
    return db


def chunks_for(c):
    out = []
    for la in range(-90, 90, LAT_STEP):
        if la + LAT_STEP <= c["latmin"] or la >= c["latmax"]:
            continue
        for lo in range(0, 360, LON_STEP):
            if lo + LON_STEP <= c["lonmin"] or lo >= c["lonmax"]:
                continue
            out.append((la, lo))
    out.sort(key=lambda x: (abs(x[0] + LAT_STEP / 2), x[1]))   # 赤道に近い区画から
    return out


def fetch_chunk(la, lo, pt, stopped):
    cc = dict(latmin=la, latmax=la + LAT_STEP, lonmin=lo, lonmax=lo + LON_STEP,
              incmin=CACHE_INC[0], incmax=CACHE_INC[1], emimin=CACHE_EMI[0], emimax=CACHE_EMI[1])
    rows, off = [], 0
    while not stopped():
        p = base_params(cc, pt); p.update(limit=PAGE, offset=off)
        prods = products_of(fetch(p))
        for pr in prods:
            r = parse_record(pr)
            if r:
                rows.append((r["id"], r["lat"], r["lon"], r["inc"], r["emi"], r["side"], r["time"]))
        off += len(prods)
        if len(prods) < PAGE:
            return rows
    return None   # 中止


def db_load(db, c):
    q = ("SELECT id,lat,lon,inc,emi,side,t FROM recs WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ? "
         "AND inc BETWEEN ? AND ? AND (emi IS NULL OR emi BETWEEN ? AND ?)")
    return [dict(id=a, lat=b, lon=c_, inc=d, emi=e, side=s, time=t)
            for a, b, c_, d, e, s, t in db.execute(
                q, (c["latmin"], c["latmax"], c["lonmin"], c["lonmax"], c["incmin"], c["incmax"], c["emimin"], c["emimax"]))]


def ramp(t):
    t = max(0.0, min(1.0, t)) * (len(STOPS) - 1)
    i = min(int(t), len(STOPS) - 2)
    f = t - i
    a, b = STOPS[i], STOPS[i + 1]
    return tuple(int(a[k] + (b[k] - a[k]) * f) for k in range(3))


def hexcol(rgb):
    return "#%02x%02x%02x" % rgb


def nice_step(rng):
    for s in (0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 20, 30, 45, 60, 90):
        if rng / s <= 12:
            return s
    return 90


# ---------------------------------------------------------------- GUI
class App:
    FIELDS = [
        ("緯度 最小 (空欄=全体)", "latmin", ""), ("緯度 最大 (空欄=全体)", "latmax", ""),
        ("経度 最小 (空欄=全体)", "lonmin", ""), ("経度 最大 (空欄=全体)", "lonmax", ""),
        ("入射角 最小 (40以上)", "incmin", "50"), ("入射角 最大 (80以下)", "incmax", "70"),
        ("エミッション角 最小 (°)", "emimin", "0"), ("エミッション角 最大 (10以下)", "emimax", "5"),
        ("照明の幅: 入射角のくくり (°)", "incwidth", "5"),
        ("グリッドの大きさ (°)", "grid", "0.5"),
        ("表示する最小撮影回数", "minobs", "3"),
    ]
    OPTIONAL = {"latmin": -90.0, "latmax": 90.0, "lonmin": 0.0, "lonmax": 360.0}
    ML, MR, MT, MB = 46, 14, 22, 24  # 地図の余白

    def __init__(self, root):
        self.root = root
        root.title("LROC NAC 同条件多重撮影ファインダー")
        root.geometry("1250x900")
        self.q = queue.Queue()
        self.rows = []
        self.cellmap = {}
        self.stop = False
        self.view = None
        self.full = None
        self.sel = None
        self.drag = None

        top = ttk.Frame(root)
        top.pack(fill="x", padx=8, pady=4)
        frm = ttk.LabelFrame(top, text="条件(ここに入力)")
        frm.pack(side="left", fill="y")
        self.v = {}
        for i, (label, key, default) in enumerate(self.FIELDS):
            r, col = divmod(i, 2)
            ttk.Label(frm, text=label).grid(row=r, column=col * 2, sticky="e", padx=4, pady=1)
            self.v[key] = tk.StringVar(value=default)
            ttk.Entry(frm, textvariable=self.v[key], width=9).grid(row=r, column=col * 2 + 1, sticky="w", padx=4)
        r = (len(self.FIELDS) + 1) // 2
        ttk.Label(frm, text="太陽の向き").grid(row=r, column=0, sticky="e", padx=4, pady=3)
        self.side = tk.StringVar(value="両方")
        ttk.Combobox(frm, textvariable=self.side, width=10, state="readonly",
                     values=["両方", "東(右・朝)", "西(左・夕)"]).grid(row=r, column=1, sticky="w", padx=4)
        ttk.Label(frm, text="画像の種類コード").grid(row=r, column=2, sticky="e", padx=4)
        self.pt = tk.StringVar(value="自動")
        self.ptbox = ttk.Combobox(frm, textvariable=self.pt, width=12, values=["自動"])
        self.ptbox.grid(row=r, column=3, sticky="w", padx=4)

        bar = ttk.Frame(top)
        bar.pack(side="left", fill="y", padx=10)
        self.btn = ttk.Button(bar, text="検索", command=self.start); self.btn.pack(fill="x", pady=1)
        ttk.Button(bar, text="中止", command=lambda: setattr(self, "stop", True)).pack(fill="x", pady=1)
        ttk.Button(bar, text="接続テスト/項目確認", command=self.test).pack(fill="x", pady=1)
        ttk.Button(bar, text="地図を全体表示", command=self.reset_view).pack(fill="x", pady=1)
        bar2 = ttk.Frame(top)
        bar2.pack(side="left", fill="y")
        ttk.Button(bar2, text="選択セルの座標をコピー", command=self.copy_cell).pack(fill="x", pady=1)
        ttk.Button(bar2, text="選択セルの画像一覧をCSV保存", command=self.save_cell).pack(fill="x", pady=1)
        ttk.Button(bar2, text="順位表をCSV保存", command=self.save_all).pack(fill="x", pady=1)
        ttk.Button(bar2, text="GeoJSON書き出し(QuickMap用)", command=lambda: self.export("geojson")).pack(fill="x", pady=1)
        ttk.Button(bar2, text="KML書き出し", command=lambda: self.export("kml")).pack(fill="x", pady=1)

        self.status = tk.StringVar(value="条件を入れて[検索]を押してください")
        ttk.Label(root, textvariable=self.status).pack(fill="x", padx=8)
        self.hover = tk.StringVar(value="")
        ttk.Label(root, textvariable=self.hover, foreground="#555").pack(fill="x", padx=8)

        pw = ttk.PanedWindow(root, orient="vertical")
        pw.pack(fill="both", expand=True, padx=8, pady=4)
        self.cv = tk.Canvas(pw, bg="white", height=330, highlightthickness=1, highlightbackground="#999")
        pw.add(self.cv, weight=3)
        cols = ("rank", "lat", "lon", "side", "inc", "obs", "n", "total")
        heads = ("順位", "緯度(中心)", "東経(中心)", "太陽の向き", "入射角の帯", "撮影回数", "画像枚数", "セル内の全枚数")
        tf = ttk.Frame(pw)
        pw.add(tf, weight=2)
        self.tree = ttk.Treeview(tf, columns=cols, show="headings", height=10)
        for c_, h in zip(cols, heads):
            self.tree.heading(c_, text=h)
            self.tree.column(c_, width=95, anchor="center")
        sb = ttk.Scrollbar(tf, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self.on_tree)

        self.cv.bind("<Configure>", lambda e: self.draw())
        self.cv.bind("<ButtonPress-1>", self.on_press)
        self.cv.bind("<B1-Motion>", self.on_drag)
        self.cv.bind("<ButtonRelease-1>", self.on_release)
        self.cv.bind("<Motion>", self.on_move)
        self.cv.bind("<Button-3>", lambda e: self.reset_view())
        root.after(150, self.poll)
        threading.Thread(target=self.load_types, daemon=True).start()

    # ---- 種類コード
    def load_types(self):
        try:
            pts = discover_types()
            if pts:
                self.q.put(("types", pts))
        except Exception as e:  # noqa
            self.q.put(("status", "画像の種類コードを自動取得できませんでした(検索時に再試行します): %s" % e))

    # ---- 入力
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
            raise ValueError("数値でない欄があります(緯度・経度の欄だけは空欄でも構いません)")
        c["minobs"] = int(c["minobs"])
        c["side"] = self.side.get()
        if c["incmin"] < CACHE_INC[0] or c["incmax"] > CACHE_INC[1] or c["emimin"] < CACHE_EMI[0] or c["emimax"] > CACHE_EMI[1]:
            raise ValueError("保存するデータの範囲は 入射角%g〜%g°・エミッション角%g〜%g° です。この範囲内で指定してください"
                             % (CACHE_INC + CACHE_EMI))
        if c["incwidth"] <= 0 or c["grid"] <= 0:
            raise ValueError("幅とグリッドは0より大きくしてください")
        if c["latmin"] >= c["latmax"] or c["lonmin"] >= c["lonmax"]:
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

    # ---- 実行
    def start(self):
        try:
            c = self.conds()
        except ValueError as e:
            messagebox.showerror("入力エラー", str(e)); return
        self.stop = False
        self.btn.state(["disabled"])
        self.tree.delete(*self.tree.get_children())
        self.rows, self.cellmap, self.sel = [], {}, None
        self.full = (c["lonmin"], c["lonmax"], c["latmin"], c["latmax"])
        self.view = self.full
        self.draw()
        threading.Thread(target=self.work, args=(c, self.pt.get()), daemon=True).start()

    def work(self, c, ptval):
        try:
            db = db_open()
            need = chunks_for(c)
            done = {r[0] for r in db.execute("SELECT chunk FROM done")}
            todo = [k for k in need if "%d,%d" % k not in done]
            if todo:
                pt = self.get_pt(ptval)
                self.q.put(("status", "初回のみ取得します: %d / %d 区画が未保存(保存先 %s)" % (len(todo), len(need), CACHE_PATH)))
                t0, finished = time.time(), 0
                with concurrent.futures.ThreadPoolExecutor(WORKERS) as ex:
                    futs = {ex.submit(fetch_chunk, la, lo, pt, lambda: self.stop): (la, lo) for la, lo in todo}
                    for fu in concurrent.futures.as_completed(futs):
                        la, lo = futs[fu]
                        rows = fu.result()
                        if rows is None:    # 中止された区画は保存しない(次回やり直し)
                            continue
                        db.executemany("INSERT OR REPLACE INTO recs VALUES(?,?,?,?,?,?,?)", rows)
                        db.execute("INSERT OR REPLACE INTO done VALUES(?,?)", ("%d,%d" % (la, lo), len(rows)))
                        db.commit()
                        finished += 1
                        el = time.time() - t0
                        eta = el / finished * (len(todo) - finished)
                        self.q.put(("status", "取得中… %d / %d 区画 保存済み(最新: 緯度%d〜 経度%d〜 %d枚) 残り約%d分。中止しても続きから再開できます"
                                    % (finished + len(need) - len(todo), len(need), la, lo, len(rows), eta / 60 + 0.5)))
                        if self.stop:
                            for f in futs: f.cancel()
            done = {r[0] for r in db.execute("SELECT chunk FROM done")}
            missing = [k for k in need if "%d,%d" % k not in done]
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

    def test(self):
        try:
            c = self.conds()
        except ValueError as e:
            messagebox.showerror("入力エラー", str(e)); return
        ptval = self.pt.get()

        def run():
            try:
                pt = self.get_pt(ptval)
                cc = dict(c, lonmin=23, lonmax=24, latmin=-1, latmax=1)
                prods = products_of(fetch(dict(base_params(cc, pt, False), limit=3, offset=0)))
                txt = "種類コード: %s\n取得できた件数: %d\n\n" % (pt, len(prods))
                for pr in prods[:2]:
                    f = flat(pr)
                    txt += "解釈結果: %s\n" % parse_record(pr)
                    txt += "項目: " + ", ".join(sorted(f.keys())) + "\n\n"
                self.q.put(("info", txt))
            except Exception as e:  # noqa
                self.q.put(("info", "失敗: %s" % e))
        threading.Thread(target=run, daemon=True).start()

    def poll(self):
        try:
            while True:
                k, v = self.q.get_nowait()
                if k == "status": self.status.set(v)
                elif k == "done": self.btn.state(["!disabled"])
                elif k == "info": messagebox.showinfo("接続テスト", v[:3000])
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
                            "%g〜%g°" % (r["inc0"], r["inc1"]), r["obs"], r["n"], r["total"]))
                    self.draw()
        except queue.Empty:
            pass
        self.root.after(150, self.poll)

    # ---- 地図
    def tx(self, lon):
        x0, x1, _, _ = self.view
        w = max(10, self.cv.winfo_width() - self.ML - self.MR)
        return self.ML + (lon - x0) / (x1 - x0) * w

    def ty(self, lat):
        _, _, y0, y1 = self.view
        h = max(10, self.cv.winfo_height() - self.MT - self.MB)
        return self.MT + (y1 - lat) / (y1 - y0) * h

    def inv(self, px, py):
        x0, x1, y0, y1 = self.view
        w = max(10, self.cv.winfo_width() - self.ML - self.MR)
        h = max(10, self.cv.winfo_height() - self.MT - self.MB)
        return x0 + (px - self.ML) / w * (x1 - x0), y1 - (py - self.MT) / h * (y1 - y0)

    def draw(self):
        cv = self.cv
        cv.delete("all")
        if not self.view:
            cv.create_text(20, 20, anchor="nw", fill="#777",
                           text="ここに地図が出ます(東が右・北が上)。条件を入れて[検索]を押してください。")
            return
        x0, x1, y0, y1 = self.view
        W, H = cv.winfo_width(), cv.winfo_height()
        L, R, T, B = self.ML, W - self.MR, self.MT, H - self.MB
        cv.create_rectangle(L, T, R, B, fill="#f4f4f4", outline="#888")
        sx, sy = nice_step(x1 - x0), nice_step(y1 - y0)
        v = math.ceil(x0 / sx) * sx
        while v <= x1:
            x = self.tx(v)
            cv.create_line(x, T, x, B, fill="#ddd")
            cv.create_text(x, B + 3, anchor="n", text="%g" % v, fill="#555", font=("", 8))
            v += sx
        v = math.ceil(y0 / sy) * sy
        while v <= y1:
            y = self.ty(v)
            cv.create_line(L, y, R, y, fill="#ddd")
            cv.create_text(L - 4, y, anchor="e", text="%g" % v, fill="#555", font=("", 8))
            v += sy
        if self.cellmap:
            mx = max(r["obs"] for r in self.cellmap.values())
            mn = min(r["obs"] for r in self.cellmap.values())
            for r in self.cellmap.values():
                g = r["g"]
                lo0, lo1, la0, la1 = r["ix"] * g, (r["ix"] + 1) * g, r["iy"] * g, (r["iy"] + 1) * g
                if lo1 < x0 or lo0 > x1 or la1 < y0 or la0 > y1:
                    continue
                t = (r["obs"] - mn) / (mx - mn) if mx > mn else 1.0
                xa, xb = max(L, self.tx(lo0)), min(R, self.tx(lo1))
                ya, yb = max(T, self.ty(la1)), min(B, self.ty(la0))
                cv.create_rectangle(xa, ya, xb, yb, fill=hexcol(ramp(t)), outline="")
            # 凡例
            for i in range(60):
                cv.create_rectangle(R - 200 + i * 2, 4, R - 198 + i * 2, 14, fill=hexcol(ramp(i / 59)), outline="")
            cv.create_text(R - 204, 9, anchor="e", text="撮影回数 %d" % mn, font=("", 8))
            cv.create_text(R - 76, 9, anchor="w", text="%d" % mx, font=("", 8))
        if self.sel is not None and self.sel < len(self.rows):
            r = self.rows[self.sel]; g = r["g"]
            cv.create_rectangle(self.tx(r["ix"] * g), self.ty((r["iy"] + 1) * g),
                                self.tx((r["ix"] + 1) * g), self.ty(r["iy"] * g),
                                outline="#0050ff", width=2)
        cv.create_text(4, 4, anchor="nw", text="緯度", font=("", 8), fill="#555")
        cv.create_text(L, H - 2, anchor="sw", text="東経(°)  →東(右)", font=("", 8), fill="#555")

    def reset_view(self):
        if self.full:
            self.view = self.full
            self.draw()

    def cell_at(self, px, py):
        if not self.view:
            return None
        lon, lat = self.inv(px, py)
        if not self.cellmap:
            return None
        g = next(iter(self.cellmap.values()))["g"]
        return self.cellmap.get((math.floor(lat / g), math.floor(lon / g)))

    def on_press(self, e):
        self.drag = (e.x, e.y)

    def on_drag(self, e):
        if self.drag:
            self.cv.delete("rb")
            self.cv.create_rectangle(self.drag[0], self.drag[1], e.x, e.y, outline="#0050ff", dash=(3, 2), tags="rb")

    def on_release(self, e):
        if not self.drag or not self.view:
            return
        x0, y0 = self.drag
        self.drag = None
        self.cv.delete("rb")
        if abs(e.x - x0) > 8 and abs(e.y - y0) > 8:
            a, b = self.inv(min(x0, e.x), max(y0, e.y)), self.inv(max(x0, e.x), min(y0, e.y))
            if b[0] > a[0] and b[1] > a[1]:
                self.view = (a[0], b[0], a[1], b[1])
                self.draw()
        else:
            r = self.cell_at(e.x, e.y)
            if r is not None:
                idx = self.rows.index(r)
                self.select(idx)

    def on_move(self, e):
        if not self.view or self.drag:
            return
        lon, lat = self.inv(e.x, e.y)
        r = self.cell_at(e.x, e.y)
        s = "緯度 %.2f° 東経 %.2f°" % (lat, lon)
        if r:
            s += "  |  撮影回数 %d / %d枚 / 入射角 %g〜%g° / 太陽 %s" % (r["obs"], r["n"], r["inc0"], r["inc1"], r["side"])
        self.hover.set(s)

    def select(self, idx):
        self.sel = idx
        if str(idx) in self.tree.get_children():
            self.tree.selection_set(str(idx)); self.tree.see(str(idx))
        self.draw()

    def on_tree(self, _e):
        s = self.tree.selection()
        if s:
            self.sel = int(s[0]); self.draw()

    # ---- 保存
    def cur(self):
        s = self.tree.selection()
        if s:
            return self.rows[int(s[0])]
        if self.sel is not None and self.sel < len(self.rows):
            return self.rows[self.sel]
        messagebox.showinfo("選択", "順位表の行か、地図のセルを選んでください")
        return None

    def copy_cell(self):
        r = self.cur()
        if r:
            t = "%.4f, %.4f" % (r["lat"], r["lon"])
            self.root.clipboard_clear(); self.root.clipboard_append(t)
            self.status.set("コピーしました(緯度, 東経): " + t + "  ← QuickMapの座標移動欄などに貼り付けてください")

    def save_cell(self):
        r = self.cur()
        if not r: return
        fn = filedialog.asksaveasfilename(defaultextension=".csv", initialfile="nac_cell.csv")
        if not fn: return
        with open(fn, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f); w.writerow(["画像ID", "緯度", "東経", "入射角", "エミッション角", "太陽の向き", "撮影時刻"])
            for i in sorted(r["items"], key=lambda x: x["time"]):
                w.writerow([i["id"], i["lat"], i["lon"], i["inc"], i["emi"], i["side"], i["time"]])
        self.status.set("保存しました: " + fn)

    def save_all(self):
        if not self.rows: return
        fn = filedialog.asksaveasfilename(defaultextension=".csv", initialfile="nac_ranking.csv")
        if not fn: return
        with open(fn, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f); w.writerow(["順位", "緯度", "東経", "太陽の向き", "入射角下限", "入射角上限", "撮影回数", "枚数", "セル内全枚数"])
            for i, r in enumerate(self.rows, 1):
                w.writerow([i, r["lat"], r["lon"], r["side"], r["inc0"], r["inc1"], r["obs"], r["n"], r["total"]])
        self.status.set("保存しました: " + fn)

    def export(self, kind):
        if not self.rows:
            messagebox.showinfo("書き出し", "先に検索してください"); return
        ext = ".geojson" if kind == "geojson" else ".kml"
        fn = filedialog.asksaveasfilename(defaultextension=ext, initialfile="nac_density" + ext)
        if not fn: return

        def ring(r):
            g = r["g"]
            a, b, c_, d = r["ix"] * g, (r["ix"] + 1) * g, r["iy"] * g, (r["iy"] + 1) * g
            if (a + b) / 2 > 180:   # -180〜180 の東経に変換
                a -= 360; b -= 360
            return [[a, c_], [b, c_], [b, d], [a, d], [a, c_]]
        mx = max(r["obs"] for r in self.rows); mn = min(r["obs"] for r in self.rows)
        if kind == "geojson":
            feats = []
            for i, r in enumerate(self.rows, 1):
                feats.append({"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [ring(r)]},
                              "properties": {"rank": i, "obs": r["obs"], "images": r["n"], "side": r["side"],
                                             "inc_min": r["inc0"], "inc_max": r["inc1"],
                                             "lat": r["lat"], "lon_east": r["lon"]}})
            with open(fn, "w", encoding="utf-8") as f:
                json.dump({"type": "FeatureCollection", "features": feats}, f, ensure_ascii=False)
        else:
            out = ['<?xml version="1.0" encoding="UTF-8"?>', '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>',
                   "<name>NAC撮影回数</name>"]
            for i, r in enumerate(self.rows, 1):
                t = (r["obs"] - mn) / (mx - mn) if mx > mn else 1.0
                R_, G_, B_ = ramp(t)
                col = "99%02x%02x%02x" % (B_, G_, R_)  # aabbggrr
                coords = " ".join("%.5f,%.5f,0" % (p[0], p[1]) for p in ring(r))
                out.append("<Placemark><name>#%d (%d回)</name><description>%s 入射角%g〜%g° %d枚</description>"
                           "<Style><PolyStyle><color>%s</color></PolyStyle><LineStyle><width>0</width></LineStyle></Style>"
                           "<Polygon><outerBoundaryIs><LinearRing><coordinates>%s</coordinates></LinearRing></outerBoundaryIs></Polygon>"
                           "</Placemark>" % (i, r["obs"], r["side"], r["inc0"], r["inc1"], r["n"], col, coords))
            out.append("</Document></kml>")
            with open(fn, "w", encoding="utf-8") as f:
                f.write("\n".join(out))
        self.status.set("書き出しました: " + fn)


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
