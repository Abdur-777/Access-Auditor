# app.py — Accessibility Auditor (polite fetch + Playwright fallback + Robust Batch)
# Run: PLAYWRIGHT=1 python3 -m streamlit run app.py
from __future__ import annotations

import os, re, io, json, time, tempfile, base64, datetime as dt, asyncio, random
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Callable
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup, Tag
import pandas as pd
import streamlit as st
import httpx

# =========================
# Feature toggles / config
# =========================
PLAYWRIGHT_ENV = os.getenv("PLAYWRIGHT", "0")  # "1" to enable browser fallback
RATE_LIMIT_PER_HOST = float(os.getenv("RATE_LIMIT_PER_HOST", "0.9"))  # seconds between hits to same host
DEFAULT_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "22"))

# Try importing Playwright (for diagnostics + fallback)
try:
    from playwright.sync_api import sync_playwright  # type: ignore
    PW_OK = True
except Exception:
    PW_OK = False

# -----------------------------
# Smoke test suite
# -----------------------------
SMOKE_CASES = [
    ("Council Melbourne", "https://www.melbourne.vic.gov.au/", "ok"),
    ("Council Sydney",    "https://www.sydney.nsw.gov.au/",    "ok"),
    ("Council Brisbane",  "https://www.brisbane.qld.gov.au/",  "ok"),
    ("404 page",          "https://httpbin.org/status/404",     "err:404"),
    ("PDF file",          "https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf", "err:application/pdf"),
    ("Long page",         "https://en.wikipedia.org/wiki/Australia", "ok"),
]
SMOKE_ALTERNATIVES = {"https://www.sydney.nsw.gov.au/": ["https://www.nsw.gov.au/","https://www.vic.gov.au/"]}

def _smoke_expectation_passed(err: Optional[str], items: List[Dict[str, str]], expect: str) -> bool:
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
# Data dirs
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
# Branding
# -----------------------------
PRIMARY = os.getenv("BRAND_PRIMARY", "#0F4C81")
APP_NAME = os.getenv("BRAND_NAME", "Accessibility Auditor")
BRAND_LOGO_URL = os.getenv("BRAND_LOGO_URL", "")

def civic_logo_data_uri(primary: str) -> str:
    svg = f"""
    <svg width="120" height="120" viewBox="0 0 120 120" xmlns="http://www.w3.org/2000/svg">
      <rect width="120" height="120" rx="24" fill="{primary}"/>
      <polygon points="60,22 18,44 102,44" fill="white"/>
      <rect x="26" y="48" width="68" height="8" fill="white"/>
      <rect x="32" y="56" width="10" height="38" fill="white"/>
      <rect x="55" y="56" width="10" height="38" fill="white"/>
      <rect x="78" y="56" width="10" height="38" fill="white"/>
      <rect x="22" y="94" width="76" height="6" fill="white"/>
    </svg>""".strip()
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode()

BRAND_LOGO = BRAND_LOGO_URL if BRAND_LOGO_URL else civic_logo_data_uri(PRIMARY)
BRAND = {"name": APP_NAME, "primary": PRIMARY, "logo": BRAND_LOGO}

# -----------------------------
# UI
# -----------------------------
st.set_page_config(page_title=APP_NAME, page_icon="✅", layout="wide", initial_sidebar_state="collapsed")
st.markdown("<style>#MainMenu{visibility:hidden} footer{visibility:hidden}</style>", unsafe_allow_html=True)

if "theme" not in st.session_state:
    st.session_state["theme"] = "light"
def toggle_theme(): st.session_state["theme"] = "dark" if st.session_state["theme"] == "light" else "light"

theme = st.session_state.get("theme", "light")
is_dark = theme == "dark"
bg      = "#0f172a" if is_dark else "#ffffff"
text    = "#e5e7eb" if is_dark else "#0b1220"
card_bg = "#0f172a" if is_dark else "#ffffff"
border  = "#1f2937" if is_dark else "#e2e8f0"
muted   = "#cbd5e1" if is_dark else "#64748b"
table_h = "#111827" if is_dark else "#f8fafc"

