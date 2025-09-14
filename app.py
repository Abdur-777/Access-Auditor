# app.py — AI Accessibility Auditor & Remediation (WCAG 2.2 AA)
# -------------------------------------------------------------
# Quick start:
#   requirements.txt (suggested)
#     streamlit>=1.32
#     beautifulsoup4>=4.12
#     lxml>=5.2
#     requests>=2.31
#     pypdf>=4.2
#     pillow>=10.3
#     openai>=1.42  # optional (for remediation plan)
#
# Run:
#   streamlit run app.py
#
# Features:
#   - Audit a URL or uploaded HTML/PDF for common WCAG 2.2 AA issues
#   - Checks: images alt, doc language, headings outline, links/buttons names,
#     form labels, duplicate IDs, page title, meta viewport, color contrast
#     (inline style approximate), ARIA misuse, PDFs text extraction presence
#   - Capped crawl (same-origin) depth 0–1 (optional)
#   - Download CSV/JSON reports
#   - Generate prioritized Remediation Plan (optional, OpenAI)
#
# Notes:
#   - Color-contrast check uses inline styles only (approximate; no external CSS parsing).
#   - This is a pragmatic starter. For production, consider integrating axe-core/pa11y via API
#     or headless browser for full CSS/JS rendering.

import os
import re
import io
import json
import time
import traceback
from urllib.parse import urljoin, urlparse

import requests
import streamlit as st
from bs4 import BeautifulSoup
from typing import List, Dict, Any, Optional, Tuple, Set

# Optional OpenAI (for remediation plan)
try:
    from openai import OpenAI
    HAS_OPENAI = True
except Exception:
    HAS_OPENAI = False

# Optional PDF text extraction
try:
    from pypdf import PdfReader
    HAS_PYPDF = True
except Exception:
    HAS_PYPDF = False

# Optional for color parsing
try:
    from PIL import ImageColor
    HAS_PIL = True
except Exception:
    HAS_PIL = False


# ---------------------------- Config ----------------------------

st.set_page_config(page_title="AI Accessibility Auditor", page_icon="♿", layout="wide")

APP_NAME = "AI Accessibility Auditor"
DEFAULT_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")


# ---------------------------- Helpers ---------------------------

def is_same_origin(base: str, url: str) -> bool:
    try:
        pb = urlparse(base)
        pu = urlparse(url)
        return (pu.scheme, pu.netloc) == (pb.scheme, pb.netloc)
    except Exception:
        return False


def fetch_url(url: str, timeout: int = 25) -> Optional[str]:
    try:
        r = requests.get(url, timeout=timeout, headers={"User-Agent": "AccessibilityAuditor/1.0"})
        r.raise_for_status()
        ct = r.headers.get("Content-Type", "").lower()
        if "text/html" in ct or url.lower().endswith((".html", ".htm")):
            return r.text
        # Attempt text for other text-like content
        if "text/" in ct:
            return r.text
        return None
    except Exception as e:
        st.warning(f"Fetch failed for {url}: {e}")
        return None


def read_pdf_bytes(file) -> str:
    if not HAS_PYPDF:
        return "[PDF text analysis unavailable: install pypdf]"
    try:
        reader = PdfReader(file)
        chunks = []
        for i, pg in enumerate(reader.pages):
            try:
                txt = pg.extract_text() or ""
            except Exception:
                txt = ""
            chunks.append(txt)
        return "\n\n".join(chunks).strip()
    except Exception as e:
        return f"[Failed to read PDF: {e}]"


def parse_color(style_val: str) -> Optional[Tuple[int, int, int]]:
    """Parse a CSS color from inline styles using PIL.ImageColor if present."""
    if not HAS_PIL:
        return None
    try:
        # Extract color tokens like 'color:#112233' or 'background:#fafafa'
        m = re.search(r"(?:color|background(?:-color)?)\s*:\s*([^;]+)", style_val, re.I)
        if not m:
            return None
        raw = m.group(1).strip()
        # PIL handles #hex, rgb(), named colors
        return ImageColor.getrgb(raw)
    except Exception:
        return None


