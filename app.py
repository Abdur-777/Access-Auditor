# app.py — Accessibility Auditor (WCAG quick-check) — Council Edition
# Run locally:  streamlit run app.py
# Core deps:    streamlit, requests, beautifulsoup4, reportlab, pandas
# Optional:     playwright (and chromium install) for computed-style contrast

import os
import re
import io
import csv
import zipfile
import shutil
import ipaddress
from datetime import datetime
from typing import List, Dict, Tuple, Optional
from urllib.parse import urlparse

import requests
import pandas as pd
from bs4 import BeautifulSoup
import streamlit as st

# ──────────────────────────────────────────────────────────────────────────────
# Streamlit config (must be first Streamlit call)
# ──────────────────────────────────────────────────────────────────────────────
st.set_page_config(page_title="Accessibility Auditor — WCAG 2.2 AA", layout="wide")

# ──────────────────────────────────────────────────────────────────────────────
# Branding / Settings (env-driven)
# ──────────────────────────────────────────────────────────────────────────────
WYNDHAM_BLUE = os.getenv("BRAND_PRIMARY", "#003B73")
ORG_NAME     = os.getenv("ORG_NAME", "Wyndham City Council")
DATA_DIR     = os.getenv("DATA_DIR", "./data")

# Make sure DATA_DIR exists (used by backups & evidence pack)
os.makedirs(DATA_DIR, exist_ok=True)

def inject_brand_css():
    st.markdown(
        f"""
        <style>
          :root {{ --brand: {WYNDHAM_BLUE}; }}
          .stButton>button, .stDownloadButton>button {{
            background: var(--brand) !important; color:#fff !important; border:0 !important;
            border-radius:12px !important; padding:8px 14px !important;
          }}
          .wy-accents h1, .wy-accents h2, .wy-accents h3, .wy-accents h4 {{ color: var(--brand); }}
          .wy-pill {{
            display:inline-block; background: color-mix(in srgb, var(--brand) 12%, white);
            color: var(--brand); padding:2px 10px; border-radius:999px; font-weight:600; font-size:12px;
          }}
          [data-testid="stMetricValue"]{{ font-weight:700; }}
          details>summary {{font-weight:600;}}
          .muted {{ color:#666; font-size:13px; }}
        </style>
        """,
        unsafe_allow_html=True,
    )

inject_brand_css()

# ──────────────────────────────────────────────────────────────────────────────
# Fetch helpers (browser-like headers + cache)
# ──────────────────────────────────────────────────────────────────────────────
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
}

@st.cache_data(ttl=600, show_spinner=False)
def fetch_html(url: str, timeout: int = 20) -> str:
    r = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
    if r.status_code in (403, 429):
        raise RuntimeError(f"{r.status_code} {r.reason} — site may block scraping; try 'Use computed-style contrast' or Paste HTML.")
    r.raise_for_status()
    text = r.text
    # Cap to ~3MB to protect memory in Streamlit
    return text[:3_000_000]

# ──────────────────────────────────────────────────────────────────────────────
# URL guard (public http/https only)
# ──────────────────────────────────────────────────────────────────────────────
def is_public_http_url(u: str) -> bool:
    try:
        p = urlparse(u)
        if p.scheme not in ("http", "https") or not p.netloc:
            return False
        host = p.hostname or ""
        if host in {"localhost", "127.0.0.1"} or host.endswith((".local", ".lan")):
            return False
        try:
            ip = ipaddress.ip_address(host)
            if ip.is_private or ip.is_loopback or ip.is_link_local:
                return False
        except ValueError:
            pass  # not an IP literal
        return True
    except Exception:
        return False

