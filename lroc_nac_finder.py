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
import urllib.request, urllib.parse, io, webbrowser, hashlib, base64
import moon_basemap as bp
import ui_theme as ut      # ライト/ダークの配色
import place_names as pn   # 地名(静かの海など)   # 月面の背景地図 (Pillow が無ければ背景なしで動く)

ODE = "https://oderest.rsl.wustl.edu/live2/"
PAGE = 1000
SETTINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lroc_nac_settings.json")
PRODUCT_URL = "https://data.lroc.im-ldi.com/lroc/view_lroc/LRO-L-LROC-2-EDR-V1.0/"   # 画像ごとのページ(ダウンロードリンクあり)
QUICKMAP_URL = "https://quickmap.lroc.im-ldi.com/query"
MOON_R = 1737400.0   # QuickMapの座標(メートル)への変換に使う月の半径


def quickmap_url(r):
    """セルを中心にした表示範囲のQuickMapのURL。範囲は prjExtent(左,下,右,上)で、proj=16 は経緯度の地図。
    QuickMapの共有リンクから読み取った書式(経度は -180〜180、x=半径×経度[rad]、y=半径×緯度[rad])。"""
    g = r["g"]
    lon, lat = (r["ix"] + 0.5) * g, (r["iy"] + 0.5) * g
    if lon > 180:
        lon -= 360
    half = max(g * 4, 0.25)    # セルの約8倍の幅が見える範囲
    k = MOON_R * math.pi / 180
    ext = [(lon - half) * k, (lat - half) * k, (lon + half) * k, (lat + half) * k]
    # 描画ポイント(drawing point): features="経度,緯度@@{id}" と selectedFeature="@@user-defined,id" の書式
    pid = base64.urlsafe_b64encode(hashlib.md5(("%d,%d" % (r["ix"], r["iy"])).encode()).digest()).decode()[:5]
    point = "%.6f,%.6f@@%s" % (lon, lat, json.dumps({"id": pid}, separators=(",", ":")))
    return QUICKMAP_URL + "?" + urllib.parse.urlencode({
        "prjExtent": ",".join("%.4f" % v for v in ext), "selectedFeature": "@@user-defined," + pid,
        "proj": 16, "features": point})
STOPS = [(255, 255, 178), (254, 204, 92), (253, 141, 60), (240, 59, 32), (189, 0, 38)]
# 配色の選択肢。STOPS をその場で入れ替えるので、地図・凡例・書き出しが同じ色になる
PALETTES = {
    "黄→赤(標準)": list(STOPS),
    "紫→黄(色覚に配慮)": [(120, 60, 170), (50, 110, 190), (30, 170, 160), (130, 215, 70), (253, 231, 37)],
}


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


def product_url(image_id):
    """画像ID(M1274075475LE など)のダウンロードページ。EDR・CDR・プレビュー画像へのリンクが載っている。"""
    return PRODUCT_URL + urllib.parse.quote(image_id)


