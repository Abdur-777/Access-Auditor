# app.py — Accessibility Auditor (Council Edition)
# Run: streamlit run app.py
# Core deps: streamlit, requests, beautifulsoup4, reportlab, pandas
# Optional: playwright (for computed-style contrast)

import os, io, csv, zipfile, time, ipaddress, shutil
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
import pandas as pd
import streamlit as st

# ──────────────────────────────────────────────────────────────────────────────
# CONFIG & BRAND
# ──────────────────────────────────────────────────────────────────────────────
st.set_page_config(page_title="Accessibility Auditor — WCAG 2.2 AA", layout="wide")

ORG_NAME = os.getenv("ORG_NAME", "Wyndham City Council")
BRAND = os.getenv("BRAND_PRIMARY", "#003B73")

DATA_DIR = os.getenv("DATA_DIR", "data")
RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "90"))
RATE_LIMIT_PER_MIN = int(os.getenv("RATE_LIMIT_PER_MIN", "12"))
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "20"))
FORCE_HTTPS = os.getenv("FORCE_HTTPS", "")

os.makedirs(DATA_DIR, exist_ok=True)

def inject_css():
    st.markdown(
        f"""
        <style>
          :root {{ --brand: {BRAND}; }}
          .stButton>button, .stDownloadButton>button {{
            background: var(--brand) !important; color: #fff !important; border: 0 !important;
            border-radius: 12px !important; padding: 8px 14px !important;
          }}
          .wy-accents h1, .wy-accents h2, .wy-accents h3, .wy-accents h4 {{ color: var(--brand); }}
          .wy-pill {{
            display:inline-block; background: rgba(0,59,115,.08); color: var(--brand);
            padding:2px 10px; border-radius:999px; font-weight:600; font-size:12px;
          }}
          /* visible focus */
          :focus {{ outline: 3px solid #1e90ff !important; outline-offset: 2px; }}
          /* skip link */
          .skip-link {{ position:absolute; left:-9999px; top:auto; width:1px; height:1px; overflow:hidden; }}
          .skip-link:focus {{ left: 8px; top: 8px; width:auto; height:auto; z-index:10000;
                               background:#fff; border:2px solid var(--brand); padding:6px 10px; }}
        </style>
        """,
        unsafe_allow_html=True,
    )

inject_css()

# ──────────────────────────────────────────────────────────────────────────────
# SIMPLE AUTH (env-based). For production, prefer a reverse proxy Basic Auth or OIDC.
# ──────────────────────────────────────────────────────────────────────────────
AUTH_USER = os.getenv("BASIC_AUTH_USER", "")
AUTH_PASS = os.getenv("BASIC_AUTH_PASS", "")
ADMIN_USERS = {u.strip() for u in os.getenv("ADMIN_USERS", "").split(",") if u.strip()}

def require_login():
    if not AUTH_USER or not AUTH_PASS:
        # Auth disabled; mark as admin for housekeeping
        st.session_state.setdefault("auth_user", "anon")
        st.session_state.setdefault("is_admin", True)
        return

    if "auth_user" in st.session_state:
        return

    with st.sidebar:
        st.subheader("Sign in")
        u = st.text_input("Username", key="auth_u", placeholder="user")
        p = st.text_input("Password", type="password", key="auth_p", placeholder="••••••••")
        if st.button("Sign in"):
            if u == AUTH_USER and p == AUTH_PASS:
                st.session_state["auth_user"] = u
                st.session_state["is_admin"] = (u in ADMIN_USERS) or (not ADMIN_USERS and True)
                st.experimental_rerun()
            else:
                st.error("Invalid credentials")
    st.stop()

require_login()

# ──────────────────────────────────────────────────────────────────────────────
# HOUSEKEEPING: retention purge, audit log helpers
# ──────────────────────────────────────────────────────────────────────────────
LOG_PATH = os.path.join(DATA_DIR, "audit_log.csv")

def purge_old_files():
    cutoff = datetime.utcnow() - timedelta(days=RETENTION_DAYS)
    removed = 0
    for root, _, files in os.walk(DATA_DIR):
        for f in files:
            p = os.path.join(root, f)
            try:
                mtime = datetime.utcfromtimestamp(os.path.getmtime(p))
                if mtime < cutoff:
                    os.remove(p); removed += 1
            except Exception:
                pass
    return removed

