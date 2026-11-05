# -*- coding: utf-8 -*-
"""選んだセルの中に点を1つ打つか四角で囲み、そこを写している画像だけに絞り込む。App に混ぜて使う。"""
import threading
from nac_data import db_open, db_feet, db_save_feet, fetch_footprints, parse_wkt, covers, unwrap

# 画像の範囲の線の色。撮影ごとに順に使う(左右カメラ LE/RE は同じ撮影なので同じ色)。
# 月面の写真の上でも見えるよう、明るくはっきりした色にする
FOOT_COLS = ["#40c4ff", "#ff5252", "#ffd740", "#69f0ae", "#e040fb",
             "#ff9100", "#b2ff59", "#18ffff", "#ff4081", "#7c4dff"]


def clip(poly, L, T, R, B):
    """多角形を地図の四角の中だけに切り取る(はみ出して余白に線が出ないように)。"""
    for axis, lim, keep_ge in ((0, L, True), (0, R, False), (1, T, True), (1, B, False)):
        inside = (lambda p: p[axis] >= lim) if keep_ge else (lambda p: p[axis] <= lim)
        out = []
        for k, cur in enumerate(poly):
            prev = poly[k - 1]
            if inside(cur) != inside(prev):   # 辺が境界をまたぐ: 交点を足す
                t = (lim - prev[axis]) / (cur[axis] - prev[axis])
                out.append((prev[0] + (cur[0] - prev[0]) * t, prev[1] + (cur[1] - prev[1]) * t))
            if inside(cur):
                out.append(cur)
        poly = out
        if not poly:
            break
    return poly