# ──────────────────────────────────────────────────────────────────────────────
# Optional: computed-style contrast via Playwright (headless Chromium)
# ──────────────────────────────────────────────────────────────────────────────
def analyze_computed_contrast(url: str, level: str = "AA", timeout_ms: int = 30000):
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except Exception as e:
        return {"error": f"Playwright not available: {e}"}

    # Thresholds (includes large text allowance)
    small_t = 7.0 if level == "AAA" else 4.5
    large_t = 4.5 if level == "AAA" else 3.0

    js = f"""
    (() => {{
      const SMALL_T = {small_t}, LARGE_T = {large_t};
      function parse(col){{const c=document.createElement('canvas').getContext('2d');c.fillStyle=col;
        const m=c.fillStyle.match(/[0-9]+/g);return m?m.slice(0,3).map(Number):[0,0,0];}}
      function lum([r,g,b]){{r/=255;g/=255;b/=255;const f=v=>v<=0.03928?v/12.92:Math.pow((v+0.055)/1.055,2.4);
        return 0.2126*f(r)+0.7152*f(g)+0.0722*f(b);}}
      function ratio(f,b){{const L1=Math.max(f,b),L2=Math.min(f,b);return (L1+0.05)/(L2+0.05);}}
      function isLarge(s){{const size=parseFloat(s.fontSize)||0;const w=parseInt(s.fontWeight)||400;
        const bold=w>=700||/bold/i.test(s.fontWeight);return size>=24||(bold&&size>=18.66);}}
      function bg(el){{let n=el;while(n){{const s=getComputedStyle(n);
        if(s.backgroundColor&&s.backgroundColor!=='rgba(0, 0, 0, 0)') return s.backgroundColor;n=n.parentElement;}}
        return 'rgb(255,255,255)';}}
      const els=[...document.querySelectorAll('*')].filter(el=>{{const s=getComputedStyle(el);
        return s && s.color && s.visibility!=='hidden' && s.display!=='none' && (el.textContent||'').trim().length>0;}});
      let failed=0, out=[];
      for(const el of els){{
        const s=getComputedStyle(el); const fg=parse(s.color), bgc=parse(bg(el));
        const cr=ratio(lum(fg),lum(bgc)); const t=isLarge(s)?LARGE_T:SMALL_T;
        if(cr<t){{ failed++; if(out.length<50) out.push({{tag:el.tagName.toLowerCase(),text:(el.textContent||'').trim().slice(0,80),ratio:Math.round(cr*100)/100}}); }}
      }}
      return {{checked:els.length, failed, examples:out}};
    }})()
    """

    try:
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            page = b.new_page()
            page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            data = page.evaluate(js)
            b.close()
            return data
    except Exception as e:
        return {"error": str(e)}

# ──────────────────────────────────────────────────────────────────────────────
# Contrast & color parsing (inline styles)
# ──────────────────────────────────────────────────────────────────────────────
HEX_RE  = re.compile(r"#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})")
RGB_RE  = re.compile(r"rgb\((\d+),\s*(\d+),\s*(\d+)\)")
RGBA_RE = re.compile(r"rgba\((\d+),\s*(\d+),\s*(\d+),\s*(0|0?\.\d+|1)\)")

