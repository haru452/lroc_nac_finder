# -*- coding: utf-8 -*-
"""NAC QuickMap アプリ(nac-quickmap-app)との連携。
そのアプリの lroc_data.db を読み取り専用で開き、登録された画像の中心にピンを立ててタグ名を出す。App に混ぜて使う。"""
import os, math, sqlite3
from pathlib import Path
import tkinter as tk
from tkinter import filedialog

HERE = os.path.dirname(os.path.abspath(__file__))
# 既定の場所: class/es/lroc_nac_finder から見て class/nac-quickmap-app/lroc_data.db
DEFAULT_DB = os.path.normpath(os.path.join(HERE, "..", "..", "nac-quickmap-app", "lroc_data.db"))
NO_TAG = "#9a9a9a"   # タグなしの画像のピンの色
GROUP_PX = 40        # 画面上でこの距離より近いピンは、まとめて1つのラベルにする


def load_quickmap(path):
    """[{id, lat, lon, tags:[(名前, 色), ...]}] を返す。QuickMap アプリのデータは書き換えない。"""
    con = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        tagmap = {}
        for pid, name, col in con.execute(
                "SELECT pt.product_id, t.name, t.color FROM product_tags pt JOIN tags t ON t.id = pt.tag_id ORDER BY t.name"):
            tagmap.setdefault(pid, []).append((name, col or NO_TAG))
        out = []
        for pid, lat, lon in con.execute(
                "SELECT product_id, center_latitude, center_longitude FROM lroc_products"
                " WHERE center_latitude IS NOT NULL AND center_longitude IS NOT NULL"):
            out.append({"id": pid, "lat": lat, "lon": lon % 360.0, "tags": tagmap.get(pid, [])})
        return out
    finally:
        con.close()


class QuickmapLink:
    def init_quickmap(self):
        self.qm_db = tk.StringVar(value=DEFAULT_DB)   # 設定ファイルに保存する
        self.qm_pins = None                           # None = つないでいない

    def qm_toggle(self):
        """ボタン: QuickMap アプリにつなげる(ピンを出す) / 外す。"""
        if self.qm_pins is not None:
            self.qm_pins = None
            self.qm_btn.configure(text="QuickMapアプリにつなげる")
            self.status.set("QuickMapアプリのピンを消しました")
            self.draw()
            return
        path = self.qm_db.get()
        if not os.path.isfile(path):
            path = filedialog.askopenfilename(
                title="NAC QuickMap アプリの lroc_data.db を選んでください",
                filetypes=[("SQLite", "*.db"), ("すべて", "*.*")])
            if not path:
                return
            self.qm_db.set(path)
        try:
            pins = load_quickmap(path)
        except sqlite3.Error as e:
            self.status.set("QuickMapアプリのデータを読めませんでした: %s" % e)
            return
        self.qm_pins = pins
        self.qm_btn.configure(text="QuickMapアプリから外す")
        if not self.view and pins:   # まだ検索していなければ、月全体を範囲にしてピンの周りを見せる
            self.full = (0.0, 360.0, -90.0, 90.0)
            los, las = [p["lon"] for p in pins], [p["lat"] for p in pins]
            self.set_view(min(los) - 2, max(los) + 2, min(las) - 2, max(las) + 2)
        msg = "QuickMapアプリにつなげました: 画像 %d 枚のピンを表示" % len(pins)
        if self.full:
            fx0, fx1, fy0, fy1 = self.full
            out = sum(not (fx0 <= p["lon"] <= fx1 and fy0 <= p["lat"] <= fy1) for p in pins)
            if out:
                msg += "(うち %d 枚は検索した範囲の外なので見えません)" % out
        self.status.set(msg)
        self.draw()

    def qm_visible(self):
        """画面に入るピンを (x, y, ピン) で返す。"""
        if not self.qm_pins or not self.view:
            return []
        L, T, R, B = self.plot_rect()
        out = []
        for p in self.qm_pins:
            x, y = self.tx(p["lon"]), self.ty(p["lat"])
            if L <= x <= R and T <= y <= B:
                out.append((x, y, p))
        return out

    def draw_quickmap(self):
        """ピンを立て、近くのピンをまとめてタグ名のラベルを1つ出す。"""
        vis = self.qm_visible()
        if not vis:
            return
        cv = self.cv
        groups = []   # [[x合計, y合計, 個数, {タグ名: 色}]]
        for x, y, p in vis:
            col = p["tags"][0][1] if p["tags"] else NO_TAG
            cv.create_line(x, y, x, y - 9, fill="#ffffff", width=2)
            cv.create_oval(x - 5, y - 17, x + 5, y - 7, fill=col, outline="#ffffff", width=1.5)
            for gr in groups:
                if math.hypot(gr[0] / gr[2] - x, gr[1] / gr[2] - y) < GROUP_PX:
                    break
            else:
                gr = [0.0, 0.0, 0, {}]
                groups.append(gr)
            gr[0] += x; gr[1] += y; gr[2] += 1
            for name, c in p["tags"]:
                gr[3].setdefault(name, c)
        L, T, R, B = self.plot_rect()
        for gx, gy, n, tags in groups:
            px, y = gx / n, gy / n - 12
            names = list(tags.items()) or [("タグなし", NO_TAG)]
            y = max(T + 8, min(y, B - 8 - (len(names) - 1) * 16))   # 地図の上下からはみ出さない
            items = []
            for i, (name, c) in enumerate(names):
                yy = y + i * 16
                tid = cv.create_text(px + 22, yy, anchor="w", text=name, font=("", 10, "bold"), fill=self.th["fg"])
                bb = cv.bbox(tid)
                bg = cv.create_rectangle(px + 10, bb[1] - 1, bb[2] + 3, bb[3] + 1, fill=self.th["label_bg"], outline=c)
                sq = cv.create_rectangle(px + 13, yy - 4, px + 19, yy + 4, fill=c, outline="")
                cv.tag_lower(bg, tid)
                items.append((bb[2] + 3, (tid, bg, sq)))
            right = max(e for e, _ in items)
            if right > R:   # 右端で切れるときは、ピンの左側に出す
                for _, ids in items:
                    for k in ids:
                        cv.move(k, px - 10 - right, 0)

    def qm_hover(self, px, py):
        """マウスの近くのピンの説明(無ければ空文字)。"""
        best = None
        for x, y, p in self.qm_visible():
            d = math.hypot(x - px, y - 12 - py)
            if d < 10 and (best is None or d < best[0]):
                best = (d, p)
        if not best:
            return ""
        p = best[1]
        tags = ", ".join(n for n, _ in p["tags"]) or "タグなし"
        return "  |  QuickMapアプリ: %s (%s)" % (p["id"], tags)