st.markdown(f"""
<style>
:root {{ --wy-primary: {PRIMARY}; }}
html, body, .stApp {{ background: {bg}; color: {text}; }}
.card {{ border:1px solid {border}; border-radius:16px; padding:18px; background:{card_bg}; box-shadow:0 2px 18px rgba(0,0,0,{0.35 if is_dark else 0.06}); }}
[data-baseweb="tab-list"] {{ border-bottom:2px solid {border}; }}
.stDataFrame thead tr th {{ background:{table_h}; color:{text}; }}
.stButton > button, .stDownloadButton > button {{ background:var(--wy-primary); color:#fff; border:none; border-radius:999px; padding:8px 14px; font-weight:700; }}
.small, .stCaption, .stMarkdown p small {{ color:{muted}; }}
.badge-dot {{ display:inline-block; width:10px; height:10px; border-radius:50%; vertical-align:middle; margin-right:6px; }}
</style>
""", unsafe_allow_html=True)

cols_head = st.columns([1,6,3])
with cols_head[0]: st.image(BRAND["logo"], width=86)
with cols_head[1]:
    st.markdown(f"### {APP_NAME}")
    st.caption("Polite, cookie-aware fetcher with **real-browser fallback**.")
with cols_head[2]:
    dot_color = "#16a34a" if (PW_OK and PLAYWRIGHT_ENV == "1") else "#ef4444"
    st.markdown(
        f"<div class='small'>"
        f"<span class='badge-dot' style='background:{dot_color}'></span>"
        f"Playwright available: <strong>{'Yes' if PW_OK else 'No'}</strong> · "
        f"Env PLAYWRIGHT=<strong>{PLAYWRIGHT_ENV}</strong>"
        f"</div>",
        unsafe_allow_html=True
    )
    st.write("")
    if st.button(("🌙 Dark mode" if st.session_state["theme"] == "light" else "☀️ Light mode"),
                 use_container_width=True, key="toggle_theme"):
        toggle_theme(); st.rerun()

scan_tab, results_tab, dashboard_tab, smoke_tab = st.tabs(["🔍 Scan","📊 Results","📁 Dashboard","🧪 Smoke Test"])

# -----------------------------
# Helpers & checks
# -----------------------------
URL_RE = re.compile(r"^https?://", re.I)
UA_POOL = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]
GENERIC_HEADERS = {
    "User-Agent": UA_POOL[0],
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-AU,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

def _session_with_retries() -> requests.Session:
    s = requests.Session()
    retries = Retry(total=3, backoff_factor=0.5, status_forcelist=[429,500,502,503,504],
                    allowed_methods=frozenset(["GET","HEAD"]), raise_on_status=False)
    s.mount("http://", HTTPAdapter(max_retries=retries))
    s.mount("https://", HTTPAdapter(max_retries=retries))
    s.headers.update(GENERIC_HEADERS)
    return s

def normalize_url(u: str) -> str:
    u = (u or "").strip()
    if not u: return u
    if not URL_RE.search(u): u = "https://" + u
    return u

def _is_html(ct: str) -> bool:
    ct = (ct or "").lower()
    return ("text/html" in ct) or ("application/xhtml+xml" in ct)

def _host_root(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}/"

# --------- Browser fallback (Playwright) ----------
def fetch_via_browser(url: str, timeout: int = DEFAULT_TIMEOUT) -> Tuple[Optional[str], Optional[str], Optional[int]]:
    if not (PW_OK and PLAYWRIGHT_ENV == "1"):
        return None, "Playwright disabled or not available", None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--disable-gpu"])
            ctx = browser.new_context(user_agent=random.choice(UA_POOL))
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=timeout*1000)
            page.wait_for_timeout(800)  # small settle
            html = page.content()
            ctx.close(); browser.close()
            return html, None, 200
    except Exception as e:
        return None, f"Playwright fetch failed: {e}", None

