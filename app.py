# app.py — Accessibility Auditor (Wyndham-specialized, WCAG 2.2 AA)
# Run:  streamlit run app.py
# What it does:
# - Clean card UI + top nav (Scan / Results / Dashboard) + Light/Dark mode
# - HTML quick-check: WCAG 1.4.3 color contrast (inline styles), missing image alts
# - PDF heuristics: tagged/un-tagged detection, image count, rough ALT keys
# - Crawler modal: paste multiple URLs, progress + summary
# - Wyndham-branded PDF report (ReportLab -> fpdf2 fallback)
# - CSV-backed history dashboard with trend charts

import os, io, re, time, json, datetime
from dataclasses import dataclass, asdict
from typing import List, Dict, Optional, Tuple
from urllib.parse import urljoin

import requests
import pandas as pd
import streamlit as st
from bs4 import BeautifulSoup, Tag

# ---------- PDF libs (fallback: reportlab -> fpdf2) ----------
HAVE_REPORTLAB = False
HAVE_FPDF = False
try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.utils import ImageReader
    HAVE_REPORTLAB = True
except Exception:
    try:
        from fpdf import FPDF
        HAVE_FPDF = True
    except Exception:
        pass

# ==========================
# Wyndham presets & storage
# ==========================
WYNDHAM = {
    "name": "Wyndham City Council",
    "logo": "https://www.wyndham.vic.gov.au/themes/custom/wyndham/logo.png",
    "primary": "#003B73",
    "links": {
        "Home": "https://www.wyndham.vic.gov.au/",
        "Waste & Recycling": "https://www.wyndham.vic.gov.au/services/waste-recycling",
        "Bin days": "https://www.wyndham.vic.gov.au/residents/waste-recycling/bin-collection",
        "Hard waste": "https://www.wyndham.vic.gov.au/services/waste-recycling/hard-and-green-waste-collection-service",
        "Accessibility statement": "https://www.wyndham.vic.gov.au/accessibility"
    }
}
DATA_DIR = ".audits"; os.makedirs(DATA_DIR, exist_ok=True)
AUDITS_CSV = os.path.join(DATA_DIR, "audits.csv")

# =====================
# Streamlit shell/theme
# =====================
st.set_page_config(page_title="Accessibility Auditor — Wyndham", page_icon="✅", layout="wide")

if "dark_mode" not in st.session_state:
    st.session_state.dark_mode = False
mode = st.toggle("🌗 Dark Mode", value=st.session_state.dark_mode)
st.session_state.dark_mode = mode

PRIMARY = WYNDHAM["primary"] if not mode else "#60A5FA"
BG      = "#ffffff" if not mode else "#0b1220"
FG      = "#0f172a" if not mode else "#e5e7eb"
BORDER  = "#e5e7eb" if not mode else "#1f2937"
KPI_BG  = "#ffffff" if not mode else "#0f172a"

st.markdown(f"""
<style>
body {{ background:{BG}; color:{FG}; }}
.block-container {{ padding-top: 16px; }}
.header {{ display:flex; gap:24px; align-items:flex-end; justify-content:space-between;
           border-bottom:1px solid {BORDER}; padding-bottom:10px; margin-bottom:14px; }}
.header .title h2 {{ margin:0 0 6px 0; }}
.header .subtitle {{ color:#6b7280; font-size:14px; }}
.navbar {{ display:flex; gap:18px; align-items:center; justify-content:flex-end; }}
.navbar a {{ color:{PRIMARY}; text-decoration:none; font-weight:700; padding:6px 10px;
            border-radius:999px; border:1px solid transparent; }}
.navbar a:hover {{ border-color:{PRIMARY}33; background:{PRIMARY}0d; }}
.card {{ background:{BG}; border:1px solid {BORDER}; border-radius:16px; padding:18px;
         box-shadow: 0 2px 14px rgba(0,0,0,.05); }}
.kpis {{ display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin: 8px 0 4px; }}
.kpi {{ background:{KPI_BG}; border:1px solid {BORDER}; border-radius:12px; padding:10px; text-align:center; }}
.kpi .v{{ font-size:22px; font-weight:800; }}
.kpi .l{{ font-size:12px; opacity:.8; }}
.btn-row {{ display:flex; gap:10px; }}
hr.soft {{ border:none; border-top:1px solid {BORDER}; margin: 14px 0; }}
.badge {{ display:inline-block; background:{PRIMARY}; color:white; padding:3px 10px; border-radius:999px; font-size:12px; font-weight:700; }}
</style>
""", unsafe_allow_html=True)

