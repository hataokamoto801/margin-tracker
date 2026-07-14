import re, io, requests, pdfplumber

URL = "https://www.jpx.co.jp/markets/statistics-equities/margin/tvdivq0000001rnl-att/syumatsu2026071000.pdf"
TARGETS = ["13260", "13570", "16050", "16620", "18010", "18020", "13770", "18120"]

pdf_bytes = requests.get(URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=60).content
print("PDF bytes:", len(pdf_bytes))

pat = re.compile(r"([0-9A-Z]{5})(JP[0-9A-Z]{10}|[A-Z]{2}[0-9A-Z]{10})")

with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
    print("pages:", len(pdf.pages))
    hits = 0
    for pno, page in enumerate(pdf.pages):
        rows = {}
        for c in page.chars:
            rows.setdefault(round(c["top"], 0), []).append(c)
        keys = sorted(rows)
        for i, k in enumerate(keys):
            line = "".join(ch["text"] for ch in rows[k])
            m = pat.search(line)
            if not m:
                continue
            hits += 1
            code = m.group(1)
            if code in TARGETS:
                nxt = "".join(ch["text"] for ch in rows[keys[i+1]]) if i+1 < len(keys) else "(なし)"
                print(f"\n### p{pno+1} code={code} top={k}")
                print(" NAME:", repr(line[:80]))
                print(" NEXT:", repr(nxt[:120]))
                ws = [w for w in page.extract_words() if abs(w["top"] - keys[i+1]) < 4 and w["x0"] >= 292]
                print(" WORDS(x>=292):", [w["text"] for w in ws])
    print("\n=== 全ページで正規表現ヒットした銘柄数:", hits, "===")
