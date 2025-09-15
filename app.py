# Accessibility Auditor (WCAG quick-check) — Streamlit app
# --------------------------------------------------------
# Features
# - Input a URL, paste HTML, or upload an .html file
# - Lightweight heuristics to flag common issues (img alt, link text, label/inputs, headings order, lang attr)
# - WCAG code mapping → remediation hints (robust to missing/variant codes)
# - Safe guards: no KeyError on missing keys, tolerant parsing
# - Results table + per-issue expanders + CSV/JSON downloads
# - Single-file app.py for Render/Streamlit Cloud

import re
import io
import json
from dataclasses import dataclass, asdict
from typing import List, Optional, Dict, Any, Iterable

import requests
from bs4 import BeautifulSoup

import streamlit as st

# -----------------------------
# WCAG mapping & helpers
# -----------------------------
WCAG_HINTS: Dict[str, str] = {
    "1.1.1": "Provide text alternatives for non-text content.",
    "1.3.1": "Use semantic HTML and landmarks to convey structure.",
    "1.4.3": "Ensure sufficient color contrast (AA).",
    "2.1.1": "All functionality must be keyboard accessible.",
    "2.4.4": "Use meaningful link text that describes the destination.",
    "2.4.6": "Use descriptive headings and labels.",
    "3.1.1": "Specify the default language of the page (e.g., <html lang='en'>).",
    "3.3.2": "Associate labels with form inputs and provide instructions.",
}

WCAG_CODE_REGEX = re.compile(r"(\d\.\d\.\d)")

def normalize_wcag_codes(wcag_codes: Any) -> List[str]:
    """Normalize input (None/str/list) to a list of bare n.n.n codes."""
    if not wcag_codes:
        return []
    if isinstance(wcag_codes, str):
        items = [wcag_codes]
    elif isinstance(wcag_codes, (list, tuple, set)):
        items = list(wcag_codes)
    else:
        return []
    out = []
    for item in items:
        if not isinstance(item, str):
            continue
        m = WCAG_CODE_REGEX.search(item)
        out.append(m.group(1) if m else item.strip())
    # dedupe while preserving order
    seen = set()
    cleaned = []
    for c in out:
        if c and c not in seen:
            cleaned.append(c)
            seen.add(c)
    return cleaned


def remediation_hints(wcag_codes: Any) -> List[str]:
    codes = normalize_wcag_codes(wcag_codes)
    hints: List[str] = []
    for c in codes:
        hint = WCAG_HINTS.get(c)
        if hint and hint not in hints:
            hints.append(hint)
    return hints


def safe_get_wcag(item: Any) -> List[str]:
    """Extract wcag codes from dict or object, robustly."""
    val = None
    if isinstance(item, dict):
        val = item.get("wcag") or item.get("wcag_codes") or item.get("wcagId") or item.get("wcag_ids")
    else:
        for attr in ("wcag", "wcag_codes", "wcagId", "wcag_ids"):
            if hasattr(item, attr):
                val = getattr(item, attr)
                break
    return normalize_wcag_codes(val)

# -----------------------------
# Issue model
# -----------------------------
@dataclass
class Issue:
    id: str
    title: str
    severity: str  # "low" | "medium" | "high"
    wcag: List[str]
    location: str  # CSS selector / tag summary / URL section
    snippet: str   # small text/HTML excerpt
    recommendation: List[str]

    def to_row(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "severity": self.severity,
            "wcag": ", ".join(self.wcag) if self.wcag else "",
            "location": self.location,
            "snippet": self.snippet,
            "recommendation": " | ".join(self.recommendation) if self.recommendation else "",
        }

# -----------------------------
# Fetch & Parse
# -----------------------------
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/117.0 Safari/537.36"
)

def fetch_url(url: str, timeout: int = 15) -> str:
    """Fetch HTML from a URL; returns text or raises requests.exceptions.RequestException."""
    headers = {"User-Agent": USER_AGENT}
    resp = requests.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    # basic content-type gate (allow text/html)
    ctype = resp.headers.get("content-type", "").lower()
    if "html" not in ctype and "xml" not in ctype:
        # still return text to allow some leniency
        pass
    return resp.text

# -----------------------------
# Heuristic checks
# -----------------------------

def summarize_tag(tag) -> str:
    if not tag:
        return ""
    name = getattr(tag, "name", "") or ""
    id_attr = tag.get("id", "") if hasattr(tag, 'get') else ""
    cls = " ".join(tag.get("class", [])) if hasattr(tag, 'get') else ""
    return f"<{name} id='{id_attr}' class='{cls}'>".strip()


def first_text(tag, maxlen: int = 120) -> str:
    if not tag:
        return ""
    txt = tag.get_text(strip=True)
    return (txt[: maxlen - 1] + "…") if len(txt) > maxlen else txt


def check_html_lang(soup: BeautifulSoup) -> List[Issue]:
    issues: List[Issue] = []
    html = soup.find("html")
    lang = html.get("lang") if html else None
    if not lang:
        issues.append(Issue(
            id="LANG-001",
            title="Missing page language",
            severity="medium",
            wcag=["3.1.1"],
            location="<html>",
            snippet="No lang attribute on <html>.",
            recommendation=remediation_hints(["3.1.1"]) or ["Add lang attribute, e.g., <html lang='en'>"],
        ))
    return issues