# --------- 403-friendly SYNC fetch with warm-up, cookies, referer, http2 fallback, THEN Playwright ----------
def fetch(url: str, timeout: int = DEFAULT_TIMEOUT) -> Tuple[Optional[str], Optional[str], Optional[int], str]:
    """
    Returns (html, error, status, method_used) where method_used in {"requests","httpx","playwright","-"}
    """
    method_used = "-"
    s = _session_with_retries()
    ua = random.choice(UA_POOL)
    s.headers.update({"User-Agent": ua})
    url = normalize_url(url)

    # Warm-up to collect cookies
    try: s.get(_host_root(url), timeout=timeout, allow_redirects=True)
    except requests.RequestException: pass

    attempts = [
        {"headers": {}},
        {"headers": {"Referer": url, "Cache-Control":"no-cache", "Pragma":"no-cache"}},
    ]
    backoff = 0.6
    last_err, last_status = None, None

    for attempt in attempts:
        try:
            r = s.get(url, headers=attempt["headers"], timeout=timeout, allow_redirects=True)
            last_status = r.status_code
            ct = r.headers.get("Content-Type", "") or ""
            if 200 <= r.status_code < 300 and _is_html(ct) and (r.text or "").strip():
                method_used = "requests"
                return r.text, None, r.status_code, method_used
            if (r.status_code not in (401,403)) and (not _is_html(ct)):
                return None, f"Unsupported content type: {ct}", r.status_code, method_used
            last_err = f"HTTP {r.status_code}"
        except requests.RequestException as e:
            last_err = str(e); last_status = None
        time.sleep(backoff + random.random()*0.4); backoff *= 1.6

    # httpx fallback (HTTP/2) with cookies
    try:
        cookie_header = requests.utils.dict_from_cookiejar(s.cookies)
        with httpx.Client(http2=True, headers={**GENERIC_HEADERS, "User-Agent": ua, "Referer": url},
                          cookies=cookie_header, timeout=timeout, follow_redirects=True) as c:
            r3 = c.get(url)
            ct3 = r3.headers.get("Content-Type", "")
            if 200 <= r3.status_code < 300 and _is_html(ct3) and (r3.text or "").strip():
                method_used = "httpx"
                return r3.text, None, r3.status_code, method_used
            if not _is_html(ct3):
                return None, f"Unsupported content type: {ct3}", r3.status_code, method_used
            last_err, last_status = f"HTTP {r3.status_code}", r3.status_code
    except Exception as e:
        last_err = f"httpx error: {e}"

    # Final: Playwright browser
    html_pw, err_pw, st_pw = fetch_via_browser(url, timeout=timeout)
    if html_pw and not err_pw:
        method_used = "playwright"
        return html_pw, None, st_pw or 200, method_used

    return None, (err_pw or last_err or "Fetch failed"), last_status, method_used

# --------- HTML analysis ----------
def get_text_snippet(tag: Tag, max_len: int = 140) -> str:
    txt = tag.get_text(" ", strip=True) if isinstance(tag, Tag) else ""
    txt = re.sub(r"\s+", " ", txt)
    return (txt[: max_len - 1] + "…") if len(txt) > max_len else txt

def px_value(v: str) -> Optional[float]:
    m = re.search(r"([0-9.]+)\s*px", v or "", flags=re.I); return float(m.group(1)) if m else None

def parse_color(value: str) -> Optional[Tuple[float,float,float]]:
    if not value: return None
    value = value.strip().lower()
    m = re.match(r"#([0-9a-f]{3,8})", value)
    if m:
        h = m.group(1)
        if len(h) in (3,4):
            r = int(h[0]*2,16); g = int(h[1]*2,16); b = int(h[2]*2,16)
        else:
            r = int(h[0:2],16); g = int(h[2:4],16); b = int(h[4:6],16)
        return (r/255.0, g/255.0, b/255.0)
    m = re.match(r"rgb\(\s*([0-9.]+)\s*,\s*([0-9.]+)\s*,\s*([0-9.]+)\s*\)", value)
    if m:
        r,g,b = map(lambda x: float(x)/255.0, m.groups()); return (r,g,b)
    return None

def rel_luminance(rgb: Tuple[float,float,float]) -> float:
    def chan(c): return c/12.92 if c <= 0.03928 else ((c+0.055)/1.055)**2.4
    r,g,b = rgb; return 0.2126*chan(r)+0.7152*chan(g)+0.0722*chan(b)

def contrast_ratio(c1: Tuple[float,float,float], c2: Tuple[float,float,float]) -> float:
    L1,L2 = rel_luminance(c1), rel_luminance(c2)
    lighter, darker = max(L1,L2), min(L1,L2)
    return (lighter + 0.05) / (darker + 0.05)

def inline_style_dict(style: str) -> Dict[str,str]:
    out = {}
    for part in (style or "").split(";"):
        if ":" in part:
            k,v = part.split(":",1); out[k.strip().lower()] = v.strip()
    return out

GENERIC_LINK_TEXT = {"click here","read more","more","learn more","here"}

