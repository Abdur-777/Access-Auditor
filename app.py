# app.py — Accessibility Auditor (WCAG quick‑check) with Wyndham styling
# Run: streamlit run app.py
# Required in requirements.txt: streamlit, requests, beautifulsoup4, reportlab

import re, time, math
from datetime import datetime
from urllib.parse import urlparse
from typing import List, Dict, Tuple, Optional

import requests
from bs4 import BeautifulSoup, Tag
import streamlit as st

# Must be the first Streamlit call in the script
st.set_page_config(page_title="Accessibility Auditor — WCAG 2.2 AA", layout="wide")

# =========================
# Branding / Styling (Wyndham blue, no logo)
# =========================
WYNDHAM_BLUE = "#003B73"


def inject_brand_css():
    st.markdown(
        f"""
        <style>
          :root {{ --wyndham: {WYNDHAM_BLUE}; }}
          .stButton>button, .stDownloadButton>button {{
            background: var(--wyndham) !important; color:#fff !important; border:0 !important;
            border-radius:12px !important; padding:8px 14px !important;
          }}
          .wy-accents h1, .wy-accents h2, .wy-accents h3, .wy-accents h4 {{ color: var(--wyndham); }}
          .wy-pill {{
            display:inline-block; background: rgba(0,59,115,.08); color: var(--wyndham);
            padding:2px 10px; border-radius:999px; font-weight:600; font-size:12px;
          }}
          /* tidy up expanders */
          details>summary {{font-weight:600;}}
        </style>
        """,
        unsafe_allow_html=True,
    )


# =========================
# Fetch helpers (browser-like headers + cache)
# =========================
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
}


@st.cache_data(ttl=600, show_spinner=False)
def fetch_html(url: str, timeout: int = 20) -> str:
    r = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
    r.raise_for_status()
    return r.text


# =========================
# Contrast math (inline-only)
# =========================

HEX_RE = re.compile(r"#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})")
RGB_RE = re.compile(r"rgb\((\d+),\s*(\d+),\s*(\d+)\)")
RGBA_RE = re.compile(r"rgba\((\d+),\s*(\d+),\s*(\d+),\s*(0|0?\.\d+|1)\)")


def parse_color(value: str) -> Optional[Tuple[int, int, int]]:
    if not value:
        return None
    value = value.strip()
    m = HEX_RE.search(value)
    if m:
        s = m.group(1)
        if len(s) == 3:
            r = int(s[0] * 2, 16)
            g = int(s[1] * 2, 16)
            b = int(s[2] * 2, 16)
        else:
            r = int(s[0:2], 16)
            g = int(s[2:4], 16)
            b = int(s[4:6], 16)
        return (r, g, b)
    m = RGB_RE.search(value)
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = RGBA_RE.search(value)
    if m:
        r, g, b, a = int(m.group(1)), int(m.group(2)), int(m.group(3)), float(m.group(4))
        # If fully transparent, treat as None
        if a == 0:
            return None
        return (r, g, b)
    # Named colors (very small subset)
    NAMED = {
        "black": (0, 0, 0),
        "white": (255, 255, 255),
        "gray": (128, 128, 128),
        "grey": (128, 128, 128),
        "red": (255, 0, 0),
    }
    return NAMED.get(value.lower())


def rel_luminance(rgb: Tuple[int, int, int]) -> float:
    def _lin(c):
        c = c / 255.0
        return c / 12.92 if c <= 0.03928 * 255 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = rgb
    R, G, B = _lin(r), _lin(g), _lin(b)
    return 0.2126 * R + 0.7152 * G + 0.0722 * B


def contrast_ratio(c1: Tuple[int, int, int], c2: Tuple[int, int, int]) -> float:
    L1 = rel_luminance(c1)
    L2 = rel_luminance(c2)
    lighter = max(L1, L2)
    darker = min(L1, L2)
    return (lighter + 0.05) / (darker + 0.05)


# =========================
# Alt suggestions
# =========================
DECORATIVE_HINTS = re.compile(r"(border|spacer|decor|sprite|shadow|corner|bg|badge|icon)", re.I)


