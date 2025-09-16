# app.py — Accessibility Auditor (WCAG quick-check) with Wyndham styling
# Run: streamlit run app.py
# Requires: streamlit, requests, beautifulsoup4, reportlab
# Optional for computed-style contrast: playwright (and install Chromium)

import os
import re, time, math
from datetime import datetime
from urllib.parse import urlparse, urlunparse
from typing import List, Dict, Tuple, Optional

import requests
from bs4 import BeautifulSoup, Tag
import streamlit as st
import ipaddress

# ──────────────────────────────────────────────────────────────────────────────
# Streamlit must configure page FIRST
# ──────────────────────────────────────────────────────────────────────────────
st.set_page_config(page_title="Accessibility Auditor — WCAG 2.2 AA", layout="wide")

# ──────────────────────────────────────────────────────────────────────────────
# Branding (Wyndham blue, no logo) — override via env if you like
# ──────────────────────────────────────────────────────────────────────────────
WYNDHAM_BLUE = os.getenv("BRAND_PRIMARY", "#003B73")
ORG_NAME = os.getenv("ORG_NAME", "Wyndham City Council")

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
          details>summary {{font-weight:600;}}
        </style>
        """,
        unsafe_allow_html=True,
    )

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
    # Cap response to ~3MB to avoid memory spikes
    if len(text) > 3_000_000:
        text = text[:3_000_000]
    return text

# ──────────────────────────────────────────────────────────────────────────────
# URL safety guard (public http/https only)
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
# Returns dict: {"checked":int, "failed":int, "examples":[{tag,text,ratio}]} or {"error": "..."}
# ──────────────────────────────────────────────────────────────────────────────
def analyze_computed_contrast(url: str, level: str = "AA", timeout_ms: int = 30000):
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except Exception as e:
        return {"error": f"Playwright not available: {e}"}

    # Thresholds incl. large text
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
# Contrast math (inline-only)
# ──────────────────────────────────────────────────────────────────────────────
HEX_RE = re.compile(r"#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})")
RGB_RE = re.compile(r"rgb\((\d+),\s*(\d+),\s*(\d+)\)")
RGBA_RE = re.compile(r"rgba\((\d+),\s*(\d+),\s*(\d+),\s*(0|0?\.\d+|1)\)")

# Parse dimensions like "10", "10px", "1.5rem" → 10 (approx px); return None if not parsable.
def parse_dimension(val) -> Optional[int]:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return int(float(val))
    s = str(val).strip()
    num = []
    dot = False
    sign_allowed = True
    for ch in s:
        if ch.isdigit():
            num.append(ch)
            sign_allowed = False
        elif ch == '.' and not dot:
            num.append(ch)
            dot = True
            sign_allowed = False
        elif ch in '+-' and sign_allowed:
            if ch == '-':
                num.append(ch)
            sign_allowed = False
        elif num:
            break
    try:
        n = ''.join(num)
        return int(float(n)) if n else None
    except Exception:
        return None

def parse_color(value: str) -> Optional[Tuple[int, int, int]]:
    if not value:
        return None
    value = value.strip()
    m = HEX_RE.search(value)
    if m:
        s = m.group(1)
        if len(s) == 3:
            r = int(s[0] * 2, 16); g = int(s[1] * 2, 16); b = int(s[2] * 2, 16)
        else:
            r = int(s[0:2], 16); g = int(s[2:4], 16); b = int(s[4:6], 16)
        return (r, g, b)
    m = RGB_RE.search(value)
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = RGBA_RE.search(value)
    if m:
        r, g, b, a = int(m.group(1)), int(m.group(2)), int(m.group(3)), float(m.group(4))
        if a == 0:
            return None
        return (r, g, b)
    NAMED = {"black": (0,0,0), "white": (255,255,255), "gray": (128,128,128), "grey": (128,128,128), "red": (255,0,0)}
    return NAMED.get(value.lower())

def rel_luminance(rgb: Tuple[int, int, int]) -> float:
    def _lin(c):
        c = c / 255.0
        return c / 12.92 if c <= 0.03928 * 255 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = rgb
    R, G, B = _lin(r), _lin(g), _lin(b)
    return 0.2126 * R + 0.7152 * G + 0.0722 * B

def contrast_ratio(c1: Tuple[int, int, int], c2: Tuple[int, int, int]) -> float:
    L1 = rel_luminance(c1); L2 = rel_luminance(c2)
    lighter = max(L1, L2); darker = min(L1, L2)
    return (lighter + 0.05) / (darker + 0.05)

# ──────────────────────────────────────────────────────────────────────────────
# Alt suggestions & checks
# ──────────────────────────────────────────────────────────────────────────────
DECORATIVE_HINTS = re.compile(r"(border|spacer|decor|sprite|shadow|corner|bg|badge|icon)", re.I)
GENERIC_ALTS = {"image","photo","graphic","pic","icon","spacer"}

def looks_like_filename(s: str) -> bool:
    if not s:
        return False
    if re.search(r"\.(png|jpe?g|gif|svg|webp)$", s, re.I):
        return True
    # no spaces, lots of separators → probably a filename-ish token
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
        return (f"{name}", "logo")  # prefer org name; 'logo' not needed for SRs
    human = re.sub(r"[-_]+", " ", stem).strip().capitalize() if stem else "Image"
    human = (human[:77] + "…") if len(human) > 80 else human
    return (human or "Image", "descriptive")

def normalize_src(src: str) -> str:
    """Normalize image URL for dedupe (drop query/fragment)"""
    try:
        u = urlparse(src)
        if u.scheme and u.netloc:
            return f"{u.scheme}://{u.netloc}{u.path}"
        return u.path or src
    except Exception:
        return src

# ──────────────────────────────────────────────────────────────────────────────
# Analyze HTML (inline contrast + alts)
# ──────────────────────────────────────────────────────────────────────────────
def analyze_html(html: str, assume_bg: Tuple[int, int, int] = (255, 255, 255), site_name: str = "", level: str = "AA") -> Dict:
    soup = BeautifulSoup(html, "html.parser")

    # WCAG threshold: AA=4.5 for normal text, AAA=7.0 (inline quick-check cannot detect large text reliably)
    threshold = 7.0 if level == "AAA" else 4.5

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
        if ratio < threshold:
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
        norm = normalize_src(src)
        if norm in seen_src:
            continue
        seen_src.add(norm)
        current_alt = (img.get("alt") or "").strip()
        w = parse_dimension(img.get("width"))
        h = parse_dimension(img.get("height"))
        alt_suggestion, cls = suggest_alt(src, w, h, site_name=site_name)

        bad_generic = current_alt.lower() in GENERIC_ALTS
        bad_filename = looks_like_filename(current_alt)

        # Flag missing, generic, filename-like, or decorative (should be empty alt)
        if current_alt == "" or bad_generic or bad_filename:
            alt_issues.append({"src": src, "suggested_alt": alt_suggestion, "classification": cls})

    # Anchor-only image with no text/label and empty alt
    for a in soup.find_all("a"):
        imgs = a.find_all("img")
        if not imgs:
            continue
        link_text = (a.get_text(strip=True) or "")
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
# PDF export (ReportLab)
# ──────────────────────────────────────────────────────────────────────────────
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
            "This quick-check evaluates inline color contrast by default. Toggle computed-style contrast to include CSS colors. "
            "A full audit should also include keyboard navigation, focus order, landmarks, ARIA roles/states, forms, and media alternatives.",
            body,
        )
    )
    if notes:
        story.append(Spacer(1, 6))
        story.append(Paragraph(f"<i>Notes:</i> {notes}", body))

    doc.build(story)

# ──────────────────────────────────────────────────────────────────────────────
# UI
# ──────────────────────────────────────────────────────────────────────────────
inject_brand_css()

st.markdown("""
<div class="wy-accents">
  <h1>Accessibility Auditor — WCAG 2.2 AA <span class="wy-pill">Wyndham</span></h1>
  <p>Quick-check contrast (inline styles), image alts, and export a Wyndham-branded PDF. Use the Test Panel for reliable demo pages.</p>