def analyze_html(url: str, html: str) -> List[Dict[str,str]]:
    soup = BeautifulSoup(html, "html.parser")
    issues: List[Dict[str,str]] = []

    if not soup.title or not soup.title.string or not soup.title.string.strip():
        issues.append(dict(url=url, check="Missing <title>", severity="MED", tag="title",
                           snippet="", recommendation="Add a descriptive <title>."))

    html_tag = soup.find("html")
    lang = (html_tag.get("lang") or "").strip().lower() if html_tag else ""
    if not lang:
        issues.append(dict(url=url, check="Missing language", severity="MED", tag="html",
                           snippet="", recommendation="Set <html lang='en'> (or appropriate)."))

    for img in soup.find_all("img"):
        alt = img.get("alt"); role = img.get("role")
        if role == "presentation": continue
        if alt is None or alt.strip()=="":
            issues.append(dict(url=url, check="Image missing alt text", severity="HIGH",
                               tag="img", snippet=(img.get("src") or "")[:140],
                               recommendation="Add alt or mark decorative via role='presentation'."))

    for el in soup.find_all(["input","select","textarea"]):
        if el.name=="input" and (el.get("type") or "").lower()=="hidden": continue
        has_label = bool(el.get("aria-label") or el.get("aria-labelledby"))
        el_id = el.get("id")
        if el_id:
            lbl = soup.find("label", attrs={"for": el_id})
            if lbl and lbl.get_text(strip=True): has_label = True
        if not has_label:
            parent = el.parent
            while isinstance(parent, Tag):
                if parent.name=="label" and parent.get_text(strip=True): has_label=True; break
                parent = parent.parent
        if not has_label:
            issues.append(dict(url=url, check="Form control without label", severity="HIGH",
                               tag=el.name, snippet=get_text_snippet(el),
                               recommendation="Use <label for>, aria-label, or aria-labelledby."))

    heading_levels = []
    for h in soup.find_all(re.compile(r"^h[1-6]$")):
        try: heading_levels.append((int(h.name[1]), get_text_snippet(h)))
        except: pass
    for i in range(1,len(heading_levels)):
        prev,_ = heading_levels[i-1]; curr,snip = heading_levels[i]
        if curr - prev > 1:
            issues.append(dict(url=url, check="Heading level jump", severity="LOW",
                               tag=f"h{curr}", snippet=snip,
                               recommendation="Don’t skip heading levels."))

    for tbl in soup.find_all("table"):
        if not tbl.find("th"):
            issues.append(dict(url=url, check="Table without headers", severity="MED",
                               tag="table", snippet=get_text_snippet(tbl),
                               recommendation="Use <th> with proper scope."))

    for a in soup.find_all("a"):
        txt = (a.get_text(" ", strip=True) or "").lower()
        if not txt or txt in GENERIC_LINK_TEXT:
            issues.append(dict(url=url, check="Non-descriptive link text", severity="LOW",
                               tag="a", snippet=get_text_snippet(a),
                               recommendation="Use meaningful link text."))

    for el in soup.find_all(True):
        style = inline_style_dict(el.get("style") or "")
        if not style: continue
        fg = parse_color(style.get("color","")); bg = parse_color(style.get("background-color",""))
        if fg and bg:
            ratio = contrast_ratio(fg,bg)
            fs = px_value(style.get("font-size","") or ""); weight = (style.get("font-weight","") or "400")
            large = (fs and fs>=18.0) or ((fs and fs>=14.0) and (weight.isdigit() and int(weight)>=700))
            threshold = 3.0 if large else 4.5
            if ratio < threshold:
                issues.append(dict(url=url, check="Low color contrast", severity="HIGH",
                                   tag=el.name, snippet=get_text_snippet(el),
                                   recommendation=f"Increase contrast (ratio {ratio:.2f} < {threshold:.1f})."))
    return issues

def audit_url(url: str) -> Tuple[List[Dict[str,str]], Optional[str], str]:
    """
    Returns (issues, error, method_used) — method_used ∈ {"requests","httpx","playwright","-"}
    """
    url = normalize_url(url)
    if not url: return [], "Empty URL.", "-"
    html, err, status, method_used = fetch(url)
    if err or not html:
        return [dict(url=url, check="Fetch failed", severity="HIGH", tag="-",
                     snippet=str(err or f"HTTP {status}"),
                     recommendation="If persistent, enable PLAYWRIGHT=1 or request IP allow-listing.")], err or f"HTTP {status}", method_used
    return analyze_html(url, html), None, method_used