def suggest_alt(src: str, width: Optional[int] = None, height: Optional[int] = None, site_name: str = "") -> Tuple[str, str]:
    """Return (alt_text, classification) classification in {'decorative','logo','descriptive'}"""
    path = urlparse(src).path if src else ""
    fname = path.split("/")[-1] if path else ""
    stem = re.sub(r"\.(png|jpg|jpeg|gif|svg|webp)$", "", fname, flags=re.I)
    tiny = (width and width < 24) or (height and height < 24)

    if tiny or DECORATIVE_HINTS.search(stem):
        return ("", "decorative")
    if re.search(r"logo", stem or "", re.I):
        name = site_name.strip() or "Site"
        return (f"{name} logo", "logo")
    human = re.sub(r"[-_]+", " ", stem).strip().capitalize() if stem else "Image"
    human = (human[:77] + "…") if len(human) > 80 else human
    return (human or "Image", "descriptive")


# =========================
# Analysis: find inline styles and image alts
# =========================

def analyze_html(html: str, assume_bg: Tuple[int, int, int] = (255, 255, 255), site_name: str = "") -> Dict:
    soup = BeautifulSoup(html, "html.parser")

    # Text nodes with inline style color/background
    contrast_checked = 0
    contrast_failed = 0
    contrast_examples: List[Dict] = []
    for el in soup.find_all(True):
        style = (el.get("style") or "").lower()
        if not style:
            continue
        color_m = re.search(r"color\s*:\s*([^;]+)", style)
        bg_m = re.search(r"background(?:-color)?\s*:\s*([^;]+)", style)
        if not color_m:
            continue
        fg = parse_color(color_m.group(1))
        bg = parse_color(bg_m.group(1)) if bg_m else assume_bg
        if not fg or not bg:
            continue
        ratio = contrast_ratio(fg, bg)
        contrast_checked += 1
        # Assume normal text AA threshold 4.5:1
        if ratio < 4.5:
            contrast_failed += 1
            if len(contrast_examples) < 8:
                contrast_examples.append({
                    "text": (el.get_text(strip=True) or "(no text)")[:80],
                    "ratio": round(ratio, 2),
                    "tag": el.name,
                })

    # Images & alt
    img_nodes = soup.find_all("img")
    alt_issues: List[Dict] = []
    seen_src = set()
    for img in img_nodes:
        src = img.get("src") or ""
        if not src:
            continue
        if src in seen_src:
            continue
        seen_src.add(src)
        current_alt = (img.get("alt") or "").strip()
        w = int(img.get("width") or 0) or None
        h = int(img.get("height") or 0) or None
        alt_suggestion, cls = suggest_alt(src, w, h, site_name=site_name)
        # Flag missing, generic, or decorative that should be empty alt
        if current_alt == "" or current_alt.lower() in {"image", "photo", "spacer", "graphic"}:
            alt_issues.append({"src": src, "suggested_alt": alt_suggestion, "classification": cls})

    return {
        "contrast_checked": contrast_checked,
        "contrast_failed": contrast_failed,
        "contrast_examples": contrast_examples,
        "alt_issues": alt_issues,
        "img_count": len(img_nodes),
    }


# =========================
# Scoring
# =========================

def compute_scores(contrast_checked: int, contrast_failed: int, alt_issues_count: int) -> Dict:
    contrast_score = 100.0 if contrast_checked == 0 else max(0.0, 100.0 * (1 - (contrast_failed / max(1, contrast_checked))))
    overall = max(0.0, contrast_score - min(40.0, float(alt_issues_count)))
    return {"contrast_score": round(contrast_score, 1), "overall_score": round(overall, 1)}


# =========================
# PDF export (ReportLab)
# =========================
from reportlab.lib import colors
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.units import mm