def rel_luminance(rgb: Tuple[int, int, int]) -> float:
    # WCAG relative luminance
    def adj(c: float) -> float:
        c = c / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055)/1.055) ** 2.4
    r, g, b = rgb
    return 0.2126*adj(r) + 0.7152*adj(g) + 0.0722*adj(b)


def contrast_ratio(fg: Tuple[int, int, int], bg: Tuple[int, int, int]) -> float:
    L1 = rel_luminance(fg)
    L2 = rel_luminance(bg)
    lighter, darker = (L1, L2) if L1 >= L2 else (L2, L1)
    return (lighter + 0.05) / (darker + 0.05)


def wcag_ref(code: str) -> str:
    # Minimal mapping helper for quick linking
    # (Not printing full URLs to keep UI clean; you can add links if you want.)
    refs = {
        "1.1.1": "Non-text Content",
        "1.3.1": "Info and Relationships",
        "1.3.2": "Meaningful Sequence",
        "1.4.3": "Contrast (Minimum)",
        "1.4.10": "Reflow",
        "2.1.1": "Keyboard",
        "2.4.1": "Bypass Blocks",
        "2.4.2": "Page Titled",
        "2.4.4": "Link Purpose (In Context)",
        "2.4.6": "Headings and Labels",
        "2.4.7": "Focus Visible",
        "3.1.1": "Language of Page",
        "3.2.3": "Consistent Navigation",
        "3.3.2": "Labels or Instructions",
        "4.1.1": "Parsing",
        "4.1.2": "Name, Role, Value",
    }
    return refs.get(code, "")


def add_issue(issues: List[Dict[str, Any]], severity: str, code: str, msg: str, nodes: List[str]):
    issues.append({
        "severity": severity,  # "high" | "medium" | "low"
        "wcag": code,
        "wcag_title": wcag_ref(code),
        "message": msg,
        "count": len(nodes),
        "examples": nodes[:5],  # cap examples
    })


# ---------------------------- Audits ----------------------------