# -----------------------------
# Async helpers
# -----------------------------
async def _fetch_async(client: httpx.AsyncClient, url: str) -> Tuple[Optional[str], Optional[str], Optional[int]]:
    try:
        r = await client.get(url)
        ct = r.headers.get("Content-Type","") or ""
        if 200 <= r.status_code < 300 and _is_html(ct):
            return r.text, None, r.status_code
        if r.status_code not in (401,403) and not _is_html(ct):
            return None, f"Unsupported content type: {ct}", r.status_code
    except httpx.RequestError as e:
        first_err = str(e)
    else:
        first_err = None

    # retry with referer
    try:
        r2 = await client.get(url, headers={"Referer": url, "Cache-Control": "no-cache", "Pragma": "no-cache"})
        ct2 = r2.headers.get("Content-Type","") or ""
        if 200 <= r2.status_code < 300 and _is_html(ct2):
            return r2.text, None, r2.status_code
        if not _is_html(ct2):
            return None, f"Unsupported content type: {ct2}", r2.status_code
        return None, f"HTTP {r2.status_code}", r2.status_code
    except httpx.RequestError as e:
        second_err = str(e)
        comb = "; ".join([x for x in [first_err, second_err] if x]) or "403/401 Forbidden or blocked by anti-bot"
        return None, f"Fetch failed: {comb}", None

# -----------------------------
# Async worker (with sync/Playwright fallback)
# -----------------------------
async def _audit_one_async(client: httpx.AsyncClient, url: str) -> List[Dict[str, str]]:
    url_norm = normalize_url(url)
    if not url_norm:
        return [dict(
            url=url, check="Fetch failed", severity="HIGH", tag="-",
            snippet="Empty URL.", recommendation="Provide a valid URL (https://…)."
        )]

    # Fast path: try async httpx first
    html, err, status = await _fetch_async(client, url_norm)
    if html and not err:
        return analyze_html(url_norm, html)

    # Fallback: run the SAME sync pipeline as Run Audit (includes Playwright if enabled)
    items, _err2, _method2 = audit_url(url_norm)
    return items

async def _audit_batch_async(urls: List[str], progress_cb: Callable[[int,int,str], None]) -> List[Dict[str,str]]:
    limits = httpx.Limits(max_connections=12, max_keepalive_connections=6)
    timeout = httpx.Timeout(DEFAULT_TIMEOUT)
    results: List[Dict[str,str]] = []

    async with httpx.AsyncClient(http2=False, headers=GENERIC_HEADERS, limits=limits, timeout=timeout, follow_redirects=True) as client:
        total = len(urls)
        idx = 0
        next_ok: Dict[str, float] = {}

        async def worker(u: str):
            nonlocal idx, results
            host = urlparse(normalize_url(u)).netloc
            now = time.time()
            wait_for = max(0.0, next_ok.get(host, 0.0) - now)
            if wait_for > 0:
                await asyncio.sleep(wait_for)
            next_ok[host] = time.time() + RATE_LIMIT_PER_HOST

            items = await _audit_one_async(client, u)
            results.extend(items)
            idx += 1
            progress_cb(idx, total, u)

        tasks = [worker(u) for u in urls]
        await asyncio.gather(*tasks)
    return results

def run_batch_concurrent(urls: List[str], prog, status_area) -> List[Dict[str, str]]:
    def _update_progress(done: int, total: int, current_url: str):
        pct = int(done / max(total, 1) * 100)
        status_area.write(f"Scanning {done}/{total}: {current_url}")
        prog.progress(pct)
    try:
        return asyncio.run(_audit_batch_async(urls, _update_progress))
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            return loop.run_until_complete(_audit_batch_async(urls, _update_progress))
        finally:
            loop.close()

# -----------------------------
# Data shaping & export
# -----------------------------
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
    if df.empty: return {"HIGH":0,"MED":0,"LOW":0,"TOTAL":0,"URLS":0}
    cts = df["severity"].str.upper().value_counts().to_dict()
    out = {"HIGH": cts.get("HIGH",0), "MED": cts.get("MED",0), "LOW": cts.get("LOW",0)}
    out["TOTAL"] = int(df.shape[0]); out["URLS"] = int(df["url"].nunique())
    return out