# Header (title left, nav right)
st.markdown(f"""
<div class='header'>
  <div class='title'>
    <h2>Accessibility Auditor — WCAG 2.2 AA</h2>
    <div class='subtitle'>Quick-check contrast, image alts, and PDF tagging. Generate Wyndham-branded reports and track improvements over time.</div>
  </div>
  <nav class='navbar'>
    <a href='#scan'>Scan</a>
    <a href='#results'>Results</a>
    <a href='#dashboard'>Dashboard</a>
  </nav>
</div>
""", unsafe_allow_html=True)

# =====================
# WCAG helpers (contrast)
# =====================
HEX_RE  = re.compile(r"#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})\b")
RGBA_RE = re.compile(r"rgba?\(([^)]+)\)")
TEXT_TAGS = {"p","span","a","li","button","label","small","em","strong","div","h1","h2","h3","h4","h5","h6"}

def _norm_hex(h: str) -> str:
    if not h: return "#000000"
    h=h.strip()
    if len(h)==4 and h.startswith('#'): return "#" + "".join([c*2 for c in h[1:]])
    return h

def _hex_to_rgb(h: str) -> Tuple[int,int,int]:
    h=_norm_hex(h)
    if not h.startswith('#'): return (0,0,0)
    return (int(h[1:3],16), int(h[3:5],16), int(h[5:7],16))

def _parse_css_color(val: Optional[str]) -> Tuple[int,int,int]:
    if not val: return (0,0,0)
    if HEX_RE.match(val): return _hex_to_rgb(val)
    m = RGBA_RE.match(val)
    if m:
        parts=[p.strip() for p in m.group(1).split(',')]
        try: return (int(float(parts[0])), int(float(parts[1])), int(float(parts[2])))
        except: return (0,0,0)
    return (0,0,0)

def _srgb_to_linear(c: float) -> float:
    c=c/255.0
    return c/12.92 if c<=0.03928 else ((c+0.055)/1.055)**2.4

def _rel_lum(rgb: Tuple[int,int,int]) -> float:
    r,g,b=rgb
    return 0.2126*_srgb_to_linear(r)+0.7152*_srgb_to_linear(g)+0.0722*_srgb_to_linear(b)

def _contrast_ratio(fg: Tuple[int,int,int], bg: Tuple[int,int,int]) -> float:
    L1,L2=_rel_lum(fg),_rel_lum(bg)
    return (max(L1,L2)+0.05)/(min(L1,L2)+0.05)

def _passes_aa(cr: float, size_px: Optional[float]=None, bold: bool=False) -> bool:
    if size_px is None: return cr>=4.5
    pt=size_px/1.3333
    is_large = pt>=18 or (bold and pt>=14)
    return cr >= (3.0 if is_large else 4.5)

@dataclass
class ContrastIssue:
    tag: str; text: str; fg: str; bg: str; ratio: float; size_px: Optional[float]; bold: bool

@dataclass
class ImgAltIssue:
    src: str; suggestion: str