def export_pdf_wyndham(filepath: str, url: str, scores: Dict, contrast_checked: int, contrast_failed: int, alt_issues: List[Dict], notes: str = ""):
    doc = SimpleDocTemplate(filepath, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=18 * mm, bottomMargin=18 * mm)
    styles = getSampleStyleSheet()
    title = ParagraphStyle("title", parent=styles["Title"], textColor=HexColor(WYNDHAM_BLUE))
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], textColor=HexColor(WYNDHAM_BLUE))
    body = styles["BodyText"]
    story = []

    story.append(Paragraph("<b>Accessibility Auditor — WCAG 2.2 AA</b>", title))
    story.append(Paragraph(datetime.now().strftime("%Y-%m-%d %H:%M"), body))
    story.append(Spacer(1, 6))
    story.append(Paragraph(f"<b>Target URL:</b> {url}", body))
    story.append(Spacer(1, 10))

    data = [
        ["Overall Score", f"{scores['overall_score']}"],
        ["Contrast Score (%)", f"{scores['contrast_score']}"],
        ["Elements Checked (contrast)", f"{contrast_checked}"],
        ["Contrast Fails (inline-only)", f"{contrast_failed}"],
        ["Alt Issues (count)", f"{len(alt_issues)}"],
    ]
    tbl = Table(data, hAlign="LEFT", colWidths=[70 * mm, 70 * mm])
    tbl.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), HexColor(WYNDHAM_BLUE)),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.whitesmoke, colors.lightgrey]),
            ]
        )
    )
    story.append(tbl)
    story.append(Spacer(1, 12))

    story.append(Paragraph("Alt Issues — examples", h2))
    if alt_issues:
        rows = [["Image src (truncated)", "Suggested alt", "Type"]]
        for issue in alt_issues[:10]:
            src = (issue.get("src") or "")[-80:]
            alt = issue.get("suggested_alt", "")
            kind = issue.get("classification", "")
            rows.append([src, alt, kind])
        t2 = Table(rows, hAlign="LEFT", colWidths=[85 * mm, 65 * mm, 25 * mm])
        t2.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), HexColor(WYNDHAM_BLUE)),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ]
            )
        )
        story.append(t2)
    else:
        story.append(Paragraph("No alt issues detected.", body))
    story.append(Spacer(1, 10))

    story.append(Paragraph("Methods & Limitations", h2))
    story.append(
        Paragraph(
            "This quick-check evaluates inline color contrast only. A full audit should also include computed style contrast, "
            "keyboard navigation, focus order, semantic landmarks, ARIA roles/states, form labels, and media alternatives.",
            body,
        )
    )
    if notes:
        story.append(Spacer(1, 6))
        story.append(Paragraph(f"<i>Notes:</i> {notes}", body))

    doc.build(story)


# =========================
# UI
# =========================


# Inject brand CSS after set_page_config
inject_brand_css()

st.markdown("""
<div class="wy-accents">
  <h1>Accessibility Auditor — WCAG 2.2 AA <span class="wy-pill">Wyndham</span></h1>
  <p>Quick‑check contrast (inline styles), image alts, and export a Wyndham‑branded PDF. Use the Test Panel for reliable demo pages.</p>
</div>
""", unsafe_allow_html=True)

# Sidebar inputs
with st.sidebar:
    st.header("Scan Settings")
    url_input = st.text_input("Page URL to scan (HTML)", value=st.session_state.get("url_input", ""), placeholder="https://example.com/page")

    pdf_urls_raw = st.text_area("PDF URLs (one per line)", placeholder="https://…/policy.pdf")

    with st.expander("Test Panel", expanded=False):
        colA, colB = st.columns(2)
        if colA.button("W3C Bad (Before)"):
            url_input = "https://www.w3.org/WAI/demos/bad/before/home.html"
            st.session_state["url_input"] = url_input
        if colB.button("W3C Good (After)"):
            url_input = "https://www.w3.org/WAI/demos/bad/after/home.html"
            st.session_state["url_input"] = url_input

    with st.expander("Paste HTML instead (fallback)", expanded=False):
        pasted_html = st.text_area("Raw HTML", height=160, placeholder="Paste a full HTML document here…")
        use_pasted = st.checkbox("Use pasted HTML for this scan", value=False)

# Session history store
if "history" not in st.session_state:
    st.session_state["history"] = []  # each item: dict with timestamp, url, scores, counts

# Run Audit row
st.subheader("Run Audit")
col1, col2, col3 = st.columns([1, 1, 1])

scan_html_clicked = col1.button("\U0001F50D Scan HTML (Contrast & Alts)")
scan_pdfs_clicked = col2.button("\U0001F4C1 Scan PDFs (Tagging & Alts)")
export_pdf_clicked = col3.button("\U0001F4BE Generate PDF (Wyndham‑branded)")

st.markdown("---")

# Results containers
results_box = st.container()

latest_run = st.session_state.get("latest_run")