def obs_ids(r):
    """セル内の撮影ID(左右カメラの LE/RE を除いた M13xxxxxxxx)を古い順に並べる。"""
    return sorted({i["id"][:-2] for i in r["items"]})


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
        ("緯度 最小", "latmin", ""), ("緯度 最大", "latmax", ""),
        ("経度 最小", "lonmin", ""), ("経度 最大", "lonmax", ""),
        ("入射角 最小", "incmin", "50"), ("入射角 最大", "incmax", "70"),
        ("エミッション角 最小", "emimin", "0"), ("エミッション角 最大", "emimax", "5"),
        ("入射角のくくり(°)", "incwidth", "5"),
        ("グリッド(°)", "grid", "0.5"),
        ("最小撮影回数", "minobs", "3"),
    ]
    OPTIONAL = {"latmin": -90.0, "latmax": 90.0, "lonmin": 0.0, "lonmax": 360.0}
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

        top = ttk.Frame(root)
        top.pack(fill="x", padx=8, pady=4)
        frm = ttk.LabelFrame(top, text="条件(ここに入力)")
        frm.pack(side="left", fill="y")
        self.v = {}
        for i, (label, key, default) in enumerate(self.FIELDS):
            r, col = divmod(i, 3)   # 3列にして条件欄の高さを抑える
            ttk.Label(frm, text=label).grid(row=r, column=col * 2, sticky="e", padx=4, pady=1)
            self.v[key] = tk.StringVar(value=default)
            ttk.Entry(frm, textvariable=self.v[key], width=9).grid(row=r, column=col * 2 + 1, sticky="w", padx=4)
        r = (len(self.FIELDS) + 2) // 3
        ttk.Label(frm, text="太陽の向き").grid(row=r, column=0, sticky="e", padx=4, pady=3)
        self.side = tk.StringVar(value="両方")
        ttk.Combobox(frm, textvariable=self.side, width=10, state="readonly",
                     values=["両方", "東(右・朝)", "西(左・夕)"]).grid(row=r, column=1, sticky="w", padx=4)
        ttk.Label(frm, text="緯度・経度は空欄なら月全体 / 入射角は40〜80° / エミッション角は0〜10° の範囲で指定",
                  style="Mute.TLabel").grid(row=r + 1, column=0, columnspan=6, sticky="w", padx=4, pady=(2, 0))
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
        self.bg_loading = False   # 月面タイルを取得中か

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
        ttk.Button(idrow, text="IDをコピー", command=self.copy_ids).pack(side="right", padx=(6, 0))
        ttk.Button(idrow, text="セル内の全IDの画像ページを開く(ダウンロード)", command=self.open_all_products).pack(side="right", padx=(6, 0))
        ttk.Entry(idrow, textvariable=self.idvar, state="readonly").pack(side="left", fill="x", expand=True)
        # 操作ヒントは地図の状態に関係なく常に表示する
        ttk.Label(root, style="Mute.TLabel", wraplength=1200, text=(
            "地図の操作:  ホイール または ＋－キー=拡大・縮小  /  矢印キー=移動  /  Home=全体表示  /  ホイール押しながら または 右ドラッグ=移動  /  "
            "左ドラッグ=囲んだ範囲を拡大  /  右クリック=全体表示  /  左クリック=セルを選ぶ  /  ダブルクリック=QuickMapでその場所を開く  /  順位表の行=その場所へ")
        ).pack(fill="x", padx=8, pady=(2, 0))

        pw = ttk.PanedWindow(root, orient="vertical")
        pw.pack(fill="both", expand=True, padx=8, pady=4)
        self.cv = tk.Canvas(pw, bg=self.th["canvas"], height=560, highlightthickness=1, highlightbackground=self.th["outline"])
        pw.add(self.cv, weight=6)
        cols = ("rank", "lat", "lon", "side", "inc", "obs", "n", "total")
        heads = ("順位", "緯度(中心)", "東経(中心)", "太陽の向き", "入射角の帯", "撮影回数", "画像枚数", "セル内の全枚数")
        tf = ttk.Frame(pw)
        pw.add(tf, weight=1)
        self.tree = ttk.Treeview(tf, columns=cols, show="headings", height=5)
        for c_, h in zip(cols, heads):
            self.tree.heading(c_, text=h, command=lambda k=c_: self.sort_tree(k))   # 見出しクリックで並べ替え
            self.tree.column(c_, width={"rank": 55, "total": 130, "inc": 110}.get(c_, 95), anchor="center")
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
        self.set_view(*self.full)
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

    def poll(self):
        try:
            while True:
                k, v = self.q.get_nowait()
                if k == "status": self.status.set(v)
                elif k == "done": self.btn.state(["!disabled"])
                elif k == "redraw": self.bg_loading = False; self.draw()
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
                    if v:   # 経度に引き伸ばされないよう、1位のセルを中心に縦横比どおりの範囲で見せる
                        self.fit_view(v[0]["lon"])
                    else:
                        self.draw()
        except queue.Empty:
            pass
        self.root.after(150, self.poll)

    # ---- 地図
    def plot_rect(self, view=None):
        """地図を描く四角 (左, 上, 右, 下)。経度1°と緯度1°を同じ長さにして(縦横比を保って)、余白の中央に置く。"""
        x0, x1, y0, y1 = view or self.view
        aw = max(10, self.cv.winfo_width() - self.ML - self.MR)
        ah = max(10, self.cv.winfo_height() - self.MT - self.MB)
        sc = min(aw / (x1 - x0), ah / (y1 - y0))
        w, h = (x1 - x0) * sc, (y1 - y0) * sc
        L = self.ML + (aw - w) / 2
        T = self.MT + (ah - h) / 2
        return L, T, L + w, T + h

    def tx(self, lon):
        x0, x1, _, _ = self.view
        L, _, R, _ = self.plot_rect()
        return L + (lon - x0) / (x1 - x0) * (R - L)

    def ty(self, lat):
        _, _, y0, y1 = self.view
        _, T, _, B = self.plot_rect()
        return T + (y1 - lat) / (y1 - y0) * (B - T)

    def inv(self, px, py):
        x0, x1, y0, y1 = self.view
        L, T, R, B = self.plot_rect()
        return x0 + (px - L) / (R - L) * (x1 - x0), y1 - (py - T) / (B - T) * (y1 - y0)

    def fit_view(self, cx):
        """経度 cx を中心に、緯度の全範囲がちょうど収まる(縦横比どおりの)表示範囲にする。"""
        _, _, fy0, fy1 = self.full
        self.set_view(cx - 0.01, cx + 0.01, fy0, fy1)   # 幅は set_view が画面の縦横比に合わせて広げる

    def draw(self):
        cv = self.cv
        cv.delete("all")
        if not self.view:
            cv.create_text(20, 20, anchor="nw", fill=self.th["hint"],
                           text="ここに地図が出ます(東が右・北が上)。条件を入れて[検索]を押してください。\n操作: ホイール=拡大縮小 / ホイール押しながら or 右ドラッグ=移動 / 左ドラッグ=範囲を囲んで拡大 / 右クリック=全体表示 / 左クリック=セル選択")
            return
        x0, x1, y0, y1 = self.view
        W, H = cv.winfo_width(), cv.winfo_height()
        L, T, R, B = self.plot_rect()
        Rc, Tc = W - self.MR, self.MT      # 凡例は地図の四角ではなく画面の右上に置く
        cv.create_rectangle(L, T, R, B, fill=self.th["plot"], outline=self.th["outline"])
        photo = self.bg_photo(R - L, B - T) if (bp.OK and self.bg_on.get()) else None
        if photo:   # 月面写真(+ヒートマップ)を1枚の画像として貼る
            cv.create_image(L, T, anchor="nw", image=photo)
        sx, sy = nice_step(x1 - x0), nice_step(y1 - y0)
        gridcol = "#fff" if photo else self.th["grid"]
        v = math.ceil(x0 / sx) * sx
        while v <= x1:
            x = self.tx(v)
            cv.create_line(x, T, x, B, fill=gridcol, dash=(2, 4) if photo else None)
            cv.create_text(x, B + 3, anchor="n", text="%g" % v, fill=self.th["axis"], font=("", 9))
            v += sx
        v = math.ceil(y0 / sy) * sy
        while v <= y1:
            y = self.ty(v)
            cv.create_line(L, y, R, y, fill=gridcol, dash=(2, 4) if photo else None)
            cv.create_text(L - 4, y, anchor="e", text="%g" % v, fill=self.th["axis"], font=("", 9))
            v += sy
        if self.cellmap:
            mx = max(r["obs"] for r in self.cellmap.values())
            mn = min(r["obs"] for r in self.cellmap.values())
            for r in ([] if photo else self.cellmap.values()):
                g = r["g"]
                lo0, lo1, la0, la1 = r["ix"] * g, (r["ix"] + 1) * g, r["iy"] * g, (r["iy"] + 1) * g
                if lo1 < x0 or lo0 > x1 or la1 < y0 or la0 > y1:
                    continue
                t = (r["obs"] - mn) / (mx - mn) if mx > mn else 1.0
                xa, xb = max(L, self.tx(lo0)), min(R, self.tx(lo1))
                ya, yb = max(T, self.ty(la1)), min(B, self.ty(la0))
                cv.create_rectangle(xa, ya, xb, yb, fill=hexcol(ramp(t)), outline="")
        if self.cellmap:
            # 凡例 (mx, mn は上のブロックで計算済み)。白地に大きめの数字で、写真の上でも読めるようにする
            bx = Rc - 300
            cv.create_rectangle(bx - 2, 1, Rc, Tc - 1, fill=self.th["legend_bg"], outline=self.th["legend_edge"])
            cv.create_text(bx + 2, Tc // 2, anchor="w", text="撮影回数", font=("", 10, "bold"), fill=self.th["fg"])
            for i in range(60):
                cv.create_rectangle(bx + 72 + i * 2, 4, bx + 74 + i * 2, Tc - 5, fill=hexcol(ramp(i / 59)), outline="")
            cv.create_text(bx + 68, Tc // 2, anchor="e", text="少 %d" % mn, font=("", 10), fill=self.th["fg"])
            cv.create_text(bx + 198, Tc // 2, anchor="w", text="%d 多" % mx, font=("", 10), fill=self.th["fg"])
        if self.names_on.get():
            self.draw_names(L, R, T, B)
        if self.sel is not None and self.sel < len(self.rows):
            r = self.rows[self.sel]; g = r["g"]
            xa, xb = self.tx(r["ix"] * g), self.tx((r["ix"] + 1) * g)
            ya, yb = self.ty((r["iy"] + 1) * g), self.ty(r["iy"] * g)
            cv.create_rectangle(xa, ya, xb, yb, outline=self.th["sel"], width=2)
            # 選んだセルの回数を枠のそばに文字でも表示(色だけに頼らない)
            tid = cv.create_text(min(xb + 6, R - 70), max(ya, T + 10), anchor="w",
                                 text="%d回" % r["obs"], font=("", 11, "bold"), fill=self.th["sel_text"])
            bb = cv.bbox(tid)
            if bb:
                bg = cv.create_rectangle(bb[0] - 3, bb[1] - 1, bb[2] + 3, bb[3] + 1, fill=self.th["label_bg"], outline=self.th["sel"])
                cv.tag_lower(bg, tid)
        cv.create_text(4, 4, anchor="nw", text="緯度", font=("", 9), fill=self.th["axis"])
        cv.create_text(L, H - 2, anchor="sw", text="東経(°)  →東(右)", font=("", 9), fill=self.th["axis"])

    def draw_names(self, L, R, T, B):
        """表示範囲に入る地名を描く。広く見ているときは海だけ、拡大したらクレーターとアポロ着陸地点も出す。"""
        x0, x1, y0, y1 = self.view
        wide = (x1 - x0) > 60
        for name, kind, lat, lon in pn.PLACES:
            if wide and kind != "mare":
                continue
            lon %= 360.0
            if not (x0 <= lon <= x1 and y0 <= lat <= y1):
                continue
            x, y = self.tx(lon), self.ty(lat)
            if not (L <= x <= R and T <= y <= B):
                continue
            mark = "▲" if kind == "apollo" else "●" if kind == "crater" else "+"
            tid = self.cv.create_text(x + 6, y, anchor="w", text=name, font=("", 10, "bold"), fill=self.th["fg"])
            bb = self.cv.bbox(tid)
            bg = self.cv.create_rectangle(bb[0] - 2, bb[1], bb[2] + 2, bb[3], fill=self.th["label_bg"], outline="")
            self.cv.tag_lower(bg, tid)
            self.cv.create_text(x, y, text=mark, font=("", 9, "bold"), fill=self.th["sel"])

    def on_jump(self, _e=None):
        """選んだ地名の周り(約10°四方)へ地図を移動する。検索した範囲の外なら知らせる。"""
        p = pn.BY_NAME.get(self.jump.get())
        if not p or not self.full:
            self.status.set("先に検索すると、地名の場所へ移動できます"); return
        lat, lon = p[2], p[3] % 360.0
        fx0, fx1, fy0, fy1 = self.full
        if not (fx0 <= lon <= fx1 and fy0 <= lat <= fy1):
            self.status.set("%s は検索した範囲の外です(緯度%.1f° 東経%.1f°)" % (p[0], lat, p[3])); return
        self.names_on.set(True)
        self.set_view(lon - 5, lon + 5, lat - 5, lat + 5)
        self.status.set("%s へ移動しました(緯度%.1f° 東経%.1f°)" % (p[0], lat, p[3]))

    def bg_photo(self, w, h):
        """月面写真にヒートマップを重ねた画像を返す。写真のタイルが足りなければ裏で取得し、届いたら描き直す。"""
        w, h = int(w), int(h)
        if w < 20 or h < 20:
            return None
        key = (self.view, w, h, round(self.alpha.get(), 2), id(self.cellmap), len(self.cellmap), self.palette.get())
        if self.bg_cache and self.bg_cache[0] == key:
            return self.bg_cache[1]
        img, missing = bp.base(self.view, w, h)
        if missing and not self.bg_loading:
            self.bg_loading = True

            def run():
                bad = bp.download(missing)
                if bad:
                    self.q.put(("status", "月面写真のタイルを %d 枚取得できませんでした(ネット接続を確認してください)" % bad))
                self.q.put(("redraw", None))
            threading.Thread(target=run, daemon=True).start()
        img = bp.with_heat(img, self.view, list(self.cellmap.values()), ramp, self.alpha.get())
        photo = bp.to_photo(img)
        if not missing:
            self.bg_cache = (key, photo)
        self.bg_last = photo    # 画像を持ち続けないと画面から消える(Tkの仕様)
        return photo

    def toggle_top(self):
        """条件・ボタンの帯を隠して地図を広げる / 元に戻す。"""
        if self.top.winfo_manager():
            self.top.pack_forget()
            self.tbtn.config(text="▼ 条件パネルを表示")
        else:
            self.top.pack(fill="x", padx=8, pady=4, before=self.row)
            self.tbtn.config(text="▲ 条件パネルを隠す(地図を広げる)")

    def on_theme(self):
        self.th = ut.apply(self.root, self.dark.get())
        self.cv.configure(bg=self.th["canvas"], highlightbackground=self.th["outline"])
        try:
            with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
                json.dump({"dark": self.dark.get()}, f)
        except OSError:
            pass
        self.draw()

    def on_palette(self, _e=None):
        STOPS[:] = PALETTES[self.palette.get()]
        self.draw()

    def set_view(self, x0, x1, y0, y1):
        """表示範囲を設定する。いつも画面いっぱいに地図が出るよう、範囲は画面の縦横比に合わせて広げる。
        検索した範囲の外には出さず、極端な拡大もしない。"""
        fx0, fx1, fy0, fy1 = self.full
        aw = max(10, self.cv.winfo_width() - self.ML - self.MR)
        ah = max(10, self.cv.winfo_height() - self.MT - self.MB)
        A = aw / ah
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        w0, h0 = max(x1 - x0, 1e-6), max(y1 - y0, 1e-6)
        w, h = (h0 * A, h0) if w0 / h0 < A else (w0, w0 / A)   # 足りない方向を広げる
        if h < 0.05:
            h = 0.05; w = h * A                                  # 拡大しすぎない
        wmax = max(fx1 - fx0, (fy1 - fy0) * A)                   # 縮小は、検索範囲の全体が画面に入るまで
        if w > wmax:
            w = wmax; h = w / A
        nx0 = (fx0 + fx1) / 2 - w / 2 if w >= fx1 - fx0 else min(max(cx - w / 2, fx0), fx1 - w)
        ny0 = (fy0 + fy1) / 2 - h / 2 if h >= fy1 - fy0 else min(max(cy - h / 2, fy0), fy1 - h)
        self.view = (nx0, nx0 + w, ny0, ny0 + h)
        self.draw()

    def on_resize(self):
        if self.view:
            self.set_view(*self.view)    # 窓の大きさが変わっても画面いっぱいに保つ
        else:
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

    def zoom_by(self, f):
        """画面の中心を基準に拡大(f<1)・縮小(f>1)。ボタンとキー操作用。"""
        if not self.view:
            return
        x0, x1, y0, y1 = self.view
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        self.set_view(cx - (cx - x0) * f, cx + (x1 - cx) * f, cy - (cy - y0) * f, cy + (y1 - cy) * f)

    def pan_by(self, fx, fy):
        """表示範囲の幅・高さの割合だけ動かす(キー操作用)。"""
        if not self.view:
            return
        x0, x1, y0, y1 = self.view
        dx, dy = (x1 - x0) * fx, (y1 - y0) * fy
        self.set_view(x0 + dx, x1 + dx, y0 + dy, y1 + dy)

    def on_wheel(self, e):
        if not self.view:
            return
        f = 0.75 if e.delta > 0 else 1 / 0.75      # 奥へ回すと拡大、手前へ回すと縮小
        lon, lat = self.inv(e.x, e.y)
        x0, x1, y0, y1 = self.view
        self.set_view(lon - (lon - x0) * f, lon + (x1 - lon) * f, lat - (lat - y0) * f, lat + (y1 - lat) * f)

    def on_rpress(self, e):
        self.pan = (e.x, e.y, self.view, False)

    def on_rdrag(self, e):
        if not getattr(self, "pan", None) or not self.view:
            return
        sx, sy, v, _ = self.pan
        if abs(e.x - sx) + abs(e.y - sy) > 4:
            self.pan = (sx, sy, v, True)
        if self.pan[3]:
            L, T, R, B = self.plot_rect(v)
            dx = -(e.x - sx) / (R - L) * (v[1] - v[0])
            dy = (e.y - sy) / (B - T) * (v[3] - v[2])
            self.set_view(v[0] + dx, v[1] + dx, v[2] + dy, v[3] + dy)

    def on_rrelease(self, e):
        if getattr(self, "pan", None) and not self.pan[3]:
            self.reset_view()
        self.pan = None

    def reset_view(self):
        if self.full:
            self.set_view(*self.full)

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
            self.cv.create_rectangle(self.drag[0], self.drag[1], e.x, e.y, outline=self.th["sel"], dash=(3, 2), tags="rb")

    def on_release(self, e):
        if not self.drag or not self.view:
            return
        x0, y0 = self.drag
        self.drag = None
        self.cv.delete("rb")
        if abs(e.x - x0) > 8 and abs(e.y - y0) > 8:
            a, b = self.inv(min(x0, e.x), max(y0, e.y)), self.inv(max(x0, e.x), min(y0, e.y))
            if b[0] > a[0] and b[1] > a[1]:
                self.set_view(a[0], b[0], a[1], b[1])
        else:
            r = self.cell_at(e.x, e.y)
            if r is not None:
                idx = self.rows.index(r)
                self.select(idx)

    def on_double(self, e):
        """セルをダブルクリック: そのセルを選び、座標をコピーしたうえでQuickMapをブラウザで開く。"""
        r = self.cell_at(e.x, e.y)
        if r is None:
            return
        self.drag = None
        self.cv.delete("rb")
        self.select(self.rows.index(r))
        self.copy_cell()
        webbrowser.open(quickmap_url(r))
        self.status.set("QuickMapを開きました(そのセルの中心に点を置いて表示)。ずれる場合は、コピー済みの座標を座標移動欄に貼り付けてください")

    def on_move(self, e):
        if not self.view or self.drag:
            return
        lon, lat = self.inv(e.x, e.y)
        r = self.cell_at(e.x, e.y)
        s = "緯度 %.2f° 東経 %.2f°" % (lat, lon)
        if r:
            s += "  |  撮影回数 %d / %d枚 / 入射角 %g〜%g° / 太陽 %s" % (r["obs"], r["n"], r["inc0"], r["inc1"], r["side"])
        self.hover.set(s)

    def copy_ids(self):
        r = self.cur()
        if r:
            self.root.clipboard_clear(); self.root.clipboard_append(" ".join(obs_ids(r)))
            self.status.set("撮影IDをコピーしました(%d件)" % len(obs_ids(r)))

    def open_all_products(self):
        """選んだセルにある撮影IDすべての画像ページを、ブラウザのタブで順に開く。
        左右カメラ(LE/RE)のうち、セルの中にある方のページを開く。"""
        r = self.cur()
        if not r:
            return
        pages = {}
        for i in r["items"]:
            pages.setdefault(i["id"][:-2], i["id"])     # 撮影IDごとに1ページ
        urls = [product_url(pages[k]) for k in sorted(pages)]
        if len(urls) > 8 and not messagebox.askokcancel("確認", "%d個のページを開きます。よろしいですか?" % len(urls)):
            return
        for n, u in enumerate(urls):
            self.root.after(n * 400, lambda u=u: webbrowser.open_new_tab(u))   # 少し間をあけて開く
        self.status.set("%d個の画像ページを開きます(EDR/CDRのダウンロードリンクがあります)" % len(urls))

    def show_ids(self, r):
        ids = obs_ids(r)
        self.idvar.set("撮影ID(%d回): %s" % (len(ids), "  ".join(ids)))

    def select(self, idx):
        self.sel = idx
        self.show_ids(self.rows[idx])
        if str(idx) in self.tree.get_children():
            self.tree.selection_set(str(idx)); self.tree.see(str(idx))
        self.draw()

    def on_tree(self, _e):
        s = self.tree.selection()
        if s and int(s[0]) != self.sel:    # 地図クリック由来(すでに選択済み)のときは拡大しない
            self.sel = int(s[0])
            r = self.rows[self.sel]
            self.show_ids(r)
            if self.full:   # 選んだセルを中心に、セルの約10倍の範囲へ拡大
                g = r["g"]
                cx, cy = (r["ix"] + 0.5) * g, (r["iy"] + 0.5) * g
                half = max(g * 5, 0.25)
                self.set_view(cx - half, cx + half, cy - half, cy + half)
            else:
                self.draw()

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
            w = csv.writer(f); w.writerow(["画像ID", "緯度", "東経", "入射角", "エミッション角", "太陽の向き", "撮影時刻", "画像ページ"])
            for i in sorted(r["items"], key=lambda x: x["time"]):
                w.writerow([i["id"], i["lat"], i["lon"], i["inc"], i["emi"], i["side"], i["time"], product_url(i["id"])])
        self.status.set("保存しました: " + fn)

    def save_all(self):
        if not self.rows: return
        fn = filedialog.asksaveasfilename(defaultextension=".csv", initialfile="nac_ranking.csv")
        if not fn: return
        with open(fn, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f); w.writerow(["順位", "緯度", "東経", "太陽の向き", "入射角下限", "入射角上限", "撮影回数", "枚数", "セル内全枚数", "撮影ID"])
            for i, r in enumerate(self.rows, 1):
                w.writerow([i, r["lat"], r["lon"], r["side"], r["inc0"], r["inc1"], r["obs"], r["n"], r["total"], " ".join(obs_ids(r))])
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
                t = (r["obs"] - mn) / (mx - mn) if mx > mn else 1.0
                col = hexcol(ramp(t))
                feats.append({"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [ring(r)]},
                              "properties": {"rank": i, "obs": r["obs"], "images": r["n"], "side": r["side"],
                                             "inc_min": r["inc0"], "inc_max": r["inc1"],
                                             "lat": r["lat"], "lon_east": r["lon"],
                                             "obs_ids": " ".join(obs_ids(r)),
                                             # simplestyle-spec: 撮影回数に応じた色
                                             "fill": col, "fill-opacity": 0.6,
                                             "stroke": col, "stroke-width": 0, "stroke-opacity": 0}})
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
                out.append("<Placemark><name>#%d (%d回)</name><description>%s 入射角%g〜%g° %d枚 ID: %s</description>"
                           "<Style><PolyStyle><color>%s</color></PolyStyle><LineStyle><width>0</width></LineStyle></Style>"
                           "<Polygon><outerBoundaryIs><LinearRing><coordinates>%s</coordinates></LinearRing></outerBoundaryIs></Polygon>"
                           "</Placemark>" % (i, r["obs"], r["side"], r["inc0"], r["inc1"], r["n"], " ".join(obs_ids(r)), col, coords))
            out.append("</Document></kml>")
            with open(fn, "w", encoding="utf-8") as f:
                f.write("\n".join(out))
        self.status.set("書き出しました: " + fn)


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