def audit_html(html: str, base_url: Optional[str] = None) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(html, "lxml")
    issues: List[Dict[str, Any]] = []

    # 1) Language of page (3.1.1)
    html_tag = soup.find("html")
    if not html_tag or not html_tag.get("lang"):
        add_issue(
            issues, "medium", "3.1.1",
            "Missing or empty lang attribute on <html>.",
            [str(html_tag)[:120] if html_tag else "<html> not found"]
        )

    # 2) Page titled (2.4.2)
    title = soup.find("title")
    if not title or not title.get_text(strip=True):
        add_issue(
            issues, "medium", "2.4.2",
            "Missing or empty <title> element.",
            [str(title)[:120] if title else "<title> not found"]
        )

    # 3) Images alt text (1.1.1)
    imgs = soup.find_all("img")
    bad_imgs = []
    for img in imgs:
        alt = (img.get("alt") or "").strip()
        role = (img.get("role") or "").strip().lower()
        # Pure decorative images should be alt="", but content images need meaningful alt
        if role == "presentation":
            continue
        if alt == "" or len(alt) <= 1:
            bad_imgs.append(str(img)[:180])
    if bad_imgs:
        add_issue(
            issues, "high", "1.1.1",
            "Images missing meaningful alt text.",
            bad_imgs
        )

    # 4) Headings outline & single H1 (1.3.1, 2.4.6)
    headings = [(h.name, h.get_text(" ", strip=True)) for h in soup.find_all(re.compile("^h[1-6]$"))]
    h1_count = sum(1 for h, _ in headings if h == "h1")
    if h1_count == 0:
        add_issue(
            issues, "medium", "2.4.6",
            "No <h1> found. Provide a clear page heading.",
            [f"{h}:{t[:80]}" for h, t in headings[:5]] or ["No headings present"]
        )
    # Check order roughly non-skipping e.g., h2 should not follow h4
    level_seq = [int(h[1]) for h, _ in headings]
    bad_seq = []
    for i in range(1, len(level_seq)):
        if level_seq[i] > level_seq[i-1] + 1:
            bad_seq.append(f"{headings[i-1][0]} → {headings[i][0]} ({headings[i][1][:60]})")
    if bad_seq:
        add_issue(
            issues, "low", "1.3.1",
            "Heading levels skip order (e.g., h2 → h4).",
            bad_seq
        )

    # 5) Links & buttons accessible name (2.4.4, 4.1.2)
    bad_links = []
    for a in soup.find_all("a"):
        text = a.get_text(" ", strip=True)
        name = text or a.get("aria-label") or a.get("title") or ""
        href = a.get("href") or ""
        if not name or name.lower() in {"", "click here", "here", "more", "read more"}:
            bad_links.append(f"<a href='{href}'> {name or '[empty]'} </a>")
    if bad_links:
        add_issue(
            issues, "medium", "2.4.4",
            "Links must have a clear accessible name/purpose.",
            bad_links
        )

    bad_buttons = []
    for b in soup.find_all(["button", "input"]):
        role = b.get("role", "").lower()
        if b.name == "input":
            typ = (b.get("type") or "").lower()
            is_buttonlike = typ in {"button", "submit", "reset", ""} or role == "button"
        else:
            is_buttonlike = True
        if is_buttonlike:
            name = (b.get_text(" ", strip=True) if b.name == "button" else (b.get("aria-label") or b.get("value") or ""))
            if not name:
                name = b.get("aria-label") or b.get("title") or ""
            if not name:
                bad_buttons.append(str(b)[:160])
    if bad_buttons:
        add_issue(
            issues, "medium", "4.1.2",
            "Buttons need an accessible name (text, aria-label, or title).",
            bad_buttons
        )

    # 6) Form labels (3.3.2)
    inputs = soup.find_all("input")
    label_map: Dict[str, bool] = {}
    for lab in soup.find_all("label"):
        if lab.get("for"):
            label_map[lab["for"]] = True
    unlabeled = []
    for inp in inputs:
        typ = (inp.get("type") or "").lower()
        if typ in {"hidden", "submit", "button", "reset", "image"}:
            continue
        has_label = False
        _id = inp.get("id")
        if _id and _id in label_map:
            has_label = True
        if not has_label:
            # aria-label or aria-labelledby acceptable
            if inp.get("aria-label") or inp.get("aria-labelledby") or inp.get("title"):
                has_label = True
        if not has_label:
            unlabeled.append(str(inp)[:160])
    if unlabeled:
        add_issue(
            issues, "high", "3.3.2",
            "Inputs missing labels or accessible names.",
            unlabeled
        )

    # 7) Duplicate IDs (4.1.1)
    ids: Dict[str, int] = {}
    dups: List[str] = []
    for el in soup.find_all(True):
        _id = el.get("id")
        if _id:
            ids[_id] = ids.get(_id, 0) + 1
    for k, v in ids.items():
        if v > 1:
            dups.append(f"id='{k}' appears {v} times")
    if dups:
        add_issue(
            issues, "low", "4.1.1",
            "Duplicate element IDs found.",
            dups
        )

    # 8) Meta viewport (1.4.10 Reflow – partial heuristic)
    vp = soup.find("meta", attrs={"name": "viewport"})
    if not vp:
        add_issue(
            issues, "low", "1.4.10",
            "Missing <meta name='viewport'> may hinder reflow on mobile.",
            ["<meta name='viewport' ...> not found"]
        )

    # 9) Inline contrast (1.4.3) — heuristic only
    low_contrast: List[str] = []
    # Check common text nodes containers with inline style specifying color/background
    for el in soup.find_all(True):
        style = el.get("style", "")
        if not style:
            continue
        fg = parse_color(style)
        bg = None
        # Try to parse background-color from same style, otherwise assume white
        try:
            m = re.search(r"background(?:-color)?\s*:\s*([^;]+)", style, re.I)
            if m and HAS_PIL:
                from PIL import ImageColor as _IC  # local import ok
                bg = _IC.getrgb(m.group(1).strip())
        except Exception:
            bg = None
        if fg:
            if not bg:
                bg = (255, 255, 255)
            try:
                cr = contrast_ratio(fg, bg)
                # AA threshold: 4.5 for normal text (we can't detect font-size reliably here)
                if cr < 4.5:
                    txt = el.get_text(" ", strip=True)[:80]
                    low_contrast.append(f"contrast≈{cr:.2f} | '{txt}' | style='{style[:80]}'")
            except Exception:
                pass
    if low_contrast:
        add_issue(
            issues, "high", "1.4.3",
            "Possible insufficient color contrast (inline styles).",
            low_contrast
        )

    # 10) Basic ARIA misuse scan (4.1.2)
    bad_aria: List[str] = []
    for el in soup.find_all(True):
        role = el.get("role")
        aria_hidden = el.get("aria-hidden")
        if aria_hidden == "true":
            # if focusable child (cannot detect reliably), warn if interactive tag used
            if el.name in {"a", "button", "input", "textarea", "select"}:
                bad_aria.append(f"{el.name} has aria-hidden='true' but is interactive")
        if role == "presentation" and el.name in {"a", "button", "input"}:
            bad_aria.append(f"{el.name} should not use role='presentation'")
    if bad_aria:
        add_issue(
            issues, "low", "4.1.2",
            "Potential ARIA misuse.",
            bad_aria
        )

    return issues


