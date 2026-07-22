# debug_pdf.py -- 最新PDF取得ロジックの診断
import re, requests
from bs4 import BeautifulSoup

JPX_PAGE = "https://www.jpx.co.jp/markets/statistics-equities/margin/05.html"
BASE = "https://www.jpx.co.jp"
UA = {"User-Agent": "Mozilla/5.0 (margin-tracker; personal use)"}

r = requests.get(JPX_PAGE, headers=UA, timeout=60)
r.raise_for_status()
soup = BeautifulSoup(r.content, "html.parser")

print("=== ページ上の全PDFリンク（syumatsuまたは日付8桁を含む） ===")
cands = []
for a in soup.find_all("a", href=True):
    href = a["href"]
    if not href.lower().endswith(".pdf"):
        continue
    m = re.search(r"(\d{8})", href)
    if "syumatsu" in href.lower() or m:
        key = m.group(1) if m else "0"
        cands.append((key, href))

for key, href in cands:
    print(f"  key={key}  {href}")

print(f"\n候補数: {len(cands)}")

cands.sort(reverse=True)
if cands:
    print("\n=== 現行ロジックが『最新』と判定するもの ===")
    print("  key =", cands[0][0])
    print("  url =", cands[0][1])