def analyze_html(url: str) -> Dict:
    r=requests.get(url,timeout=15); r.raise_for_status()
    soup=BeautifulSoup(r.text,"html.parser")
    issues: List[ContrastIssue] = []
    text_checked=0; pass_count=0

    for el in soup.find_all(TEXT_TAGS):
        t=(el.get_text(strip=True) or "")[:120]
        if not t: continue
        text_checked+=1
        style=el.get("style","") or ""
        fg,bg,size_px,bold=(0,0,0),(255,255,255),None,False
        for part in [p.strip() for p in style.split(';') if ':' in p]:
            k,v=[x.strip().lower() for x in part.split(':',1)]
            if k=="color": fg=_parse_css_color(v)
            if k=="background-color": bg=_parse_css_color(v)
            if k=="font-weight": bold=("bold" in v) or (v.isdigit() and int(v)>=600)
            if k=="font-size":
                try:
                    size_px = float(v[:-2]) if v.endswith('px') else (float(v[:-3])*16.0 if v.endswith('rem') else (float(v[:-2])*16.0 if v.endswith('em') else None))
                except: size_px=None
        cr=_contrast_ratio(fg,bg)
        if _passes_aa(cr,size_px,bold):
            pass_count+=1
        else:
            issues.append(ContrastIssue(el.name,t,f"#{fg[0]:02x}{fg[1]:02x}{fg[2]:02x}",f"#{bg[0]:02x}{bg[1]:02x}{bg[2]:02x}",round(cr,2),size_px,bold))

    img_issues: List[ImgAltIssue] = []
    for img in soup.find_all("img"):
        alt=(img.get("alt") or "").strip()
        if alt=="":
            img_issues.append(ImgAltIssue(urljoin(url,img.get("src") or ""), "Add meaningful alt text"))

    score=round((pass_count/max(text_checked,1))*100.0,2)
    return {"score":score,"checked":text_checked,"pass_count":pass_count,
            "contrast_issues":[asdict(i) for i in issues],
            "img_alt_issues":[asdict(i) for i in img_issues]}

def suggest_fix_for_contrast(issue: Dict) -> str:
    # Escape CSS braces using doubled braces inside f-string
    return (
        f"Element <{issue['tag']}> ratio {issue['ratio']}:\n"
        f"• Darken text or lighten background.\n"
        f"• Example CSS: {issue['tag']} {{ color:#111111; }}\n"
    )

def suggest_fix_for_img_alt(img_issue: Dict) -> str:
    return f"Add alt text: <img src='{img_issue['src']}' alt='Wyndham City Council logo'>"

# =====================
# PDF heuristics & report
# =====================
@dataclass
class PdfAccessibility:
    url: str; pages: int; is_tagged: bool; image_count: int; alt_text_count: int; notes: str

def analyze_pdf(url: str) -> PdfAccessibility:
    try:
        from pypdf import PdfReader
    except Exception:
        raise RuntimeError("Install pypdf in requirements.txt to analyze PDFs")
    resp = requests.get(url, timeout=30); resp.raise_for_status()
    reader = PdfReader(io.BytesIO(resp.content))
    root = reader.trailer.get("/Root", {})
    is_tagged = bool(root.get("/StructTreeRoot"))
    pages = len(reader.pages)
    image_count = 0
    try:
        for i in range(pages):
            page = reader.pages[i]
            res = page.get("/Resources") or {}
            xobj = res.get("/XObject") or {}
            if hasattr(xobj, "items"):
                for _, obj in xobj.items():
                    try:
                        if obj.get("/Subtype") == "/Image": image_count += 1
                    except Exception: pass
    except Exception:
        pass
    raw = resp.content.decode("latin-1", errors="ignore")
    alt_text_count = raw.count("/Alt(") + raw.count("/Alt ")
    notes = "Tagged PDF" if is_tagged else "PDF appears untagged (no /StructTreeRoot)"
    return PdfAccessibility(url=url, pages=pages, is_tagged=is_tagged,
                            image_count=image_count, alt_text_count=alt_text_count, notes=notes)

