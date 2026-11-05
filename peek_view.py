# -*- coding: utf-8 -*-
"""重なり表示モード(Shiftで切り替え)。選んだセルの画像の範囲を1枚ずつ色を変えて重ね、
クリックした場所に写っている画像の詳しい情報を出す。App に混ぜて使う。"""
import time
from nac_data import covers, unwrap
from point_filter import FOOT_COLS, clip

# 画像1枚ごとに違う色にするので、点・四角のときより多めに用意する
PEEK_COLS = FOOT_COLS + ["#ffffff", "#c6ff00", "#f48fb1", "#1de9b6", "#ffab40", "#bcaaa4"]
PEEK_ROWS = 18     # 凡例に並べる画像の数の上限
INFO_MAX = 6       # 詳しい情報を出す画像の数の上限
TOAST_SEC = 1.3    # 切り替えたときに、地図の中央にモード名を大きく出す時間

MODE_NAMES = {False: "通常モード", True: "重なり表示モード"}
HINTS = {
    False: ("地図の操作【通常モード】:  ホイール または ＋－キー=拡大・縮小  /  矢印キー=移動  /  Home=全体表示  /  "
            "ホイール押しながら または 右ドラッグ=移動  /  右クリック=全体表示  /  左クリック=セルを選ぶ  /  "
            "選んだセルの中をクリック=点を打つ(点が写っている画像だけに絞る。点は1つで、クリックやドラッグで動かせる)  /  "
            "セルの中を左ドラッグ=四角で囲む(四角全体を写している画像だけに絞る)  /  Delete=点・四角を消す  /  "
            "Shift=重なり表示モードへ  /  ダブルクリック=QuickMapでその場所を開く  /  順位表の行=その場所へ"),
    True: ("地図の操作【重なり表示モード】:  選んだセルの画像を1枚ずつ色を変えて重ねて表示中  /  "
           "左クリック=その場所に写っている画像の詳しい情報を出す(画像の無い場所をクリックすると、そこのセルを選ぶ)  /  "
           "点を打つ・四角で囲む操作は、このモードでは使えません  /  ホイール または ＋－キー=拡大・縮小  /  矢印キー・右ドラッグ=移動  /  "
           "右クリック=全体表示  /  ダブルクリック=QuickMapでその場所を開く  /  順位表の行=そのセルへ  /  Shift=通常モードへ戻る"),
}


class PeekView:
    def init_peek(self):
        self.peek = False          # 重なり表示モードか
        self.shift_down = False    # Shiftを押しっぱなしか(押しっぱなしの連続入力で何度も切り替えないため)
        self.peek_hits = []        # クリックした場所に写っている画像のID
        self.peek_at = None        # クリックした場所 (緯度, 経度)
        self.toast_until = 0.0
        self.set_mode_hint()

    # ---- 切り替え
    def peek_toggle(self, _e=None):
        """Shiftを押すたびに、通常モードと重なり表示モードを切り替える。"""
        if self.shift_down:   # 押しっぱなしの連続入力は無視
            return
        self.shift_down = True
        if not self.peek and self.sel_row() is None:
            self.status.set("先にセルを選んでください(選んだあとShiftで、重なり表示モードになります)")
            return
        self.peek = not self.peek
        self.peek_hits, self.peek_at = [], None
        self.drag = self.pdrag = None
        self.cv.delete("rb")
        if self.peek:
            self.load_feet(self.sel_row())
        self.status.set("%sに切り替えました(Shiftで%sへ)" % (MODE_NAMES[self.peek], MODE_NAMES[not self.peek]))
        self.set_mode_hint()
        self.toast_until = time.time() + TOAST_SEC
        self.root.after(int(TOAST_SEC * 1000) + 50, self.draw)   # 時間が来たらモード名を消す
        self.draw()

    def shift_up(self, _e=None):
        self.shift_down = False

    def set_mode_hint(self):
        if hasattr(self, "hint_var"):
            self.hint_var.set(HINTS[self.peek])

    # ---- クリックした場所の画像
    def peek_click(self, px, py):
        """クリックした場所に写っている画像を探して、詳しい情報を出す。無ければ、そこのセルを選ぶ。"""
        r = self.sel_row()
        if r is None or not self.view:
            return
        lon, lat = self.inv(px, py)
        hits = [i["id"] for i in sorted(self.point_items(r), key=lambda i: i["id"])
                if i["id"] in self.foot and covers(self.foot[i["id"]], lat, lon)]
        if hits:
            self.peek_hits, self.peek_at = hits, (lat, lon)
            self.status.set("緯度%.4f° 東経%.4f° に写っている画像: %d枚" % (lat, lon, len(hits)))
            self.draw()
            return
        self.peek_hits, self.peek_at = [], None
        other = self.cell_at(px, py)
        if other is not None and other is not r:
            self.select(self.rows.index(other))
            self.status.set("このセルの重なりを表示しています(%sのまま)" % MODE_NAMES[True])
        else:
            self.status.set("そこに写っている画像はありません")
            self.draw()

    # ---- 描画
    def draw_peek(self, r):
        """セルの画像(点・四角で絞っていればその画像)の範囲を、画像ごとの色で描き、左下に凡例を出す。
        クリックした場所の画像は太い線で描き、右上に詳しい情報を出す。"""
        if (any(i["id"] not in self.foot for i in r["items"])
                and (r["iy"], r["ix"], r["g"]) not in self.foot_tried):
            self.load_feet(r)   # 表示中に別のセルを選んだとき、そのセルの範囲を用意する
        cv = self.cv
        L, T, R, B = self.plot_rect()
        clon = (r["ix"] + 0.5) * r["g"]
        items = sorted(self.point_items(r), key=lambda i: i["id"])
        hits = [h for h in self.peek_hits if any(i["id"] == h for i in items)]
        cols, rows = {}, []
        for n, i in enumerate(items):
            col = cols[i["id"]] = PEEK_COLS[n % len(PEEK_COLS)]
            wide = i["id"] in hits
            for ring in self.foot.get(i["id"], []):
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
            info = [(None, "クリックした場所に写っている画像: %d枚" % len(hits))]
            for h in hits[:INFO_MAX]:
                info += self.image_info(by_id[h], cols[h])
            if len(hits) > INFO_MAX:
                info.append((None, "…ほか %d枚" % (len(hits) - INFO_MAX)))
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
        name = MODE_NAMES[self.peek] + ("  (Shiftで通常モードへ)" if self.peek else "  (Shiftで重なり表示モードへ)")
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
