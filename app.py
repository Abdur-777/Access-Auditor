# app.py — Accessibility Auditor 2.0 (Wyndham-specialized)
# Run locally:  streamlit run app.py
# Requires: streamlit, requests, beautifulsoup4, pandas, pypdf, reportlab, pillow
# Optional (already common): python-dotenv

import os, io, re, time, math, json, datetime
from urllib.parse import urljoin, urlparse
from dataclasses import dataclass, asdict
from typing import List, Dict, Optional, Tuple

import requests
from bs4 import BeautifulSoup, Tag
import pandas as pd
import streamlit as st
from PIL import Image

# ------- PDF utilities (pypdf) -------
from pypdf import PdfReader

# ------- Report generation (reportlab) -------
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.enums import TA_LEFT
from reportlab.lib import utils
from reportlab.lib.units import mm

# ==========================
# Wyndham presets & branding
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

DATA_DIR = ".audits"
AUDITS_CSV = os.path.join(DATA_DIR, "audits.csv")
os.makedirs(DATA_DIR, exist_ok=True)

st.set_page_config(
    page_title="Accessibility Auditor — Wyndham",
    page_icon="✅",
    layout="wide"
)

# =====================
# Utility: Color / WCAG
# =====================
HEX_RE = re.compile(r"#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})\b")
RGBA_RE = re.compile(r"rgba?\(([^)]+)\)")


def normalize_hex(h: str) -> str:
    if not h:
        return "#000000"
    h = h.strip()
    if not h.startswith("#"):
        return h
    if len(h) == 4:
        # #abc -> #aabbcc
        return "#" + "".join([c * 2 for c in h[1:]])
    return h


def hex_to_rgb(hex_color: str) -> Tuple[int, int, int]:
    hex_color = normalize_hex(hex_color)
    if not hex_color.startswith("#"):
        return (0, 0, 0)
    r = int(hex_color[1:3], 16)
    g = int(hex_color[3:5], 16)
    b = int(hex_color[5:7], 16)
    return (r, g, b)


def parse_css_color(val: Optional[str]) -> Tuple[int, int, int]:
    """Parse inline CSS color value into RGB (0-255). Defaults to black/white fallbacks."""
    if not val:
        return (0, 0, 0)
    val = val.strip()
    if HEX_RE.match(val):
        return hex_to_rgb(val)
    m = RGBA_RE.match(val)
    if m:
        parts = [p.strip() for p in m.group(1).split(",")]
        if len(parts) >= 3:
            try:
                r = int(float(parts[0]))
                g = int(float(parts[1]))
                b = int(float(parts[2]))
                return (r, g, b)
            except Exception:
                pass
    # named colors or unknown -> fallback to black
    return (0, 0, 0)


def srgb_to_linear(c: float) -> float:
    c = c / 255.0
    if c <= 0.03928:
        return c / 12.92
    return ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(rgb: Tuple[int, int, int]) -> float:
    r, g, b = rgb
    R = srgb_to_linear(r)
    G = srgb_to_linear(g)
    B = srgb_to_linear(b)
    return 0.2126 * R + 0.7152 * G + 0.0722 * B


def contrast_ratio(fg: Tuple[int, int, int], bg: Tuple[int, int, int]) -> float:
    L1 = relative_luminance(fg)
    L2 = relative_luminance(bg)
    L_light = max(L1, L2)
    L_dark = min(L1, L2)
    return (L_light + 0.05) / (L_dark + 0.05)


def passes_wcag_aa(cr: float, font_size_px: Optional[float] = None, bold: bool = False) -> bool:
    """WCAG 1.4.3 thresholds: 4.5:1 normal text, 3:1 for large (>=18pt ~ 24px or 14pt bold ~ 18.66px)."""
    if font_size_px is None:
        return cr >= 4.5
    # rough mapping: assume 1pt ≈ 1.3333px
    pt = font_size_px / 1.3333
    is_large = pt >= 18 or (bold and pt >= 14)
    return cr >= (3.0 if is_large else 4.5)


# =====================
# HTML analysis helpers
# =====================
TEXT_TAGS = {"p", "span", "a", "li", "button", "label", "small", "em", "strong", "div", "h1", "h2", "h3", "h4", "h5", "h6"}