def build_pdf_report(path: str, brand: Dict, target_url: str,
                     html_result: Optional[Dict], pdf_results: List[PdfAccessibility]):
    if HAVE_REPORTLAB:
        styles = getSampleStyleSheet(); styles["Heading1"].alignment = TA_LEFT
        doc = SimpleDocTemplate(path, pagesize=A4, leftMargin=24, rightMargin=24, topMargin=24, bottomMargin=24)
        story = []
        # Logo optional (if fail, still render)
        try:
            _ = ImageReader(requests.get(brand["logo"], timeout=10).content)
        except Exception:
            pass
        story.append(Paragraph(f"<b>{brand['name']}</b>", styles['Heading1']))
        story.append(Paragraph("Accessibility Audit Report", styles['Heading1']))
        story.append(Paragraph(f"Target URL: {target_url}", styles['Normal']))
        story.append(Paragraph(datetime.datetime.now().strftime("Generated: %Y-%m-%d %H:%M"), styles['Normal']))
        story.append(Spacer(1,10))
        if html_result:
            story.append(Paragraph("WCAG 1.4.3 — HTML Contrast", styles['Heading2']))
            data = [["Checked", html_result['checked']], ["Passed", html_result['pass_count']], ["Score (%)", html_result['score']]]
            t = Table(data, colWidths=[120, 360]); t.setStyle(TableStyle([('INNERGRID',(0,0),(-1,-1),0.25,colors.grey), ('BOX',(0,0),(-1,-1),0.25,colors.grey)]))
            story.append(t); story.append(Spacer(1,6))
            if html_result["contrast_issues"]:
                rows = [["Tag","Text","FG","BG","Ratio"]]
                for i in html_result["contrast_issues"][:10]:
                    rows.append([i["tag"], i["text"][:50], i["fg"], i["bg"], i["ratio"]])
                t2 = Table(rows, colWidths=[40,220,60,60,50])
                t2.setStyle(TableStyle([('INNERGRID',(0,0),(-1,-1),0.25,colors.grey), ('BOX',(0,0),(-1,-1),0.25,colors.grey)]))
                story.append(t2); story.append(Spacer(1,6))
        if pdf_results:
            story.append(Paragraph("PDF Accessibility (heuristics)", styles['Heading2']))
            rows = [["PDF URL","Pages","Tagged?","Images","Alt est.","Notes"]]
            for r in pdf_results[:12]:
                rows.append([r.url, r.pages, "Yes" if r.is_tagged else "No", r.image_count, r.alt_text_count, r.notes])
            t3 = Table(rows, colWidths=[180,40,50,50,70,120])
            t3.setStyle(TableStyle([('INNERGRID',(0,0),(-1,-1),0.25,colors.grey), ('BOX',(0,0),(-1,-1),0.25,colors.grey)]))
            story.append(t3)
        doc.build(story)
        return
    if HAVE_FPDF:
        pdf = FPDF(); pdf.set_auto_page_break(auto=True, margin=15)
        pdf.add_page()
        pdf.set_font("Arial","B",16); pdf.cell(0,10,txt=f"{brand['name']} — Accessibility Audit", ln=True)
        pdf.set_font("Arial", size=12); pdf.cell(0,8,txt=f"Target URL: {target_url}", ln=True)
        pdf.cell(0,8,txt=datetime.datetime.now().strftime("Generated: %Y-%m-%d %H:%M"), ln=True); pdf.ln(4)
        if html_result:
            pdf.set_font("Arial","B",13); pdf.cell(0,8,txt="WCAG 1.4.3 — HTML Contrast", ln=True)
            pdf.set_font("Arial", size=12); pdf.multi_cell(0,6, txt=f"Checked: {html_result['checked']}  Passed: {html_result['pass_count']}  Score: {html_result['score']}%")
        if pdf_results:
            pdf.set_font("Arial","B",13); pdf.cell(0,8,txt="PDF Accessibility (heuristics)", ln=True)
            pdf.set_font("Arial", size=11)
            for r in pdf_results[:12]:
                pdf.multi_cell(0,6, txt=f"- {r.url} — pages={r.pages} tagged={'Yes' if r.is_tagged else 'No'} images={r.image_count} alt_est={r.alt_text_count} notes={r.notes}")
        pdf.output(path); return
    raise RuntimeError("No PDF library available. Add 'reportlab' or 'fpdf2' to requirements.txt.")

# =====================
# Persistence
# =====================
def _append_audit(row: Dict):
    if os.path.exists(AUDITS_CSV):
        try: df = pd.read_csv(AUDITS_CSV)
        except Exception: df = pd.DataFrame()
    else:
        df = pd.DataFrame(columns=["timestamp","url","html_score","contrast_issues","img_alt_issues","pdfs_scanned","pdfs_tagged"])
    df.loc[len(df)] = row
    df.to_csv(AUDITS_CSV, index=False)

