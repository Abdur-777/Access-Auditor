# app.py — Accessibility Auditor (generic, no council branding)
# Run: streamlit run app.py

from __future__ import annotations
import os, re, io, json, time, tempfile, math, textwrap, base64, datetime as dt
from pathlib import Path
from typing import Dict, List, Tuple, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup, Tag
import pandas as pd
import streamlit as st

# -----------------------------
# Preset smoke-test suite
# -----------------------------
SMOKE_CASES = [
    ("Council Melbourne", "https://www.melbourne.vic.gov.au/", "ok"),
    ("Council Sydney",    "https://www.sydney.nsw.gov.au/",    "ok"),
    ("Council Brisbane",  "https://www.brisbane.qld.gov.au/",  "ok"),
    ("404 page",          "https://httpbin.org/status/404",     "err:404"),  # stable 404
    ("PDF file",          "https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf", "err:application/pdf"),
    ("Long page",         "https://en.wikipedia.org/wiki/Australia", "ok"),
]

# If a primary URL fails for 'ok' cases, try these alternates in order.
SMOKE_ALTERNATIVES = {
    "https://www.sydney.nsw.gov.au/": [
        "https://www.nsw.gov.au/",
        "https://www.vic.gov.au/",
    ],
}

def _smoke_expectation_passed(err: Optional[str], items: List[Dict[str, str]], expect: str) -> bool:
    """Pass if we got HTML for 'ok', or the expected error token for 'err:<token>'. Looser 404 match."""
    if expect == "ok":
        return err is None
    if expect.startswith("err:"):
        token = expect.split(":", 1)[1].lower()
        hay = (err or "").lower()
        if not hay and items:
            hay = (items[0].get("snippet") or "").lower()
        if token == "404":
            return ("404" in hay) or ("not found" in hay)
        return token in hay
    return False

# -----------------------------
# Writable data directories
# -----------------------------
def resolve_data_dir() -> str:
    env_dir = os.getenv("DATA_DIR")
    if env_dir:
        try:
            Path(env_dir).mkdir(parents=True, exist_ok=True)
            return env_dir
        except PermissionError:
            pass
    tmp_dir = os.path.join(tempfile.gettempdir(), "access-auditor")
    Path(tmp_dir).mkdir(parents=True, exist_ok=True)
    return tmp_dir

DATA_DIR    = resolve_data_dir()
UPLOADS_DIR = os.path.join(DATA_DIR, "uploads")
EXPORTS_DIR = os.path.join(DATA_DIR, "exports")
CACHE_DIR   = os.path.join(DATA_DIR, "cache")
for _d in (UPLOADS_DIR, EXPORTS_DIR, CACHE_DIR):
    Path(_d).mkdir(parents=True, exist_ok=True)

# -----------------------------
# Generic brand (no council)
# -----------------------------
PRIMARY = os.getenv("BRAND_PRIMARY", "#0F4C81")  # classic civic blue
APP_NAME = os.getenv("BRAND_NAME", "Accessibility Auditor")
BRAND_LOGO_URL = os.getenv("BRAND_LOGO_URL", "")  # supply your own if you like

def civic_logo_data_uri(primary: str) -> str:
    """Simple civic/pillars SVG as a data URI (no external dependencies)."""
    svg = f"""
    <svg width="120" height="120" viewBox="0 0 120 120" xmlns="http://www.w3.org/2000/svg">
      <rect width="120" height="120" rx="24" fill="{primary}"/>
      <polygon points="60,22 18,44 102,44" fill="white"/>
      <rect x="26" y="48" width="68" height="8" fill="white"/>
      <rect x="32" y="56" width="10" height="38" fill="white"/>
      <rect x="55" y="56" width="10" height="38" fill="white"/>
      <rect x="78" y="56" width="10" height="38" fill="white"/>
      <rect x="22" y="94" width="76" height="6" fill="white"/>
    </svg>
    """.strip()
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode()