def get_inline_style_color(tag: Tag) -> Tuple[Tuple[int, int, int], Tuple[int, int, int], Optional[float], bool]:
    style = tag.get("style", "") or ""
    # Very light-weight parsing; we do NOT compute cascading CSS or external styles.
    fg = (0, 0, 0)
    bg = (255, 255, 255)
    size_px: Optional[float] = None
    bold = False

    parts = [p.strip() for p in style.split(";") if p.strip()]
    for p in parts:
        if ":" not in p:
            continue
        k, v = p.split(":", 1)
        k = k.strip().lower()
        v = v.strip().lower()
        if k == "color":
            fg = parse_css_color(v)
        elif k == "background-color":
            bg = parse_css_color(v)
        elif k == "font-weight":
            bold = ("bold" in v) or (v.isdigit() and int(v) >= 600)
        elif k == "font-size":
            try:
                if v.endswith("px"):
                    size_px = float(v[:-2])
                elif v.endswith("rem"):
                    size_px = float(v[:-3]) * 16.0
                elif v.endswith("em"):
                    size_px = float(v[:-2]) * 16.0
            except Exception:
                pass

    return fg, bg, size_px, bold


@dataclass
class ContrastIssue:
    tag: str
    text: str
    fg: str
    bg: str
    ratio: float
    size_px: Optional[float]
    bold: bool
    selector_hint: str


@dataclass
class ImgAltIssue:
    src: str
    suggestion: str


def analyze_html(url: str) -> Dict:
    res = requests.get(url, timeout=15)
    res.raise_for_status()
    soup = BeautifulSoup(res.text, "html.parser")

    contrast_issues: List[ContrastIssue] = []
    text_checked = 0
    pass_count = 0

    for el in soup.find_all(TEXT_TAGS):
        # Skip if no visible text
        text = (el.get_text(strip=True) or "")[:120]
        if not text:
            continue
        text_checked += 1
        fg_rgb, bg_rgb, size_px, bold = get_inline_style_color(el)
        cr = contrast_ratio(fg_rgb, bg_rgb)
        if passes_wcag_aa(cr, size_px, bold):
            pass_count += 1
        else:
            contrast_issues.append(
                ContrastIssue(
                    tag=el.name,
                    text=text,
                    fg="#%02x%02x%02x" % fg_rgb,
                    bg="#%02x%02x%02x" % bg_rgb,
                    ratio=round(cr, 2),
                    size_px=size_px,
                    bold=bold,
                    selector_hint=(el.get("id") or el.get("class") or "") and str(el)[:120]
                )
            )

    # Image alt checks
    img_issues: List[ImgAltIssue] = []
    for img in soup.find_all("img"):
        alt = (img.get("alt") or "").strip()
        if alt == "":
            src = img.get("src") or ""
            full_src = urljoin(url, src)
            img_issues.append(ImgAltIssue(src=full_src, suggestion="Add descriptive alt text, e.g., alt=\"Council logo\""))

    # Simple score: (passes / checked)
    score = 0.0
    if text_checked > 0:
        score = (pass_count / text_checked) * 100.0

    return {
        "score": round(score, 2),
        "checked": text_checked,
        "pass_count": pass_count,
        "contrast_issues": [asdict(i) for i in contrast_issues],
        "img_alt_issues": [asdict(i) for i in img_issues],
        "html_size_bytes": len(res.text.encode("utf-8")),
        "title": soup.title.string if soup.title else "",
    }


# =====================
# PDF Accessibility scan
# =====================
@dataclass
class PdfAccessibility:
    url: str
    pages: int
    is_tagged: bool
    image_count: int
    alt_text_count: int
    notes: str


def analyze_pdf(url: str) -> PdfAccessibility:
    # Download to bytes
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    bio = io.BytesIO(r.content)
    reader = PdfReader(bio)

    # Heuristics:
    # - Tagged PDF usually has /StructTreeRoot in catalog
    root = reader.trailer.get("/Root", {})
    is_tagged = bool(root.get("/StructTreeRoot"))

    pages = len(reader.pages)

    # Count images & look for any /Alt occurrences by scanning raw XObject names
    image_count = 0
    alt_text_count = 0

    try:
        for i in range(pages):
            page = reader.pages[i]
            resources = page.get("/Resources") or {}
            xobj = resources.get("/XObject") or {}
            if hasattr(xobj, "items"):
                for name, obj in xobj.items():
                    try:
                        subtype = obj.get("/Subtype")
                        if subtype and subtype == "/Image":
                            image_count += 1
                            # Alt text is typically in structure tree, not directly here; as a heuristic, count none here.
                    except Exception:
                        pass
    except Exception:
        pass

    # Heuristic search of raw bytes for "/Alt" keys (very rough)
    try:
        raw = r.content.decode("latin-1", errors="ignore")
        alt_text_count = raw.count("/Alt(") + raw.count("/Alt ")
    except Exception:
        alt_text_count = 0

    notes = "Tagged PDF" if is_tagged else "PDF appears untagged (no /StructTreeRoot)"

    return PdfAccessibility(url=url, pages=pages, is_tagged=is_tagged, image_count=image_count, alt_text_count=alt_text_count, notes=notes)


