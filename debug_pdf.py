# debug_pdf.py  -- 7/10 PDFの構造診断用（使い捨て）
import re, io, sys, requests, pdfplumber
from bs4 import BeautifulSoup

BASE = "https://www.jpx.co.jp"
LIST_URL = f"{BASE}/markets/statistics-equities/margin/05.html"
HEADERS = {"User-Agent": "Mozilla/5.0"}

# 調べたい銘柄コード（失敗組＋成功組を混ぜる）
TARGETS = ["1326", "1357", "1605", "1662", "1801", "1802", "1803", "1377", "1419", "1812"]

def latest_pdf_url():
    html = requests.get(LIST_URL, headers=HEADERS, timeout=30).text
    soup = BeautifulSoup(html, "html.parser")
    links = [a["href"] for a in soup.find_all("a", href=True) if a["href"].lower().endswith(".pdf")]
    print("=== PDFリンク候補（先頭5件） ===")
    for l in links[:5]:
        print(" ", l)
    if not links:
        sys.exit("PDFリンクが見つかりません")
    u = links[0]
    return u if u.startswith("http") else BASE + u

def main():
    url = latest_pdf_url()
    print("\n=== 使用するPDF ===\n", url)
    pdf_bytes = requests.get(url, headers=HEADERS, timeout=60).content
    print("PDFサイズ:", len(pdf_bytes), "bytes")

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        print("総ページ数:", len(pdf.pages))
        page = pdf.pages[0]
        print("ページ幅/高さ:", page.width, page.height)

        # --- 1) 描画順char連結（現行方式）で行を作る ---
        chars = page.chars
        print("charsの数:", len(chars))

        # y座標でグルーピング
        rows = {}
        for c in chars:
            key = round(c["top"], 0)
            rows.setdefault(key, []).append(c)

        print("\n=== 現行の正規表現でヒットするか（各対象コード） ===")
        pat = re.compile(r"([0-9A-Z]{5})(JP[0-9A-Z]{10}|[A-Z]{2}[0-9A-Z]{10})")
        for key in sorted(rows):
            line = "".join(ch["text"] for ch in rows[key])
            for t in TARGETS:
                if line.startswith(t) or (t + "0") in line[:12]:
                    m = pat.search(line)
                    print(f"\n--- code={t} top={key} ---")
                    print("RAW  :", repr(line[:160]))
                    print("REGEX:", m.groups() if m else "★ヒットせず★")
                    # 単語ベースも見る
                    words = page.extract_words(use_text_flow=False)
                    ws = [w for w in words if abs(w["top"] - key) < 3]
                    print("WORDS:", [(round(w['x0']), w['text']) for w in ws])
                    break

        # --- 2) 先頭20行のRAWをそのまま見る（ヘッダ構造の確認） ---
        print("\n\n=== 先頭25行のRAW ===")
        for key in sorted(rows)[:25]:
            line = "".join(ch["text"] for ch in rows[key])
            print(f"[{key:>6}] {line[:150]}")

if __name__ == "__main__":
    main()