def log_scan(row: Dict):
    is_new = not os.path.exists(LOG_PATH)
    with open(LOG_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=[
            "timestamp","user","url","level","computed","contrast_checked","contrast_failed",
            "alt_issues","contrast_score","overall_score"
        ])
        if is_new:
            w.writeheader()
        w.writerow(row)

def delete_all_data():
    # wipe data dir except log? (we wipe all)
    try:
        shutil.rmtree(DATA_DIR)
    except Exception:
        pass
    os.makedirs(DATA_DIR, exist_ok=True)
    st.session_state.pop("history", None)
    st.session_state.pop("latest_run", None)

purge_old_files()

# ──────────────────────────────────────────────────────────────────────────────
# RATE LIMIT (per session)
# ──────────────────────────────────────────────────────────────────────────────
def check_rate_limit() -> bool:
    now = time.time()
    times = st.session_state.setdefault("scan_times", [])
    times = [t for t in times if now - t < 60]
    if len(times) >= RATE_LIMIT_PER_MIN:
        st.error(f"Rate limit: {RATE_LIMIT_PER_MIN} scans per minute. Please wait a moment.")
        return False
    times.append(now)
    st.session_state["scan_times"] = times
    return True

# ──────────────────────────────────────────────────────────────────────────────
# FETCH + SAFETY
# ──────────────────────────────────────────────────────────────────────────────
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
}

@st.cache_data(ttl=600, show_spinner=False)
def fetch_html(url: str, timeout: int) -> str:
    r = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
    if r.status_code in (403, 429):
        raise RuntimeError(f"{r.status_code} {r.reason} — site may block scraping; try 'Use computed-style contrast' or Paste HTML.")
    r.raise_for_status()
    text = r.text
    return text[:3_000_000]  # cap size

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
            pass
        return True
    except Exception:
        return False