def audit_pdf(text: str) -> List[Dict[str, Any]]:
    """Very light PDF heuristic: check if any extractable text exists."""
    issues: List[Dict[str, Any]] = []
    if not text or text.strip() == "":
        add_issue(
            issues, "high", "1.1.1",
            "PDF appears to have no selectable text (likely scanned without OCR).",
            ["No text extracted"]
        )
    return issues


def crawl_and_collect(url: str, depth: int = 0, max_pages: int = 5) -> List[Tuple[str, Optional[str]]]:
    """Return list of (url, html_text) for same-origin pages up to depth."""
    seen: Set[str] = set()
    out: List[Tuple[str, Optional[str]]] = []
    base = url
    q: List[Tuple[str, int]] = [(url, 0)]
    while q and len(out) < max_pages:
        u, d = q.pop(0)
        if u in seen:
            continue
        seen.add(u)
        html = fetch_url(u)
        out.append((u, html))
        if d < depth and html:
            try:
                soup = BeautifulSoup(html, "lxml")
                for a in soup.find_all("a", href=True):
                    nxt = urljoin(u, a["href"])
                    if nxt.startswith("mailto:") or nxt.startswith("tel:"):
                        continue
                    if is_same_origin(base, nxt):
                        q.append((nxt, d+1))
                        if len(q) > max_pages * 2:  # keep queue bounded
                            q = q[:max_pages * 2]
            except Exception:
                pass
    return out


# ---------------------- Remediation Plan (LLM) ----------------------

def llm_available() -> bool:
    return bool(HAS_OPENAI and OPENAI_API_KEY)


