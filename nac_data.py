# -*- coding: utf-8 -*-
"""データの取得(ODE REST)・解釈・集計・ローカル保存と、ヒートマップの色。画面(tkinter)には依存しない。"""
import csv, json, math, time, collections, os, sqlite3
import urllib.request, urllib.parse, io, hashlib, base64


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
def n_obs(items):
    return len({i["id"][:-2] for i in items})


def best_group(items, dinc, demi):
    """画像ごとに、その画像から入射角の差が dinc 以内・エミッション角の差が demi 以内にある画像を集め、
    撮影回数が最も多い組を返す。エミッション角が不明な画像は、エミッション角の条件を問わない。"""
    best, best_key, eps = [], (-1, -1), 1e-9
    for a in items:
        g = [i for i in items if abs(i["inc"] - a["inc"]) <= dinc + eps
             and (a["emi"] is None or i["emi"] is None or abs(i["emi"] - a["emi"]) <= demi + eps)]
        key = (n_obs(g), len(g))
        if key > best_key:
            best, best_key = g, key
    return best


def span(vals):
    vals = [v for v in vals if v is not None]
    return (min(vals), max(vals)) if vals else (None, None)


def analyse(recs, c):
    g = c["grid"]
    cells = collections.defaultdict(list)
    for r in recs:
        if c["side"] != "両方" and not r["side"].startswith(c["side"][0]):
            continue
        cells[(math.floor(r["lat"] / g), math.floor(r["lon"] / g), r["side"])].append(r)
    rows = []
    for (iy, ix, side), all_items in cells.items():
        items = best_group(all_items, c["incdiff"], c["emidiff"])
        obs = n_obs(items)
        if obs < c["minobs"]:
            continue
        inc0, inc1 = span(i["inc"] for i in items)
        emi0, emi1 = span(i["emi"] for i in items)
        rows.append(dict(iy=iy, ix=ix, g=g, lat=(iy + .5) * g, lon=(ix + .5) * g, side=side,
                         inc0=inc0, inc1=inc1, emi0=emi0, emi1=emi1, obs=obs, n=len(items),
                         total=len(all_items), items=items))
    rows.sort(key=lambda r: (-r["obs"], -r["n"]))
    return rows


def rng(a, b):
    """角度の範囲を '52.1〜54.8°' の形にする。"""
    return "-" if a is None else "%.1f〜%.1f°" % (a, b)


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
