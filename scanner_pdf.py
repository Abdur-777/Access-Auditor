from typing import List, Dict, Any
from bs4 import BeautifulSoup
import requests, io
from pypdf import PdfReader




def find_pdf_links(html: str, base_url: str) -> List[str]:
soup = BeautifulSoup(html, "html.parser")
out = []
for a in soup.find_all("a", href=True):
href = a["href"].strip()
if href.lower().endswith(".pdf"):
if href.startswith("http"):
out.append(href)
else:
# naive join
if base_url.endswith("/") and href.startswith("/"):
out.append(base_url[:-1] + href)
elif not base_url.endswith("/") and not href.startswith("/"):
out.append(base_url + "/" + href)
else:
out.append(base_url + href)
# de‑dupe
return list(dict.fromkeys(out))




def quick_pdf_check(url: str) -> Dict[str, Any]:
"""
Heuristics (MVP):
- can text be extracted? (if near‑zero, likely scanned image without tags)
- has document info title? (metadata hint)
- page count
"""
try:
r = requests.get(url, timeout=25)
r.raise_for_status()
data = io.BytesIO(r.content)
pdf = PdfReader(data)
pages = len(pdf.pages)
text_chars = 0
sample_pages = min(3, pages)
for i in range(sample_pages):
try:
t = pdf.pages[i].extract_text() or ""
text_chars += len(t)
except Exception:
pass
meta = pdf.metadata or {}
title = meta.get("/Title") or meta.get("Title")
flags = []
if text_chars < 50 * sample_pages:
flags.append("Very low text — likely scanned or unlabeled")
if not title:
flags.append("Missing Title metadata")
return {
"url": url,
"pages": pages,
"text_sample_chars": text_chars,
"metadata_title": bool(title),
"issues": flags,
}
except Exception as e:
return {"url": url, "error": str(e), "issues": ["Fetch/parse error"]}