def df_to_csv_bytes(df: pd.DataFrame) -> bytes:  return df.to_csv(index=False).encode("utf-8")
def df_to_json_bytes(df: pd.DataFrame) -> bytes: return df.to_json(orient="records", indent=2).encode("utf-8")

def df_to_html_bytes(df: pd.DataFrame, title: str, branding: Dict) -> bytes:
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    pages = int(df["url"].nunique()) if (not df.empty and "url" in df.columns) else 0
    total = int(len(df))
    high = int((df["severity"].str.upper() == "HIGH").sum()) if "severity" in df.columns else 0
    med  = int((df["severity"].str.upper() == "MED").sum())  if "severity" in df.columns else 0
    low  = int((df["severity"].str.upper() == "LOW").sum())  if "severity" in df.columns else 0

    df2 = df.copy()
    if "check" in df2.columns:
        df2["check"] = df2["check"].replace({"Fetch failed": "Page not available (blocked/timeout/non-HTML)"})
    if "url" in df2.columns:
        df2["url"] = df2["url"].apply(lambda u: f"<a href='{u}' target='_blank' rel='noopener noreferrer'>{u}</a>")
    if "severity" in df2.columns:
        df2["severity"] = df2["severity"].apply(lambda s: f"<span class='badge {s}'>{s}</span>")
    html_table = df2.to_html(escape=False, index=False)

    css = f"""
    <style>
      body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif; margin:24px; }}
      h1 {{ color:{branding['primary']}; margin-bottom: 6px; }}
      a {{ color:{branding['primary']}; text-decoration:none; }}
      a:hover {{ text-decoration:underline; }}
      .summary-top {{ margin: 6px 0 16px 0; font-weight:600; color:#334155; }}
      .generated {{ margin: 4px 0 16px 0; color:#64748b; font-size:12px; }}
      table {{ border-collapse: collapse; width: 100%; }}
      th, td {{ text-align: left; padding: 8px; border-bottom: 1px solid #e2e8f0; vertical-align: top; }}
      th {{ background:#f8fafc; }}
      .badge {{ display:inline-block; padding:2px 8px; border-radius:999px; font-size:12px; font-weight:700; }}
      .HIGH {{ background:#fee2e2; color:#991b1b; border:1px solid #fecaca; }}
      .MED  {{ background:#fef3c7; color:#92400e; border:1px solid #fde68a; }}
      .LOW  {{ background:#ecfeff; color:#155e75; border:1px solid #a5f3fc; }}
      .muted {{ color:#64748b; font-size:12px; }}
      .brand {{ font-weight:800;font-size:18px;color:{branding['primary']}; vertical-align:middle; }}
      .brand img {{ height:44px;vertical-align:middle;margin-right:10px; }}
    </style>"""
    summary_html = (f"<div class='summary-top'><strong>{pages}</strong> page(s) • {total} issue(s) "
                    f"(<span class='badge HIGH'>HIGH {high}</span> "
                    f"<span class='badge MED'>MED {med}</span> "
                    f"<span class='badge LOW'>LOW {low}</span>)</div>")
    html = f"""<!doctype html><html><head><meta charset="utf-8"><title>{title}</title>{css}</head>
    <body>
      <div class="brand"><img src="{branding['logo']}" alt="logo"/>{branding['name']}</div>
      <h1>Accessibility Audit Report</h1>
      <div class="generated">Generated: {now}</div>
      {summary_html}
      {html_table}
      <p class="muted">Automated WCAG pre-check. For blocked pages, request an IP allow-list or enable browser fallback.</p>
    </body></html>"""
    return html.encode("utf-8")

# -----------------------------
# Session storage
# -----------------------------
if "results" not in st.session_state: st.session_state["results"] = []
if "last_run_meta" not in st.session_state: st.session_state["last_run_meta"] = []

