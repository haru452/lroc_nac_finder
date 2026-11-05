# -*- coding: utf-8 -*-
"""重なり表示モード(Ctrlで切り替え)。選んだセルの画像の範囲を1枚ずつ色を変えて重ね、
クリックした場所に写っている画像の詳しい情報を出す。App に混ぜて使う。"""
import time
import webbrowser
from nac_data import covers, unwrap, ramp, hexcol
from point_filter import FOOT_COLS, clip

# 画像1枚ごとに違う色にするので、点・四角のときより多めに用意する
PEEK_COLS = FOOT_COLS + ["#ffffff", "#c6ff00", "#f48fb1", "#1de9b6", "#ffab40", "#bcaaa4"]
PEEK_ROWS = 18     # 凡例に並べる画像の数の上限
INFO_MAX = 6       # 詳しい情報を出す画像の数の上限
OPEN_MAX = 6       # 一度にブラウザで開く画像の数の上限
LROC_URL = "https://wms.lroc.asu.edu/lroc/view_lroc/LRO-L-LROC-2-EDR-V1.0/%s"   # LROCの画像ページ(ダウンロードもここから)
HEAT_N = 48        # ヒートマップの細かさ(セルを縦横この数に分ける)
TOAST_SEC = 1.3    # 切り替えたときに、地図の中央にモード名を大きく出す時間

MODE_NAMES = {False: "通常モード", True: "重なり表示モード"}
HINTS = {
    False: ("地図の操作【通常モード】:  ホイール または ＋－キー=拡大・縮小  /  矢印キー=移動  /  Home=全体表示  /  "
            "ホイール押しながら または 右ドラッグ=移動  /  右クリック=全体表示  /  左クリック=セルを選ぶ  /  "
            "選んだセルの中をクリック=点を打つ(点が写っている画像だけに絞る。点は1つで、クリックやドラッグで動かせる)  /  "
            "セルの中を左ドラッグ=四角で囲む(四角全体を写している画像だけに絞る)  /  Delete=点・四角を消す  /  "
            "2キー=ヒートマップ(点・四角は一旦外れ、もう一度2キーで戻る)  /  Ctrl=重なり表示モードへ  /  ダブルクリック=QuickMapでその場所を開く  /  順位表の行=その場所へ"),
    True: ("地図の操作【重なり表示モード】:  選んだセルの画像を1枚ずつ色を変えて重ねて表示中  /  "
           "左クリック=その場所に写っている画像の詳しい情報を出す(画像の無い場所をクリックすると、そこのセルを選ぶ)  /  Shift+クリック=画像を追加で選ぶ(複数選択)  /  1キー=選んだ画像の入手ページを開く  /  2キー=ヒートマップ(どこがよく写っているか)の表示・非表示  /  "
           "点を打つ・四角で囲む操作は、このモードでは使えません  /  ホイール または ＋－キー=拡大・縮小  /  矢印キー・右ドラッグ=移動  /  "
           "右クリック=全体表示  /  ダブルクリック=QuickMapでその場所を開く  /  順位表の行=そのセルへ  /  Ctrl=通常モードへ戻る"),
}


