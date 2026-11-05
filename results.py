# -*- coding: utf-8 -*-
"""選んだセルの撮影ID表示・画像ページを開く・CSV保存・GeoJSON/KML書き出し。App に混ぜて使う。"""
from tkinter import filedialog, messagebox
import json, csv
import webbrowser
from nac_data import rng, product_url, obs_ids, ramp, hexcol


def item_ids(items):
    return sorted({i["id"][:-2] for i in items})


class ResultActions:
    def copy_ids(self):
        r = self.cur()
        if r:
            ids = item_ids(self.point_items(r))
            self.root.clipboard_clear(); self.root.clipboard_append(" ".join(ids))
            self.status.set("撮影IDをコピーしました(%d件)" % len(ids))

    def open_all_products(self):
        """選んだセルにある撮影IDすべての画像ページを、ブラウザのタブで順に開く。
        左右カメラ(LE/RE)のうち、セルの中にある方のページを開く。"""
        r = self.cur()
        if not r:
            return
        pages = {}
        for i in self.point_items(r):   # 点を打っていれば、その点を写す画像だけ
            pages.setdefault(i["id"][:-2], i["id"])     # 撮影IDごとに1ページ
        urls = [product_url(pages[k]) for k in sorted(pages)]
        if len(urls) > 8 and not messagebox.askokcancel("確認", "%d個のページを開きます。よろしいですか?" % len(urls)):
            return
        for n, u in enumerate(urls):
            self.root.after(n * 400, lambda u=u: webbrowser.open_new_tab(u))   # 少し間をあけて開く
        self.status.set("%d個の画像ページを開きます(EDR/CDRのダウンロードリンクがあります)" % len(urls))

    def show_ids(self, r):
        ids = item_ids(self.point_items(r))
        head = "点を写す撮影ID" if (self.pts and r is self.pt_row) else "撮影ID"
        self.idvar.set("%s(%d回): %s" % (head, len(ids), "  ".join(ids)))

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
            for i in sorted(self.point_items(r), key=lambda x: x["time"]):
                w.writerow([i["id"], i["lat"], i["lon"], i["inc"], i["emi"], i["side"], i["time"], product_url(i["id"])])
        self.status.set("保存しました: " + fn)

    def save_all(self):
        if not self.rows: return
        fn = filedialog.asksaveasfilename(defaultextension=".csv", initialfile="nac_ranking.csv")
        if not fn: return
        with open(fn, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f); w.writerow(["順位", "緯度", "東経", "太陽の向き", "入射角下限", "入射角上限", "エミッション角下限", "エミッション角上限", "撮影回数", "枚数", "セル内全枚数", "撮影ID"])
            for i, r in enumerate(self.rows, 1):
                w.writerow([i, r["lat"], r["lon"], r["side"], r["inc0"], r["inc1"], r["emi0"], r["emi1"], r["obs"], r["n"], r["total"], " ".join(obs_ids(r))])
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
                                             "inc_min": r["inc0"], "inc_max": r["inc1"], "emi_min": r["emi0"], "emi_max": r["emi1"],
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
                out.append("<Placemark><name>#%d (%d回)</name><description>%s 入射角%s エミッション角%s %d枚 ID: %s</description>"
                           "<Style><PolyStyle><color>%s</color></PolyStyle><LineStyle><width>0</width></LineStyle></Style>"
                           "<Polygon><outerBoundaryIs><LinearRing><coordinates>%s</coordinates></LinearRing></outerBoundaryIs></Polygon>"
                           "</Placemark>" % (i, r["obs"], r["side"], rng(r["inc0"], r["inc1"]), rng(r["emi0"], r["emi1"]), r["n"], " ".join(obs_ids(r)), col, coords))
            out.append("</Document></kml>")
            with open(fn, "w", encoding="utf-8") as f:
                f.write("\n".join(out))
        self.status.set("書き出しました: " + fn)