# ──────────────────────────────────────────────────────────────────────────────
# COMPUTED-STYLE (Playwright) — optional
# ──────────────────────────────────────────────────────────────────────────────
def analyze_computed_contrast(url: str, level: str = "AA", timeout_ms: int = 30000):
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
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
        if(cr<t){{ failed++; if(out.length<50) out.push({{tag:el.tagName.toLowerCase(),text:(el.textContent||'').trim().slice(0,80),ratio:Math.round(cr*100)/100}}); }}
      }}
      return {{checked:els.length, failed, examples:out}};
    }})()
    """
    try:
        from playwright.sync_api import sync_playwright
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
# ANALYSIS (inline)
# ──────────────────────────────────────────────────────────────────────────────
import re
HEX_RE = re.compile(r"#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})")
RGB_RE = re.compile(r"rgb\((\d+),\s*(\d+),\s*(\d+)\)")
RGBA_RE = re.compile(r"rgba\((\d+),\s*(\d+),\s*(\d+),\s*(0|0?\.\d+|1)\)")
DECORATIVE_HINTS = re.compile(r"(border|spacer|decor|sprite|shadow|corner|bg|badge|icon)", re.I)
GENERIC_ALTS = {"image","photo","graphic","pic","icon","spacer"}

def parse_dimension(val) -> Optional[int]:
    if val is None: return None
    if isinstance(val, (int, float)): return int(float(val))
    s = str(val).strip(); num=[]; dot=False; sign=True
    for ch in s:
        if ch.isdigit(): num.append(ch); sign=False
        elif ch=='.' and not dot: num.append(ch); dot=True; sign=False
        elif ch in '+-' and sign: sign=False
        elif num: break
    try:
        n=''.join(num); return int(float(n)) if n else None
    except Exception:
        return None

def parse_color(value: str) -> Optional[Tuple[int, int, int]]:
    if not value: return None
    value=value.strip()
    m=HEX_RE.search(value)
    if m:
        s=m.group(1)
        if len(s)==3: r=int(s[0]*2,16); g=int(s[1]*2,16); b=int(s[2]*2,16)
        else: r=int(s[0:2],16); g=int(s[2:4],16); b=int(s[4:6],16)
        return (r,g,b)
    m=RGB_RE.search(value)
    if m: return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m=RGBA_RE.search(value)
    if m:
        r,g,b,a=int(m.group(1)),int(m.group(2)),int(m.group(3)),float(m.group(4))
        if a==0: return None
        return (r,g,b)
    NAMED={"black":(0,0,0),"white":(255,255,255),"gray":(128,128,128),"grey":(128,128,128),"red":(255,0,0)}
    return NAMED.get(value.lower())

def rel_luminance(rgb: Tuple[int,int,int]) -> float:
    def _lin(c): c=c/255.0; return c/12.92 if c <= 0.03928 else ((c+0.055)/1.055)**2.4
    r,g,b=rgb; R,G,B=_lin(r),_lin(g),_lin(b)
    return 0.2126*R+0.7152*G+0.0722*B

def contrast_ratio(c1: Tuple[int,int,int], c2: Tuple[int,int,int]) -> float:
    L1=rel_luminance(c1); L2=rel_luminance(c2)
    lighter=max(L1,L2); darker=min(L1,L2)
    return (lighter+0.05)/(darker+0.05)

def looks_like_filename(s: str) -> bool:
    if not s: return False
    if re.search(r"\.(png|jpe?g|gif|svg|webp)$", s, re.I): return True
    return bool(re.search(r"[a-z0-9_-]", s) and " " not in s)

def normalize_src(src: str) -> str:
    try:
        u=urlparse(src)
        if u.scheme and u.netloc: return f"{u.scheme}://{u.netloc}{u.path}"
        return u.path or src
    except Exception:
        return src

def suggest_alt(src: str, width: Optional[int]=None, height: Optional[int]=None, site_name: str="") -> Tuple[str,str]:
    path=urlparse(src).path if src else ""
    fname=path.split("/")[-1] if path else ""
    stem=re.sub(r"\.(png|jpg|jpeg|gif|svg|webp)$","",fname,flags=re.I)
    tiny=(width and width<24) or (height and height<24)
    if tiny or DECORATIVE_HINTS.search(stem or ""): return ("","decorative")
    if re.search(r"logo", stem or "", re.I): return (site_name.strip() or "Site", "logo")
    human=re.sub(r"[-_]+"," ",stem).strip().capitalize() if stem else "Image"
    human=(human[:77]+"…") if len(human)>80 else human
    return (human or "Image","descriptive")

def analyze_html(html: str, assume_bg=(255,255,255), site_name: str="", level: str="AA") -> Dict:
    soup=BeautifulSoup(html,"html.parser")
    threshold=7.0 if level=="AAA" else 4.5

    contrast_checked=0; contrast_failed=0; contrast_examples=[]
    for el in soup.find_all(True):
        style=(el.get("style") or "").lower()
        if not style: continue
        color_m=re.search(r"color\s*:\s*([^;]+)", style)
        bg_m=re.search(r"background(?:-color)?\s*:\s*([^;]+)", style)
        if not color_m: continue
        fg=parse_color(color_m.group(1)); bg=parse_color(bg_m.group(1)) if bg_m else assume_bg
        if not fg or not bg: continue
        ratio=contrast_ratio(fg,bg)
        contrast_checked+=1
        if ratio < threshold and len(contrast_examples)<8:
            contrast_failed+=1
            contrast_examples.append({"text":(el.get_text(strip=True) or "(no text)")[:80],
                                      "ratio":round(ratio,2), "tag":el.name})
        elif ratio < threshold:
            contrast_failed+=1

    img_nodes=soup.find_all("img")
    alt_issues=[]; seen=set()
    for img in img_nodes:
        src=img.get("src") or ""; 
        if not src: continue
        n=normalize_src(src)
        if n in seen: continue
        seen.add(n)
        cur=(img.get("alt") or "").strip()
        w=parse_dimension(img.get("width")); h=parse_dimension(img.get("height"))
        alt_s, cls = suggest_alt(src, w, h, site_name=site_name)
        if cur=="" or cur.lower() in GENERIC_ALTS or looks_like_filename(cur):
            alt_issues.append({"src": src, "suggested_alt": alt_s, "classification": cls})

    # anchor-only image without text/label
    for a in soup.find_all("a"):
        imgs=a.find_all("img")
        if not imgs: continue
        link_text=(a.get_text(strip=True) or "")
        link_label=(a.get("aria-label") or a.get("title") or "").strip()
        if not link_text and not link_label:
            for _img in imgs:
                if (_img.get("alt") or "").strip()=="":
                    alt_issues.append({
                        "src": _img.get("src") or "",
                        "suggested_alt": "Add aria-label/title on link (e.g., 'Home') or give the image that label.",
                        "classification": "link_image_no_text",
                    })
                    break

    return {"contrast_checked": contrast_checked, "contrast_failed": contrast_failed,
            "contrast_examples": contrast_examples, "alt_issues": alt_issues, "img_count": len(img_nodes)}

def compute_scores(contrast_checked: int, contrast_failed: int, alt_issues_count: int) -> Dict:
    contrast_score = 100.0 if contrast_checked == 0 else max(0.0, 100.0 * (1 - (contrast_failed / max(1, contrast_checked))))
    overall = max(0.0, contrast_score - min(40.0, float(alt_issues_count)))
    return {"contrast_score": round(contrast_score, 1), "overall_score": round(overall, 1)}

# ──────────────────────────────────────────────────────────────────────────────
# PDF EXPORT
# ──────────────────────────────────────────────────────────────────────────────
from reportlab.lib import colors
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.units import mm

def export_pdf_wyndham(filepath: str, url: str, scores: Dict, contrast_checked: int, contrast_failed: int, alt_issues: List[Dict], level_label: str, notes: str = ""):
    doc = SimpleDocTemplate(filepath, pagesize=A4, leftMargin=18*mm, rightMargin=18*mm, topMargin=18*mm, bottomMargin=18*mm)
    styles = getSampleStyleSheet()
    title = ParagraphStyle("title", parent=styles["Title"], textColor=HexColor(BRAND))
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], textColor=HexColor(BRAND))
    body = styles["BodyText"]
    story = []

    story.append(Paragraph("<b>Accessibility Auditor — WCAG 2.2</b>", title))
    story.append(Paragraph(datetime.now().strftime("%Y-%m-%d %H:%M UTC"), body))
    story.append(Spacer(1, 6))
    story.append(Paragraph(f"<b>Target URL:</b> {url}", body))
    story.append(Paragraph(f"<b>Contrast threshold:</b> {level_label}", body))
    story.append(Spacer(1, 10))

    data = [
        ["Overall Score", f"{scores['overall_score']}"],
        [f"Contrast Score (%) — {level_label}", f"{scores['contrast_score']}"],
        ["Elements Checked (contrast)", f"{contrast_checked}"],
        ["Contrast Fails", f"{contrast_failed}"],
        ["Images needing alt (count)", f"{len(alt_issues)}"],
    ]
    tbl = Table(data, hAlign="LEFT", colWidths=[80*mm, 80*mm])
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), HexColor(BRAND)),
        ("TEXTCOLOR", (0,0), (-1,0), colors.white),
        ("GRID", (0,0), (-1,-1), 0.25, colors.grey),
        ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.whitesmoke, colors.lightgrey]),
    ]))
    story.append(tbl); story.append(Spacer(1, 12))

    story.append(Paragraph("Images needing alt — examples", h2))
    if alt_issues:
        rows = [["Image src (truncated)", "Suggested alt", "Type"]]
        for issue in alt_issues[:12]:
            rows.append([(issue.get("src") or "")[-80:], issue.get("suggested_alt",""), issue.get("classification","")])
        t2 = Table(rows, hAlign="LEFT", colWidths=[85*mm, 65*mm, 25*mm])
        t2.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,0), HexColor(BRAND)),
            ("TEXTCOLOR", (0,0), (-1,0), colors.white),
            ("GRID", (0,0), (-1,-1), 0.25, colors.grey),
            ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
        ]))
        story.append(t2)
    else:
        story.append(Paragraph("No missing/weak alt text detected.", body))
    story.append(Spacer(1, 10))

    story.append(Paragraph("Methods & Limitations", h2))
    story.append(Paragraph(
        "This tool provides an automated quick-check. Inline contrast is always scanned; you may enable computed-style contrast to include CSS colors. "
        "Full compliance requires manual testing (keyboard, focus order, screen reader behavior, forms, media, PDFs, reflow/zoom, reduced motion). "
        "Results indicate potential issues and are not a legal certification.", body))
    if notes:
        story.append(Spacer(1,6)); story.append(Paragraph(f"<i>Notes:</i> {notes}", body))
    doc.build(story)

# ──────────────────────────────────────────────────────────────────────────────
# UI — HEADER
# ──────────────────────────────────────────────────────────────────────────────
st.markdown('<a class="skip-link" href="#results">Skip to results</a>', unsafe_allow_html=True)

st.markdown(
    f"""
