import streamlit as st
import requests
from bs4 import BeautifulSoup
import pandas as pd
from scanner_web import run_axe_on_url, summarize_axe
from scanner_pdf import find_pdf_links, quick_pdf_check
from report import export_report
from utils import safe_filename
from datetime import datetime


st.set_page_config(page_title="Accessibility Auditor", layout="wide")


st.title("Accessibility Auditor — MVP")
url = st.text_input("Council URL to scan", value="https://www.wyndham.vic.gov.au/")
run_btn = st.button("Scan Now", type="primary")


@st.cache_data(show_spinner=False)
def fetch_html(u: str) -> str:
r = requests.get(u, timeout=25)
r.raise_for_status()
return r.text


if run_btn and url:
with st.spinner("Running web accessibility checks (axe)…"):
axe_raw = run_axe_on_url(url)
web_summary = summarize_axe(axe_raw)


st.subheader("Web Scan Summary")
cols = st.columns(3)
cols[0].metric("Heuristic Score", web_summary.get("score", 0))
cols[1].metric("Violations", len(web_summary.get("violations", [])))
cols[2].metric("Incomplete", len(web_summary.get("incomplete", [])))


if web_summary.get("violations"):
st.write("### Top Violations")
rows = []
for v in web_summary["violations"]:
rows.append({
"Rule": v.get("id"),
"Impact": v.get("impact"),
"Help": v.get("help"),
"Help URL": v.get("helpUrl"),
"Instances": len(v.get("nodes", [])),
})
df = pd.DataFrame(rows)
st.dataframe(df, use_container_width=True)


# PDFs
st.write("---")
st.subheader("PDF Checks (quick heuristics)")
try:
html = fetch_html(url)
pdf_links = find_pdf_links(html, url)
except Exception as e:
pdf_links = []
st.warning(f"Could not fetch page to discover PDFs: {e}")


st.caption(f"Found {len(pdf_links)} PDF links on this page")
pdf_links_preview = pdf_links[:5]
if pdf_links_preview:
results = [quick_pdf_check(p) for p in pdf_links_preview]
st.dataframe(pd.DataFrame(results), use_container_width=True)
else:
results = []


# Export
st.write("---")
st.subheader("Export Council Report")
council_name = st.text_input("Council name for report header", value="Wyndham City Council")
if st.button("Generate PDF Report"):
ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
fname = f"{safe_filename(council_name)}-{ts}.pdf"
export_report(fname, council_name, url, web_summary, results)
st.success("Report generated.")
with open(fname, "rb") as f:
st.download_button("Download Report PDF", data=f.read(), file_name=fname, mime="application/pdf")


st.info("This MVP runs axe-core checks for WCAG 2.0/2.1/2.2 AA and simple PDF heuristics. Full PDF/UA checks will be added in Pro.")