# =====================
# Fix Suggestions
# =====================

def suggest_fix_for_contrast(issue: Dict) -> str:
    # Increase contrast by darkening text or lightening background. Simple suggestion:
    return (
        f"For element `{issue['tag']}` with ratio {issue['ratio']}:\n"
        f"- If text is too light, try a darker color (e.g., color: #111111).\n"
        f"- If background is too dark, lighten it (e.g., background-color: #FFFFFF).\n"
        f"- Example CSS: `{issue['tag']} {{ color: #111111; }}`\n"
    )


def suggest_fix_for_img_alt(img_issue: Dict) -> str:
    return (
        f"Image `{img_issue['src']}` is missing alt text. Add a meaningful description, e.g.:\n"
        f"`<img src=\"{img_issue['src']}\" alt=\"Wyndham City Council logo\">`"
    )


# =====================
# Report (PDF) builder
# =====================

def build_pdf_report(path: str, brand: Dict, target_url: str, html_result: Optional[Dict], pdf_results: List[PdfAccessibility]):
    doc = SimpleDocTemplate(path, pagesize=A4, leftMargin=24, rightMargin=24, topMargin=24, bottomMargin=24)
    styles = getSampleStyleSheet()
    styles["Normal"].fontName = "Helvetica"
    styles["Heading1"].alignment = TA_LEFT
    story = []

    # Header with logo
    try:
        logo_img = ImageReader(requests.get(brand["logo"], timeout=10).content)
        story.append(utils.Image(logo_img, width=120, height=30))
    except Exception:
        story.append(Paragraph(f"<b>{brand['name']}</b>", styles["Heading1"]))

    story.append(Spacer(1, 8))
    story.append(Paragraph("<b>Accessibility Audit Report</b>", styles["Heading1"]))
    story.append(Spacer(1, 6))
    story.append(Paragraph(f"Target URL: {target_url}", styles["Normal"]))
    story.append(Paragraph(f"Generated: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}", styles["Normal"]))
    story.append(Spacer(1, 12))

    if html_result:
        story.append(Paragraph("<b>WCAG 1.4.3 Color Contrast (HTML)</b>", styles["Heading2"]))
        summary = [["Checked elements", html_result["checked"]],
                   ["Passed", html_result["pass_count"]],
                   ["Score (%)", html_result["score"]]]
        t = Table(summary, colWidths=[140, 340])
        t.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), colors.HexColor(brand["primary"])),
            ('TEXTCOLOR', (0,0), (-1,0), colors.white),
            ('INNERGRID', (0,0), (-1,-1), 0.25, colors.grey),
            ('BOX', (0,0), (-1,-1), 0.25, colors.grey)
        ]))
        story.append(t)
        story.append(Spacer(1, 8))

        if html_result["contrast_issues"]:
            story.append(Paragraph("<b>Contrast Issues (examples)</b>", styles["Heading3"]))
            rows = [["Tag", "Text (truncated)", "FG", "BG", "Ratio"]]
            for i in html_result["contrast_issues"][:12]:
                rows.append([i["tag"], i["text"][:60], i["fg"], i["bg"], i["ratio"]])
            tt = Table(rows, colWidths=[50, 250, 60, 60, 60])
            tt.setStyle(TableStyle([
                ('INNERGRID', (0,0), (-1,-1), 0.25, colors.grey),
                ('BOX', (0,0), (-1,-1), 0.25, colors.grey)
            ]))
            story.append(tt)
            story.append(Spacer(1, 8))

        if html_result["img_alt_issues"]:
            story.append(Paragraph("<b>Images Missing Alt Text (examples)</b>", styles["Heading3"]))
            rows = [["Image src"]]
            for i in html_result["img_alt_issues"][:12]:
                rows.append([i["src"]])
            tt = Table(rows, colWidths=[480])
            tt.setStyle(TableStyle([
                ('INNERGRID', (0,0), (-1,-1), 0.25, colors.grey),
                ('BOX', (0,0), (-1,-1), 0.25, colors.grey)
            ]))
            story.append(tt)
            story.append(Spacer(1, 12))

    if pdf_results:
        story.append(Paragraph("<b>PDF Accessibility (WCAG-related)</b>", styles["Heading2"]))
        rows = [["PDF URL", "Pages", "Tagged?", "Images", "Alt texts (heuristic)", "Notes"]]
        for r in pdf_results[:10]:
            rows.append([r.url, r.pages, "Yes" if r.is_tagged else "No", r.image_count, r.alt_text_count, r.notes])
        t = Table(rows, colWidths=[180, 40, 50, 50, 70, 120])
        t.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), colors.HexColor(brand["primary"])),
            ('TEXTCOLOR', (0,0), (-1,0), colors.white),
            ('INNERGRID', (0,0), (-1,-1), 0.25, colors.grey),
            ('BOX', (0,0), (-1,-1), 0.25, colors.grey)
        ]))
        story.append(t)

    doc.build(story)