<div class="wy-accents">
  <h1>Accessibility Auditor — WCAG 2.2 AA <span class="wy-pill">{ORG_NAME}</span></h1>
  <p>Quick-check contrast, “images needing alt”, CSV exports and a branded PDF. Use the Test Panel for reliable demo pages.</p>
  <p><small><b>Scope:</b> Automated checks only (contrast, image alts). Optional computed-style contrast. Manual testing required for full conformance.</small></p>
</div>
""",
    unsafe_allow_html=True,
)

if not FORCE_HTTPS:
    st.warning("HTTPS/Proxy hardening not detected (FORCE_HTTPS env not set). In production, terminate TLS and restrict CORS at your reverse proxy.")

# ──────────────────────────────────────────────────────────────────────────────
# SIDEBAR — SETTINGS & ADMIN
# ──────────────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("Scan Settings")
    wcag_level = st.selectbox("WCAG level (contrast)", ["AA","AAA"], index=0, help="AA=4.5:1 for normal text; AAA=7:1.")
    use_computed = st.checkbox("Use computed-style contrast (headless)", value=False, help="Requires Playwright + Chromium.")
    url_input = st.text_input("Page URL to scan (HTML)", value=st.session_state.get("url_input",""), placeholder="https://example.com/page")

    with st.expander("Test Panel", expanded=False):
        c1, c2 = st.columns(2)
        if c1.button("W3C Bad (Before)"):
            url_input = "https://www.w3.org/WAI/demos/bad/before/home.html"; st.session_state["url_input"]=url_input
        if c2.button("W3C Good (After)"):
            url_input = "https://www.w3.org/WAI/demos/bad/after/home.html"; st.session_state["url_input"]=url_input

    with st.expander("Paste HTML instead (fallback)", expanded=False):
        pasted_html = st.text_area("Raw HTML", height=140, placeholder="Paste a full HTML document here…")
        use_pasted = st.checkbox("Use pasted HTML for this scan", value=False)

    st.markdown("---")
    auto_save = st.checkbox("Auto-save after scan", value=st.session_state.get("auto_save", True))
    st.session_state["auto_save"] = auto_save

    if st.session_state.get("is_admin"):
        st.subheader("Admin")
        st.caption(f"Data dir: `{DATA_DIR}` • Retention: {RETENTION_DAYS} days • Rate: {RATE_LIMIT_PER_MIN}/min")
        if st.button("Delete all data"):
            delete_all_data()
            st.success("All stored data deleted.")
        if os.path.exists(LOG_PATH):
            with open(LOG_PATH, "rb") as f:
                st.download_button("Download audit log (CSV)", f, file_name="audit_log.csv", mime="text/csv")

# ──────────────────────────────────────────────────────────────────────────────
# SESSION
# ──────────────────────────────────────────────────────────────────────────────
if "history" not in st.session_state:
    st.session_state["history"] = []

# ──────────────────────────────────────────────────────────────────────────────
# RUN ROW (side-by-side)
# ──────────────────────────────────────────────────────────────────────────────
st.subheader("Run")
cA, cB, cC = st.columns([1,1,1])
scan_html_clicked = cA.button("🔎 Scan HTML (Contrast & Images)")
export_pdf_clicked = cB.button("💾 Generate Audit PDF")
draft_conf_clicked = cC.button("📄 Draft Conformance Statement (open form)", help="Fills a draft you can finalize.")

st.markdown("---")

results_box = st.container()
latest_run = st.session_state.get("latest_run")

# ──────────────────────────────────────────────────────────────────────────────
# SCAN
# ──────────────────────────────────────────────────────────────────────────────
if scan_html_clicked:
    if not check_rate_limit():
        pass
    else:
        if use_pasted and pasted_html.strip():
            html = pasted_html; url_for_report = url_input or "(pasted HTML)"; ok=True; err=""
        elif url_input.strip():
            if not is_public_http_url(url_input.strip()):
                ok=False; err="URL must be public http(s) (not localhost/private)."
            else:
                try:
                    with st.spinner("Fetching page…"):
                        html = fetch_html(url_input.strip(), timeout=HTTP_TIMEOUT)
                    url_for_report = url_input.strip(); ok=True; err=""
                except Exception as e:
                    ok=False; err=str(e)
        else:
            ok=False; err="Provide a URL or paste HTML."

        if not ok:
            st.error(f"HTML scan failed: {err}")
        else:
            with st.spinner("Analyzing…"):
                report = analyze_html(html, assume_bg=(255,255,255), site_name=ORG_NAME, level=wcag_level)
                # Optional computed-style override
                used_computed = False
                if use_computed and url_input.strip():
                    comp = analyze_computed_contrast(url_input.strip(), level=wcag_level)
                    if comp and not comp.get("error"):
                        report["contrast_checked"]  = int(comp.get("checked", report["contrast_checked"]))
                        report["contrast_failed"]   = int(comp.get("failed",  report["contrast_failed"]))
                        report["contrast_examples"] = comp.get("examples", report["contrast_examples"]) or []
                        used_computed = True
                    else:
                        st.warning(f"Computed-style contrast unavailable: {comp.get('error','unknown error') if isinstance(comp, dict) else 'error'} — showing inline-only results.")

                scores = compute_scores(report["contrast_checked"], report["contrast_failed"], len(report["alt_issues"]))
                latest_run = {
                    "timestamp": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                    "url": url_for_report,
                    "scores": scores,
                    "contrast_checked": report["contrast_checked"],
                    "contrast_failed": report["contrast_failed"],
                    "alt_issues": report["alt_issues"],
                    "contrast_examples": report["contrast_examples"],
                    "used_computed": used_computed,
                    "level": wcag_level,
                }
                st.session_state["latest_run"] = latest_run

                # Audit log row
                log_scan({
                    "timestamp": latest_run["timestamp"],
                    "user": st.session_state.get("auth_user","anon"),
                    "url": url_for_report,
                    "level": wcag_level,
                    "computed": "yes" if used_computed else "no",
                    "contrast_checked": report["contrast_checked"],
                    "contrast_failed": report["contrast_failed"],
                    "alt_issues": len(report["alt_issues"]),
                    "contrast_score": scores["contrast_score"],
                    "overall_score": scores["overall_score"],
                })

                if st.session_state.get("auto_save", False):
                    st.session_state["history"].append({
                        "timestamp": latest_run["timestamp"],
                        "url": latest_run["url"],
                        "contrast_score": latest_run["scores"]["contrast_score"],
                        "overall_score": latest_run["scores"]["overall_score"],
                        "contrast_checked": latest_run["contrast_checked"],
                        "contrast_failed": latest_run["contrast_failed"],
                        "alt_issues": len(latest_run["alt_issues"]),
                        "level": wcag_level,
                        "computed": "yes" if used_computed else "no",
                    })
                    st.success("Saved automatically to Dashboard.")

# ──────────────────────────────────────────────────────────────────────────────
# PDF EXPORT
# ──────────────────────────────────────────────────────────────────────────────
if export_pdf_clicked:
    run = st.session_state.get("latest_run")
    if not run:
        st.warning("Run an HTML scan first.")
    else:
        path = os.path.join(DATA_DIR, "audit_report.pdf")
        export_pdf_wyndham(
            path,
            url=run["url"],
            scores=run["scores"],
            contrast_checked=run["contrast_checked"],
            contrast_failed=run["contrast_failed"],
            alt_issues=run["alt_issues"],
            level_label=f"{run['level']}",
        )
        with open(path, "rb") as f:
            st.download_button("Download PDF report", f, file_name="audit_report.pdf")
        st.success("Report ready.")

# ──────────────────────────────────────────────────────────────────────────────
# RESULTS
# ──────────────────────────────────────────────────────────────────────────────
if latest_run:
    with results_box:
        st.markdown('<div id="results"></div>', unsafe_allow_html=True)
        st.subheader("Results")
        m1, m2, m3 = st.columns(3)
        m1.metric(f"Contrast Score (%) — {latest_run['level']}", latest_run["scores"]["contrast_score"])
        m2.metric("Overall Score", latest_run["scores"]["overall_score"])
        m3.metric("Images needing alt", len(latest_run["alt_issues"]))

        # Downloads
        if latest_run["alt_issues"]:
            alt_df = pd.DataFrame(latest_run["alt_issues"])
            st.download_button("Download CSV (Alt issues)", data=alt_df.to_csv(index=False).encode("utf-8"),
                               file_name="alt_issues.csv", mime="text/csv")

        # Overall CSV (contrast + alts)
        overall_rows = []
        for ex in latest_run.get("contrast_examples", [])[:200]:
            overall_rows.append({"type":"contrast","tag":ex.get("tag",""),"text":ex.get("text",""),
                                 "ratio":ex.get("ratio",""),"src":"","suggested_alt":"","classification":""})
        for it in latest_run.get("alt_issues", []):
            overall_rows.append({"type":"alt","tag":"img","text":"","ratio":"",
                                 "src":it.get("src",""),"suggested_alt":it.get("suggested_alt",""),
                                 "classification":it.get("classification","")})
        overall_df = pd.DataFrame(overall_rows) if overall_rows else pd.DataFrame(columns=[
            "type","tag","text","ratio","src","suggested_alt","classification"
        ])
        st.download_button("Download CSV (Overall issues)",
                           data=overall_df.to_csv(index=False).encode("utf-8"),
                           file_name="overall_issues.csv", mime="text/csv")

        # Contrast examples
        with st.expander("Contrast fails (examples)"):
            if latest_run["contrast_failed"] == 0:
                st.write("No contrast failures found.")
            else:
                for ex in latest_run.get("contrast_examples", [])[:10]:
                    st.write(f"• <{ex.get('tag','?')}> ratio {ex.get('ratio','?')}: {ex.get('text','')}")

        # Images needing alt + breakdown
        with st.expander("Images needing alt — details"):
            if not latest_run["alt_issues"]:
                st.write("No missing/weak alt text detected.")
            else:
                kinds = pd.Series([i.get("classification","") for i in latest_run["alt_issues"]]).value_counts()
                st.caption("Breakdown: " + " • ".join(f"{k.capitalize()}: {v}" for k,v in kinds.items()))
                for item in latest_run["alt_issues"]:
                    st.markdown(f"- `{item['src']}` → **{item['suggested_alt'] or '(decorative)'}** *({item['classification']})*")

# ──────────────────────────────────────────────────────────────────────────────
# DASHBOARD & POLICY
# ──────────────────────────────────────────────────────────────────────────────
st.subheader("Dashboard")
hist = st.session_state.get("history", [])
if not hist:
    st.info("No history yet. Run a scan and Save (auto-save is on by default).")
else:
    st.dataframe(pd.DataFrame(hist), use_container_width=True)

with st.expander("Policy & Security"):
    st.markdown(f"""
**Privacy.** We do not store personal data beyond the scan history and generated files inside `{DATA_DIR}`.  
**Retention.** Files are purged after **{RETENTION_DAYS} days** (configurable).  
**Security.** Run behind HTTPS. Lock down CORS at your proxy. Rate limit is **{RATE_LIMIT_PER_MIN}/min per session**.  
**Audit log.** Every scan is appended to `audit_log.csv` (timestamp, user, URL, counts/scores).  
**Accessibility of this tool.** Keyboardable UI with visible focus; headings and labels are provided.  
**Disclaimer.** Automated quick-check only; not a legal certification. Manual testing is required for full WCAG conformance.
""")