class PeekView:
    def init_peek(self):
        self.peek = False          # 重なり表示モードか
        self.ctrl_down = False    # Ctrlを押しっぱなしか(押しっぱなしの連続入力で何度も切り替えないため)
        self.peek_hits = []        # クリックした場所に写っている画像のID
        self.peek_at = None        # クリックした場所 (緯度, 経度)
        self.heat = False          # ヒートマップを出しているか
        self.heat_saved = None     # ヒートマップのために外している点・四角 (pts, pt_row, rect)
        self.heat_cache = {}       # (セル, 画像) → 各区画を写している枚数
        self.toast_until = 0.0
        self.set_mode_hint()

    # ---- 切り替え
    def peek_toggle(self, _e=None):
        """Ctrlを押すたびに、通常モードと重なり表示モードを切り替える。"""
        if self.ctrl_down:   # 押しっぱなしの連続入力は無視
            return
        self.ctrl_down = True
        if self.heat:
            self.heat_off()
        self.peek = not self.peek
        self.peek_hits, self.peek_at = [], None
        self.drag = self.pdrag = None
        self.cv.delete("rb")
        if self.peek and self.sel_row() is not None:
            self.load_feet(self.sel_row())
        self.status.set("%sに切り替えました(Ctrlで%sへ)" % (MODE_NAMES[self.peek], MODE_NAMES[not self.peek]))
        self.set_mode_hint()
        self.toast_until = time.time() + TOAST_SEC
        self.root.after(int(TOAST_SEC * 1000) + 50, self.draw)   # 時間が来たらモード名を消す
        self.draw()

    def ctrl_up(self, _e=None):
        self.ctrl_down = False

    def set_mode_hint(self):
        if hasattr(self, "hint_var"):
            self.hint_var.set(HINTS[self.peek])

    # ---- クリックした場所の画像
    def peek_click(self, px, py, add=False):
        """クリックした場所に写っている画像を探して、詳しい情報を出す。add(Shift+クリック)なら、
        すでに選んだ画像に加える(全部選択済みの場所なら外す)。画像が無ければ、そこのセルを選ぶ。"""
        if not self.view:
            return
        r = self.sel_row()
        if r is None:   # まだセルを選んでいないときは、クリックしたセルを選ぶ
            other = self.cell_at(px, py)
            if other is None:
                self.status.set("色の付いたセルをクリックすると、そのセルの重なりを表示します")
                return
            self.select(self.rows.index(other))
            self.status.set("このセルの重なりを表示しています(%sのまま)" % MODE_NAMES[True])
            return
        lon, lat = self.inv(px, py)
        hits = [i["id"] for i in sorted(r["items"], key=lambda i: i["id"])
                if i["id"] in self.foot and covers(self.foot[i["id"]], lat, lon)]
        if hits:
            if add:
                if all(h in self.peek_hits for h in hits):
                    self.peek_hits = [h for h in self.peek_hits if h not in hits]
                else:
                    self.peek_hits = self.peek_hits + [h for h in hits if h not in self.peek_hits]
            else:
                self.peek_hits = hits
            self.peek_at = (lat, lon) if self.peek_hits else None
            self.status.set("緯度%.4f° 東経%.4f° に写っている画像: %d枚 / 選択中の画像: %d枚(Shift+クリックで追加、1キーで入手ページを開く)"
                            % (lat, lon, len(hits), len(self.peek_hits)))
            self.draw()
            return
        if add:
            self.status.set("そこに写っている画像はありません(選択はそのままです)")
            return
        self.peek_hits, self.peek_at = [], None
        other = self.cell_at(px, py)
        if other is not None and other is not r:
            self.select(self.rows.index(other))
            self.status.set("このセルの重なりを表示しています(%sのまま)" % MODE_NAMES[True])
        else:
            self.status.set("そこに写っている画像はありません")
            self.draw()

    def heat_toggle(self, _e=None):
        """2キー: セルの中で、どこが何枚の画像に写っているかのヒートマップを出す・消す。
        点・四角を置いていれば、いったん外して(覚えておいて)ヒートマップを出し、もう一度押すと元に戻す。
        重なり表示モードで画像を選んでいるときは、選んだ画像だけで数える。"""
        if self.heat:
            self.heat_off()
            self.status.set("ヒートマップを消しました" + ("(点・四角を元に戻しました)" if self.pts else ""))
            return
        if self.sel_row() is None:
            self.status.set("先にセルを選んでください(2キーで、そのセルのヒートマップを見られます)")
            return
        had = bool(self.pts)
        if had:
            self.heat_saved = (self.pts, self.pt_row, self.rect)
            self.pts, self.rect = [], None
        self.heat = True
        self.refresh_points()
        self.status.set("ヒートマップを表示しました(色が明るいほど、多くの画像に写っている場所)"
                        + ("。点・四角はいったん外しています(もう一度2キーで戻ります)" if had else ""))

    def heat_off(self):
        """ヒートマップを消し、外していた点・四角があれば戻す。"""
        self.heat = False
        saved, self.heat_saved = self.heat_saved, None
        if saved:
            self.pts, self.pt_row, self.rect = saved
            self.refresh_points()
        else:
            self.draw()

    def heat_counts(self, r, items):
        """セルを HEAT_N×HEAT_N に分けて、各区画の中心を写している画像の枚数を数える。"""
        ids = tuple(i["id"] for i in items)
        key = (r["iy"], r["ix"], r["g"], ids, sum(1 for h in ids if h in self.foot))
        if key in self.heat_cache:
            return self.heat_cache[key]
        g, clon = r["g"], (r["ix"] + 0.5) * r["g"]
        rings = [unwrap(ring, clon) for h in ids for ring in self.foot.get(h, [])]
        owner = [h for h in ids for _ in self.foot.get(h, [])]
        grid = []
        for a in range(HEAT_N):
            lat = (r["iy"] + (a + 0.5) / HEAT_N) * g
            row = []
            for b in range(HEAT_N):
                lon = (r["ix"] + (b + 0.5) / HEAT_N) * g
                hit = set()
                for pts, o in zip(rings, owner):
                    inside, j = False, len(pts) - 1
                    for k in range(len(pts)):
                        (xi, yi), (xj, yj) = pts[k], pts[j]
                        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
                            inside = not inside
                        j = k
                    if inside:
                        hit.add(o)
                row.append(len(hit))
            grid.append(row)
        self.heat_cache[key] = grid
        return grid

    def draw_heat_normal(self, r):
        """通常モード: 点・四角が無いとき、選んだセルのヒートマップを描く(セルの全画像で数える)。"""
        if (any(i["id"] not in self.foot for i in r["items"])
                and (r["iy"], r["ix"], r["g"]) not in self.foot_tried):
            self.load_feet(r)
        L, T, R, B = self.plot_rect()
        self.draw_heat(r, r["items"], L, T, R, B)
        if self.foot_busy:
            self.text_box(L + 8, B - 24, [(None, "画像の範囲を取得中…")], "nw")

    def draw_heat(self, r, items, L, T, R, B):
        """ヒートマップを半透明で描き、右下に色と枚数の対応を出す。"""
        grid = self.heat_counts(r, items)
        top = max((max(row) for row in grid), default=0)
        if top == 0:
            return
        g = r["g"]
        for a, row in enumerate(grid):
            ya, yb = self.ty(r["iy"] * g + (a + 1) * g / HEAT_N), self.ty(r["iy"] * g + a * g / HEAT_N)
            if yb < T or ya > B:
                continue
            for b, c in enumerate(row):
                if not c:
                    continue
                xa, xb = self.tx(r["ix"] * g + b * g / HEAT_N), self.tx(r["ix"] * g + (b + 1) * g / HEAT_N)
                if xb < L or xa > R:
                    continue
                t = (c - 1) / (top - 1) if top > 1 else 1.0
                self.cv.create_rectangle(max(L, xa), max(T, ya), min(R, xb), min(B, yb),
                                         fill=hexcol(ramp(t)), outline="", stipple="gray25")
        levels = sorted({c for row in grid for c in row if c})
        if len(levels) > 6:   # 凡例は最大6段にまびく
            levels = [levels[round(k * (len(levels) - 1) / 5)] for k in range(6)]
        lines = [(None, "ヒートマップ: 写っている枚数")] + [
            (hexcol(ramp((c - 1) / (top - 1) if top > 1 else 1.0)), "%d枚" % c) for c in levels]
        self.text_box(R - 8, B - 8 - 16 * len(lines), lines, "ne")

    def peek_open(self, _e=None):
        """クリックした場所に写っている画像の、入手ページ(LROC公式サイト)をブラウザで開く。"""
        if not self.peek:
            return
        if not self.peek_hits:
            self.status.set("先に地図をクリックして、画像を選んでください(その場所に写っている画像のページを開きます)")
            return
        ids = self.peek_hits[:OPEN_MAX]
        for h in ids:
            webbrowser.open_new_tab(LROC_URL % h.split(".")[0])
        extra = len(self.peek_hits) - len(ids)
        self.status.set("LROCの画像ページを%d枚分ブラウザで開きました" % len(ids) + ("(残り%d枚は上限のため開いていません)" % extra if extra else ""))

    # ---- 描画
    def draw_peek(self, r):
        """セルの画像すべての範囲を、画像ごとの色で描き、左下に凡例を出す。
        クリックした場所の画像は太い線で描き、右上に詳しい情報を出す。"""
        if (any(i["id"] not in self.foot for i in r["items"])
                and (r["iy"], r["ix"], r["g"]) not in self.foot_tried):
            self.load_feet(r)   # 表示中に別のセルを選んだとき、そのセルの範囲を用意する
        cv = self.cv
        L, T, R, B = self.plot_rect()
        clon = (r["ix"] + 0.5) * r["g"]
        items = sorted(r["items"], key=lambda i: i["id"])   # 点・四角を置いていても、セルの画像すべてを出す
        hits = [h for h in self.peek_hits if any(i["id"] == h for i in items)]
        cols, rows = {}, []
        if self.heat:   # 枠線の代わりに描く
            self.draw_heat(r, [i for i in items if i["id"] in hits] if hits else items, L, T, R, B)
        for n, i in enumerate(items):
            col = cols[i["id"]] = PEEK_COLS[n % len(PEEK_COLS)]
            wide = i["id"] in hits
            for ring in ([] if self.heat else self.foot.get(i["id"], [])):   # ヒートマップ中は色つきの枠線を描かない
                poly = clip([(self.tx(x), self.ty(y)) for x, y in unwrap(ring, clon)], L, T, R, B)
                if len(poly) >= 3:
                    xy = [v for p in poly for v in p]
                    cv.create_polygon(*xy, outline="#101010", fill="", width=7 if wide else 4)
                    cv.create_polygon(*xy, outline=col, fill="", width=4 if wide else 2)
            rows.append((col, "%s  入射角%.1f°" % (i["id"], i["inc"])
                         + ("  %.2fm" % i["res"] if i.get("res") is not None else "")))
        if self.foot_busy:
            head = "画像の範囲を取得中…"
        else:
            miss = sum(1 for i in items if i["id"] not in self.foot)
            head = "重なり %d枚" % len(items) + ("(範囲不明 %d枚)" % miss if miss else "")
        lines = [(None, head)] + rows[:PEEK_ROWS]
        if len(rows) > PEEK_ROWS:
            lines.append((None, "…ほか %d枚" % (len(rows) - PEEK_ROWS)))
        self.text_box(L + 8, B - 8 - 16 * len(lines), lines, "nw")
        if hits and self.peek_at:
            la, lo = self.peek_at
            x, y = self.tx(lo), self.ty(la)
            cv.create_line(x - 9, y, x + 9, y, fill="#ffffff", width=2)
            cv.create_line(x, y - 9, x, y + 9, fill="#ffffff", width=2)
            by_id = {i["id"]: i for i in items}
            info = [(None, "選択中の画像: %d枚" % len(hits))]
            for h in hits[:INFO_MAX]:
                info += self.image_info(by_id[h], cols[h])
            if len(hits) > INFO_MAX:
                info.append((None, "…ほか %d枚" % (len(hits) - INFO_MAX)))
            info.append((None, "1キー: これらの画像の入手ページを開く(最大%d枚)" % OPEN_MAX))
            self.text_box(R - 8, T + 30, info, "ne")

    def image_info(self, i, col):
        """1枚の画像の詳しい情報(凡例の行の形)。"""
        res = "%.2f m/画素" % i["res"] if i.get("res") is not None else "不明"
        emi = "%.2f°" % i["emi"] if i["emi"] is not None else "不明"
        return [(col, "%s   撮影 %s" % (i["id"], (i["time"] or "不明")[:19].replace("T", " "))),
                ("", "入射角 %.2f°  エミッション角 %s  ピクセルスケール %s" % (i["inc"], emi, res)),
                ("", "中心 緯度%.4f° 東経%.4f°  太陽の向き %s" % (i["lat"], i["lon"], i["side"]))]

    def text_box(self, x, y, lines, corner):
        """下地つきの文字の箱を描く。lines は (色見本の色, 文字)。色が None は見出し、"" は字下げした行。
        corner が "ne" なら (x, y) を右上として置く。"""
        cv = self.cv
        ids, yy = [], y
        for col, text in lines:
            if col:
                ids.append(cv.create_rectangle(x, yy + 3, x + 18, yy + 11, fill=col, outline="#101010"))
            ids.append(cv.create_text(x + (0 if col is None else 24), yy + 7, anchor="w", text=text,
                                      font=("", 9, "bold" if col != "" else ""), fill=self.th["fg"]))
            yy += 16
        bb = cv.bbox(*ids)
        if not bb:
            return
        if corner == "ne":   # 右端を x にそろえる
            for k in ids:
                cv.move(k, x - bb[2] - 6, 0)
            bb = cv.bbox(*ids)
        bg = cv.create_rectangle(bb[0] - 4, bb[1] - 3, bb[2] + 6, bb[3] + 3,
                                 fill=self.th["legend_bg"], outline=self.th["legend_edge"])
        cv.tag_lower(bg, ids[0])

    def draw_mode(self):
        """地図の左上に今のモード名を出す。切り替えた直後は、地図の中央にも大きく出す。"""
        if not self.view:
            return
        cv = self.cv
        L, T, R, B = self.plot_rect()
        name = MODE_NAMES[self.peek] + ("  (Ctrlで通常モードへ)" if self.peek else "  (Ctrlで重なり表示モードへ)")
        tid = cv.create_text(L + 8, T + 8, anchor="nw", text=name, font=("", 10, "bold"),
                             fill="#ffd740" if self.peek else self.th["fg"])
        bb = cv.bbox(tid)
        if bb:
            bg = cv.create_rectangle(bb[0] - 4, bb[1] - 2, bb[2] + 4, bb[3] + 2, fill=self.th["legend_bg"],
                                     outline="#ffd740" if self.peek else self.th["legend_edge"])
            cv.tag_lower(bg, tid)
        if time.time() < self.toast_until:
            tid = cv.create_text((L + R) / 2, (T + B) / 2, text=MODE_NAMES[self.peek], font=("", 22, "bold"),
                                 fill="#ffd740" if self.peek else self.th["fg"])
            bb = cv.bbox(tid)
            if bb:
                bg = cv.create_rectangle(bb[0] - 18, bb[1] - 8, bb[2] + 18, bb[3] + 8,
                                         fill=self.th["label_bg"], outline="#ffd740" if self.peek else self.th["outline"])
                cv.tag_lower(bg, tid)