def remediation_plan(issues: List[Dict[str, Any]], target: str) -> str:
    if not llm_available():
        return (
            "⚠️ OpenAI not configured. Set OPENAI_API_KEY to generate a remediation plan.\n\n"
            f"Target: {target}\n\n"
            f"Here is the raw JSON of issues you can use:\n{json.dumps(issues, indent=2)[:5000]}"
        )
    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        summary = json.dumps(issues, ensure_ascii=False)
        prompt = f"""
You are an experienced WCAG 2.2 AA accessibility consultant.
Given this audit (JSON array of issues with wcag code, severity, counts, and examples), produce:

1) Executive summary (non-technical, 3–5 bullets).
2) Prioritized remediation backlog (P0/P1/P2), grouped by WCAG criterion.
3) Concrete code-level fixes (CSS/HTML/ARIA) with short snippets where relevant.
4) Owner suggestions (Design, Frontend, Content) and realistic timelines (Quick Wins: <1 day, Short: <1 week, Medium: 2–4 weeks).
5) A re-test checklist.

Target: {target}

Audit JSON:
{summary}
"""
        resp = client.chat.completions.create(
            model=DEFAULT_MODEL,
            messages=[
                {"role": "system", "content": "You deliver precise, standards-based accessibility guidance."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        return f"❌ LLM call failed: {e}\n\n{traceback.format_exc()}"


# ---------------------------- UI ----------------------------

st.title("♿ AI Accessibility Auditor")
st.caption("WCAG 2.2 AA checks for URLs, HTML, and PDFs. Generate reports & a remediation plan.")

with st.sidebar:
    st.subheader("Audit Target")
    mode = st.radio("Choose input type:", ["URL", "Upload HTML", "Upload PDF"], index=0)

    crawl_depth = 0
    max_pages = 5
    base_url = ""
    uploaded_html = None
    uploaded_pdf = None

    if mode == "URL":
        base_url = st.text_input("URL to audit", placeholder="https://www.example.com")
        crawl_depth = st.slider("Crawl depth (same origin)", 0, 1, 0, help="Depth 0 = single page; Depth 1 ≈ follow internal links.")
        max_pages = st.slider("Max pages", 1, 20, 5)
    elif mode == "Upload HTML":
        uploaded_html = st.file_uploader("Upload .html", type=["html", "htm"])
    else:
        uploaded_pdf = st.file_uploader("Upload .pdf", type=["pdf"])

    st.divider()
    st.subheader("Export & Plan")
    want_plan = st.toggle("Generate remediation plan (OpenAI)", value=False)
    st.caption("Set OPENAI_API_KEY to enable.")

col_report, col_right = st.columns([2, 1])

with col_report:
    st.subheader("Audit Report")

    issues_all: List[Dict[str, Any]] = []
    pages_audited: List[str] = []

    if mode == "URL":
        if st.button("Run Audit", type="primary", use_container_width=True):
            if not base_url.strip():
                st.warning("Please enter a URL.")
            else:
                with st.spinner("Fetching and analyzing..."):
                    pages = crawl_and_collect(base_url.strip(), depth=crawl_depth, max_pages=max_pages)
                    for u, html in pages:
                        pages_audited.append(u)
                        if html:
                            issues = audit_html(html, base_url=u)
                            # Annotate each issue with page
                            for it in issues:
                                it["page"] = u
                            issues_all.extend(issues)
                        else:
                            issues_all.append({
                                "severity": "high",
                                "wcag": "4.1.1",
                                "wcag_title": wcag_ref("4.1.1"),
                                "message": "Page not HTML or failed to load content.",
                                "count": 1,
                                "examples": [u],
                                "page": u,
                            })
    elif mode == "Upload HTML":
        if st.button("Run Audit", type="primary", use_container_width=True):
            if not uploaded_html:
                st.warning("Please upload an HTML file.")
            else:
                with st.spinner("Analyzing HTML..."):
                    html_text = uploaded_html.read().decode("utf-8", errors="ignore")
                    pages_audited.append(uploaded_html.name)
                    issues_all = audit_html(html_text)
                    for it in issues_all:
                        it["page"] = uploaded_html.name
    else:
        if st.button("Run Audit", type="primary", use_container_width=True):
            if not uploaded_pdf:
                st.warning("Please upload a PDF.")
            else:
                with st.spinner("Reading PDF..."):
                    text = read_pdf_bytes(uploaded_pdf)
                    pages_audited.append(uploaded_pdf.name)
                    issues_all = audit_pdf(text)
                    for it in issues_all:
                        it["page"] = uploaded_pdf.name

    if issues_all:
        # Summary chips
        sev_counts = {"high": 0, "medium": 0, "low": 0}
        for it in issues_all:
            sev_counts[it["severity"]] = sev_counts.get(it["severity"], 0) + it.get("count", 1)

        st.markdown(
            f"""
**Pages audited:** {len(set(pages_audited))}  
**Issues (approx counts by severity):**  
- 🔴 High: **{sev_counts.get('high', 0)}**  
- 🟠 Medium: **{sev_counts.get('medium', 0)}**  
- 🟡 Low: **{sev_counts.get('low', 0)}**
"""
        )

        # Table-like details
        for i, it in enumerate(issues_all, start=1):
            with st.expander(f"{i}. [{it['severity'].upper()}] WCAG {it['wcag']} {('— ' + it['wcag_title']) if it.get('wcag_title') else ''} — {it['message']}  (page: {it.get('page','')})"):
                st.write(f"**Count:** {it['count']}")
                if it.get("examples"):
                    st.code("\n\n".join(it["examples"]), language="html")
                # Quick remediation hints
                st.markdown("**Remediation tips:**")
                hints = remediation_hints(it["wcag"])
                if hints:
                    st.markdown(hints)
                else:
                    st.write("Provide semantic HTML, proper ARIA if needed, and ensure keyboard and screen reader support.")

        # Export
        st.subheader("Export")
        as_json = json.dumps(issues_all, ensure_ascii=False, indent=2)
        st.download_button("Download JSON", as_json, file_name="accessibility_report.json", mime="application/json")
        # CSV (flat rows)
        import csv
        from io import StringIO
        csv_buf = StringIO()
        writer = csv.writer(csv_buf)
        writer.writerow(["page", "severity", "wcag", "wcag_title", "message", "count", "example"])
        for it in issues_all:
            exs = it.get("examples", []) or [""]
            for ex in exs:
                writer.writerow([it.get("page",""), it["severity"], it["wcag"], it.get("wcag_title",""), it["message"], it["count"], ex])
        st.download_button("Download CSV", csv_buf.getvalue(), file_name="accessibility_report.csv", mime="text/csv")

        st.divider()

        if want_plan:
            target = base_url or (uploaded_html.name if uploaded_html else uploaded_pdf.name if uploaded_pdf else "Uploaded file")
            st.subheader("Remediation Plan")
            with st.spinner("Drafting plan..."):
                plan = remediation_plan(issues_all, target)
                st.write(plan)
    else:
        st.info("Run an audit to see results here.")

with col_right:
    st.subheader("Guidance & Scope")
    st.markdown(
        """
This tool runs pragmatic WCAG 2.2 AA checks:

- **Document**: `<html lang>`, `<title>`, `<meta viewport>`
- **Structure**: heading presence/order, duplicate IDs
- **Text alternatives**: `<img alt>`
- **Navigation/Controls**: link and button names
- **Forms**: labels or accessible names
- **Visual**: inline color contrast (heuristic)
- **PDFs**: checks for extractable text (OCR hint)

For full coverage (JS/CSS/ARIA in runtime), pair with a headless runner (axe-core/pa11y) in CI.
"""
    )
    st.divider()
    st.markdown("**OpenAI status:** " + ("✅ enabled" if llm_available() else "⚠️ not configured"))

# ---------------------- Remediation Hints ----------------------

def remediation_hints(wcag_code: str) -> str:
    tips = {
        "1.1.1": """
- Provide meaningful `alt` for informative images; decorative images should use `alt=""` or `role="presentation"`.
- Avoid repeating nearby text; keep alt concise and specific.""",
        "1.3.1": """
- Use proper heading levels (`h1`..`h6`) without skipping.
- Convey structure with semantic HTML (lists, tables with headers).""",
        "1.4.3": """
- Ensure contrast ratio ≥ 4.5:1 for normal text (3:1 for large text).
- Adjust colors or add background, avoid text over images without overlay.""",
        "1.4.10": """
- Include `<meta name="viewport" content="width=device-width, initial-scale=1">`.
- Ensure layouts reflow without two-dimensional scrolling.""",
        "2.4.2": """
- Provide a concise, unique `<title>` describing the page purpose.""",
        "2.4.4": """
- Ensure links have clear, descriptive text (avoid “Click here”).
- Use `aria-label` or `title` only when visible text cannot be used.""",
        "2.4.6": """
- Use headings and labels that describe topic or purpose.""",
        "3.1.1": """
- Set the primary language on `<html lang="en">` (or your language code).""",
        "3.3.2": """
- Associate labels with form fields using `<label for="id">` and `id`.
- Alternatively, ensure `aria-label`/`aria-labelledby` is present and meaningful.""",
        "4.1.1": """
- Ensure valid HTML without duplicate IDs and with properly nested elements.""",
        "4.1.2": """
- Interactive controls must expose name/role/state programmatically.
- Prefer native elements; use ARIA only when necessary and correctly."""
    }
    return tips.get(wcag_code, "")
