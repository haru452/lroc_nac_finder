# -*- coding: utf-8 -*-
"""地図(Canvas)の描画と操作(拡大・移動・セルの選択)。App に混ぜて使う。"""
import threading, math
import webbrowser
import moon_basemap as bp   # 月面の背景地図 (Pillow が無ければ背景なしで動く)
import place_names as pn   # 地名(静かの海など)
from nac_data import quickmap_url, rng, ramp, hexcol, nice_step


class MapView:
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
                           text="ここに地図が出ます(東が右・北が上)。条件を入れて[検索]を押してください。\n操作: ホイール=拡大縮小 / ホイール押しながら or 右ドラッグ=移動 / 右クリック=全体表示 / 左クリック=セル選択 / セルの中をクリック=点を打つ / セルの中を左ドラッグ=四角で囲む(そこを写す画像だけに絞る)")
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
        self.draw_points()
        self.draw_mode()
        cv.create_text(4, 4, anchor="nw", text="緯度", font=("", 9), fill=self.th["axis"])
        cv.create_text(L, H - 2, anchor="sw", text="東経(°)  →東(右)", font=("", 9), fill=self.th["axis"])
        if getattr(self, "searching", False):   # 検索中は地図の中央に表示
            tid = cv.create_text((L + R) / 2, (T + B) / 2, text="検索中…", font=("", 20, "bold"), fill=self.th["fg"])
            bb = cv.bbox(tid)
            if bb:
                bg = cv.create_rectangle(bb[0] - 16, bb[1] - 8, bb[2] + 16, bb[3] + 8,
                                         fill=self.th["label_bg"], outline=self.th["outline"])
                cv.tag_lower(bg, tid)

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
        img, missing, ahead = bp.base(self.view, w, h)
        if (missing or ahead) and (missing, ahead) != self.bg_asked:
            # 中心に近いタイルから裏で取得し、1枚届くごとに描き直す(届くまでは粗いタイルで仮表示)
            self.bg_asked = (missing, ahead)
            bp.request(missing + ahead, lambda ok: self.q.put(("tile", ok)))
        img = bp.with_heat(img, self.view, list(self.cellmap.values()), ramp, self.alpha.get())
        photo = bp.to_photo(img)
        if not missing:
            self.bg_cache = (key, photo)
        self.bg_last = photo    # 画像を持ち続けないと画面から消える(Tkの仕様)
        return photo

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
        if not e.state & 4:
            self.ctrl_down = False
        if self.peek:   # 重なり表示モード: 点・四角は使わず、離したときに画像の情報を出す
            self.drag = self.pdrag = None
            self.peek_press = (e.x, e.y)
            return
        self.added_now = False
        self.pdrag = self.point_at(e.x, e.y)   # 点の上を押したら、その点を動かす
        self.drag = None if self.pdrag is not None else (e.x, e.y)

    def on_drag(self, e):
        if self.pdrag is not None:
            self.move_point(e.x, e.y)
        elif self.drag:
            self.cv.delete("rb")
            self.cv.create_rectangle(self.drag[0], self.drag[1], e.x, e.y, outline=self.th["sel"], dash=(3, 2), tags="rb")

    def on_release(self, e):
        if self.peek:
            p, self.peek_press = getattr(self, "peek_press", None), None
            if p and abs(e.x - p[0]) <= 8 and abs(e.y - p[1]) <= 8:   # ドラッグしたときは何もしない
                self.peek_click(e.x, e.y, bool(e.state & 1))
            return
        if self.pdrag is not None:
            self.pdrag = None
            return
        if not self.drag or not self.view:
            return
        x0, y0 = self.drag
        self.drag = None
        self.cv.delete("rb")
        if abs(e.x - x0) > 8 and abs(e.y - y0) > 8:
            # 四角で囲む: その四角を全部写している画像だけに絞る。四角は押し始めたセルの中に収める
            (lo0, la0), (lo1, la1) = self.inv(x0, y0), self.inv(e.x, e.y)
            cur = self.sel_row()
            if cur is None or not self.in_row(cur, la0, lo0):
                cur = self.cell_at(x0, y0)
                if cur is None:
                    self.status.set("四角は、色の付いたセルの中から囲み始めてください"); return
                self.select(self.rows.index(cur))
            self.set_rect(cur, lo0, la0, lo1, la1)
        else:
            lon, lat = self.inv(e.x, e.y)
            cur = self.sel_row()
            if cur is not None and self.in_row(cur, lat, lon):   # 選んだセルの中のクリックは点を打つ
                self.add_point(cur, lat, lon)
                self.added_now = True
                return
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
        if self.added_now:   # 1回目のクリックで打った(移した)点は元に戻す
            self.added_now = False
            self.restore_point()
        self.select(self.rows.index(r))
        self.copy_cell()
        webbrowser.open(quickmap_url(r))
        self.status.set("QuickMapを開きました(そのセルの中心に点を置いて表示)。ずれる場合は、コピー済みの座標を座標移動欄に貼り付けてください")

    def on_move(self, e):
        if not e.state & 4:   # Ctrlを離したイベントを取りこぼしても、次のCtrlで切り替えられるようにする
            self.ctrl_down = False
        if not self.view or self.drag:
            return
        self.cv.configure(cursor="fleur" if not self.peek and self.point_at(e.x, e.y) is not None else "")   # 点の上では移動用の矢印
        lon, lat = self.inv(e.x, e.y)
        r = self.cell_at(e.x, e.y)
        s = "緯度 %.2f° 東経 %.2f°" % (lat, lon)
        if r:
            s += "  |  撮影回数 %d / %d枚 / 入射角 %s / エミッション角 %s / 太陽 %s" % (
                r["obs"], r["n"], rng(r["inc0"], r["inc1"]), rng(r["emi0"], r["emi1"]), r["side"])
        self.hover.set(s)