</div>
""", unsafe_allow_html=True)

# Sidebar inputs
with st.sidebar:
    st.header("Scan Settings")
    wcag_level = st.selectbox("WCAG level", ["AA", "AAA"], index=0, help="Sets the contrast threshold (AA 4.5:1, AAA 7:1).")
    use_computed = st.checkbox("Use computed-style contrast (headless)", value=False, help="Requires Playwright + Chromium.")
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

    st.caption("Please ensure you have permission to scan the provided URL. Respect site terms and robots.txt.")
    st.markdown("---")

    cols = st.columns(2)
    with cols[0]:
        auto_save = st.checkbox("Auto-save after scan", value=st.session_state.get("auto_save", True), help="Automatically save successful scans to the Dashboard.")
        st.session_state["auto_save"] = auto_save
    with cols[1]:
        if st.button("Reset form"):
            for k in ("url_input", "latest_run"):
                st.session_state.pop(k, None)
            st.experimental_rerun()

# Session history store
if "history" not in st.session_state:
    st.session_state["history"] = []  # timestamp, url, scores, counts

# Run Audit row
st.subheader("Run Audit")
col1, col2, col3 = st.columns([1, 1, 1])
scan_html_clicked = col1.button("🔍 Scan HTML (Contrast & Alts)")
scan_pdfs_clicked = col2.button("📁 Scan PDFs (Tagging & Alts)", disabled=True, help="Coming soon — HTML quick-check is live.")
export_pdf_clicked = col3.button("💾 Generate PDF (Wyndham-branded)")

st.markdown("---")
results_box = st.container()
latest_run = st.session_state.get("latest_run")

# ======== HTML scan ========
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
        ok = False; err_msg = "Please provide a URL or paste HTML."

    if not ok:
        st.error(f"HTML scan failed: {err_msg}")
    else:
        with
