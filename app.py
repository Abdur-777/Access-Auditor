# app.py — Accessibility Auditor (Council Edition)
# Run: streamlit run app.py
# Requires: streamlit, requests, beautifulsoup4, reportlab, pandas
# Optional for computed-style contrast: playwright  (then: playwright install chromium)

import os
import io
import re
import zipfile
from datetime import datetime
from urllib.parse import urlparse
from typing import List, Dict, Tuple, Optional

import requests
from bs4 import BeautifulSoup
import streamlit as st
import pandas as pd
import ipaddress

# ──────────────────────────────────────────────────────────────────────────────
# Streamlit page setup (FIRST)
# ──────────────────────────────────────────────────────────────────────────────
st.set_page_config(page_title="Accessibility Auditor — Council Edition (WCAG 2.2)", layout="wide")

# ──────────────────────────────────────────────────────────────────────────────
# Branding & constants (override via env)
# ──────────────────────────────────────────────────────────────────────────────
WYNDHAM_BLUE = os.getenv("BRAND_PRIMARY", "#003B73")
ORG_NAME = os.getenv("ORG_NAME", "Wyndham City Council")

SAFE_FOOTER = (
    "This tool provides an automated quick-check against selected WCAG 2.2 AA criteria. "
    "It does not replace a full accessibility audit. Several criteria require manual testing "
    "(keyboard navigation, screen reader behavior, forms, media, PDFs, reflow/zoom, reduced motion). "
    "Results indicate potential issues and are not a legal confirmation of compliance."
)

def inject_brand_css():
    st.markdown(
        f"""
        <style>
          :root {{ --wy:{WYNDHAM_BLUE}; }}
          .stButton>button, .stDownloadButton>button {{
            background: var(--wy) !important; color:#fff !important; border:0 !important;
            border-radius:12px !important; padding:8px 14px !important; font-weight:600;
          }}
          .wy-accents h1, .wy-accents h2, .wy-accents h3, .wy-accents h4 {{ color: var(--wy); }}
          .wy-pill {{
            display:inline-block; background: color-mix(in srgb, var(--wy) 12%, white);
            color: var(--wy); padding:2px 10px; border-radius:999px; font-weight:700; font-size:12px;
          }}
          .wy-muted {{ color:#4b5563; }}
          footer {{ visibility:hidden; }}
        </style>
        """,
        unsafe_allow_html=True,
    )

inject_brand_css()

# ──────────────────────────────────────────────────────────────────────────────
# HTTP fetch helpers (browser-like headers + cache)
# ──────────────────────────────────────────────────────────────────────────────
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
}

@st.cache_data(ttl=600, show_spinner=False)
def fetch_html(url: str, timeout: int = 25) -> str:
    r = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
    if r.status_code in (403, 429):
        raise RuntimeError(f"{r.status_code} {r.reason} — site may block scraping; try 'Use computed-style contrast' or Paste HTML.")
    r.raise_for_status()
    text = r.text
    if len(text) > 3_000_000:  # avoid memory spikes
        text = text[:3_000_000]
    return text

# ──────────────────────────────────────────────────────────────────────────────
# URL safety: public http(s) only
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
            pass  # hostname, not IP literal
        return True
    except Exception:
        return False

