#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""JPX日次の制度信用残高をreport.md / history_daily.csvに出力。
python margin_tracker.py
python margin_tracker.py --pdf 20261001_mtall.pdf
旧週次のhistory.csvは変更しません。
"""
import argparse
import csv
import io
import logging
import os
import re
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen
from html.parser import HTMLParser
from bisect import bisect_left

import pdfplumber

HERE = Path(__file__).resolve().parent
WATCHLIST = HERE / "watchlist.txt"
REPORT = HERE / "report.md"
HISTORY = HERE / "history_daily.csv"
JPX_PAGE = "https://www.jpx.co.jp/markets/statistics-equities/margin/01.html"
JST = timezone(timedelta(hours=9))
UA = {"User-Agent": "Mozilla/5.0 (margin-tracker; personal use)"}
# 2026年9月25日申込分以降の新形式。A4横、14数値列。
# 合計売残/前日比/上場比、合計買残/前日比/上場比、
# 一般売残/前日比、制度売残/前日比、一般買残/前日比、制度買残/前日比。
EDGES = (251, 293, 334, 365, 406, 448, 478, 520,
         561, 603, 644, 685, 726, 768, 810)
CODE_RE = re.compile(r"([0-9A-Z]{5})([A-Z]{2}[0-9A-Z]{10})")
FIELDS = ["date", "code", "name", "std_sell", "std_sell_dc",
          "std_buy", "std_buy_dc", "std_ratio", "total_sell", "total_buy"]


def log(message):
    print(f"[{datetime.now(JST):%H:%M:%S}] {message}", flush=True)


def atomic_write(path, text):
    path = Path(path)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="",
                                     dir=path.parent, delete=False) as f:
        temp = f.name
        f.write(text)
    try:
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def load_watchlist():
    codes = []
    for line in WATCHLIST.read_text(encoding="utf-8-sig").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        code = line.split()[0].upper()
        if not re.fullmatch(r"[0-9A-Z]{4,5}", code):
            raise ValueError(f"銘柄コードを確認してください: {code}")
        if code not in codes:
            codes.append(code)
    if not codes:
        raise ValueError("watchlist.txt に銘柄がありません")
    return codes


def fetch(url):
    with urlopen(Request(url, headers=UA), timeout=120) as response:
        return response.read()


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.hrefs.append(href)


def find_latest_pdf():
    parser = Links()
    parser.feed(fetch(JPX_PAGE).decode("utf-8"))
    candidates = []
    for href in parser.hrefs:
        url = urljoin(JPX_PAGE, href)
        filename = Path(urlparse(url).path).name
        m = re.fullmatch(r"(\d{8})_mtall\.pdf", filename, re.I)
        if m:
            asof = datetime.strptime(m[1], "%Y%m%d").date().isoformat()
            candidates.append((asof, url))
    if not candidates:
        raise ValueError("日次PDF（YYYYMMDD_mtall.pdf）が見つかりません")
    return max(candidates)


def download_pdf(url):
    log("最新の日次PDFを取得中...")
    content = fetch(url)
    if not content.startswith(b"%PDF-"):
        raise ValueError("取得したファイルがPDFではありません")
    # 毎回取得して、公表後の訂正も反映する。
    return io.BytesIO(content)


def integer(text):
    s = re.sub(r"\s+", "", text).replace(",", "")
    for sign in ("▲", "△", "−", "－"):
        s = s.replace(sign, "-")
    if s in ("-", "—"):
        return None
    if not re.fullmatch(r"[+-]?\d+", s):
        raise ValueError(f"数値を読み取れません: {text!r}")
    return int(s)


def parse_pdf(source, codes=None):
    wanted = set(codes) if codes is not None else None
    found = {}
    logging.getLogger("pdfminer").setLevel(logging.ERROR)
    with pdfplumber.open(source) as pdf:
        first = pdf.pages[0].extract_text(x_tolerance=1, y_tolerance=1) or ""
        compact = re.sub(r"\s+", "", first)
        m = re.search(r"(\d{4})/(\d{1,2})/(\d{1,2})申込(?:み)?現在", compact)
        if not m:
            raise ValueError("PDFのデータ基準日を読み取れません")
        asof = date(*map(int, m.groups())).isoformat()
        if "前日比" not in compact or "銘柄別信用取引残高" not in compact:
            raise ValueError("日次の新形式PDFを指定してください（旧週次形式は非対応）")
        log(f"基準日 {asof} / {len(pdf.pages)}ページを解析")
        for page in pdf.pages:
            if abs(page.width - 842) > 2 or abs(page.height - 595) > 2:
                raise ValueError(f"{page.page_number}ページ: PDFの用紙サイズが変わっています")
            # 株数ラベル Shs. を文字列と配置で識別する。
            # 日付により約1px動くため、特定のx座標との完全一致は使わない。
            # extract_wordsでは長い銘柄名と結合するので描画順の文字を使う。
            anchors = []
            for i, ch in enumerate(page.chars):
                if ch["text"] != "S" or not 220 <= ch["x0"] < EDGES[0]:
                    continue
                label = page.chars[i:i + 4]
                if ("".join(c["text"] for c in label) == "Shs."
                        and all(abs(c["top"] - ch["top"]) < 1 for c in label)
                        and all(0 < label[j + 1]["x0"] - label[j]["x0"] < 5
                                for j in range(3))):
                    anchors.append(ch)
            if not anchors:
                raise ValueError(f"{page.page_number}ページ: 株数行が見つかりません")
            # 行ごとの文字を一度に振り分ける。
            rows = [[] for _ in anchors]
            anchors.sort(key=lambda w: w["top"])
            tops = [w["top"] for w in anchors]
            for ch in page.chars:
                i = bisect_left(tops, ch["top"])
                for j in (i - 1, i):
                    if 0 <= j < len(tops) and abs(tops[j] - ch["top"]) < 1:
                        rows[j].append(ch)
                        break
            for chars in rows:
                # 描画順を使い、長い銘柄名と市場区分の重なりを避ける。
                raw = "".join(ch["text"] for ch in chars)
                match = CODE_RE.search(raw)
                if not match and re.search(r"\d+銘柄株数", re.sub(r"\s+", "", raw)):
                    continue  # 最終ページの市場別集計
                if not match:
                    raise ValueError(f"{page.page_number}ページ: 銘柄コードを読めません")
                code = match[1][:-1] if match[1].endswith("0") else match[1]
                if wanted is not None and code not in wanted:
                    continue
                name = raw[:match.start()]
                name = re.split(r"普通株式|受益証券|投資証券|ＪＤＲ|優先出資証券|種類株式|優先株式", name)[0]
                name = re.sub(r"^[AJKBMCTF]", "", name).strip()
                # 描画順でコード・ISINより前の銘柄名を数値領域から除外。
                offset = 0
                numeric_chars = []
                for ch in chars:
                    offset += len(ch["text"])
                    if offset > match.end():
                        numeric_chars.append(ch)
                cells = []
                for left, right in zip(EDGES, EDGES[1:]):
                    cell = sorted((ch for ch in numeric_chars
                                   if left <= (ch["x0"] + ch["x1"]) / 2 < right),
                                  key=lambda ch: ch["x0"])
                    cells.append("".join(ch["text"] for ch in cell))
                try:
                    v = {i: integer(cells[i]) for i in range(14) if i not in (2, 5)}
                    # 合計＝一般＋制度。列ずれは出力前にエラーにする。
                    if any(v[i] is None for i in (0, 3, 6, 8, 10, 12)):
                        raise ValueError("残高が欠落しています")
                    if v[0] != v[6] + v[8] or v[3] != v[10] + v[12]:
                        raise ValueError("残高の合計が一致しません")
                    for total, general, standard in ((1, 7, 9), (4, 11, 13)):
                        if all(v[i] is not None for i in (total, general, standard)):
                            if v[total] != v[general] + v[standard]:
                                raise ValueError("前日比の合計が一致しません")
                    if any(v[i] < 0 for i in (0, 3, 6, 8, 10, 12)):
                        raise ValueError("残高が負数です")
                except ValueError as exc:
                    raise ValueError(f"{page.page_number}ページ・{code}: {exc}") from exc
                if code in found:
                    raise ValueError(f"銘柄コードが重複しています: {code}")
                found[code] = dict(name=name, s_std=v[8], s_dc=v[9],
                                   b_std=v[12], b_dc=v[13], s_tot=v[0], b_tot=v[3],
                                   ratio=v[12] / v[8] if v[8] else None)
            page.close()
    if not found:
        raise ValueError("対象銘柄を1件も抽出できませんでした")
    return asof, found


def fmt(v):
    return f"{v:,}"


def change(v):
    if v is None:
        return "—"
    return f"+{v:,}" if v > 0 else (f"−{abs(v):,}" if v < 0 else "0")


def report_text(asof, found, codes, source):
    lines = ["# 制度信用 日次残高レポート", "",
             f"- **データ基準日**：{asof} 申込み現在",
             f"- **生成日時**：{datetime.now(JST):%Y-%m-%d %H:%M}（日本時間）",
             f"- **抽出**：{len(found)} / {len(codes)} 銘柄",
             f"- **出典**：{source}", "",
             "**左側：信用買い ／ 右側：信用売り**（制度信用・株数）", "",
             "- **前日比**：PDF記載の前営業日比。＋は増加、−は減少、—は比較値なし。",
             "- **信用倍率**：制度買い残 ÷ 制度売り残。売り残0は「—」。",
             "- **★**：信用倍率が1倍以下（丸める前の数値で判定）。",
             "- ETF等は1口を1株として表示。金額ではありません。", "",
             "| コード | 銘柄名 | 買い残 | 買い残の前日比 | 売り残 | 売り残の前日比 | 信用倍率 |",
             "|---|---|---:|---:|---:|---:|---:|"]
    for code in codes:
        d = found.get(code)
        if d is None:
            lines.append(f"| {code} | 対象PDFに見つかりません | — | — | — | — | — |")
            continue
        # WORD JOINERで数値・単位・★の間の改行を防ぐ（GitHub Markdown対応）。
        ratio = f"{d['ratio']:.2f}&#8288;倍" if d['ratio'] is not None else "—"
        if d['s_std'] > 0 and d['b_std'] <= d['s_std']:
            ratio += "&#8288;★"
        name = d['name'].replace("|", "&#124;").replace("\n", " ")
        lines.append(f"| {code} | {name} | **{fmt(d['b_std'])}** | {change(d['b_dc'])} | "
                     f"**{fmt(d['s_std'])}** | {change(d['s_dc'])} | {ratio} |")
    missing = [c for c in codes if c not in found]
    if missing:
        lines += ["", "## 対象PDFに見つからなかった銘柄", "", "、".join(missing),
                  "", "コード・信用対象・上場状況・PDFの形式をご確認ください。"]
    return "\n".join(lines) + "\n"


def save_history(asof, found, codes):
    rows = {}
    if HISTORY.exists():
        with HISTORY.open(encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames != FIELDS:
                raise ValueError("history_daily.csv の列形式が違います")
            for row in reader:
                rows[(row['date'], row['code'])] = row
    for code in codes:
        if code not in found:
            continue
        d = found[code]
        values = [asof, code, d['name'], d['s_std'], d['s_dc'],
                  d['b_std'], d['b_dc'],
                  f"{d['ratio']:.2f}" if d['ratio'] is not None else "",
                  d['s_tot'], d['b_tot']]
        rows[(asof, code)] = dict(zip(FIELDS, values))
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=FIELDS)
    writer.writeheader()
    writer.writerows(rows[k] for k in sorted(rows))
    atomic_write(HISTORY, out.getvalue())


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pdf", help="手元の新形式日次PDF")
    args = ap.parse_args()
    codes = load_watchlist()
    expected = None
    if args.pdf:
        source = Path(args.pdf)
        source_label = source.name
    else:
        expected, url = find_latest_pdf()
        source = download_pdf(url)
        source_label = f"[JPX 銘柄別信用取引残高]({url})"
    asof, found = parse_pdf(source, codes)
    if expected is not None and asof != expected:
        raise ValueError("リンクの日付とPDFの基準日が一致しません")
    text = report_text(asof, found, codes, source_label)
    save_history(asof, found, codes)
    atomic_write(REPORT, text)
    log(f"完了: {len(found)}/{len(codes)}銘柄、report.md / history_daily.csv を更新")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as exc:
        raise SystemExit(f"エラー: {exc}")