# =====================
# Persistence for dashboard
# =====================

def append_audit_row(row: Dict):
    df = None
    if os.path.exists(AUDITS_CSV):
        try:
            df = pd.read_csv(AUDITS_CSV)
        except Exception:
            df = None
    if df is None:
        df = pd.DataFrame(columns=["timestamp", "url", "html_score", "contrast_issues", "img_alt_issues", "pdfs_scanned", "pdfs_tagged"])
    df.loc[len(df)] = row
    df.to_csv(AUDITS_CSV, index=False)


def load_audits() -> pd.DataFrame:
    if os.path.exists(AUDITS_CSV):
        try:
            return pd.read_csv(AUDITS_CSV)
        except Exception:
            pass
    return pd.DataFrame(columns=["timestamp", "url", "html_score", "contrast_issues", "img_alt_issues", "pdfs_scanned", "pdfs_tagged"])


# =====================
# UI
# =====================

primary = WYNDHAM["primary"]

st.markdown(
    f"""
    <style>
    .wyndham-header {{
        display:flex; align-items:center; gap:12px; padding:10px 0 6px 0;
        border-bottom: 3px solid {primary}20;
    }}
    .wyndham-badge {{
        background:{primary}; color:white; padding:2px 8px; border-radius:999px; font-size:12px;
    }}
    .small-muted {{ color:#6b7280; font-size:12px; }}
    </style>
    """,
    unsafe_allow_html=True,
)

col_logo, col_title = st.columns([1,4])
with col_logo:
    try:
        st.image(WYNDHAM["logo"], width=160)
    except Exception:
        st.write("**Wyndham City Council**")
with col_title:
    st.markdown("<div class='wyndham-header'><h2>Accessibility Auditor — WCAG 2.2 AA</h2> <span class='wyndham-badge'>Wyndham-specialized</span></div>", unsafe_allow_html=True)
    st.caption("Quick-check color contrast, image alts, and PDF tagging. Generate branded reports and track improvements over time.")

# Input
with st.sidebar:
    st.markdown("### Scan Settings")
    target_url = st.text_input("Page URL to scan (HTML)", value=WYNDHAM["links"]["Home"])
    st.markdown("**Optional: PDF URLs (one per line)**")
    pdf_urls_text = st.text_area("PDF URLs", height=120, placeholder="https://.../report.pdf\nhttps://.../policy.pdf")
    st.markdown("---")
    st.markdown("**Quick Links**")
    for label, link in WYNDHAM["links"].items():
        st.markdown(f"- [{label}]({link})")

col1, col2 = st.columns([2,1])