BRAND_LOGO = BRAND_LOGO_URL if BRAND_LOGO_URL else civic_logo_data_uri(PRIMARY)
BRAND = {"name": APP_NAME, "primary": PRIMARY, "logo": BRAND_LOGO}

# -----------------------------
# UI Setup
# -----------------------------
st.set_page_config(
    page_title=APP_NAME,
    page_icon="✅",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# Hide Streamlit chrome (menu + footer)
st.markdown("<style>#MainMenu{visibility:hidden} footer{visibility:hidden}</style>", unsafe_allow_html=True)

# Light/Dark toggle state
if "theme" not in st.session_state:
    st.session_state["theme"] = "light"

def toggle_theme():
    st.session_state["theme"] = "dark" if st.session_state["theme"] == "light" else "light"

# --- Dynamic theme CSS (styles the whole app) ---
theme = st.session_state.get("theme", "light")
is_dark = theme == "dark"

bg      = "#0f172a" if is_dark else "#ffffff"   # page background
text    = "#e5e7eb" if is_dark else "#0b1220"   # main text
card_bg = "#0f172a" if is_dark else "#ffffff"   # card background
border  = "#1f2937" if is_dark else "#e2e8f0"   # borders
muted   = "#cbd5e1" if is_dark else "#64748b"   # captions/muted
table_h = "#111827" if is_dark else "#f8fafc"   # table header

st.markdown(f"""
<style>
:root {{ --wy-primary: {PRIMARY}; }}

/* App background + text */
html, body, .stApp {{ background: {bg}; color: {text}; }}

/* Cards */
.card {{
  border: 1px solid {border};
  border-radius: 16px;
  padding: 18px 18px 16px 18px;
  background: {card_bg};
  box-shadow: 0 2px 18px rgba(0,0,0,{0.35 if is_dark else 0.06});
}}

/* Tabs underline */
[data-baseweb="tab-list"] {{ border-bottom: 2px solid {border}; }}

/* DataFrame header */
.stDataFrame thead tr th {{ background: {table_h}; color: {text}; }}

/* Buttons (primary look) */
.stButton > button, .stDownloadButton > button {{
  background: var(--wy-primary);
  color: #fff;
  border: none;
  border-radius: 999px;
  padding: 8px 14px;
  font-weight: 700;
}}

/* Muted/caption */
.small, .stCaption, .stMarkdown p small {{ color: {muted}; }}
</style>
""", unsafe_allow_html=True)

# Header with logo + theme toggle
cols_head = st.columns([1, 6, 2])
with cols_head[0]:
    st.image(BRAND["logo"], width=86)
with cols_head[1]:
    st.markdown(f"### {APP_NAME}")
    st.caption("Quick WCAG checks for public pages (beta)")
with cols_head[2]:
    st.write("")
    st.write("")
    if st.button(("🌙 Dark mode" if st.session_state["theme"] == "light" else "☀️ Light mode"),
                 use_container_width=True, key="toggle_theme"):
        toggle_theme()
        st.rerun()

# Tabs (includes 🧪 Smoke Test)
scan_tab, results_tab, dashboard_tab, smoke_tab = st.tabs(
    ["🔍 Scan", "📊 Results", "📁 Dashboard", "🧪 Smoke Test"]
)

# --- Sidebar intentionally empty ---

# -----------------------------
# Helpers and checks
# -----------------------------
URL_RE = re.compile(r"^https?://", re.I)

GENERIC_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-AU,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

def _session_with_retries() -> requests.Session:
    s = requests.Session()
    retries = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET", "HEAD"]),
        raise_on_status=False,
    )
    s.mount("http://", HTTPAdapter(max_retries=retries))
    s.mount("https://", HTTPAdapter(max_retries=retries))
    s.headers.update(GENERIC_HEADERS)
    return s

def normalize_url(u: str) -> str:
    u = (u or "").strip()
    if not u:
        return u
    if not URL_RE.search(u):
        u = "https://" + u
    return u