# -----------------------------
# SCAN TAB
# -----------------------------
with scan_tab:
    with st.expander("Scanner settings", expanded=False):
        ua = st.text_input("Default User-Agent", value=GENERIC_HEADERS["User-Agent"])
        if ua and ua != GENERIC_HEADERS["User-Agent"]:
            GENERIC_HEADERS["User-Agent"] = ua
            st.caption("User-Agent updated for this session.")
        rlp = st.number_input("Per-host delay (seconds)", min_value=0.0, max_value=3.0, value=RATE_LIMIT_PER_HOST, step=0.1)
        if rlp != RATE_LIMIT_PER_HOST:
            RATE_LIMIT_PER_HOST = float(rlp)
            st.caption(f"Per-host rate limit set to {RATE_LIMIT_PER_HOST:.1f}s")

        st.caption(f"Playwright import: {'OK' if PW_OK else 'missing'} · PLAYWRIGHT env: {PLAYWRIGHT_ENV}")

    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.subheader("Run Audit")
    st.caption("Scan a single URL or paste multiple URLs in the batch tool.")
    url = st.text_input("Page URL", value="https://www.australia.gov.au/", label_visibility="collapsed", key="single_url")
    c1, c2 = st.columns([1,1])
    with c1: run_single = st.button("Run Audit", use_container_width=True, key="btn_run_single")
    with c2: clear_btn  = st.button("Clear Results", use_container_width=True, key="btn_clear")
    st.markdown('</div>', unsafe_allow_html=True)

    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.subheader("Batch Scan")
    st.caption("Paste multiple URLs (one per line)")
    batch_text = st.text_area("URLs", height=180, placeholder="https://example.org/page-1\nhttps://example.org/page-2", label_visibility="collapsed", key="batch_urls")
    c_run, c_pad, c_max = st.columns([1,2,1])
    with c_run: run_batch = st.button("Run Batch Scan", use_container_width=True, key="btn_run_batch")
    with c_max: max_n = st.number_input("Max pages", min_value=1, max_value=500, value=25, step=1, key="max_pages")
    st.markdown('</div>', unsafe_allow_html=True)

    if clear_btn:
        st.session_state["results"] = []; st.session_state["last_run_meta"] = {}
        st.success("Cleared previous results.")

    if run_single:
        st.info("Scanning…")
        issues, _err, method_used = audit_url(url)
        if _err is None:
            for it in issues: it.setdefault("tag","-")
        st.session_state["results"] = issues
        st.session_state["last_run_meta"] = {"urls":[normalize_url(url)], "ts": dt.datetime.utcnow().isoformat(), "method": method_used}
        st.success(f"Done via {method_used or '-'} — found {len(issues)} issue(s). See the Results tab.")

    if run_batch and batch_text.strip():
        urls = [normalize_url(u) for u in batch_text.splitlines() if u.strip()]
        urls = [u for u in urls if u]
        if not urls:
            st.warning("No valid URLs provided.")
        else:
            # NEW: robust toggle – guaranteed Playwright fallback per URL (slower)
            robust = st.checkbox("Use robust (browser) fallback in batch (slower, fewer 403s)",
                                 value=True, key="batch_robust")

            n = min(len(urls), int(max_n))
            urls = urls[:n]
            prog = st.progress(0)
            status_area = st.empty()

            if robust:
                # Run the SAME sync pipeline as Run Audit for each URL
                all_items: List[Dict[str, str]] = []
                total = len(urls)
                for i, u in enumerate(urls, start=1):
                    status_area.write(f"Scanning {i}/{total}: {u}")
                    items, _err, _method = audit_url(u)   # includes Playwright if enabled
                    all_items.extend(items)
                    prog.progress(int(i / total * 100))
                results = all_items
                suite_name = "batch-robust"
            else:
                # Keep the fast async path (now also falls back to sync if needed)
                results = run_batch_concurrent(urls, prog, status_area)
                suite_name = "batch-async"

            st.session_state["results"] = results
            st.session_state["last_run_meta"] = {
                "urls": urls,
                "ts": dt.datetime.utcnow().isoformat(),
                "suite": suite_name,
            }
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
    c1,c2,c3,c4,c5 = st.columns(5)
    c1.metric("HIGH", cts["HIGH"]); c2.metric("MED", cts["MED"]); c3.metric("LOW", cts["LOW"])
    c4.metric("Total", cts["TOTAL"]); c5.metric("Pages", cts["URLS"])
    st.markdown('</div>', unsafe_allow_html=True)

    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.subheader("Grouped view")
    if df.empty:
        st.caption("No results yet.")
    else:
        df_group = (df.groupby(["url","severity","check"]).size().reset_index(name="count")
                      .sort_values(["url","severity","count"], ascending=[True,True,False]))
        st.dataframe(df_group, use_container_width=True, hide_index=True)
    st.markdown('</div>', unsafe_allow_html=True)

    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.subheader("Issues")
    if df.empty: st.info("No results yet. Run a scan on the **Scan** tab.")
    else:        st.dataframe(df, use_container_width=True, hide_index=True)
    st.markdown('</div>', unsafe_allow_html=True)

    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.subheader("Export Report")
    if df.empty:
        st.caption("Nothing to export yet.")
    else:
        now_slug = dt.datetime.now().strftime("%Y%m%d_%H%M")
        base = f"access_audit_{now_slug}"
        st.download_button("⬇️ CSV",  data=df_to_csv_bytes(df),  file_name=f"{base}.csv",  mime="text/csv", use_container_width=True)
        st.download_button("⬇️ JSON", data=df_to_json_bytes(df), file_name=f"{base}.json", mime="application/json", use_container_width=True)
        st.download_button("⬇️ HTML", data=df_to_html_bytes(df, title=f"{APP_NAME} — Report", branding=BRAND),
                           file_name=f"{base}.html", mime="text/html", use_container_width=True)
        try:
            with open(os.path.join(EXPORTS_DIR, f"{base}.saved.json"), "w", encoding="utf-8") as f:
                json.dump({"meta": st.session_state.get("last_run_meta", {}), "rows": st.session_state["results"]}, f, ensure_ascii=False, indent=2)
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
- Start with most-visited pages & forms; fix HIGH/MED first.
- Replace “click here” with descriptive links.
- Ensure inputs have `<label for>` or `aria-*`.
- Avoid heading jumps (e.g., h2 → h5).
- Inline-styled low contrast is flagged; also test your CSS theme.
    """)
    st.markdown('</div>', unsafe_allow_html=True)

    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.subheader("Last Run")
    last = st.session_state.get("last_run_meta") or {}
    urls = last.get("urls") or []; ts_iso = last.get("ts")
    if not urls: st.caption("No scans yet.")
    else:
        when = ts_iso.replace("T"," ").split(".")[0] if isinstance(ts_iso,str) else "—"
        cols = st.columns(3)
        cols[0].metric("Pages", len(urls)); cols[1].metric("When", when); cols[2].metric("Suite", last.get("suite","manual"))
        st.markdown("\n".join([f"- [{u}]({u})" for u in urls[:8]]))
        if len(urls) > 8: st.caption(f"+{len(urls)-8} more…")
        st.caption("Open **Results** to explore issues or export a report.")
    st.markdown('</div>', unsafe_allow_html=True)

# -----------------------------
# SMOKE TEST TAB
# -----------------------------
with smoke_tab:
    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.subheader("One-click Smoke Test")
    st.caption("6 URLs (3 councils, 404, PDF, long page). Uses alternates if blocked. Results load into the Results tab.")
    if st.button("Run Smoke Test", use_container_width=True, key="btn_smoke"):
        rows: List[Dict[str, object]] = []; all_issues: List[Dict[str, str]] = []
        prog = st.progress(0); status = st.empty(); total = len(SMOKE_CASES)
        for i, (name, url, expect) in enumerate(SMOKE_CASES, start=1):
            status.write(f"Testing {i}/{total}: {name} — {url}")
            candidates = [url] + (SMOKE_ALTERNATIVES.get(url, []) if expect=="ok" else [])
            used = url; used_alt = False; issues: List[Dict[str,str]] = []; err = None; passed = False
            issues, err, method_used = audit_url(url)  # try primary first with full pipeline
            if (expect=="ok" and err is None) or (expect!="ok" and _smoke_expectation_passed(err, issues, expect)):
                used = url; passed = True
            else:
                for candidate in candidates[1:]:
                    issues, err, method_used = audit_url(candidate)
                    if (expect=="ok" and err is None) or (expect!="ok" and _smoke_expectation_passed(err, issues, expect)):
                        used = candidate; used_alt = True; passed = True; break
            rows.append({"name":name,"url":url,"resolved_url":used,"expected":expect,
                         "passed":"✅ (alt)" if (passed and used_alt) else ("✅" if passed else "❌"),
                         "issues_found":len(issues),"error":err or ""})
            all_issues.extend(issues); prog.progress(int(i/total*100))
        st.session_state["results"] = all_issues
        st.session_state["last_run_meta"] = {"urls":[r["resolved_url"] for r in rows],
                                             "ts": dt.datetime.utcnow().isoformat(), "suite":"smoke"}
        st.success("Smoke test complete — results loaded into the Results tab.")
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    st.markdown('</div>', unsafe_allow_html=True)