def parse_dimension(val) -> Optional[int]:
    """Accept 10, '10px', '1.5rem' -> returns approx px int or None."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return int(float(val))
    s = str(val).strip()
    num, dot, sign_allowed = [], False, True
    for ch in s:
        if ch.isdigit():
            num.append(ch); sign_allowed = False
        elif ch == '.' and not dot:
            num.append(ch); dot = True; sign_allowed = False
        elif ch in '+-' and sign_allowed:
            if ch == '-': num.append(ch)
            sign_allowed = False
        elif num:
            break
    try:
        n = ''.join(num)
        return int(float(n)) if n else None
    except Exception:
        return None

def parse_color(value: str) -> Optional[Tuple[int, int, int]]:
    if not value: return None
    value = value.strip()
    m = HEX_RE.search(value)
    if m:
        s = m.group(1)
        if len(s) == 3:
            r = int(s[0]*2,16); g = int(s[1]*2,16); b = int(s[2]*2,16)
        else:
            r = int(s[0:2],16); g = int(s[2:4],16); b = int(s[4:6],16)
        return (r,g,b)
    m = RGB_RE.search(value)
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = RGBA_RE.search(value)
    if m:
        r,g,b,a = int(m.group(1)), int(m.group(2)), int(m.group(3)), float(m.group(4))
        if a == 0: return None
        return (r,g,b)
    NAMED = {"black":(0,0,0), "white":(255,255,255), "gray":(128,128,128), "grey":(128,128,128), "red":(255,0,0)}
    return NAMED.get(value.lower())

def rel_luminance(rgb: Tuple[int, int, int]) -> float:
    def _lin(c):
        c = c/255.0
        return c/12.92 if c <= 0.03928 else ((c+0.055)/1.055)**2.4
    r,g,b = rgb
    R,G,B = _lin(r), _lin(g), _lin(b)
    return 0.2126*R + 0.7152*G + 0.0722*B

def contrast_ratio(c1: Tuple[int,int,int], c2: Tuple[int,int,int]) -> float:
    L1, L2 = rel_luminance(c1), rel_luminance(c2)
    lighter, darker = max(L1,L2), min(L1,L2)
    return (lighter + 0.05) / (darker + 0.05)

# ──────────────────────────────────────────────────────────────────────────────
# Alt text heuristics
# ──────────────────────────────────────────────────────────────────────────────
DECORATIVE_HINTS = re.compile(r"(border|spacer|decor|sprite|shadow|corner|bg|badge|icon)", re.I)
GENERIC_ALTS     = {"image","photo","graphic","pic","icon","spacer"}

def looks_like_filename(s: str) -> bool:
    if not s: return False
    if re.search(r"\.(png|jpe?g|gif|svg|webp)$", s, re.I): return True
    return bool(re.search(r"[a-z0-9_-]", s) and " " not in s)

def normalize_src(src: str) -> str:
    try:
        u = urlparse(src)
        if u.scheme and u.netloc:
            return f"{u.scheme}://{u.netloc}{u.path}"
        return u.path or src
    except Exception:
        return src

def suggest_alt(src: str, width: Optional[int] = None, height: Optional[int] = None, site_name: str = "") -> Tuple[str, str]:
    """Return (alt_text, classification) in {'decorative','logo','descriptive'}"""
    path = urlparse(src).path if src else ""
    fname = path.split("/")[-1] if path else ""
    stem  = re.sub(r"\.(png|jpg|jpeg|gif|svg|webp)$", "", fname, flags=re.I)
    tiny  = (width and width < 24) or (height and height < 24)
    if tiny or DECORATIVE_HINTS.search(stem or ""):
        return ("", "decorative")
    if re.search(r"logo", stem or "", re.I):
        name = site_name.strip() or "Site"
        return (f"{name}", "logo")  # keep SR string clean; 'logo' implied
    human = re.sub(r"[-_]+", " ", stem).strip().capitalize() if stem else "Image"
    human = (human[:77] + "…") if len(human) > 80 else human
    return (human or "Image", "descriptive")

# ──────────────────────────────────────────────────────────────────────────────
# HTML analysis (inline contrast + alt heuristics)
# ──────────────────────────────────────────────────────────────────────────────
def analyze_html(html: str, assume_bg: Tuple[int,int,int]=(255,255,255), site_name: str="", level: str="AA") -> Dict:
    soup = BeautifulSoup(html, "html.parser")
    threshold = 7.0 if level == "AAA" else 4.5

    # Inline contrast (style attributes only)
    contrast_checked = 0
    contrast_failed  = 0
    contrast_examples: List[Dict] = []
    for el in soup.find_all(True):
        style = (el.get("style") or "").lower()
        if not style: continue
        color_m = re.search(r"color\s*:\s*([^;]+)", style)
        bg_m    = re.search(r"background(?:-color)?\s*:\s*([^;]+)", style)
        if not color_m: continue
        fg = parse_color(color_m.group(1))
        bg = parse_color(bg_m.group(1)) if bg_m else assume_bg
        if not fg or not bg: continue
        ratio = contrast_ratio(fg, bg)
        contrast_checked += 1
        if ratio < threshold:
            contrast_failed += 1
            if len(contrast_examples) < 8:
                contrast_examples.append({
                    "text": (el.get_text(strip=True) or "(no text)")[:80],
                    "ratio": round(ratio, 2),
                    "tag": el.name,
                })

    # Image alts
    img_nodes = soup.find_all("img")
    alt_issues: List[Dict] = []
    seen_src = set()
    for img in img_nodes:
        src = img.get("src") or ""
        if not src: continue
        norm = normalize_src(src)
        if norm in seen_src: continue
        seen_src.add(norm)
        current_alt = (img.get("alt") or "").strip()
        w = parse_dimension(img.get("width"))
        h = parse_dimension(img.get("height"))
        alt_suggestion, cls = suggest_alt(src, w, h, site_name=site_name)

        bad_generic  = current_alt.lower() in GENERIC_ALTS
        bad_filename = looks_like_filename(current_alt)

        if current_alt == "" or bad_generic or bad_filename:
            alt_issues.append({"src": src, "suggested_alt": alt_suggestion, "classification": cls})

    # Links that are images only (no text/aria)
    for a in soup.find_all("a"):
        imgs = a.find_all("img")
        if not imgs: continue
        link_text  = (a.get_text(strip=True) or "")
        link_label = (a.get("aria-label") or a.get("title") or "").strip()
        if not link_text and not link_label:
            for _img in imgs:
                cur_alt = (_img.get("alt") or "").strip()
                if cur_alt == "":
                    alt_issues.append({
                        "src": _img.get("src") or "",
                        "suggested_alt": "Add aria-label/title on link (e.g., 'Home') or give the image that label.",
                        "classification": "link_image_no_text",
                    })
                    break

    return {
        "contrast_checked": contrast_checked,
        "contrast_failed":  contrast_failed,
        "contrast_examples": contrast_examples,
        "alt_issues":        alt_issues,
        "img_count":         len(img_nodes),
    }

# ──────────────────────────────────────────────────────────────────────────────
# Scoring
# ──────────────────────────────────────────────────────────────────────────────
def compute_scores(contrast_checked: int, contrast_failed: int, alt_issues_count: int) -> Dict:
    contrast_score = 100.0 if contrast_checked == 0 else max(0.0, 100.0 * (1 - (contrast_failed / max(1, contrast_checked))))
    overall = max(0.0, contrast_score - min(40.0, float(alt_issues_count)))
    return {"contrast_score": round(contrast_score, 1), "overall_score": round(overall, 1)}

# ──────────────────────────────────────────────────────────────────────────────
# PDF export (ReportLab)
# ──────────────────────────────────────────────────────────────────────────────
from reportlab.lib import colors
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.units import mm

def export_pdf_wyndham(filepath: str, url: str, scores: Dict, contrast_checked: int, contrast_failed: int, alt_issues: List[Dict], notes: str = ""):
    doc = SimpleDocTemplate(filepath, pagesize=A4, leftMargin=18*mm, rightMargin=18*mm, topMargin=18*mm, bottomMargin=18*mm)
    styles = getSampleStyleSheet()
    title = ParagraphStyle("title", parent=styles["Title"], textColor=HexColor(WYNDHAM_BLUE))
    h2    = ParagraphStyle("h2", parent=styles["Heading2"], textColor=HexColor(WYNDHAM_BLUE))
    body  = styles["BodyText"]
    story = []

    story.append(Paragraph(f"<b>{ORG_NAME} — Accessibility Auditor (WCAG 2.2)</b>", title))
    story.append(Paragraph(datetime.now().strftime("%Y-%m-%d %H:%M"), body))
    story.append(Spacer(1, 6))
    story.append(Paragraph(f"<b>Target URL:</b> {url}", body))
    story.append(Spacer(1, 10))

    data = [
        ["Overall Score", f"{scores['overall_score']}"],
        ["Contrast Score (%)", f"{scores['contrast_score']}"],
        ["Elements Checked (contrast)", f"{contrast_checked}"],
        ["Contrast Fails", f"{contrast_failed}"],
        ["Images needing alt (count)", f"{len(alt_issues)}"],
    ]
    tbl = Table(data, hAlign="LEFT", colWidths=[75*mm, 75*mm])
    tbl.setStyle(
        TableStyle([
            ("BACKGROUND", (0,0), (-1,0), HexColor(WYNDHAM_BLUE)),
            ("TEXTCOLOR",  (0,0), (-1,0), colors.white),
            ("GRID",       (0,0), (-1,-1), 0.25, colors.grey),
            ("FONTNAME",   (0,0), (-1,0), "Helvetica-Bold"),
            ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.whitesmoke, colors.lightgrey]),
        ])
    )
    story.append(tbl)
    story.append(Spacer(1, 12))

    story.append(Paragraph("Images needing alt — examples", h2))
    if alt_issues:
        rows = [["Image src (truncated)", "Suggested alt", "Type"]]
        for issue in alt_issues[:12]:
            src  = (issue.get("src") or "")[-90:]
            alt  = issue.get("suggested_alt", "")
            kind = issue.get("classification", "")
            rows.append([src, alt, kind])
        t2 = Table(rows, hAlign="LEFT", colWidths=[90*mm, 60*mm, 20*mm])
        t2.setStyle(
            TableStyle([
                ("BACKGROUND", (0,0), (-1,0), HexColor(WYNDHAM_BLUE)),
                ("TEXTCOLOR",  (0,0), (-1,0), colors.white),
                ("GRID",       (0,0), (-1,-1), 0.25, colors.grey),
                ("FONTNAME",   (0,0), (-1,0), "Helvetica-Bold"),
            ])
        )
        story.append(t2)
    else:
        story.append(Paragraph("No issues detected.", body))
    story.append(Spacer(1, 10))

    story.append(Paragraph("Methods & Limitations", h2))
    story.append(Paragraph(
        "This is an automated quick-check. Inline styles are always scanned; computed-style contrast is optional. "
        "A full WCAG assessment also requires manual testing (keyboard navigation, screen reader behavior, forms, media, PDFs, reflow/zoom, reduced motion). "
        "Results indicate potential issues and are not a legal conformance guarantee.",
        body
    ))

    if notes:
        story.append(Spacer(1, 6))
        story.append(Paragraph(f"<i>Notes:</i> {notes}", body))

    doc.build(story)

# ──────────────────────────────────────────────────────────────────────────────
# Header
# ──────────────────────────────────────────────────────────────────────────────
st.markdown(
    f"""