def check_img_alt(soup: BeautifulSoup) -> List[Issue]:
    issues: List[Issue] = []
    imgs = soup.find_all("img")
    for i, img in enumerate(imgs, start=1):
        if img.has_attr("role") and img.get("role") == "presentation":
            continue
        if not img.has_attr("alt") or (img.get("alt") or "").strip() == "":
            issues.append(Issue(
                id=f"IMGALT-{i:03d}",
                title="Image missing alt text",
                severity="high",
                wcag=["1.1.1"],
                location=summarize_tag(img),
                snippet=str(img)[:160] + ("…" if len(str(img)) > 160 else ""),
                recommendation=remediation_hints(["1.1.1"]) or ["Add meaningful alt text describing the image purpose."],
            ))
    return issues


def check_link_text(soup: BeautifulSoup) -> List[Issue]:
    issues: List[Issue] = []
    bad_texts = {"click here", "read more", "more", "here"}
    anchors = soup.find_all("a")
    for i, a in enumerate(anchors, start=1):
        text = (a.get_text(" ", strip=True) or "").lower()
        if text in bad_texts:
            issues.append(Issue(
                id=f"LINKTXT-{i:03d}",
                title="Non-descriptive link text",
                severity="medium",
                wcag=["2.4.4"],
                location=summarize_tag(a),
                snippet=a.get_text(" ", strip=True)[:120],
                recommendation=remediation_hints(["2.4.4"]) or ["Make link text describe the destination or action."],
            ))
    return issues


def check_labels(soup: BeautifulSoup) -> List[Issue]:
    issues: List[Issue] = []
    inputs = soup.find_all(["input", "select", "textarea"])
    # Build map of label[for]
    labels = {lbl.get("for"): lbl for lbl in soup.find_all("label") if lbl.get("for")}
    for i, el in enumerate(inputs, start=1):
        # Skip hidden inputs
        if el.name == "input" and el.get("type") == "hidden":
            continue
        has_label = False
        # 1) Explicit label via id/for
        eid = el.get("id")
        if eid and eid in labels:
            has_label = True
        # 2) Implicit label wrapping the input
        if not has_label:
            parent = el.parent
            if parent and parent.name == "label":
                has_label = True
        # 3) aria-label or aria-labelledby acceptable
        if not has_label and (el.get("aria-label") or el.get("aria-labelledby")):
            has_label = True
        if not has_label:
            issues.append(Issue(
                id=f"LABEL-{i:03d}",
                title="Form control missing accessible label",
                severity="high",
                wcag=["3.3.2"],
                location=summarize_tag(el),
                snippet=str(el)[:160] + ("…" if len(str(el)) > 160 else ""),
                recommendation=remediation_hints(["3.3.2"]) or ["Associate a <label> or aria-label/aria-labelledby with the form control."],
            ))
    return issues


def check_headings_order(soup: BeautifulSoup) -> List[Issue]:
    issues: List[Issue] = []
    headings = []
    for level in range(1, 7):
        for h in soup.find_all(f"h{level}"):
            headings.append((level, h))
    # Keep document order
    headings.sort(key=lambda x: x[1].sourceline if hasattr(x[1], 'sourceline') and x[1].sourceline else 0)
    last_level = 1
    for idx, (level, tag) in enumerate(headings, start=1):
        # Allow same level or +1 deeper; flag big jumps (e.g., h1 -> h4)
        if level > last_level + 1:
            issues.append(Issue(
                id=f"HEADINGS-{idx:03d}",
                title="Heading level jumps by more than one",
                severity="low",
                wcag=["1.3.1", "2.4.6"],
                location=summarize_tag(tag),
                snippet=first_text(tag),
                recommendation=remediation_hints(["1.3.1", "2.4.6"]) or ["Use headings sequentially (e.g., h2 under h1, not h4)."],
            ))
        last_level = level
    return issues


def analyze_html(html: str, url: Optional[str] = None) -> List[Issue]:
    soup = BeautifulSoup(html, "html.parser")
    issues: List[Issue] = []
    for fn in (check_html_lang, check_img_alt, check_link_text, check_labels, check_headings_order):
        try:
            issues.extend(fn(soup))
        except Exception as e:  # keep going even if one checker fails
            issues.append(Issue(
                id=f"CHECKERR-{fn.__name__}",
                title=f"Checker error in {fn.__name__}",
                severity="low",
                wcag=[],
                location=url or "(input)",
                snippet=str(e),
                recommendation=["Checker crashed; please report this case."],
            ))
    return issues

# -----------------------------
# Streamlit UI
# -----------------------------
st.set_page_config(page_title="Accessibility Auditor (WCAG)", page_icon="♿", layout="wide")

st.title("♿ Accessibility Auditor — WCAG quick-check")
st.markdown(
    "Scan a web page or pasted HTML for common accessibility issues. "
    "This is a heuristic pre-check — not a full audit."
)

