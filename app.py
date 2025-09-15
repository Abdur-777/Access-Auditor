# app.py — Accessibility Auditor (WCAG Quick-Check) with Wyndham presets
# Run: streamlit run app.py

import re, json
from dataclasses import dataclass, asdict
from typing import List, Dict, Optional, Tuple
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup, Tag
import pandas as pd
import streamlit as st

# ---------- Brand / Council presets ----------
WYNDHAM = {
    "name": "Wyndham City Council",
    "logo": "https://www.wyndham.vic.gov.au/themes/custom/wyndham/logo.png",
    "primary": "#003B73",
    "links": {
        "Home": "https://www.wyndham.vic.gov.au/",
        "Waste & Recycling": "https://www.wyndham.vic.gov.au/services/waste-recycling",
        "Bin days": "https://www.wyndham.vic.gov.au/residents/waste-recycling/bin-collection",
        "Hard waste": "https://www.wyndham.vic.gov.au/services/waste-recycling/hard-and-green-waste-collection-service",
        "Accessibility statement": "https://www.wyndham.vic.gov.au/accessibility",
    },
    "quick_targets": [
        "https://www.wyndham.vic.gov.au/services/waste-recycling",
        "https://www.wyndham.vic.gov.au/residents/waste-recycling/bin-collection",
        "https://www.wyndham.vic.gov.au/services/planning-building",
        "https://www.wyndham.vic.gov.au/residents/parking-roads",
    ],
}

APP_NAME = "Accessibility Auditor — WCAG Quick-Check"
st.set_page_config(page_title=APP_NAME, page_icon="✅", layout="wide")


# ---------- Helpers ----------
def short(text: str, limit: int = 140) -> str:
    s = " ".join((text or "").split())
    return s[: limit - 1] + "…" if len(s) > limit else s


@st.cache_data(show_spinner=False, ttl=300)
def fetch(url: str, timeout: int = 12) -> Tuple[str, str]:
    """Fetch URL and return (final_url, html). Raises if non-HTML."""
    headers = {"User-Agent": "Mozilla/5.0 (WCAG-QuickCheck; +https://example.local)"}
    r = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
    ctype = r.headers.get("content-type", "")
    if "text/html" not in ctype:
        raise ValueError(f"URL is not HTML (content-type: {ctype})")
    r.raise_for_status()
    r.encoding = r.apparent_encoding or r.encoding
    return r.url, r.text


def label_targeted(soup: BeautifulSoup, el: Tag) -> bool:
    """Is there a <label for=id>, wrapping <label>, aria-labelledby (with text) or aria-label?"""
    if not el:
        return False
    # Wrapped by a <label>
    p = el.parent
    while p and isinstance(p, Tag):
        if p.name == "label" and p.get_text(strip=True):
            return True
        p = p.parent
    # <label for="">
    el_id = el.get("id")
    if el_id:
        lab = soup.find("label", attrs={"for": el_id})
        if lab and lab.get_text(strip=True):
            return True
    # aria-labelledby targets with text
    ll = el.get("aria-labelledby")
    if ll:
        for _id in str(ll).split():
            tgt = soup.find(id=_id)
            if tgt and tgt.get_text(strip=True):
                return True
    # aria-label
    if el.get("aria-label"):
        return True
    return False


def has_accessible_name(el: Tag, soup: BeautifulSoup) -> bool:
    """Does a button/link have an accessible name?"""
    if not el:
        return False
    if el.get_text(strip=True):
        return True
    for attr in ("aria-label", "title"):
        if el.has_attr(attr) and str(el[attr]).strip():
            return True
    if el.has_attr("aria-labelledby"):
        for ref_id in str(el["aria-labelledby"]).split():
            tgt = soup.find(id=ref_id)
            if tgt and tgt.get_text(strip=True):
                return True
    if el.name == "input" and el.get("type") == "image" and el.get("alt"):
        return True
    if el.name == "svg" and el.get("aria-label"):
        return True
    return False


# ---- Contrast utilities (simple / inline styles only) ----
def _parse_color(val: str) -> Optional[Tuple[float, float, float]]:
    val = (val or "").strip().lower()
    if val.startswith("#"):
        h = val[1:]
        if len(h) == 3:
            r, g, b = [int(c * 2, 16) for c in h]
        elif len(h) == 6:
            r, g, b = [int(h[i : i + 2], 16) for i in (0, 2, 4)]
        else:
            return None
        return (r / 255.0, g / 255.0, b / 255.0)
    if val.startswith("rgb"):
        nums = re.findall(r"[\d.]+", val)
        if len(nums) >= 3:
            r, g, b = [min(255, max(0, int(float(n)))) for n in nums[:3]]
            return (r / 255.0, g / 255.0, b / 255.0)
    return None