# ──────────────────────────────────────────────────────────────────────────────
# Optional: computed-style contrast via Playwright (Chromium)
# ──────────────────────────────────────────────────────────────────────────────
def analyze_computed_contrast(url: str, level: str = "AA", timeout_ms: int = 35000):
    try:
        from playwright.sync_api import sync_playwright  # optional dep
    except Exception as e:
        return {"error": f"Playwright not available: {e}"}

    small_t = 7.0 if level == "AAA" else 4.5
    large_t = 4.5 if level == "AAA" else 3.0

    js = f"""
    (() => {{
      const SMALL_T = {small_t}, LARGE_T = {large_t};
      function parse(col){{const c=document.createElement('canvas').getContext('2d');c.fillStyle=col;
        const m=c.fillStyle.match(/[0-9]+/g);return m?m.slice(0,3).map(Number):[0,0,0];}}
      function lum([r,g,b]){{r/=255;g/=255;b/=255;const f=v=>v<=0.03928?v/12.92:Math.pow((v+0.055)/1.055,2.4);
        r=f(r);g=f(g);b=f(b);return 0.2126*r+0.7152*g+0.0722*b;}}
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
        const s=getComputedStyle(el);
        const fg=parse(s.color), bgc=parse(bg(el));
        const cr=ratio(lum(fg),lum(bgc));
        const t=isLarge(s)?LARGE_T:SMALL_T;
        if(cr<t){{ failed++; if(out.length<60) out.push({{tag:el.tagName.toLowerCase(),text:(el.textContent||'').trim().slice(0,80),ratio:Math.round(cr*100)/100}}); }}
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
# Color / contrast helpers (inline only)
# ──────────────────────────────────────────────────────────────────────────────
HEX_RE = re.compile(r"#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})")
RGB_RE = re.compile(r"rgb\((\d+),\s*(\d+),\s*(\d+)\)")
RGBA_RE = re.compile(r"rgba\((\d+),\s*(\d+),\s*(\d+),\s*(0|0?\.\d+|1)\)")

def parse_dimension(val) -> Optional[int]:
    """Parse '10', '10px', '1.5rem' → 10 (approx px); None if not parsable."""
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
            r = int(s[0]*2, 16); g = int(s[1]*2, 16); b = int(s[2]*2, 16)
        else:
            r = int(s[0:2], 16); g = int(s[2:4], 16); b = int(s[4:6], 16)
        return (r, g, b)
    m = RGB_RE.search(value)
    if m: return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = RGBA_RE.search(value)
    if m:
        r, g, b, a = int(m.group(1)), int(m.group(2)), int(m.group(3)), float(m.group(4))
        if a == 0: return None
        return (r, g, b)
    NAMED = {"black": (0,0,0), "white": (255,255,255), "gray": (128,128,128), "grey": (128,128,128), "red": (255,0,0)}
    return NAMED.get(value.lower())

def rel_luminance(rgb: Tuple[int, int, int]) -> float:
    def _lin(c):
        c = c / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = rgb
    return 0.2126*_lin(r) + 0.7152*_lin(g) + 0.0722*_lin(b)

def contrast_ratio(c1: Tuple[int, int, int], c2: Tuple[int, int, int]) -> float:
    L1, L2 = rel_luminance(c1), rel_luminance(c2)
    lighter, darker = max(L1, L2), min(L1, L2)
    return (lighter + 0.05) / (darker + 0.05)

# ──────────────────────────────────────────────────────────────────────────────
# Alt-text helpers
# ──────────────────────────────────────────────────────────────────────────────
DECORATIVE_HINTS = re.compile(r"(border|spacer|decor|sprite|shadow|corner|bg|badge|icon)", re.I)
GENERIC_ALTS = {"image","photo","graphic","pic","icon","spacer"}

def looks_like_filename(s: str) -> bool:
    if not s: return False
    if re.search(r"\.(png|jpe?g|gif|svg|webp)$", s, re.I): return True
    return bool(re.search(r"[a-z0-9_-]", s) and " " not in s)

def suggest_alt(src: str, width: Optional[int] = None, height: Optional[int] = None, site_name: str = "") -> Tuple[str, str]:
    """Return (alt_text, classification) in {'decorative','logo','descriptive'}"""
    path = urlparse(src).path if src else ""
    fname = path.split("/")[-1] if path else ""
    stem = re.sub(r"\.(png|jpg|jpeg|gif|svg|webp)$", "", fname, flags=re.I)
    tiny = (width and width < 24) or (height and height < 24)
    if tiny or DECORATIVE_HINTS.search(stem or ""):
        return ("", "decorative")
    if re.search(r"logo", stem or "", re.I):
        name = site_name.strip() or "Site"
        return (f"{name}", "logo")  # SRs don't need the word "logo"
    human = re.sub(r"[-_]+", " ", stem).strip().capitalize() if stem else "Image"
    human = (human[:77] + "…") if len(human) > 80 else human
    return (human or "Image", "descriptive")

def normalize_src(src: str) -> str:
    try:
        u = urlparse(src)
        if u.scheme and u.netloc:
            return f"{u.scheme}://{u.netloc}{u.path}"
        return u.path or src
    except Exception:
        return src

# ──────────────────────────────────────────────────────────────────────────────
# HTML analyzer (inline contrast + alt issues)
# ──────────────────────────────────────────────────────────────────────────────
def analyze_html(html: str, assume_bg: Tuple[int, int, int] = (255,255,255), site_name: str = "", level: str = "AA") -> Dict:
    soup = BeautifulSoup(html, "html.parser")
    threshold = 7.0 if level == "AAA" else 4.5

    # Contrast (inline styles only)
    contrast_checked = 0
    contrast_failed = 0
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
            if len(contrast_examples) < 10:
                contrast_examples.append({
                    "text": (el.get_text(strip=True) or "(no text)")[:80],
                    "ratio": round(ratio, 2),
                    "tag": el.name,
                })

    # Alt issues
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

    # Links that are images only with no text/label and empty alt
    for a in soup.find_all("a"):
        imgs = a.find_all("img")
        if not imgs: continue
        if (a.get_text(strip=True) or "") or (a.get("aria-label") or a.get("title") or "").strip():
            continue
        for _img in imgs:
            cur_alt = (_img.get("alt") or "").strip()
            if cur_alt == "":
                alt_issues.append({
                    "src": _img.get("src") or "",
                    "suggested_alt": "Add aria-label/title on the link (e.g., ‘Home’) or give the image that label.",
                    "classification": "link_image_no_text",
                })
                break

    return {
        "contrast_checked": contrast_checked,
        "contrast_failed": contrast_failed,
        "contrast_examples": contrast_examples,
        "alt_issues": alt_issues,
        "img_count": len(img_nodes),
    }

# ──────────────────────────────────────────────────────────────────────────────
# Scoring
# ──────────────────────────────────────────────────────────────────────────────
def compute_scores(contrast_checked: int, contrast_failed: int, alt_issues_count: int) -> Dict:
    contrast_score = 100.0 if contrast_checked == 0 else max(0.0, 100.0 * (1 - (contrast_failed / max(1, contrast_checked))))
    overall = max(0.0, contrast_score - min(40.0, float(alt_issues_count)))
    return {"contrast_score": round(contrast_score, 1), "overall_score": round(overall, 1)}

# ──────────────────────────────────────────────────────────────────────────────
# PDF exports (ReportLab)
# ──────────────────────────────────────────────────────────────────────────────
from reportlab.lib import colors
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.units import mm

def export_pdf_audit(filepath: str, url: str, level: str, scores: Dict, report: Dict, org_name: str = ORG_NAME):
    styles = getSampleStyleSheet()
    title = ParagraphStyle("title", parent=styles["Title"], textColor=HexColor(WYNDHAM_BLUE))
    h2    = ParagraphStyle("h2", parent=styles["Heading2"], textColor=HexColor(WYNDHAM_BLUE))
    body  = styles["BodyText"]
    doc = SimpleDocTemplate(filepath, pagesize=A4, leftMargin=18*mm, rightMargin=18*mm, topMargin=18*mm, bottomMargin=18*mm)
    story = []

    story.append(Paragraph(f"<b>{org_name} — Accessibility Quick-Check (WCAG 2.2 {level})</b>", title))
    story.append(Paragraph(datetime.now().strftime("%Y-%m-%d %H:%M"), body))
    story.append(Spacer(1, 6))
    story.append(Paragraph(f"<b>Target URL:</b> {url}", body))
    story.append(Spacer(1, 8))

    data = [
        ["Overall Score", f"{scores['overall_score']}"],
        [f"Contrast Score (%) — {level}", f"{scores['contrast_score']}"],
        ["Elements Checked (contrast)", f"{report['contrast_checked']}"],
        ["Contrast Fails", f"{report['contrast_failed']}"],
        ["Images needing alt (count)", f"{len(report['alt_issues'])}"],
    ]
    tbl = Table(data, hAlign="LEFT", colWidths=[80*mm, 80*mm])
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), HexColor(WYNDHAM_BLUE)),
        ("TEXTCOLOR", (0,0), (-1,0), colors.white),
        ("GRID", (0,0), (-1,-1), 0.25, colors.grey),
        ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.whitesmoke, colors.lightgrey]),
    ]))
    story.append(tbl)
    story.append(Spacer(1, 10))

    # Alt breakdown
    story.append(Paragraph("Images needing alt — breakdown", h2))
    if report["alt_issues"]:
        counts = {}
        for it in report["alt_issues"]:
            counts[it.get("classification","other")] = counts.get(it.get("classification","other"), 0) + 1
        breakdown = " • ".join([f"{k}: {v}" for k,v in counts.items()])
        story.append(Paragraph(breakdown, body))
    else:
        story.append(Paragraph("No images flagged.", body))
    story.append(Spacer(1, 8))

    story.append(Paragraph("Contrast fails — examples", h2))
    if report["contrast_failed"] == 0:
        story.append(Paragraph("No contrast failures found.", body))
    else:
        for ex in report.get("contrast_examples", [])[:10]:
            story.append(Paragraph(f"&lt;{ex.get('tag','?')}&gt; ratio {ex.get('ratio','?')}: {ex.get('text','')}", body))
    story.append(Spacer(1, 10))

    story.append(Paragraph("Methods & Limitations", h2))
    story.append(Paragraph(SAFE_FOOTER, body))

    doc.build(story)

def export_pdf_conformance(filepath: str, meta: Dict, findings: Dict, outstanding: List[str], org_name: str = ORG_NAME):
    styles = getSampleStyleSheet()
    title = ParagraphStyle("title", parent=styles["Title"], textColor=HexColor(WYNDHAM_BLUE))
    h2    = ParagraphStyle("h2", parent=styles["Heading2"], textColor=HexColor(WYNDHAM_BLUE))
    body  = styles["BodyText"]
    doc = SimpleDocTemplate(filepath, pagesize=A4, leftMargin=18*mm, rightMargin=18*mm, topMargin=18*mm, bottomMargin=18*mm)
    story = []

    story.append(Paragraph(f"<b>{org_name} — Draft WCAG 2.2 {meta['level']} Conformance Statement</b>", title))
    story.append(Paragraph(datetime.now().strftime("%Y-%m-%d %H:%M"), body))
    story.append(Spacer(1, 8))
    story.append(Paragraph(f"<b>Site:</b> {meta['site']}", body))
    story.append(Paragraph(f"<b>Pages tested:</b> {meta['pages']}", body))
    story.append(Paragraph(f"<b>Methods used:</b> {meta['methods']}", body))
    story.append(Spacer(1, 6))
    story.append(Paragraph("<b>Automated quick-check findings</b>", h2))
    story.append(Paragraph(findings["summary"], body))
    story.append(Spacer(1, 6))

    story.append(Paragraph("<b>Manual checks outstanding</b>", h2))
    if outstanding:
        for item in outstanding:
            story.append(Paragraph(f"• {item}", body))
    else:
        story.append(Paragraph("Customer/team to confirm.", body))
    story.append(Spacer(1, 6))

    story.append(Paragraph("<b>Disclaimer</b>", h2))
    story.append(Paragraph(SAFE_FOOTER, body))
    doc.build(story)

# ──────────────────────────────────────────────────────────────────────────────
# Header
# ──────────────────────────────────────────────────────────────────────────────
st.markdown(
    f"""