def fetch(url: str, timeout: int = 20) -> Tuple[Optional[str], Optional[str], Optional[int]]:
    """Fetch URL robustly with retries and a browser-like UA. Returns (html, error, status_code)."""
    http = _session_with_retries()
    try:
        r = http.get(url, timeout=timeout, allow_redirects=True)
        ct = r.headers.get("Content-Type", "") or ""
        if "text/html" not in ct and "application/xhtml+xml" not in ct:
            return None, f"Unsupported content type: {ct}", r.status_code
        r.raise_for_status()  # raises HTTPError on 4xx/5xx
        return r.text, None, r.status_code
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if getattr(e, "response", None) is not None else None
        return None, str(e), status
    except requests.exceptions.RequestException as e:
        # network/ssl/reset/timeouts
        return None, str(e), None

def get_text_snippet(tag: Tag, max_len: int = 140) -> str:
    txt = tag.get_text(" ", strip=True) if isinstance(tag, Tag) else ""
    txt = re.sub(r"\s+", " ", txt)
    return (txt[: max_len - 1] + "…") if len(txt) > max_len else txt

def px_value(v: str) -> Optional[float]:
    m = re.search(r"([0-9.]+)\s*px", v or "", flags=re.I)
    return float(m.group(1)) if m else None

def parse_color(value: str) -> Optional[Tuple[float, float, float]]:
    if not value:
        return None
    value = value.strip().lower()
    m = re.match(r"#([0-9a-f]{3,8})", value)
    if m:
        h = m.group(1)
        if len(h) in (3, 4):
            r = int(h[0]*2, 16); g = int(h[1]*2, 16); b = int(h[2]*2, 16)
        else:
            r = int(h[0:2], 16); g = int(h[2:4], 16); b = int(h[4:6], 16)
        return (r/255.0, g/255.0, b/255.0)
    m = re.match(r"rgb\(\s*([0-9.]+)\s*,\s*([0-9.]+)\s*,\s*([0-9.]+)\s*\)", value)
    if m:
        r, g, b = map(lambda x: float(x)/255.0, m.groups())
        return (r, g, b)
    return None

def rel_luminance(rgb: Tuple[float,float,float]) -> float:
    def chan(c):
        return c/12.92 if c <= 0.03928 else ((c+0.055)/1.055)**2.4
    r, g, b = rgb
    return 0.2126*chan(r) + 0.7152*chan(g) + 0.0722*chan(b)

def contrast_ratio(c1: Tuple[float,float,float], c2: Tuple[float,float,float]) -> float:
    L1, L2 = rel_luminance(c1), rel_luminance(c2)
    lighter = max(L1, L2); darker = min(L1, L2)
    return (lighter + 0.05) / (darker + 0.05)

def inline_style_dict(style: str) -> Dict[str, str]:
    out = {}
    for part in (style or "").split(";"):
        if ":" in part:
            k, v = part.split(":", 1)  # ✅ fixed
            out[k.strip().lower()] = v.strip()
    return out

GENERIC_LINK_TEXT = {"click here", "read more", "more", "learn more", "here"}