def _load_audits() -> pd.DataFrame:
    if os.path.exists(AUDITS_CSV):
        try: return pd.read_csv(AUDITS_CSV)
        except Exception: pass
    return pd.DataFrame(columns=["timestamp","url","html_score","contrast_issues","img_alt_issues","pdfs_scanned","pdfs_tagged"])

# =====================
# Sidebar: inputs + crawler
# =====================
with st.sidebar:
    st.header("Scan Settings")
    target_url = st.text_input("Page URL to scan (HTML)", value=WYNDHAM["links"]["Home"], placeholder="https://…")
    pdf_urls_text = st.text_area("PDF URLs (one per line)", height=120, placeholder="https://…/policy.pdf")
    st.markdown("---")
    st.subheader("Crawler (Batch)")
    with st.expander("Paste URLs and run batch scan"):
        url_list = st.text_area("Enter multiple URLs (one per line)")
        run_crawl = st.button("🚀 Run Crawler")
        if run_crawl:
            urls = [u.strip() for u in (url_list or "").splitlines() if u.strip()]
            if not urls:
                st.warning("No URLs provided.")
            else:
                progress = st.progress(0.0)
                crawled = []
                for i,u in enumerate(urls):
                    try:
                        res = analyze_html(u)
                        crawled.append({"url": u, "score": res["score"], "checked": res["checked"], "alt_issues": len(res["img_alt_issues"])})
                    except Exception as e:
                        crawled.append({"url": u, "score": None, "checked": 0, "alt_issues": None})
                    progress.progress((i+1)/len(urls))
                st.success(f"Crawled {len(crawled)} URLs")
                st.dataframe(pd.DataFrame(crawled), use_container_width=True, hide_index=True)

# =====================
# Cards — Run Audit / Export Report
# =====================
st.markdown("<span id='scan'></span>", unsafe_allow_html=True)
col1, col2 = st.columns([2,1])

with col1:
    st.markdown("<div class='card'>", unsafe_allow_html=True)
    st.markdown("<h3>Run Audit <span class='badge'>Wyndham</span></h3>", unsafe_allow_html=True)
    b1, b2 = st.columns(2)
    with b1:
        run_html = st.button("🔍 Scan HTML (Contrast & Alts)")
    with b2:
        run_pdfs = st.button("📄 Scan PDFs (Tagging & Alts)")

    html_result = st.session_state.get("html_result")
    pdf_results: List[PdfAccessibility] = st.session_state.get("pdf_results", [])

    if run_html and target_url:
        try:
            with st.spinner("Scanning HTML…"):
                html_result = analyze_html(target_url)
                st.session_state["html_result"] = html_result
        except Exception as e:
            st.error(f"HTML scan failed: {e}")

    if run_pdfs:
        pdf_results = []
        for line in (pdf_urls_text or "").splitlines():
            u = line.strip()
            if not u: continue
            try:
                with st.spinner(f"Analyzing PDF: {u}"):
                    pdf_results.append(analyze_pdf(u))
            except Exception as e:
                st.warning(f"Failed PDF: {u} — {e}")
        st.session_state["pdf_results"] = pdf_results

    st.markdown("</div>", unsafe_allow_html=True)

with col2:
    st.markdown("<div class='card'>", unsafe_allow_html=True)
    st.markdown("<h3>Export Report</h3>", unsafe_allow_html=True)
    if HAVE_REPORTLAB or HAVE_FPDF:
        if st.button("⬇️ Generate PDF (Wyndham-branded)"):
            html_result = st.session_state.get("html_result")
            pdf_results = st.session_state.get("pdf_results", [])
            if not (html_result or pdf_results):
                st.info("Run a scan first.")
            else:
                out_path = os.path.join(DATA_DIR, f"audit_{int(time.time())}.pdf")
                try:
                    build_pdf_report(out_path, WYNDHAM, target_url or "(none)", html_result, pdf_results)
                    with open(out_path, "rb") as f:
                        st.download_button("⬇️ Download Report PDF", data=f.read(),
                                           file_name=os.path.basename(out_path), mime="application/pdf")
                except Exception as e:
                    st.error(f"Report failed: {e}")
    else:
        st.info("Install **reportlab** or **fpdf2** in requirements.txt for PDF export.")
    st.markdown("</div>", unsafe_allow_html=True)