with st.sidebar:
    st.header("Input")
    mode = st.radio("Choose input mode", ["URL", "Paste HTML", "Upload .html"], index=0)
    show_debug = st.checkbox("Debug mode", value=False)

    st.markdown("---")
    st.caption("WCAG checks covered in this quick scan:")
    st.write("- Page language (3.1.1)\n- Image alt text (1.1.1)\n- Link text descriptiveness (2.4.4)\n- Form labels (3.3.2)\n- Heading order (1.3.1, 2.4.6)")

html_text: Optional[str] = None
source_label = ""

if mode == "URL":
    url = st.text_input("Enter a URL", placeholder="https://www.example.com")
    if st.button("Analyze URL", type="primary"):
        if not url:
            st.error("Please enter a URL.")
        else:
            try:
                with st.spinner("Fetching page…"):
                    html_text = fetch_url(url)
                source_label = url
            except requests.exceptions.RequestException as e:
                st.error(f"Failed to fetch: {e}")

elif mode == "Paste HTML":
    html_text = st.text_area("Paste HTML here", height=300)
    source_label = "(pasted HTML)"
    st.info("Click the Analyze button when ready.")
    st.button("Analyze pasted HTML", type="primary")

else:  # Upload .html
    up = st.file_uploader("Upload an .html file", type=["html", "htm"])
    if up:
        try:
            content = up.read().decode("utf-8", errors="replace")
            html_text = content
            source_label = up.name
            st.success(f"Loaded {up.name}")
        except Exception as e:
            st.error(f"Failed to read file: {e}")

# Gate to run analysis (for Paste HTML mode we also require a click, so guard by session)
run = False
if mode == "URL":
    run = html_text is not None
elif mode == "Paste HTML":
    # Require explicit analyze click
    run = st.session_state.get("analyze_paste_clicked", False)
    # Set when the button above is clicked
    for k in st.session_state.keys():
        pass
    # Workaround: Use on_click in a lightweight way
    def _set_clicked():
        st.session_state["analyze_paste_clicked"] = True
    st.button("Analyze", on_click=_set_clicked, key="analyze_paste_btn")
    if st.session_state.get("analyze_paste_clicked") and html_text:
        run = True
elif mode == "Upload .html":
    run = html_text is not None

if run and html_text:
    with st.spinner("Analyzing HTML…"):
        issues = analyze_html(html_text, url=source_label)

    # Summaries
    total = len(issues)
    high = sum(1 for i in issues if i.severity == "high")
    med = sum(1 for i in issues if i.severity == "medium")
    low = sum(1 for i in issues if i.severity == "low")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total issues", total)
    c2.metric("High", high)
    c3.metric("Medium", med)
    c4.metric("Low", low)

    # Table view
    rows = [iss.to_row() for iss in issues]
    st.subheader("Results")
    if rows:
        st.dataframe(rows, use_container_width=True, hide_index=True)
    else:
        st.success("No issues detected by these checks.")

    # Detail expanders
    st.subheader("Details")
    for iss in issues:
        with st.expander(f"[{iss.severity.upper()}] {iss.title} — {iss.id}"):
            st.write(f"**Location:** {iss.location}")
            if iss.wcag:
                st.write("**WCAG:** ", ", ".join(iss.wcag))
            if iss.recommendation:
                st.write("**Recommendations:**")
                for rec in iss.recommendation:
                    st.write(f"- {rec}")
            if iss.snippet:
                st.code(iss.snippet, language="html")

    # Downloads
    st.subheader("Export")
    csv_buf = io.StringIO()
    # Build CSV manually to avoid pandas dependency
    headers = ["id", "title", "severity", "wcag", "location", "snippet", "recommendation"]
    csv_buf.write(",".join(headers) + "\n")
    for r in rows:
        line = [
            r.get("id", ""),
            r.get("title", ""),
            r.get("severity", ""),
            r.get("wcag", ""),
            r.get("location", "").replace("\n", " "),
            r.get("snippet", "").replace(",", " ").replace("\n", " ")[:500],
            r.get("recommendation", "").replace(",", ";").replace("\n", " ")[:500],
        ]
        csv_buf.write(",".join([json.dumps(x)[1:-1] for x in line]) + "\n")
    st.download_button("Download CSV", data=csv_buf.getvalue(), file_name="wcag_issues.csv", mime="text/csv")

    json_data = json.dumps([asdict(i) for i in issues], ensure_ascii=False, indent=2)
    st.download_button("Download JSON", data=json_data, file_name="wcag_issues.json", mime="application/json")

    if show_debug:
        st.subheader("Debug")
        st.text_area("Raw HTML (truncated)", html_text[:4000], height=200)
        st.write("Parsed issues (Python objects):")
        st.write(issues)

else:
    st.info("Provide input and click Analyze to run the checks.")

st.markdown("---")
st.caption(
    "This is a heuristic scanner to help you spot common problems quickly. "
    "For compliance, conduct a full WCAG 2.x evaluation with manual testing, keyboard checks, and assistive technologies.")