def analyze_html(url: str, html: str) -> List[Dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    issues: List[Dict[str, str]] = []

    # 1) <title> present
    if not soup.title or not soup.title.string or not soup.title.string.strip():
        issues.append(dict(
            url=url, check="Missing <title>", severity="MED",
            tag="title", snippet="", recommendation="Add a descriptive <title> for the page."
        ))

    # 2) <html lang="">
    html_tag = soup.find("html")
    lang = (html_tag.get("lang") or "").strip().lower() if html_tag else ""
    if not lang:
        issues.append(dict(
            url=url, check="Missing language", severity="MED",
            tag="html", snippet="", recommendation="Set <html lang='en'> (or appropriate language)."
        ))

    # 3) Images need alt
    for img in soup.find_all("img"):
        alt = img.get("alt"); role = img.get("role")
        if role == "presentation":
            continue
        if alt is None or alt.strip() == "":
            issues.append(dict(
                url=url, check="Image missing alt text", severity="HIGH",
                tag=str(img.name), snippet=(img.get("src") or "")[:140],
                recommendation="Provide meaningful alt text or mark as decorative with role='presentation'."
            ))

    # 4) Form controls need accessible name
    form_controls = soup.find_all(["input","select","textarea"])
    for el in form_controls:
        if el.name == "input" and (el.get("type") or "").lower() == "hidden":
            continue
        has_label = False
        if el.get("aria-label") or el.get("aria-labelledby"):
            has_label = True
        el_id = el.get("id")
        if el_id:
            lbl = soup.find("label", attrs={"for": el_id})
            if lbl and lbl.get_text(strip=True):
                has_label = True
        if not has_label:
            parent = el.parent
            while isinstance(parent, Tag):
                if parent.name == "label" and parent.get_text(strip=True):
                    has_label = True; break
                parent = parent.parent
        if not has_label:
            issues.append(dict(
                url=url, check="Form control without label", severity="HIGH",
                tag=el.name, snippet=get_text_snippet(el),
                recommendation="Use <label for='id'>, aria-label, or aria-labelledby to name the control."
            ))

    # 5) Heading order (avoid large jumps)
    heading_levels = []
    for h in soup.find_all(re.compile(r"^h[1-6]$")):
        try:
            level = int(h.name[1]); heading_levels.append((level, get_text_snippet(h)))
        except Exception:
            pass
    for i in range(1, len(heading_levels)):
        prev, _ = heading_levels[i-1]
        curr, snippet = heading_levels[i]
        if curr - prev > 1:
            issues.append(dict(
                url=url, check="Heading level jump", severity="LOW",
                tag=f"h{curr}", snippet=snippet,
                recommendation="Avoid skipping heading levels; maintain hierarchical structure."
            ))

    # 6) Tables: ensure header cells
    for tbl in soup.find_all("table"):
        if not tbl.find("th"):
            issues.append(dict(
                url=url, check="Table without headers", severity="MED",
                tag="table", snippet=get_text_snippet(tbl),
                recommendation="Use <th> for header cells and scope='col/row' where appropriate."
            ))

    # 7) Links with poor text
    for a in soup.find_all("a"):
        txt = (a.get_text(" ", strip=True) or "").lower()
        if not txt or txt in GENERIC_LINK_TEXT:
            issues.append(dict(
                url=url, check="Non-descriptive link text", severity="LOW",
                tag="a", snippet=get_text_snippet(a),
                recommendation="Use meaningful link text describing the destination."
            ))

    # 8) Basic color contrast for inline-styled text
    for el in soup.find_all(True):
        style = inline_style_dict(el.get("style") or "")
        if not style: continue
        fg = parse_color(style.get("color", ""))
        bg = parse_color(style.get("background-color", ""))
        if fg and bg:
            ratio = contrast_ratio(fg, bg)
            fs = px_value(style.get("font-size","") or "")
            weight = (style.get("font-weight","") or "400")
            large = (fs and fs >= 18.0) or ((fs and fs >= 14.0) and (weight.isdigit() and int(weight) >= 700))
            threshold = 3.0 if large else 4.5
            if ratio < threshold:
                issues.append(dict(
                    url=url, check="Low color contrast", severity="HIGH",
                    tag=el.name, snippet=get_text_snippet(el),
                    recommendation=f"Increase contrast (ratio {ratio:.2f} < {threshold:.1f}); adjust text or background."
                ))
    return issues

def audit_url(url: str) -> Tuple[List[Dict[str,str]], Optional[str]]:
    url = normalize_url(url)
    if not url:
        return [], "Empty URL."
    html, err, status = fetch(url)
    if err or not html:
        return [dict(
            url=url, check="Fetch failed", severity="HIGH", tag="-",
            snippet=str(err or f"HTTP {status}"), recommendation="Verify URL and connectivity; ensure the page returns HTML."
        )], err or f"HTTP {status}"
    issues = analyze_html(url, html)
    return issues, None

def results_to_df(results: List[Dict[str,str]]) -> pd.DataFrame:
    if not results:
        return pd.DataFrame(columns=["url","severity","check","tag","snippet","recommendation"])
    df = pd.DataFrame(results)
    cols = ["url","severity","check","tag","snippet","recommendation"]
    df = df[[c for c in cols if c in df.columns]]
    sev_order = {"HIGH":0,"MED":1,"LOW":2}
    df["sev_rank"] = df["severity"].map(lambda s: sev_order.get(str(s).upper(), 9))
    df = df.sort_values(["sev_rank","url","check"]).drop(columns=["sev_rank"])
    return df

def summarize(df: pd.DataFrame) -> Dict[str,int]:
    if df.empty:
        return {"HIGH":0,"MED":0,"LOW":0,"TOTAL":0,"URLS":0}
    cts = df["severity"].str.upper().value_counts().to_dict()
    out = {"HIGH": cts.get("HIGH",0), "MED": cts.get("MED",0), "LOW": cts.get("LOW",0)}
    out["TOTAL"] = int(df.shape[0]); out["URLS"] = int(df["url"].nunique())
    return out

def save_json(obj: dict, name: str) -> str:
    path = os.path.join(EXPORTS_DIR, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    return path

def df_to_csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8")

def df_to_json_bytes(df: pd.DataFrame) -> bytes:
    return df.to_json(orient="records", indent=2).encode("utf-8")

def df_to_html_bytes(df: pd.DataFrame, title: str, branding: Dict) -> bytes:
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    css = f"""
    <style>
      body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif; margin:24px; }}
      h1 {{ color:{branding['primary']}; }}
      .summary {{ margin: 8px 0 16px 0; color:#475569; }}
      table {{ border-collapse: collapse; width: 100%; }}
      th, td {{ text-align: left; padding: 8px; border-bottom: 1px solid #e2e8f0; vertical-align: top; }}
      th {{ background:#f8fafc; }}
      .badge {{ display:inline-block; padding:2px 8px; border-radius:999px; font-size:12px; font-weight:700; }}
      .HIGH {{ background:#fee2e2; color:#991b1b; border:1px solid #fecaca; }}
      .MED  {{ background:#fef3c7; color:#92400e; border:1px solid #fde68a; }}
      .LOW  {{ background:#ecfeff; color:#155e75; border:1px solid #a5f3fc; }}
      .muted {{ color:#64748b; font-size:12px; }}
    </style>
    """
    df2 = df.copy()
    df2["severity"] = df2["severity"].apply(lambda s: f"<span class='badge {s}'>{s}</span>")
    html_table = df2.to_html(escape=False, index=False)
    html = f"""<!doctype html><html><head><meta charset="utf-8"><title>{title}</title>{css}</head>
    <body>
      <img src="{branding['logo']}" alt="logo" style="height:44px;vertical-align:middle;margin-right:10px;"/>
      <span style="font-weight:800;font-size:18px;color:{branding['primary']};">{branding['name']}</span>
      <h1>Accessibility Audit Report</h1>
      <div class="summary">Generated: {now}</div>
      {html_table}
      <p class="muted">This is a quick automated check. A manual review is recommended for WCAG conformance.</p>
    </body></html>"""
    return html.encode("utf-8")

# -----------------------------
# Session storage
# -----------------------------
if "results" not in st.session_state:
    st.session_state["results"] = []
if "last_run_meta" not in st.session_state:
    st.session_state["last_run_meta"] = {}

# -----------------------------
# SCAN TAB
# -----------------------------
with scan_tab:
    # Single URL card
    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.subheader("Run Audit")
    st.caption("Scan a single URL or paste multiple URLs in the batch tool.")
    url = st.text_input(
        "Page URL",
        value="https://www.australia.gov.au/",
        placeholder="https://example.gov.au/path",
        label_visibility="collapsed",
        key="single_url",
    )
    c1, c2 = st.columns([1,1])
    with c1:
        run_single = st.button("Run Audit", use_container_width=True, key="btn_run_single")
    with c2:
        clear_btn = st.button("Clear Results", use_container_width=True, key="btn_clear")
    st.markdown('</div>', unsafe_allow_html=True)

    # Batch Scan card
    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.subheader("Batch Scan")
    st.caption("Paste multiple URLs (one per line)")
    batch_text = st.text_area(
        "URLs",
        height=180,
        placeholder="https://example.org/page-1\nhttps://example.org/page-2",
        label_visibility="collapsed",
        key="batch_urls",
    )
    c_run, c_pad, c_max = st.columns([1, 2, 1])
    with c_run:
        run_batch = st.button("Run Batch Scan", use_container_width=True, key="btn_run_batch")
    with c_max:
        max_n = st.number_input("Max pages", min_value=1, max_value=500, value=25, step=1, key="max_pages")
    st.markdown('</div>', unsafe_allow_html=True)

    # Actions
    if clear_btn:
        st.session_state["results"] = []
        st.session_state["last_run_meta"] = {}
        st.success("Cleared previous results.")

    if run_single:
        st.info("Scanning…")
        issues, _err = audit_url(url)
        st.session_state["results"] = issues
        st.session_state["last_run_meta"] = {"urls": [normalize_url(url)], "ts": dt.datetime.utcnow().isoformat()}
        st.success(f"Done. Found {len(issues)} issue(s). See the Results tab.")

    if run_batch and batch_text.strip():
        urls = [normalize_url(u) for u in batch_text.splitlines() if u.strip()]
        urls = [u for u in urls if u]
        if not urls:
            st.warning("No valid URLs provided.")
        else:
            results: List[Dict[str,str]] = []
            prog = st.progress(0)
            status_area = st.empty()
            n = min(len(urls), int(max_n))
            for i, u in enumerate(urls[:n], start=1):
                status_area.write(f"Scanning {i}/{n}: {u}")
                items, _ = audit_url(u)
                results.extend(items)
                prog.progress(int(i/n*100))
            st.session_state["results"] = results
            st.session_state["last_run_meta"] = {"urls": urls[:n], "ts": dt.datetime.utcnow().isoformat()}
            cts = summarize(results_to_df(results))
            st.success(f"Batch complete — {cts['TOTAL']} issues across {cts['URLS']} page(s). See the Results tab.")

# -----------------------------
# RESULTS TAB
# -----------------------------
with results_tab:
    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.subheader("Summary")
    df = results_to_df(st.session_state.get("results", []))
    cts = summarize(df)
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("HIGH", cts["HIGH"])
    c2.metric("MED",  cts["MED"])
    c3.metric("LOW",  cts["LOW"])
    c4.metric("Total",cts["TOTAL"])
    c5.metric("Pages",cts["URLS"])
    st.markdown('</div>', unsafe_allow_html=True)

    # Grouped view (URL → Severity → Check)
    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.subheader("Grouped view")
    if df.empty:
        st.caption("No results yet.")
    else:
        df_group = (
            df.groupby(["url", "severity", "check"])
              .size().reset_index(name="count")
              .sort_values(["url", "severity", "count"], ascending=[True, True, False])
        )
        st.dataframe(df_group, use_container_width=True, hide_index=True)
    st.markdown('</div>', unsafe_allow_html=True)

    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.subheader("Issues")
    if df.empty:
        st.info("No results yet. Run a scan on the **Scan** tab.")
    else:
        st.dataframe(df, use_container_width=True, hide_index=True)
    st.markdown('</div>', unsafe_allow_html=True)

    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.subheader("Export Report")
    if df.empty:
        st.caption("Nothing to export yet.")
    else:
        now_slug = dt.datetime.now().strftime("%Y%m%d_%H%M")
        base = f"access_audit_{now_slug}"
        csv_bytes  = df_to_csv_bytes(df)
        json_bytes = df_to_json_bytes(df)
        html_bytes = df_to_html_bytes(df, title=f"{APP_NAME} — Report", branding=BRAND)

        c1, c2, c3 = st.columns([1,1,1])
        with c1:
            st.download_button("⬇️ CSV",  data=csv_bytes,  file_name=f"{base}.csv",  mime="text/csv", use_container_width=True)
        with c2:
            st.download_button("⬇️ JSON", data=json_bytes, file_name=f"{base}.json", mime="application/json", use_container_width=True)
        with c3:
            st.download_button("⬇️ HTML", data=html_bytes, file_name=f"{base}.html", mime="text/html", use_container_width=True)

        try:
            save_json({"meta": st.session_state.get("last_run_meta", {}), "rows": st.session_state["results"]}, f"{base}.saved.json")
        except Exception as e:
            st.caption(f"Could not save server copy: {e}")
    st.markdown('</div>', unsafe_allow_html=True)

# -----------------------------
# DASHBOARD TAB
# -----------------------------
with dashboard_tab:
    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.subheader("Quick Tips")
    st.markdown("""
- Start with your most-visited pages and forms; fix HIGH/MED first.
- Replace vague anchor text (e.g., “click here”) with descriptive links.
- Ensure inputs have `<label for='...'>` or `aria-label`.
- Avoid large heading jumps (e.g., `h2` → `h5`).
- Inline-styled text with low contrast is flagged; also test real CSS themes.
    """)
    st.markdown('</div>', unsafe_allow_html=True)

    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.subheader("Last Run")
    last = st.session_state.get("last_run_meta") or {}
    st.json(last or {"note": "No scans yet."})
    st.markdown('</div>', unsafe_allow_html=True)

# -----------------------------
# SMOKE TEST TAB (with alternates)
# -----------------------------
with smoke_tab:
    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.subheader("One-click Smoke Test")
    st.caption("Runs 6 URLs (3 councils, a 404, a PDF, and a long page). Uses alternates if a site blocks automated fetches. Results load into the Results tab.")

    if st.button("Run Smoke Test", use_container_width=True, key="btn_smoke"):
        rows: List[Dict[str, object]] = []
        all_issues: List[Dict[str, str]] = []

        prog = st.progress(0)
        status = st.empty()
        total = len(SMOKE_CASES)

        for i, (name, url, expect) in enumerate(SMOKE_CASES, start=1):
            status.write(f"Testing {i}/{total}: {name} — {url}")

            # Try primary (and alternates only for 'ok' cases)
            candidates = [url] + (SMOKE_ALTERNATIVES.get(url, []) if expect == "ok" else [])
            used = url
            used_alt = False
            issues: List[Dict[str, str]] = []
            err: Optional[str] = None
            passed = False

            for candidate in candidates:
                issues, err = audit_url(candidate)
                if expect == "ok":
                    if err is None:
                        used = candidate
                        used_alt = (candidate != url)
                        passed = True
                        break
                else:
                    if _smoke_expectation_passed(err, issues, expect):
                        used = candidate
                        used_alt = (candidate != url)
                        passed = True
                        break

            rows.append({
                "name": name,
                "url": url,
                "resolved_url": used,
                "expected": expect,
                "passed": "✅ (alt)" if (passed and used_alt) else ("✅" if passed else "❌"),
                "issues_found": len(issues),
                "error": err or "",
            })
            all_issues.extend(issues)
            prog.progress(int(i / total * 100))

        # Load combined issues into the main Results tab so you can export them
        st.session_state["results"] = all_issues
        st.session_state["last_run_meta"] = {
            "urls": [r["resolved_url"] for r in rows],
            "ts": dt.datetime.utcnow().isoformat(),
            "suite": "smoke",
        }

        st.success("Smoke test complete — results loaded into the Results tab.")
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    st.markdown('</div>', unsafe_allow_html=True)
