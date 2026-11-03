# -*- coding: utf-8 -*-
"""
月面の背景地図 (LROC WAC モザイク) を取得して、ヒートマップを重ねた1枚の画像にする。

- 画像の出どころ: NASA Moon Trek のタイル (WMTS, 緯度経度が等間隔の地図, 経度 -180〜180, 北が上)
- タイルは tile_cache/ に保存するので、2回目からはネットを使わない
- Pillow (pip install pillow) が入っていないと使えない。無い場合 OK=False になり、呼び出し側は背景なしで動く
"""
import os, math, threading, concurrent.futures, urllib.request

try:
    from PIL import Image, ImageDraw, ImageTk
    OK = True
except Exception:  # noqa
    OK = False

URL = "https://trek.nasa.gov/tiles/Moon/EQ/LRO_WAC_Mosaic_Global_303ppd_v02/1.0.0/default/default028mm/%d/%d/%d.jpg"
DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tile_cache")
MAXZ = 8          # 提供されている最大の細かさ
MAX_TILES = 48    # 1回の描画で使うタイル数の上限 (多いほど細かいが重い)
TS = 256

_mem = {}
_failed = set()   # 取得に失敗したタイル。同じものを何度も取りに行かない
_lock = threading.Lock()


def _path(k):
    return os.path.join(DIR, "%d_%d_%d.jpg" % k)


def _tile(k):
    """保存済みのタイルを返す。無ければ None。"""
    im = _mem.get(k)
    if im is not None:
        return im
    p = _path(k)
    if not os.path.exists(p):
        return None
    try:
        im = Image.open(p).convert("RGB")
        im.load()
    except Exception:  # noqa  壊れた保存は消して取り直す
        try:
            os.remove(p)
        except OSError:
            pass
        return None
    with _lock:
        if len(_mem) > 400:
            _mem.clear()
        _mem[k] = im
    return im


def download(keys, workers=8):
    """タイルをまとめて取得して保存する。失敗した数を返す。"""
    os.makedirs(DIR, exist_ok=True)

    def one(k):
        try:
            req = urllib.request.Request(URL % k, headers={"User-Agent": "lroc-nac-finder/2.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
            tmp = _path(k) + ".part"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, _path(k))
            return 0
        except Exception:  # noqa
            _failed.add(k)
            return 1
    with concurrent.futures.ThreadPoolExecutor(workers) as ex:
        return sum(ex.map(one, keys))


def _plan(view, w, h):
    """表示範囲と画面の大きさから、使うタイルの細かさ(z)と範囲を決める。"""
    x0, x1, y0, y1 = view
    need = max(w / (x1 - x0), h / (y1 - y0))          # 画面1度あたりの画素数
    z = 0
    while z < MAXZ and TS * 2 ** (z + 1) / 360.0 < need:
        z += 1
    while True:
        n = 2 ** (z + 1)
        step = 360.0 / n
        u0, u1 = math.floor(x0 / step), math.ceil(x1 / step)
        r0, r1 = max(0, math.floor((90 - y1) / step)), min(n // 2, math.ceil((90 - y0) / step))
        if (u1 - u0) * max(1, r1 - r0) <= MAX_TILES or z == 0:
            return z, n, step, u0, u1, r0, max(r1, r0 + 1)
        z -= 1


def base(view, w, h):
    """表示範囲の月面画像 (w×h) を作る。戻り値: (画像, 足りないタイルのリスト)。
    足りないタイルは灰色のまま。download() で取ってから、もう一度呼ぶ。"""
    x0, x1, y0, y1 = view
    z, n, step, u0, u1, r0, r1 = _plan(view, w, h)
    mos = Image.new("RGB", ((u1 - u0) * TS, (r1 - r0) * TS), (60, 60, 60))
    missing = []
    for u in range(u0, u1):
        c = (u + n // 2) % n          # 東経0〜360 の列番号 → タイルの列番号 (経度-180始まり)
        for r in range(r0, r1):
            k = (z, r, c)
            if k in _failed:
                continue
            t = _tile(k)
            if t is None:
                missing.append(k)
                continue
            if t.size != (TS, TS):
                t = t.resize((TS, TS))
            mos.paste(t, ((u - u0) * TS, (r - r0) * TS))
    box = ((x0 - u0 * step) / step * TS, (90 - y1 - r0 * step) / step * TS,
           (x1 - u0 * step) / step * TS, (90 - y0 - r0 * step) / step * TS)
    box = (max(0, box[0]), max(0, box[1]), min(mos.width, box[2]), min(mos.height, box[3]))
    return mos.resize((w, h), Image.BILINEAR, box=box), missing


def with_heat(img, view, cells, ramp, alpha):
    """月面画像の上に、撮影回数のヒートマップを半透明で重ねる。cells は analyse() の行(1セル1件)。"""
    if not cells:
        return img
    x0, x1, y0, y1 = view
    W, H = img.size
    mn = min(r["obs"] for r in cells)
    mx = max(r["obs"] for r in cells)
    ov = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    a = int(255 * max(0.0, min(1.0, alpha)))
    for r in cells:
        g = r["g"]
        lo0, lo1, la0, la1 = r["ix"] * g, (r["ix"] + 1) * g, r["iy"] * g, (r["iy"] + 1) * g
        if lo1 < x0 or lo0 > x1 or la1 < y0 or la0 > y1:
            continue
        xa, xb = (lo0 - x0) / (x1 - x0) * W, (lo1 - x0) / (x1 - x0) * W
        ya, yb = (y1 - la1) / (y1 - y0) * H, (y1 - la0) / (y1 - y0) * H
        t = (r["obs"] - mn) / (mx - mn) if mx > mn else 1.0
        d.rectangle([xa, ya, max(xb - 1, xa + 1), max(yb - 1, ya + 1)], fill=ramp(t) + (a,))
    return Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB")


def to_photo(img):
    return ImageTk.PhotoImage(img)