<div class="wy-accents">
  <h1>{ORG_NAME} — Accessibility Auditor <span class="wy-pill">WCAG 2.2</span></h1>
  <p>Quick-check contrast (inline + optional computed) and image alts. Export a council-branded PDF and CSVs.</p>
  <p class="muted"><b>Scope:</b> Automated quick-check only. Manual testing still required for full conformance.</p>
</div>
""",
    unsafe_allow_html=True,
)

# ──────────────────────────────────────────────────────────────────────────────
# Sidebar
# ──────────────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("Scan Settings")
    wcag_level  = st.selectbox("WCAG level", ["AA", "AAA"], index=0, help="Sets contrast threshold (AA 4.5:1, AAA 7:1).")
    use_computed = st.checkbox("Use computed-style contrast (headless)", value=False, help="Requires Playwright + Chromium.")
    url_input   = st.text_input("Page URL to scan (HTML)", value=st.session_state.get("url_input",""), placeholder="https://example.com/page")

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
        use_pasted  = st.checkbox("Use pasted HTML for this scan", value=False)

    st.caption("Please ensure you have permission to scan the provided URL and respect site terms/robots.")
    st.markdown("---")
    auto_save = st.checkbox("Auto-save after scan", value=st.session_state.get("auto_save", True))
    st.session_state["auto_save"] = auto_save

# ──────────────────────────────────────────────────────────────────────────────
# Session state
# ──────────────────────────────────────────────────────────────────────────────
if "history" not in st.session_state:
    st.session_state["history"] = []

latest_run = st.session_state.get("latest_run")

# ──────────────────────────────────────────────────────────────────────────────
# Top action row
# ──────────────────────────────────────────────────────────────────────────────
st.subheader("Run Audit")
col1, col2, col3 = st.columns([1,1,1])
scan_html_clicked  = col1.button("🔍 Scan HTML (Contrast & Alts)")
queue_pdfs_clicked = col2.button("📁 Queue PDFs (stub)", help="Coming soon — add a PDF/UA checker service.")
export_pdf_clicked = col3.button("💾 Generate PDF Report")

st.markdown("---")
results_box = st.container()

# ──────────────────────────────────────────────────────────────────────────────
# HTML scan
# ──────────────────────────────────────────────────────────────────────────────
if scan_html_clicked:
    if use_pasted and 'pasted_html' in locals() and pasted_html.strip():
        html = pasted_html
        url_for_report = url_input or "(pasted HTML)"
        ok, err_msg = True, ""
    elif url_input.strip():
        if not is_public_http_url(url_input.strip()):
            ok, err_msg = False, "URL must be public http(s) (not localhost/private)."
        else:
            try:
                with st.spinner("Fetching page…"):
                    html = fetch_html(url_input.strip())
                url_for_report = url_input.strip()
                ok, err_msg = True, ""
            except Exception as e:
                ok, err_msg = False, str(e)
    else:
        ok, err_msg = False, "Please provide a URL or paste HTML."

    if not ok:
        st.error(f"HTML scan failed: {err_msg}")
    else:
        with st.spinner("Analyzing HTML…"):
            report = analyze_html(html, assume_bg=(255,255,255), site_name=ORG_NAME, level=wcag_level)

            # Optional computed-style contrast
            if use_computed and url_input.strip():
                comp = analyze_computed_contrast(url_input.strip(), level=wcag_level)
                if isinstance(comp, dict) and not comp.get("error"):
                    report["contrast_checked"]  = int(comp.get("checked", report["contrast_checked"]))
                    report["contrast_failed"]   = int(comp.get("failed",  report["contrast_failed"]))
                    report["contrast_examples"] = comp.get("examples", report["contrast_examples"]) or []
                else:
                    st.warning(f"Computed-style contrast unavailable: {comp.get('error','unknown error') if isinstance(comp, dict) else 'error'} — showing inline-only results.")

            scores = compute_scores(report["contrast_checked"], report["contrast_failed"], len(report["alt_issues"]))
            latest_run = {
                "timestamp": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                "url": url_for_report,
                "scores": scores,
                "contrast_checked": report["contrast_checked"],
                "contrast_failed":  report["contrast_failed"],
                "alt_issues":       report["alt_issues"],
                "contrast_examples":report["contrast_examples"],
                "wcag_level":       wcag_level,
            }
            st.session_state["latest_run"] = latest_run

            # Append to audit log (DATA_DIR/audit_log.csv)
            try:
                logp = os.path.join(DATA_DIR, "audit_log.csv")
                new = not os.path.exists(logp)
                with open(logp, "a", newline="", encoding="utf-8") as f:
                    w = csv.writer(f)
                    if new:
                        w.writerow(["timestamp","url","level","contrast_checked","contrast_failed","alt_issues","overall_score","contrast_score"])
                    w.writerow([
                        latest_run["timestamp"], latest_run["url"], wcag_level,
                        latest_run["contrast_checked"], latest_run["contrast_failed"],
                        len(latest_run["alt_issues"]),
                        latest_run["scores"]["overall_score"], latest_run["scores"]["contrast_score"]
                    ])
            except Exception as e:
                st.warning(f"Could not write audit log: {e}")

            # Auto-save summary row to dashboard table
            if st.session_state.get("auto_save", False):
                st.session_state["history"].append(
                    {
                        "timestamp":       latest_run["timestamp"],
                        "url":             latest_run["url"],
                        "level":           wcag_level,
                        "contrast_score":  latest_run["scores"]["contrast_score"],
                        "overall_score":   latest_run["scores"]["overall_score"],
                        "contrast_checked":latest_run["contrast_checked"],
                        "contrast_failed": latest_run["contrast_failed"],
                        "images_needing_alt": len(latest_run["alt_issues"]),
                    }
                )
                st.success("Saved automatically to Dashboard.")

# ──────────────────────────────────────────────────────────────────────────────
# PDF queue (stub)
# ──────────────────────────────────────────────────────────────────────────────
if queue_pdfs_clicked:
    st.info("PDF tagging/alt scanning is not implemented in this quick-check build. This button is a queue stub for future PDF/UA checks.")

# ──────────────────────────────────────────────────────────────────────────────
# Export PDF
# ──────────────────────────────────────────────────────────────────────────────
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
        # Copy into DATA_DIR for backups/evidence
        try:
            shutil.copyfile(path, os.path.join(DATA_DIR, "audit_report.pdf"))
        except Exception:
            pass
        st.toast("Report ready")
        with open(path, "rb") as f:
            st.download_button("Download PDF report", f, file_name="audit_report.pdf")

# ──────────────────────────────────────────────────────────────────────────────
# Results
# ──────────────────────────────────────────────────────────────────────────────
if latest_run:
    with results_box:
        st.subheader("Results")

        # Metrics (side-by-side) with AA/AAA label
        m1, m2, m3 = st.columns(3)
        m1.metric(f"Contrast Score ({latest_run.get('wcag_level','AA')})", latest_run["scores"]["contrast_score"])
        m2.metric("Overall Score", latest_run["scores"]["overall_score"])
        m3.metric("Images needing alt", len(latest_run["alt_issues"]))

        # Build CSVs (always prepare Overall Issues CSV, even if empty)
        alt_rows = latest_run["alt_issues"]
        if alt_rows:
            alt_df = pd.DataFrame(alt_rows)  # columns: src, suggested_alt, classification
            alt_csv = alt_df.to_csv(index=False).encode("utf-8")
            st.download_button("Download CSV (Images needing alt)", data=alt_csv, file_name="alt_issues.csv", mime="text/csv")
            # also persist for evidence pack/backups
            try:
                alt_df.to_csv(os.path.join(DATA_DIR, "alt_issues.csv"), index=False)
            except Exception:
                pass

        overall_rows = []
        for ex in latest_run.get("contrast_examples", [])[:200]:
            overall_rows.append({
                "type":"contrast","tag":ex.get("tag",""),"text":ex.get("text",""),
                "ratio":ex.get("ratio",""),"src":"","suggested_alt":"","classification":""
            })
        for it in latest_run.get("alt_issues", []):
            overall_rows.append({
                "type":"alt","tag":"img","text":"","ratio":"",
                "src":it.get("src",""),"suggested_alt":it.get("suggested_alt",""),"classification":it.get("classification","")
            })

        # Ensure columns exist even if no rows
        if overall_rows:
            overall_df = pd.DataFrame(overall_rows)
        else:
            overall_df = pd.DataFrame(columns=["type","tag","text","ratio","src","suggested_alt","classification"])

        st.download_button(
            "Download CSV (Overall issues)",
            data=overall_df.to_csv(index=False).encode("utf-8"),
            file_name="overall_issues.csv",
            mime="text/csv",
        )
        # persist overall for evidence/backups
        try:
            overall_df.to_csv(os.path.join(DATA_DIR, "overall_issues.csv"), index=False)
        except Exception:
            pass

        # Alt-type breakdown
        if latest_run["alt_issues"]:
            from collections import Counter
            c = Counter([a.get("classification","unknown") for a in latest_run["alt_issues"]])
            st.caption(
                f"Breakdown — Decorative: {c.get('decorative',0)} • Logos: {c.get('logo',0)} • "
                f"Descriptive: {c.get('descriptive',0)} • Link image (no text): {c.get('link_image_no_text',0)}"
            )

        # Detail expanders (side-by-side)
        left, right = st.columns(2)

        with left:
            with st.expander("Contrast fails (examples)"):
                if latest_run["contrast_failed"] == 0:
                    st.write("No contrast failures found.")
                else:
                    for ex in latest_run.get("contrast_examples", [])[:12]:
                        st.write(f"• <{ex.get('tag','?')}> ratio {ex.get('ratio','?')}: {ex.get('text','')}")

        with right:
            with st.expander("Images needing alt (details)"):
                if not latest_run["alt_issues"]:
                    st.write("No missing/weak alt text detected.")
                else:
                    for item in latest_run["alt_issues"]:
                        label = item['suggested_alt'] or '(decorative)'
                        st.markdown(f"- `{item['src']}` → **{label}** *({item['classification']})*")

        # Auto-fix suggestions
        st.subheader("Auto Fix Suggestions")
        if not latest_run["alt_issues"]:
            st.write("No suggestions. Nice!")
        else:
            for issue in latest_run["alt_issues"][:20]:
                if issue["classification"] == "decorative":
                    suggestion = f"<img src='{issue['src']}' alt='' aria-hidden='true'>"
                else:
                    suggestion = f"<img src='{issue['src']}' alt='{issue['suggested_alt']}'>"
                st.code(suggestion, language="html")

        # Save run (manual)
        if st.button("Save this run to Dashboard"):
            st.session_state["history"].append(
                {
                    "timestamp": latest_run["timestamp"],
                    "url":       latest_run["url"],
                    "level":     latest_run.get("wcag_level","AA"),
                    "contrast_score": latest_run["scores"]["contrast_score"],
                    "overall_score":  latest_run["scores"]["overall_score"],
                    "contrast_checked": latest_run["contrast_checked"],
                    "contrast_failed":  latest_run["contrast_failed"],
                    "images_needing_alt": len(latest_run["alt_issues"]),
                }
            )
            st.success("Saved. See Dashboard below.")

# ──────────────────────────────────────────────────────────────────────────────
# Dashboard
# ──────────────────────────────────────────────────────────────────────────────
st.subheader("Dashboard")
hist = st.session_state.get("history", [])
if not hist:
    st.info("No history yet. Run a scan and Save.")
else:
    st.dataframe(pd.DataFrame(hist), use_container_width=True)
    st.caption("Note: Inline contrast by default. Enable 'computed-style contrast' to include CSS-based colors. Full audits require manual checks.")

# ──────────────────────────────────────────────────────────────────────────────
# Admin · Data controls (Evidence pack + Delete all data)
# ──────────────────────────────────────────────────────────────────────────────
with st.expander("Admin · Data controls", expanded=False):
    a, b = st.columns(2)

    if a.button("Create Evidence Pack (ZIP)"):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            # Include latest exports if they exist
            for name in ("alt_issues.csv", "overall_issues.csv", "audit_report.pdf"):
                if os.path.exists(name):
                    z.write(name, arcname=name)
            # Include persisted versions in DATA_DIR if any
            for filename in ("alt_issues.csv","overall_issues.csv","audit_report.pdf","audit_log.csv"):
                p = os.path.join(DATA_DIR, filename)
                if os.path.exists(p):
                    z.write(p, arcname=f"data/{filename}")
        st.success("Evidence pack ready.")
        st.download_button("Download Evidence Pack", buf.getvalue(), file_name="evidence_pack.zip")

    if b.button("Delete ALL stored data", type="secondary"):
        try:
            if os.path.isdir(DATA_DIR):
                shutil.rmtree(DATA_DIR)
            os.makedirs(DATA_DIR, exist_ok=True)
            st.success("All stored data deleted.")
        except Exception as e:
            st.error(f"Delete failed: {e}")

# ──────────────────────────────────────────────────────────────────────────────
# Footer (safe language + policy links)
# ──────────────────────────────────────────────────────────────────────────────
st.markdown(
    """
    <hr>
    <p class="muted">
    This tool provides an automated quick-check against selected WCAG 2.2 criteria. It does not replace a full audit and is not a legal
    determination of conformance. Manual testing is required (keyboard, screen reader behavior, forms, media, PDFs, reflow/zoom, reduced motion).<br>
    <a href="/docs/privacy.html" target="_blank">Privacy</a> ·
    <a href="/docs/terms.html" target="_blank">Terms/DPA</a> ·
    <a href="/docs/security.html" target="_blank">Security</a> ·
    <a href="/docs/acr.html" target="_blank">ACR (VPAT-lite)</a>
    </p>
    """,
    unsafe_allow_html=True
)