# ======== HTML scan ========
if scan_html_clicked:
    if use_pasted and pasted_html.strip():
        html = pasted_html
        url_for_report = url_input or "(pasted HTML)"
        ok = True
        err_msg = ""
    elif url_input.strip():
        try:
            with st.spinner("Fetching page…"):
                html = fetch_html(url_input.strip())
            url_for_report = url_input.strip()
            ok = True
            err_msg = ""
        except Exception as e:
            ok = False
            err_msg = str(e)
    else:
        ok = False
        err_msg = "Please provide a URL or paste HTML."

    if not ok:
        st.error(f"HTML scan failed: {err_msg}")
    else:
        with st.spinner("Analyzing HTML…"):
            report = analyze_html(html, assume_bg=(255, 255, 255), site_name="Wyndham City Council")
            scores = compute_scores(report["contrast_checked"], report["contrast_failed"], len(report["alt_issues"]))
            latest_run = {
                "timestamp": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                "url": url_for_report,
                "scores": scores,
                "contrast_checked": report["contrast_checked"],
                "contrast_failed": report["contrast_failed"],
                "alt_issues": report["alt_issues"],
                "contrast_examples": report["contrast_examples"],
            }
            st.session_state["latest_run"] = latest_run

# ======== PDF scan (placeholder) ========
if scan_pdfs_clicked:
    urls = [u.strip() for u in pdf_urls_raw.splitlines() if u.strip()]
    if not urls:
        st.warning("Add PDF URLs (one per line) in the sidebar.")
    else:
        st.info("PDF tagging/alt scanning is not implemented in this quick‑check build. The HTML audit is fully functional.")

# ======== Export PDF ========
if export_pdf_clicked:
    run = st.session_state.get("latest_run")
    if not run:
        st.warning("Run an HTML scan first.")
    else:
        path = "audit_report.pdf"
        export_pdf_wyndham(
            path,
            url=run["url"],
            scores=run["scores"],
            contrast_checked=run["contrast_checked"],
            contrast_failed=run["contrast_failed"],
            alt_issues=run["alt_issues"],
        )
        with open(path, "rb") as f:
            st.download_button("Download PDF report", f, file_name="audit_report.pdf")

# ======== Show results (if any) ========
if latest_run:
    with results_box:
        st.subheader("Results")
        m1, m2, m3 = st.columns(3)
        m1.metric("Contrast Score (%)", latest_run["scores"]["contrast_score"])
        m2.metric("Overall Score", latest_run["scores"]["overall_score"])
        m3.metric("Alt issues", len(latest_run["alt_issues"]))

        # Expanders
        with st.expander("Contrast fails (examples)"):
            if latest_run["contrast_failed"] == 0:
                st.write("No inline-style contrast failures found.")
            else:
                st.write("Examples of elements that failed 1.4.3 (AA) threshold:")
                for ex in latest_run.get("contrast_examples", [])[:10]:
                    st.write(f"• <{ex.get('tag','?')}> ratio {ex.get('ratio','?')}: {ex.get('text','')} ")

        with st.expander("Images missing alt text"):
            if not latest_run["alt_issues"]:
                st.write("No missing/weak alt text detected.")
            else:
                for item in latest_run["alt_issues"]:
                    st.markdown(f"- `{item['src']}` → **{item['suggested_alt'] or '(decorative)'}** *({item['classification']})*")

        # Auto Fix Suggestions
        st.subheader("Auto Fix Suggestions")
        if not latest_run["alt_issues"]:
            st.write("No suggestions. Nice!")
        else:
            for issue in latest_run["alt_issues"][:20]:
                if issue["classification"] == "decorative":
                    suggestion = f"Add alt text: `<img src='{issue['src']}' alt='' aria-hidden='true'>`"
                else:
                    suggestion = f"Add alt text: `<img src='{issue['src']}' alt='{issue['suggested_alt']}'>`"
                st.code(suggestion, language="html")

        # Save run
        if st.button("Save this run to Dashboard"):
            st.session_state["history"].append(
                {
                    "timestamp": latest_run["timestamp"],
                    "url": latest_run["url"],
                    "contrast_score": latest_run["scores"]["contrast_score"],
                    "overall_score": latest_run["scores"]["overall_score"],
                    "contrast_checked": latest_run["contrast_checked"],
                    "contrast_failed": latest_run["contrast_failed"],
                    "alt_issues": len(latest_run["alt_issues"]),
                }
            )
            st.success("Saved. See Dashboard below.")

# ======== Dashboard ========
st.subheader("Dashboard")

hist = st.session_state.get("history", [])
if not hist:
    st.info("No history yet. Run a scan and Save.")
else:
    # Simple table view
    import pandas as pd

    df = pd.DataFrame(hist)
    st.dataframe(df, use_container_width=True)

    # Notes
    st.caption(
        "Notes: Contrast quick‑check uses inline styles only. Full audit requires computed styles, keyboard navigation, and ARIA checks."
    )