def _rel_lum(rgb: Tuple[float, float, float]) -> float:
    def lin(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = [lin(c) for c in rgb]
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast_ratio(fg: Tuple[float, float, float], bg: Tuple[float, float, float]) -> float:
    L1, L2 = sorted([_rel_lum(fg), _rel_lum(bg)], reverse=True)
    return (L1 + 0.05) / (L2 + 0.05)


# ---------- Data models ----------
@dataclass
class Issue:
    id: str
    title: str
    severity: str  # high/medium/low
    wcag: str
    location: str
    snippet: str
    recommendation: str


def issues_df(issues: List[Issue]) -> pd.DataFrame:
    return pd.DataFrame([asdict(i) for i in issues])


def counts(df: pd.DataFrame) -> Dict[str, int]:
    if df.empty:
        return {"high": 0, "medium": 0, "low": 0}
    return {
        "high": int((df["severity"] == "high").sum()),
        "medium": int((df["severity"] == "medium").sum()),
        "low": int((df["severity"] == "low").sum()),
    }


# ---------- Analyzer ----------
def analyze(
    html: str,
    url: str,
    council: Optional[str] = None,
    check_contrast: bool = False,
    check_footer_access_link: bool = True,
    check_pdf_links: bool = True,
    check_tabindex: bool = True,
) -> List[Issue]:
    soup = BeautifulSoup(html, "html.parser")
    issues: List[Issue] = []
    seq = 1

    def add(title, severity, wcag, location, snippet, recommendation):
        nonlocal seq
        issues.append(
            Issue(
                id=f"{severity.upper()}-{seq:03d}",
                title=title,
                severity=severity,
                wcag=wcag,
                location=location,
                snippet=snippet,
                recommendation=(recommendation or "").strip(),
            )
        )
        seq += 1

    # 1) <html lang>
    html_tag = soup.find("html")
    if not html_tag or not html_tag.get("lang"):
        add(
            "Page language missing",
            "medium",
            "3.1.1",
            "<html>",
            "<html> tag missing a lang attribute.",
            "Add a valid primary language code (e.g., <html lang='en-AU'>).",
        )

    # 2) <title>
    t = soup.find("title")
    if not t or not t.get_text(strip=True):
        add(
            "Page title missing or empty",
            "high",
            "2.4.2",
            "<head><title>",
            "Missing or empty <title> tag.",
            "Provide a concise, descriptive page title (e.g., “Bin collection – Wyndham City Council”).",
        )

    # 3) Heading order (no jumps)
    headings = [(h.name, h.get_text(strip=True)) for h in soup.find_all(re.compile("^h[1-6]$"))]
    last = 0
    for name, text_ in headings:
        level = int(name[1])
        if last and (level - last) > 1:
            add(
                "Heading level skipped",
                "low",
                "1.3.1",
                f"<{name}>",
                f"Found heading '{text_}' ({name}) after h{last}.",
                "Don’t skip heading levels (e.g., use h2 after h1, not h3).",
            )
        last = level

    # 4) Inputs without accessible label/name
    inputs = soup.find_all(["input", "select", "textarea"])
    for el in inputs:
        # ignore hidden and non-data submit/button controls
        if el.name == "input" and el.get("type") in ["hidden", "submit", "button", "image"]:
            continue
        if not label_targeted(soup, el):
            add(
                "Form control missing accessible label",
                "high",
                "3.3.2",
                short(str(el), 300),
                short(str(el), 160),
                "Associate a visible <label for='id'> or add aria-label / aria-labelledby with meaningful text.",
            )

    # 5) Buttons/links without accessible name
    actionable = soup.find_all(["button", "a"])
    for el in actionable:
        if el.name == "a" and not el.get("href"):
            continue
        if not has_accessible_name(el, soup):
            add(
                "Interactive element missing accessible name",
                "high",
                "4.1.2",
                short(str(el), 300),
                short(str(el), 160),
                "Provide visible text, or set aria-label / aria-labelledby with a clear action name.",
            )

    # 6) Images without alt
    for img in soup.find_all("img"):
        if not img.get("alt"):
            add(
                "Image missing alt text",
                "medium",
                "1.1.1",
                short(str(img), 300),
                short(str(img), 160),
                "Add descriptive alt text. Use empty alt (alt='') only for decorative images.",
            )

    # 7) Vague link text
    vague = re.compile(r"^(click here|read more|more|learn more|here)$", re.I)
    for a in soup.find_all("a"):
        label = a.get_text(" ", strip=True)
        if label and vague.match(label):
            dest = a.get("href", "")
            add(
                "Ambiguous link text",
                "low",
                "2.4.4",
                dest or short(str(a), 300),
                f"Link text: “{label}”",
                "Make link text specific to its destination (e.g., “Book hard waste collection”).",
            )

    # 8) Simple contrast check for inline styles (beta)
    if check_contrast:
        for el in soup.find_all(True):
            style = el.get("style") or ""
            if "color" in style and "background-color" in style:
                mfg = re.search(r"color\s*:\s*([^;]+)", style, re.I)
                mbg = re.search(r"background-color\s*:\s*([^;]+)", style, re.I)
                if not (mfg and mbg):
                    continue
                fg = _parse_color(mfg.group(1))
                bg = _parse_color(mbg.group(1))
                if not fg or not bg:
                    continue
                ratio = _contrast_ratio(fg, bg)
                if ratio < 4.5:
                    add(
                        "Low text contrast (inline styles)",
                        "medium",
                        "1.4.3",
                        short(str(el), 300),
                        f"Computed contrast ≈ {ratio:.2f}:1",
                        "Increase contrast to ≥4.5:1 for normal text (≥3:1 for large text ≥18pt or 14pt bold).",
                    )

    # 9) PDF link warning (council-heavy)
    if check_pdf_links:
        pdf_links = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if ".pdf" in href.lower():
                text = a.get_text(" ", strip=True)
                pdf_links.append((href, text))
        if pdf_links:
            add(
                "PDFs linked on page",
                "low",
                "1.1.1 / 1.3.1",
                url,
                f"Found {len(pdf_links)} PDF link(s).",
                "Provide an accessible HTML equivalent or ensure the PDF is tagged and WCAG-compliant.",
            )

    # 10) Footer accessibility link presence
    if check_footer_access_link:
        footer = soup.find("footer")
        has_access_link = False
        pattern = re.compile(r"accessibility|accessibility statement|report an accessibility issue", re.I)
        search_scope = footer or soup
        for a in search_scope.find_all("a"):
            if pattern.search(a.get_text(" ", strip=True)) or (a.get("href") and pattern.search(a["href"])):
                has_access_link = True
                break
        if not has_access_link:
            add(
                "No visible Accessibility link",
                "low",
                "2.4.5",
                "<footer>",
                "Could not find an Accessibility/Accessibility Statement link in footer or page.",
                "Add a visible link to the Accessibility page and a short form for reporting accessibility issues.",
            )

    # 11) Keyboard: taboo tabindex values
    if check_tabindex:
        for el in soup.find_all(True):
            if el.has_attr("tabindex") and str(el["tabindex"]).strip() == "-1":
                # If it's interactive, warn
                if el.name in ("a", "button", "input", "select", "textarea") or el.get("role") in ("button", "link"):
                    add(
                        "Interactive element removed from tab order",
                        "medium",
                        "2.1.1",
                        short(str(el), 300),
                        short(str(el), 160),
                        "Avoid tabindex='-1' on interactive controls needed by keyboard users.",
                    )

    # Council-specific nudge
    if council and "Wyndham" in council:
        add(
            "Wyndham: add ‘Report an accessibility issue’ link",
            "low",
            "2.4.5",
            url,
            "Council pages should expose a clear feedback / report link.",
            "Add a footer link to the Accessibility page and a short form for reporting accessibility problems.",
        )

    return issues


# ---------- UI bits ----------
def header_wyndham():
    st.markdown(
        f"""
        <div style="display:flex;align-items:center;gap:14px;margin:8px 0 22px 0">
            <img src="{WYNDHAM['logo']}" alt="Wyndham" height="40"/>
            <div style="font-size:22px;font-weight:700">{APP_NAME}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def stat_chip(label: str, value: int):
    st.markdown(
        f"""
        <div style="
            border-radius:14px;padding:10px 14px;border:1px solid #e8e8e8;
            background:#fff;box-shadow:0 1px 4px rgba(0,0,0,.04);text-align:center">
            <div style="font-size:12px;color:#555">{label}</div>
            <div style="font-size:22px;font-weight:700">{value}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def make_downloads(df: pd.DataFrame, page_url: str):
    csv = df.to_csv(index=False)
    js = df.to_json(orient="records", indent=2)
    c1, c2, c3 = st.columns([1, 1, 2])
    with c1:
        st.download_button("Download CSV", data=csv, file_name="accessibility_report.csv", mime="text/csv")
    with c2:
        st.download_button("Download JSON", data=js, file_name="accessibility_report.json", mime="application/json")
    with c3:
        st.code(
            f'# Paste into your bug ticket\nURL = "{page_url}"\nissues = {json.dumps(json.loads(js)[:2], indent=2)}\n# (truncated preview)',
            language="python",
        )


# ---------- App ----------
def main():
    header_wyndham()

    left, right = st.columns([1.2, 1], vertical_alignment="center")

    with left:
        st.write("Quickly spot WCAG issues on a single page. For compliance, follow up with a full WCAG 2.x evaluation.")
        url = st.text_input("Page URL", value=WYNDHAM["links"]["Hard waste"], placeholder="https://…")
        do_contrast = st.checkbox("Run color contrast check (beta)", value=False)
        analyze_btn = st.button("Analyze URL", type="primary")

    with right:
        st.write("**Wyndham shortcuts**")
        col1, col2 = st.columns(2)
        for i, link in enumerate(WYNDHAM["quick_targets"]):
            (col1 if i % 2 == 0 else col2).markdown(f"- [{link.split('/')[3].title()}]({link})")
        st.caption(f"[Accessibility statement]({WYNDHAM['links']['Accessibility statement']})")

    st.divider()

    if analyze_btn and url.strip():
        try:
            with st.spinner("Fetching & analyzing…"):
                final_url, html = fetch(url.strip())
                iss = analyze(
                    html,
                    final_url,
                    council=WYNDHAM["name"],
                    check_contrast=do_contrast,
                    check_footer_access_link=True,
                    check_pdf_links=True,
                    check_tabindex=True,
                )
                df = issues_df(iss)
                cnt = counts(df)

            a, b, c, d = st.columns([1, 1, 1, 6])
            with a:
                stat_chip("Total issues", len(df))
            with b:
                stat_chip("High", cnt["high"])
            with c:
                stat_chip("Medium", cnt["medium"])
            with d:
                stat_chip("Low", cnt["low"])

            st.subheader("Results")
            if df.empty:
                st.success("No issues found in these heuristics. Run a full manual + assistive tech evaluation to confirm.")
            else:
                show_cols = ["id", "title", "severity", "wcag", "location", "snippet"]
                grid = df[show_cols].rename(
                    columns={
                        "id": "ID",
                        "title": "Title",
                        "severity": "Severity",
                        "wcag": "WCAG",
                        "location": "Location",
                        "snippet": "Snippet",
                    }
                )
                st.dataframe(grid, use_container_width=True, hide_index=True)

                st.subheader("Details")
                for _, row in df.iterrows():
                    with st.expander(f"[{row['severity'].upper()}] {row['title']} — {row['id']}"):
                        st.markdown(f"**Location:** `{short(row['location'], 300)}`")
                        st.markdown(f"**WCAG:** {row['wcag']}")
                        st.markdown("**Snippet:**")
                        st.code(row["snippet"], language="html")
                        st.markdown("**Recommendations:**")
                        st.write(row["recommendation"])

                st.subheader("Export")
                make_downloads(df, final_url)

            st.divider()
            st.caption(
                "This is a heuristic scanner. For compliance, conduct a full WCAG 2.x evaluation with manual testing, keyboard checks, and assistive technologies."
            )
        except Exception as e:
            st.error(f"Failed to analyze: {e}")

    with st.sidebar:
        st.markdown("### Council")
        st.write(f"Auditing: **{WYNDHAM['name']}**")
        st.color_picker("Theme (primary)", WYNDHAM["primary"], key="theme_color", help="Visual only")
        st.markdown("---")
        st.markdown("### Tips")
        st.markdown(
            """
- Start with **transactional pages** (forms, payments, bin bookings).
- Fix **High** first: titles, form labels, button names.
- Prefer **explicit labels** over placeholders.
- Avoid “click here”; make links descriptive.
- Provide an **Accessibility contact** on every page.
"""
        )


if __name__ == "__main__":
    main()