# =====================
# Results section
# =====================
st.markdown("<h3 id='results'>Results</h3>", unsafe_allow_html=True)
if html_result:
    st.markdown("<div class='kpis'>", unsafe_allow_html=True)
    st.markdown(f"<div class='kpi'><div class='v'>{html_result['score']}</div><div class='l'>Score (%)</div></div>", unsafe_allow_html=True)
    st.markdown(f"<div class='kpi'><div class='v'>{html_result['checked']}</div><div class='l'>Checked</div></div>", unsafe_allow_html=True)
    st.markdown(f"<div class='kpi'><div class='v'>{html_result['pass_count']}</div><div class='l'>Passed</div></div>", unsafe_allow_html=True)
    st.markdown(f"<div class='kpi'><div class='v'>{len(html_result['img_alt_issues'])}</div><div class='l'>Alt issues</div></div>", unsafe_allow_html=True)
    st.markdown("</div>", unsafe_allow_html=True)

    with st.expander("Contrast fails (examples)"):
        if html_result["contrast_issues"]:
            df_ci = pd.DataFrame(html_result["contrast_issues"])
            st.dataframe(df_ci, use_container_width=True, hide_index=True)
        else:
            st.success("No inline-style contrast failures detected.")

    with st.expander("Images missing alt text"):
        if html_result["img_alt_issues"]:
            st.dataframe(pd.DataFrame(html_result["img_alt_issues"]), use_container_width=True, hide_index=True)
        else:
            st.success("No images without alt text detected in this page.")

    st.markdown("#### Auto Fix Suggestions")
    if html_result["contrast_issues"]:
        for issue in html_result["contrast_issues"][:8]:
            st.code(suggest_fix_for_contrast(issue))
    if html_result["img_alt_issues"]:
        for issue in html_result["img_alt_issues"][:5]:
            st.code(suggest_fix_for_img_alt(issue))

if 'pdf_results' in st.session_state and st.session_state['pdf_results']:
    st.markdown("#### PDF Accessibility Summary")
    df_pdf = pd.DataFrame([asdict(r) for r in st.session_state['pdf_results']])
    st.dataframe(df_pdf, use_container_width=True, hide_index=True)

# Save history
if html_result or ('pdf_results' in st.session_state and st.session_state['pdf_results']):
    if st.button("💾 Save to Dashboard History"):
        row = {
            "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
            "url": target_url or "",
            "html_score": html_result["score"] if html_result else None,
            "contrast_issues": len(html_result["contrast_issues"]) if html_result else 0,
            "img_alt_issues": len(html_result["img_alt_issues"]) if html_result else 0,
            "pdfs_scanned": len(st.session_state.get('pdf_results', [])),
            "pdfs_tagged": sum(1 for p in st.session_state.get('pdf_results', []) if p.is_tagged)
        }
        _append_audit(row); st.success("Saved. See Dashboard below.")

# =====================
# Dashboard
# =====================
st.markdown("<h3 id='dashboard'>Dashboard</h3>", unsafe_allow_html=True)
df_hist = _load_audits()
if df_hist.empty:
    st.info("No history yet. Run a scan and click Save.")
else:
    try: df_hist["timestamp"] = pd.to_datetime(df_hist["timestamp"])  # type: ignore
    except: pass
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**HTML Score (%)**")
        st.line_chart(df_hist.set_index("timestamp")["html_score"].fillna(method="ffill"))
    with c2:
        st.markdown("**Contrast Issues (count)**")
        st.line_chart(df_hist.set_index("timestamp")["contrast_issues"].fillna(0))
    st.markdown("**Recent Audits**")
    st.dataframe(df_hist.sort_values("timestamp", ascending=False).head(20), use_container_width=True, hide_index=True)

st.caption("Notes: Contrast quick-check uses inline styles only. Full audit requires computed styles, keyboard navigation, and ARIA checks.")
