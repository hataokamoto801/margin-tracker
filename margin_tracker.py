#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
margin_tracker.py
JPX「銘柄別信用取引週末残高」PDFを取得し、watchlist.txt の銘柄について
制度信用（売残・買残・前週比・倍率）を抽出して report.md / history.csv を作る。

使い方:
    python margin_tracker.py                 # JPXから最新PDFを自動取得
    python margin_tracker.py --pdf x.pdf     # 手元のPDFファイルを使う
"""

import argparse
import csv
import os
import re
import sys
from datetime import datetime

import pdfplumber
import requests
from bs4 import BeautifulSoup

JPX_PAGE = "https://www.jpx.co.jp/markets/statistics-equities/margin/05.html"
BASE = "https://www.jpx.co.jp"

HERE = os.path.dirname(os.path.abspath(__file__))
WATCHLIST = os.path.join(HERE, "watchlist.txt")
HISTORY = os.path.join(HERE, "history.csv")
REPORT = os.path.join(HERE, "report.md")
PDF_DIR = os.path.join(HERE, "pdf")

UA = {"User-Agent": "Mozilla/5.0 (margin-tracker; personal use)"}

# PDFの1行は「銘柄名 …… コード5桁 ISIN 数値×12」の順に記述されている。
# 文字を「描画順（＝PDF内部の記述順）」のまま連結して復元する。
#   ...受益証券13570JP3047780006  ← ここからコードとISINを取る
CODE_RE = re.compile(r"([0-9A-Z]{5})(JP[0-9A-Z]{10}|[A-Z]{2}[0-9A-Z]{10})")

NUM_COL_X = 292   # 通常はこのx座標より右が数値列
NUM_COL_X_WIDE = 250  # 銘柄名が長く先頭数値が食い込む行の救済用
ROW_TOL = 3       # 同じ行とみなすy方向の許容差(px)


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def load_watchlist():
    codes = []
    with open(WATCHLIST, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            codes.append(line.split()[0])
    if not codes:
        sys.exit("watchlist.txt に銘柄がありません")
    return codes


def find_latest_pdf_url():
    """JPXの一覧ページから最新のPDFリンクを探す。"""
    log("JPXページを取得中...")
    r = requests.get(JPX_PAGE, headers=UA, timeout=60)
    r.raise_for_status()
    soup = BeautifulSoup(r.content, "html.parser")

    cands = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href.lower().endswith(".pdf"):
            continue
        m = re.search(r"(\d{8})", href)
        if "syumatsu" in href.lower() or m:
            url = href if href.startswith("http") else BASE + href
            key = m.group(1) if m else "0"
            cands.append((key, url))

    if not cands:
        sys.exit("PDFリンクが見つかりません。JPXのページ構成が変わった可能性があります。")

    cands.sort(reverse=True)  # 日付が新しい順
    return cands[0][1]


def download_pdf(url):
    os.makedirs(PDF_DIR, exist_ok=True)
    name = os.path.basename(url.split("?")[0])
    path = os.path.join(PDF_DIR, name)
    if os.path.exists(path):
        log(f"取得済みのPDFを使用: {name}")
        return path
    log(f"PDFをダウンロード中: {name}")
    r = requests.get(url, headers=UA, timeout=120)
    r.raise_for_status()
    with open(path, "wb") as f:
        f.write(r.content)
    return path


def numbers_from_words(word_rows, key, x_min):
    """指定行の単語列から、▲を直後の数値に統合しつつ数値リストを作る。

    ▲ は extract_words() で独立トークンに分割されることがあるため、
    ここで直後の数値に符号として畳み込む。融合単語は複数数字を分解する。
    """
    ws = sorted(
        (w for w in word_rows.get(key, []) if w["x1"] > x_min),
        key=lambda w: w["x0"],
    )
    vals = []
    neg = False
    for w in ws:
        t = w["text"]
        if t == "▲":
            neg = True
            continue
        found_digit = False
        for num in re.findall(r"[\d,]+", t):
            v = int(num.replace(",", ""))
            vals.append(-v if neg else v)
            neg = False
            found_digit = True
        # 数字を含まない記号だけの単語は無視（negは維持しない）
        if not found_digit and t not in ("-",):
            neg = False
    return vals


def parse_pdf(path, codes):
    """PDFから対象銘柄の制度信用データを抜き出す。"""
    code5 = {c + "0": c for c in codes}
    found = {}

    with pdfplumber.open(path) as pdf:
        asof = extract_asof(pdf)
        log(f"データ基準日: {asof} / 全{len(pdf.pages)}ページを解析中...")

        for page in pdf.pages:
            # y座標で行にまとめる（charsは描画順を保持している）
            rows = {}
            for c in page.chars:
                rows.setdefault(round(c["top"] / ROW_TOL), []).append(c)

            words = page.extract_words()
            wrows = {}
            for w in words:
                wrows.setdefault(round(w["top"] / ROW_TOL), []).append(w)

            for key, cs in rows.items():
                joined = "".join(c["text"] for c in cs)
                m = CODE_RE.search(joined)
                if not m:
                    continue
                c5 = m.group(1)
                if c5 not in code5:
                    continue

                # 銘柄名 = コードの手前から、種類表記を除いた部分
                name = joined[:m.start()]
                name = re.sub(r"(普通株式|受益証券|投資証券|ＪＤＲ|優先出資証券|"
                              r"種類株式|優先株式).*$", "", name)
                name = re.sub(r"^[A-Z]", "", name)   # 行頭の区分記号(A/B/J...)を除去
                name = name.replace("\u3000", " ").strip()

                # 数値12個を抽出（▲統合方式）。
                # まず通常の境界で取り、12個に満たなければ境界を広げて
                # 銘柄名に食い込んだ先頭数値を救済する。右揃えなので末尾12個を採用。
                # 数値は銘柄名と同じ束にある場合と、直後の束にある場合がある。
                # 候補となる束を順に試し、12個そろったものを採用する。
                nums = []
                for k in (key, key + 1):
                    for xmin in (NUM_COL_X, NUM_COL_X_WIDE):
                        cand = numbers_from_words(wrows, k, xmin)
                        if len(cand) >= 12:
                            nums = cand
                            break
                    if nums:
                        break
                if len(nums) < 12:
                    continue
                nums = nums[-12:]

                (s_tot, s_tot_wc, b_tot, b_tot_wc,
                 s_neg, s_neg_wc, s_std, s_std_wc,
                 b_neg, b_neg_wc, b_std, b_std_wc) = nums

                ratio = None
                if s_std:  # 0 と None を除外
                    ratio = round(b_std / s_std, 2)

                found[code5[c5]] = dict(
                    name=name, asof=asof,
                    s_std=s_std, s_std_wc=s_std_wc,
                    b_std=b_std, b_std_wc=b_std_wc,
                    s_tot=s_tot, b_tot=b_tot, ratio=ratio,
                )
    return asof, found


def extract_asof(pdf):
    """PDF先頭から「2026/7/3 申込み現在」の日付を拾う。"""
    txt = pdf.pages[0].extract_text() or ""
    m = re.search(r"(\d{4})/(\d{1,2})/(\d{1,2})\s*申込み現在", txt)
    if m:
        y, mo, d = (int(x) for x in m.groups())
        return f"{y:04d}-{mo:02d}-{d:02d}"
    return datetime.now().strftime("%Y-%m-%d")


def fmt(v):
    return "-" if v is None else f"{v:,}"


def fmt_wc(v):
    if v is None:
        return "-"
    if v > 0:
        return f"+{v:,}"
    if v < 0:
        return f"▲{abs(v):,}"
    return "0"


def write_report(asof, found, codes):
    lines = [
        "# 制度信用 週末残高レポート",
        "",
        f"- **データ基準日**: {asof} 申込み現在（JPX 銘柄別信用取引週末残高）",
        f"- **生成日時**: {datetime.now():%Y-%m-%d %H:%M} (UTC)",
        f"- **抽出**: {len(found)} / {len(codes)} 銘柄",
        "- **制度信用倍率** = 制度買残 ÷ 制度売残",
        "",
        "| コード | 銘柄名 | 制度売残 | 前週比 | 制度買残 | 前週比 | 倍率 |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for c in codes:
        d = found.get(c)
        if not d:
            lines.append(f"| {c} | (抽出できず) | - | - | - | - | - |")
            continue
        ratio = f"{d['ratio']:.2f}" if d["ratio"] is not None else "-"
        lines.append(
            f"| {c} | {d['name']} | {fmt(d['s_std'])} | {fmt_wc(d['s_std_wc'])} | "
            f"{fmt(d['b_std'])} | {fmt_wc(d['b_std_wc'])} | {ratio} |"
        )

    missing = [c for c in codes if c not in found]
    if missing:
        lines += ["", "## 抽出できなかった銘柄", "",
                  "、".join(missing),
                  "",
                  "> コードの入力ミス、上場廃止、または信用対象外の可能性があります。"]

    with open(REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    log(f"report.md を書き出しました（{len(found)}/{len(codes)}銘柄）")


def append_history(asof, found, codes):
    """同じ基準日の行が既にあれば追記しない（重複防止）。"""
    existing = set()
    if os.path.exists(HISTORY):
        with open(HISTORY, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                existing.add((row["date"], row["code"]))

    new_rows = []
    for c in codes:
        d = found.get(c)
        if not d:
            continue
        if (asof, c) in existing:
            continue
        new_rows.append([asof, c, d["name"], d["s_std"], d["s_std_wc"],
                         d["b_std"], d["b_std_wc"], d["ratio"],
                         d["s_tot"], d["b_tot"]])

    if not new_rows:
        log("history.csv: 同じ基準日のデータが既にあるため追記しません")
        return False

    is_new = not os.path.exists(HISTORY)
    with open(HISTORY, "a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if is_new:
            w.writerow(["date", "code", "name", "std_sell", "std_sell_wc",
                        "std_buy", "std_buy_wc", "std_ratio",
                        "total_sell", "total_buy"])
        w.writerows(new_rows)
    log(f"history.csv に {len(new_rows)} 行追記しました")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", help="手元のPDFを使う場合のパス")
    args = ap.parse_args()

    codes = load_watchlist()
    log(f"ウォッチ銘柄: {len(codes)}件")

    path = args.pdf or download_pdf(find_latest_pdf_url())
    asof, found = parse_pdf(path, codes)

    if not found:
        sys.exit("1銘柄も抽出できませんでした。PDFの様式が変わった可能性があります。")

    write_report(asof, found, codes)
    append_history(asof, found, codes)
    log("完了")


if __name__ == "__main__":
    main()