with col1:
    st.subheader("1) Run Audit")
    run_html = st.button("Scan HTML (Contrast & Alts)")
    run_pdfs = st.button("Scan PDFs (Tagging & Alts)")

    html_result = st.session_state.get("html_result")
    pdf_results: List[PdfAccessibility] = st.session_state.get("pdf_results", [])

    if run_html and target_url:
        try:
            with st.spinner("Scanning HTML for contrast and alt text issues..."):
                html_result = analyze_html(target_url)
                st.session_state["html_result"] = html_result
        except Exception as e:
            st.error(f"HTML scan failed: {e}")

    if run_pdfs:
        pdf_results = []
        for line in (pdf_urls_text or "").splitlines():
            u = line.strip()
            if not u:
                continue
            try:
                with st.spinner(f"Analyzing PDF: {u}"):
                    pdf_results.append(analyze_pdf(u))
            except Exception as e:
                st.warning(f"Failed PDF: {u} — {e}")
        st.session_state["pdf_results"] = pdf_results

    # Show results
    if html_result:
        st.markdown("### HTML — WCAG 1.4.3 Color Contrast")
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Score (%)", html_result["score"])
        m2.metric("Checked", html_result["checked"])
        m3.metric("Passed", html_result["pass_count"])
        m4.metric("Alt issues", len(html_result["img_alt_issues"]))

        with st.expander("Contrast fails (examples)"):
            if html_result["contrast_issues"]:
                df_ci = pd.DataFrame(html_result["contrast_issues"])
                st.dataframe(df_ci)
                st.info("Fix suggestions are listed below.")
            else:
                st.success("No contrast issues found in inline-styled elements scanned.")

        with st.expander("Images missing alt text"):
            if html_result["img_alt_issues"]:
                df_ai = pd.DataFrame(html_result["img_alt_issues"])
                st.dataframe(df_ai)
            else:
                st.success("No images without alt text detected in this page.")

        st.markdown("### HTML — Auto Fix Suggestions")
        if html_result["contrast_issues"]:
            for issue in html_result["contrast_issues"][:10]:
                st.code(suggest_fix_for_contrast(issue))
        if html_result["img_alt_issues"]:
            for issue in html_result["img_alt_issues"][:5]:
                st.code(suggest_fix_for_img_alt(issue))

    if pdf_results:
        st.markdown("### PDF Accessibility Summary")
        df_pdf = pd.DataFrame([asdict(r) for r in pdf_results])
        st.dataframe(df_pdf)

    if (html_result or pdf_results):
        # Append to dashboard history
        if st.button("Save to Dashboard History"):
            row = {
                "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
                "url": target_url or "",
                "html_score": html_result["score"] if html_result else None,
                "contrast_issues": len(html_result["contrast_issues"]) if html_result else 0,
                "img_alt_issues": len(html_result["img_alt_issues"]) if html_result else 0,
                "pdfs_scanned": len(pdf_results),
                "pdfs_tagged": sum(1 for p in pdf_results if p.is_tagged)
            }
            append_audit_row(row)
            st.success("Saved! View trends in the Dashboard panel →")

with col2:
    st.subheader("2) Export Report")
    if st.button("Generate PDF Report (Wyndham-branded)"):
        html_result = st.session_state.get("html_result")
        pdf_results = st.session_state.get("pdf_results", [])
        if not (html_result or pdf_results):
            st.warning("Run a scan first.")
        else:
            out_path = os.path.join(DATA_DIR, f"audit_{int(time.time())}.pdf")
            try:
                build_pdf_report(out_path, WYNDHAM, target_url or "(none)", html_result, pdf_results)
                with open(out_path, "rb") as f:
                    st.download_button("Download Report PDF", data=f.read(), file_name=os.path.basename(out_path), mime="application/pdf")
            except Exception as e:
                st.error(f"Report failed: {e}")

st.markdown("---")

# =====================
# Dashboard — track over time
# =====================
st.subheader("Dashboard — Improvements Over Time")

df_hist = load_audits()
if df_hist.empty:
    st.info("No history yet. Run a scan and click 'Save to Dashboard History'.")
else:
    # Convert timestamp
    try:
        df_hist["timestamp"] = pd.to_datetime(df_hist["timestamp"])  # type: ignore
    except Exception:
        pass

    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**HTML Score (%)**")
        st.line_chart(df_hist.set_index("timestamp")["html_score"].fillna(method="ffill"))
    with c2:
        st.markdown("**Contrast Issues (count)**")
        st.line_chart(df_hist.set_index("timestamp")["contrast_issues"].fillna(0))

    st.markdown("**Recent Audits**")
    st.dataframe(df_hist.sort_values("timestamp", ascending=False).head(20))

st.caption("Note: HTML contrast analysis is based on inline styles only (quick-check). Full WCAG audit requires computed styles and keyboard/ARIA tests.")