class PointFilter:
    def init_points(self):
        self.pts = []          # 打った点 [(緯度, 経度)]。点は1つだけ。四角で囲んだときは四隅の4点
        self.rect = None       # 囲んだ四角 (西経度, 南緯度, 東経度, 北緯度)。点のときは None
        self.pt_row = None     # 点を打ったセル(順位表の行)
        self.foot = {}         # 画像ID → 範囲の輪のリスト
        self.foot_tried = set()   # ODEに範囲を問い合わせ済みのセル
        self.foot_busy = False
        self.pdrag = None      # ドラッグで動かしている点の番号
        self.added_now = False   # 直前のクリックで点を打ったか(ダブルクリック時の取り消し用)
        self.prev_pt = ([], None, None)   # 点を打つ前の状態(ダブルクリックで元に戻す)

    def sel_row(self):
        if self.sel is not None and self.sel < len(self.rows):
            return self.rows[self.sel]
        return None

    def in_row(self, r, lat, lon):
        g = r["g"]
        return r["ix"] * g <= lon < (r["ix"] + 1) * g and r["iy"] * g <= lat < (r["iy"] + 1) * g

    def point_items(self, r):
        """セルの画像のうち、打った点(四角なら四隅すべて=四角全体)を写している画像。点が無ければセルの画像すべて。
        NACの画像の範囲は凸な四角形なので、四隅が入っていれば四角全体が入っている。"""
        if not self.pts or r is not self.pt_row:
            return r["items"]
        return [i for i in r["items"] if i["id"] in self.foot
                and all(covers(self.foot[i["id"]], la, lo) for la, lo in self.pts)]

    def add_point(self, r, lat, lon):
        """点を打つ。すでに点があれば、クリックした場所へ移す。"""
        self.prev_pt = (self.pts, self.pt_row, self.rect)
        self.pts, self.pt_row, self.rect = [(lat, lon)], r, None
        self.load_feet(r)
        self.refresh_points()

    def set_rect(self, r, lon0, lat0, lon1, lat1):
        """四角で囲む。四角はセルの中に収める。"""
        g, eps = r["g"], r["g"] * 1e-6
        lon0, lon1 = (min(max(v, r["ix"] * g), (r["ix"] + 1) * g - eps) for v in (min(lon0, lon1), max(lon0, lon1)))
        lat0, lat1 = (min(max(v, r["iy"] * g), (r["iy"] + 1) * g - eps) for v in (min(lat0, lat1), max(lat0, lat1)))
        self.prev_pt = (self.pts, self.pt_row, self.rect)
        self.rect, self.pt_row = (lon0, lat0, lon1, lat1), r
        self.pts = [(lat0, lon0), (lat0, lon1), (lat1, lon1), (lat1, lon0)]
        self.load_feet(r)
        self.refresh_points()

    def point_at(self, px, py):
        """画面の位置(px, py)の近くに点があれば 0。無ければ None。"""
        r = self.pt_row
        if not self.pts or self.rect or r is None or self.sel_row() is not r:
            return None
        la, lo = self.pts[0]
        return 0 if abs(self.tx(lo) - px) <= 8 and abs(self.ty(la) - py) <= 8 else None

    def move_point(self, px, py):
        """ドラッグ中の点を動かす。選んだセルの外には出さない。"""
        r = self.pt_row
        lon, lat = self.inv(px, py)
        g, eps = r["g"], r["g"] * 1e-6
        lon = min(max(lon, r["ix"] * g), (r["ix"] + 1) * g - eps)
        lat = min(max(lat, r["iy"] * g), (r["iy"] + 1) * g - eps)
        self.pts[self.pdrag] = (lat, lon)
        self.refresh_points()

    def restore_point(self):
        """ダブルクリックの1回目で打った点を取り消し、前の点に戻す。"""
        self.pts, self.pt_row, self.rect = self.prev_pt
        self.refresh_points()

    def clear_points(self, _e=None):
        if self.pts:
            self.pts, self.rect = [], None
            self.refresh_points()

    def load_feet(self, r):
        """セルの画像の範囲を用意する。PC内に無ければ、裏でODEから取ってくる。"""
        need = {i["id"] for i in r["items"]} - set(self.foot)
        if not need:
            return
        db = db_open()
        for k, w in db_feet(db, need).items():
            self.foot[k] = parse_wkt(w)
        db.close()
        key = (r["iy"], r["ix"], r["g"])
        if not (need - set(self.foot)) or key in self.foot_tried or self.foot_busy:
            return
        self.foot_tried.add(key)
        self.foot_busy = True
        g, ptval = r["g"], self.pt.get()

        def run():
            try:
                pt = self.get_pt(ptval)
                feet = fetch_footprints(r["iy"] * g, (r["iy"] + 1) * g, r["ix"] * g, (r["ix"] + 1) * g, pt)
                db2 = db_open()
                db_save_feet(db2, feet)
                db2.close()
                self.q.put(("feet", feet))
            except Exception as e:  # noqa
                self.foot_tried.discard(key)   # 次に点を打ったときにやり直す
                self.q.put(("feet", None))
                self.q.put(("status", "画像の範囲を取得できませんでした: %s" % e))
        threading.Thread(target=run, daemon=True).start()

    def on_feet(self, feet):
        self.foot_busy = False
        for k, w in (feet or {}).items():
            self.foot[k] = parse_wkt(w)
        if feet is not None:
            self.refresh_points()

    def refresh_points(self):
        r = self.pt_row
        if r is not None and self.sel_row() is r:
            self.show_ids(r)
            if self.pts:
                items = self.point_items(r)
                nobs = len({i["id"][:-2] for i in items})
                what = "囲んだ四角を全部写している画像" if self.rect else "点を写している画像"
                s = "%s: %d枚(撮影%d回) / セルの%d枚" % (what, len(items), nobs, len(r["items"]))
                if self.foot_busy:
                    s = "画像の範囲をODEから取得中…(初回のみ)  " + s
                else:
                    miss = sum(1 for i in r["items"] if i["id"] not in self.foot)
                    if miss:
                        s += "  ※範囲が分からず除いた画像 %d枚" % miss
                self.status.set(s + "  [クリック=点 / ドラッグ=四角で囲む / Delete=消す]")
            else:
                self.status.set("点・四角を消しました。セル内の全画像を表示しています")
        self.draw()

    def draw_points(self):
        """点と、点を写している画像の範囲を地図に描く。"""
        r = self.pt_row
        if not self.pts or r is None or self.sel_row() is not r:
            return
        cv = self.cv
        L, T, R, B = self.plot_rect()
        clon = (r["ix"] + 0.5) * r["g"]
        items = self.point_items(r)
        obs = sorted({i["id"][:-2] for i in items})   # 撮影IDの並び(古い順)で色を決める
        for i in items:
            col = FOOT_COLS[obs.index(i["id"][:-2]) % len(FOOT_COLS)]
            for ring in self.foot.get(i["id"], []):
                poly = clip([(self.tx(x), self.ty(y)) for x, y in unwrap(ring, clon)], L, T, R, B)
                if len(poly) >= 3:   # 黒い縁を下に敷いて、どの背景色の上でも線が見えるようにする
                    xy = [v for p in poly for v in p]
                    cv.create_polygon(*xy, outline="#101010", fill="", width=4)
                    cv.create_polygon(*xy, outline=col, fill="", width=2)
        if self.rect:   # 囲んだ四角: 白い線(黒い縁つき)で描き、右上に枚数を出す
            lon0, lat0, lon1, lat1 = self.rect
            box = clip([(self.tx(lon0), self.ty(lat0)), (self.tx(lon1), self.ty(lat0)),
                        (self.tx(lon1), self.ty(lat1)), (self.tx(lon0), self.ty(lat1))], L, T, R, B)
            if len(box) < 3:
                return
            xy = [v for p in box for v in p]
            cv.create_polygon(*xy, outline="#101010", fill="", width=5)
            cv.create_polygon(*xy, outline="#ffffff", fill="", width=2)
            spots = [(min(R - 60, max(p[0] for p in box)) - 9, max(T + 20, min(p[1] for p in box)) + 9)]
        else:
            spots = []
            for la, lo in self.pts:
                x, y = self.tx(lo), self.ty(la)
                if L <= x <= R and T <= y <= B:
                    cv.create_oval(x - 5, y - 5, x + 5, y + 5, fill="#ffffff", outline=self.th["sel"], width=2)
                    spots.append((x, y))
        for x, y in spots:
            # 点(四角)のそばに、そこを写している画像の枚数を出す
            text = "取得中…" if self.foot_busy else "%d枚" % len(self.point_items(r))
            tid = cv.create_text(x + 9, y - 9, anchor="sw", text=text, font=("", 11, "bold"), fill=self.th["sel_text"])
            bb = cv.bbox(tid)
            if bb:
                bg = cv.create_rectangle(bb[0] - 3, bb[1] - 1, bb[2] + 3, bb[3] + 1,
                                         fill=self.th["label_bg"], outline=self.th["sel"])
                cv.tag_lower(bg, tid)