<div class="wy-accents">
  <h1>Accessibility Auditor — Council Edition <span class="wy-pill">WCAG 2.2</span></h1>
  <p class="wy-muted">Quick-check contrast (inline & optional computed) and image alt text, with council-branded PDFs, CSVs, a draft conformance statement, and an evidence pack.</p>
</div>
""",
    unsafe_allow_html=True,
)

# ──────────────────────────────────────────────────────────────────────────────
# Sidebar (controls)
# ──────────────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("Scan Settings")
    wcag_level = st.selectbox("WCAG level", ["AA", "AAA"], index=0, help="Sets contrast thresholds (AA: 4.5/3.0, AAA: 7.0/4.5).")
    use_computed = st.checkbox("Use computed-style contrast (headless)", value=False, help="Requires Playwright + Chromium.")
    url_input = st.text_input("Public page URL", value=st.session_state.get("url_input",""), placeholder="https://example.com/page")
    with st.expander("Or paste HTML (fallback)"):
        pasted_html = st.text_area("Raw HTML", height=160, placeholder="Paste a full HTML document here…")
        use_pasted = st.checkbox("Use pasted HTML for this scan", value=False)

    st.caption("Please ensure you have permission to scan the URL. Respect robots.txt and site terms.")
    st.markdown("---")
    cols = st.columns([1,1])
    with cols[0]:
        auto_save = st.checkbox("Auto-save successful scans to Dashboard", value=st.session_state.get("auto_save", True))
        st.session_state["auto_save"] = auto_save
    with cols[1]:
        if st.button("Reset form", use_container_width=True):
            for k in ("url_input","latest_run"): st.session_state.pop(k, None)
            st.experimental_rerun()

# Session state priming
st.session_state.setdefault("history", [])
st.session_state.setdefault("latest_run", None)

# ──────────────────────────────────────────────────────────────────────────────
# Run buttons — side-by-side layout
# ──────────────────────────────────────────────────────────────────────────────
st.subheader("Run")
c1, c2, c3, c4 = st.columns([1, 1, 1, 1], gap="small")
with c1:
    scan_html_clicked = st.button("🔍 Scan HTML (Contrast & Images)", use_container_width=True)
with c2:
    export_pdf_clicked = st.button("💾 Generate Audit PDF", use_container_width=True)
with c3:
    conf_form_open = st.button("📄 Draft Conformance Statement (open form)", use_container_width=True)
with c4:
    evidence_pack_click = st.button("📦 Build Evidence Pack (.zip)", use_container_width=True)

st.markdown("---")

# ──────────────────────────────────────────────────────────────────────────────
# Scan flow
# ──────────────────────────────────────────────────────────────────────────────
if scan_html_clicked:
    if use_pasted and pasted_html.strip():
        html = pasted_html
        url_for_report = url_input or "(pasted HTML)"
        ok = True; err_msg = ""
    elif url_input.strip():
        if not is_public_http_url(url_input.strip()):
            ok = False; err_msg = "URL must be public http(s) (not localhost/private)."
        else:
            try:
                with st.spinner("Fetching page…"):
                    html = fetch_html(url_input.strip())
                url_for_report = url_input.strip()
                ok = True; err_msg = ""
            except Exception as e:
                ok = False; err_msg = str(e)
    else:
        ok = False; err_msg = "Provide a URL or paste HTML."

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
                    st.warning(f"Computed-style contrast unavailable: {comp.get('error','unknown error') if isinstance(comp, dict) else 'error'} — showing inline results.")

            scores = compute_scores(report["contrast_checked"], report["contrast_failed"], len(report["alt_issues"]))
            latest_run = {
                "timestamp": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                "url": url_for_report,
                "wcag_level": wcag_level,
                "scores": scores,
                "report": report,  # includes contrast_examples + alt_issues
            }
            st.session_state["latest_run"] = latest_run
            if st.session_state.get("auto_save", False):
                st.session_state["history"].append({
                    "timestamp": latest_run["timestamp"],
                    "url": latest_run["url"],
                    "level": latest_run["wcag_level"],
                    "contrast_score": scores["contrast_score"],
                    "overall_score": scores["overall_score"],
                    "contrast_checked": report["contrast_checked"],
                    "contrast_failed": report["contrast_failed"],
                    "images_needing_alt": len(report["alt_issues"]),
                })
                st.success("Saved to Dashboard.")

# ──────────────────────────────────────────────────────────────────────────────
# Results
# ──────────────────────────────────────────────────────────────────────────────
lr = st.session_state.get("latest_run")
if lr:
    st.subheader("Results")
    col1, col2, col3 = st.columns(3)
    col1.metric(f"Contrast Score (%) — {lr['wcag_level']}", lr["scores"]["contrast_score"])
    col2.metric("Overall Score", lr["scores"]["overall_score"])
    col3.metric("Images needing alt", len(lr["report"]["alt_issues"]))

    # CSVs (always show "Overall issues", even if empty)
    # Alt issues CSV
    alt_df = pd.DataFrame(lr["report"]["alt_issues"])
    st.download_button(
        "⬇️ Download CSV — Images needing alt",
        data=(alt_df.to_csv(index=False).encode("utf-8")),
        file_name="alt_issues.csv",
        mime="text/csv",
    )

    # Overall issues CSV (contrast + alts)
    overall_rows = []
    for ex in lr["report"].get("contrast_examples", [])[:500]:
        overall_rows.append({
            "type": "contrast", "tag": ex.get("tag",""), "text": ex.get("text",""),
            "ratio": ex.get("ratio",""), "src": "", "suggested_alt": "", "classification": ""
        })
    for it in lr["report"].get("alt_issues", []):
        overall_rows.append({
            "type": "alt", "tag": "img", "text": "", "ratio": "",
            "src": it.get("src",""), "suggested_alt": it.get("suggested_alt",""), "classification": it.get("classification","")
        })
    overall_df = pd.DataFrame(overall_rows)
    st.download_button(
        "⬇️ Download CSV — Overall issues",
        data=(overall_df.to_csv(index=False).encode("utf-8")),
        file_name="overall_issues.csv",
        mime="text/csv",
    )

    # Alt breakdown
    with st.expander("Images needing alt — breakdown"):
        if lr["report"]["alt_issues"]:
            counts = {}
            for it in lr["report"]["alt_issues"]:
                k = it.get("classification","other")
                counts[k] = counts.get(k, 0) + 1
            line = " • ".join([f"{k}: {v}" for k,v in counts.items()])
            st.write(line)
            st.dataframe(alt_df, use_container_width=True)
        else:
            st.write("No images flagged.")

    with st.expander("Contrast fails — examples"):
        if lr["report"]["contrast_failed"] == 0:
            st.write("No contrast failures found.")
        else:
            for ex in lr["report"].get("contrast_examples", [])[:10]:
                st.write(f"• <{ex.get('tag','?')}> ratio {ex.get('ratio','?')}: {ex.get('text','')}")

    # Auto-fix suggestions (alts)
    st.subheader("Auto-fix Suggestions (alts)")
    if not lr["report"]["alt_issues"]:
        st.write("No suggestions — nice!")
    else:
        for issue in lr["report"]["alt_issues"][:25]:
            if issue["classification"] == "decorative":
                suggestion = f"<img src='{issue['src']}' alt='' aria-hidden='true'>"
            else:
                suggestion = f"<img src='{issue['src']}' alt='{issue['suggested_alt']}'>"
            st.code(suggestion, language="html")

# ──────────────────────────────────────────────────────────────────────────────
# Export Audit PDF (with toast)
# ──────────────────────────────────────────────────────────────────────────────
if export_pdf_clicked:
    run = st.session_state.get("latest_run")
    if not run:
        st.warning("Run an HTML scan first.")
    else:
        path = "audit_report.pdf"
        export_pdf_audit(
            path,
            url=run["url"],
            level=run["wcag_level"],
            scores=run["scores"],
            report=run["report"],
            org_name=ORG_NAME,
        )
        try:
            st.toast("Report ready")
        except Exception:
            st.success("Report ready")
        with open(path, "rb") as f:
            st.download_button("⬇️ Download Audit PDF", f, file_name="audit_report.pdf")

# ──────────────────────────────────────────────────────────────────────────────
# Draft Conformance Statement — form + exports
# ──────────────────────────────────────────────────────────────────────────────
if conf_form_open:
    st.subheader("Draft WCAG 2.2 AA Conformance Statement — Generator")

    latest = st.session_state.get("latest_run")
    site_prefill = (latest["url"] if latest else "")
    pages_prefill = site_prefill or "Home, Key Journey Pages"

    with st.form("conf_form"):
        site   = st.text_input("Site", value=site_prefill)
        level  = st.selectbox("Claimed level (draft)", ["AA","AAA"], index=(0 if not latest else (0 if latest["wcag_level"]=="AA" else 1)))
        methods_used = st.multiselect(
            "Methods used (automated)",
            [
                "Inline contrast scan",
                "Computed-style contrast (headless)",
                "Alt text heuristic check",
                "Overall issues CSV export",
                "Branded PDF export",
            ],
            default=["Inline contrast scan","Alt text heuristic check","Overall issues CSV export","Branded PDF export"] + (["Computed-style contrast (headless)"] if latest and latest["wcag_level"] else []),
        )
        pages = st.text_area("Pages tested", value=pages_prefill, height=80)
        key_findings = st.text_area("Key findings (optional)", value="", height=120)

        outstanding_defaults = [
            "Keyboard navigation & focus order",
            "Screen reader usability (NVDA/JAWS/VoiceOver)",
            "Forms (labels, errors, help text)",
            "Media (captions, transcripts, audio description)",
            "PDFs (PDF/UA tagging)",
            "Reflow/zoom (320 CSS px; 200–400%)",
            "Reduced motion (prefers-reduced-motion)",
            "Color alone not used",
        ]
        outstanding = st.multiselect("Manual checks outstanding", outstanding_defaults, default=outstanding_defaults)

        export_pdf = st.form_submit_button("Generate Draft (PDF)")
        export_md  = st.form_submit_button("Download Draft (Markdown)")

    if export_pdf or export_md:
        latest = st.session_state.get("latest_run")
        findings_summary = ""
        if latest:
            r, s = latest["report"], latest["scores"]
            findings_summary = (
                f"Contrast checked: {r['contrast_checked']}, fails: {r['contrast_failed']}. "
                f"Images needing alt: {len(r['alt_issues'])}. Contrast score: {s['contrast_score']}%, Overall score: {s['overall_score']}."
            )
        if key_findings.strip():
            findings_summary = (findings_summary + " " if findings_summary else "") + key_findings.strip()

        meta = {
            "site": site or "(not specified)",
            "level": level,
            "pages": pages.replace("\n", ", "),
            "methods": ", ".join(methods_used) if methods_used else "Automated quick-check",
        }
        findings = {"summary": findings_summary or "Automated quick-check executed; see attached evidence."}

        if export_pdf:
            path = "conformance_statement.pdf"
            export_pdf_conformance(path, meta, findings, outstanding, org_name=ORG_NAME)
            try:
                st.toast("Conformance statement ready")
            except Exception:
                st.success("Conformance statement ready")
            with open(path, "rb") as f:
                st.download_button("⬇️ Download Conformance Statement (PDF)", f, file_name="conformance_statement.pdf")

        if export_md:
            md = io.StringIO()
            md.write(f"# Draft WCAG 2.2 {meta['level']} Conformance Statement — {ORG_NAME}\n\n")
            md.write(f"- **Site:** {meta['site']}\n")
            md.write(f"- **Date:** {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
            md.write(f"- **Pages tested:** {meta['pages']}\n")
            md.write(f"- **Methods used:** {meta['methods']}\n\n")
            md.write("## Automated quick-check findings\n")
            md.write(f"{findings['summary']}\n\n")
            md.write("## Manual checks outstanding\n")
            for item in outstanding: md.write(f"- {item}\n")
            md.write("\n## Disclaimer\n")
            md.write(SAFE_FOOTER + "\n")
            st.download_button("⬇️ Download Conformance Statement (Markdown)", data=md.getvalue().encode("utf-8"), file_name="conformance_statement.md", mime="text/markdown")

# ──────────────────────────────────────────────────────────────────────────────
# Evidence Pack (.zip)
# ──────────────────────────────────────────────────────────────────────────────
if evidence_pack_click:
    run = st.session_state.get("latest_run")
    if not run:
        st.warning("Run a scan first.")
    else:
        # Ensure we have up-to-date CSVs
        alt_df = pd.DataFrame(run["report"]["alt_issues"])
        overall_rows = []
        for ex in run["report"].get("contrast_examples", [])[:500]:
            overall_rows.append({
                "type": "contrast", "tag": ex.get("tag",""), "text": ex.get("text",""),
                "ratio": ex.get("ratio",""), "src": "", "suggested_alt": "", "classification": ""
            })
        for it in run["report"].get("alt_issues", []):
            overall_rows.append({
                "type": "alt", "tag": "img", "text": "", "ratio": "",
                "src": it.get("src",""), "suggested_alt": it.get("suggested_alt",""), "classification": it.get("classification","")
            })
        overall_df = pd.DataFrame(overall_rows)

        # Generate audit PDF (to disk) if needed
        audit_path = "audit_report.pdf"
        export_pdf_audit(audit_path, run["url"], run["wcag_level"], run["scores"], run["report"], org_name=ORG_NAME)

        # Minimal manual checks sheet (Markdown)
        manual_md = io.StringIO()
        manual_md.write("# Manual Checks to Complete\n")
        manual_md.write("(Customer/team to complete or verify)\n\n")
        for item in [
            "Keyboard navigation & focus order",
            "Screen reader usability (NVDA/JAWS/VoiceOver)",
            "Forms: labels, errors, help text",
            "Media: captions, transcripts, audio descriptions",
            "PDFs: tagging (PDF/UA) and alternatives",
            "Reflow/zoom: 320 CSS px, 200–400% zoom",
            "Reduced motion: honors prefers-reduced-motion",
            "Color is not sole means of conveying information",
        ]:
            manual_md.write(f"- [ ] {item}\n")

        # Build zip in-memory
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
            z.writestr("alt_issues.csv", alt_df.to_csv(index=False))
            z.writestr("overall_issues.csv", overall_df.to_csv(index=False))
            # attach audit pdf
            with open(audit_path, "rb") as f:
                z.writestr("audit_report.pdf", f.read())
            # attach draft conformance (markdown)
            conf_md = io.StringIO()
            conf_md.write(f"# Draft WCAG 2.2 {run['wcag_level']} Conformance Statement — {ORG_NAME}\n\n")
            conf_md.write(f"- **Site:** {run['url']}\n")
            conf_md.write(f"- **Date:** {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
            conf_md.write(f"- **Methods used:** Inline contrast, Alt checks{', Computed contrast' if use_computed else ''}\n\n")
            conf_md.write("## Automated quick-check findings\n")
            conf_md.write(f"Contrast checked: {run['report']['contrast_checked']}, fails: {run['report']['contrast_failed']}.\n")
            conf_md.write(f"Images needing alt: {len(run['report']['alt_issues'])}.\n")
            conf_md.write(f"Contrast score: {run['scores']['contrast_score']}%, Overall score: {run['scores']['overall_score']}.\n\n")
            conf_md.write("## Manual checks outstanding\n")
            for item in [
                "Keyboard navigation & focus order",
                "Screen reader usability (NVDA/JAWS/VoiceOver)",
                "Forms (labels, errors, help text)",
                "Media (captions, transcripts, audio description)",
                "PDFs (PDF/UA tagging)",
                "Reflow/zoom (320 CSS px; 200–400%)",
                "Reduced motion (prefers-reduced-motion)",
            ]:
                conf_md.write(f"- {item}\n")
            conf_md.write("\n## Disclaimer\n" + SAFE_FOOTER + "\n")
            z.writestr("conformance_statement_draft.md", conf_md.getvalue().encode("utf-8"))
            # manual sheet
            z.writestr("manual_checks_to_complete.md", manual_md.getvalue().encode("utf-8"))

        try:
            st.toast("Evidence pack ready")
        except Exception:
            st.success("Evidence pack ready")
        st.download_button("⬇️ Download Evidence Pack (.zip)", data=buf.getvalue(), file_name="evidence_pack.zip", mime="application/zip")

# ──────────────────────────────────────────────────────────────────────────────
# Dashboard
# ──────────────────────────────────────────────────────────────────────────────
st.subheader("Dashboard")
hist = st.session_state.get("history", [])
if not hist:
    st.info("No history yet. Run a scan and it will appear here.")
else:
    st.dataframe(pd.DataFrame(hist), use_container_width=True)
    st.caption("Tip: Use computed-style contrast to include CSS colors. The quick-check never replaces a full audit.")

# ──────────────────────────────────────────────────────────────────────────────
# Roadmap (non-blocking stubs you can wire later)
# ──────────────────────────────────────────────────────────────────────────────
with st.expander("Roadmap (not a guarantee)"):
    st.markdown("- axe-core via Playwright for DOM/ARIA rules (TODO)")
    st.markdown("- Heuristics: headings/landmarks, duplicate IDs, accessible names, link purpose (TODO)")
    st.markdown("- Keyboard simulation (tab/shift+tab) to detect traps & hidden focus (TODO)")
    st.markdown("- Reflow & zoom screenshots (320 CSS px; 200/400%) (TODO)")
    st.markdown("- prefers-reduced-motion check for animations (TODO)")
    st.markdown("- Media placeholders: flag missing captions/transcripts (TODO)")
    st.markdown("- PDF queue + PDF/UA checker hook (TODO)")

# ──────────────────────────────────────────────────────────────────────────────
# Footer disclaimer (always visible in app)
# ──────────────────────────────────────────────────────────────────────────────
st.markdown("---")
st.caption(SAFE_FOOTER)
