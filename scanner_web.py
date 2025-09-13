from typing import Dict, Any, List
from playwright.sync_api import sync_playwright
from pathlib import Path
import json
from utils import ensure_axe_js




def run_axe_on_url(url: str, timeout_ms: int = 30000) -> Dict[str, Any]:
axe_path = ensure_axe_js()
with sync_playwright() as p:
browser = p.chromium.launch(args=["--no-sandbox"], headless=True)
context = browser.new_context()
page = context.new_page()
page.set_default_timeout(timeout_ms)
page.goto(url)
# inject axe
page.add_script_tag(path=axe_path)
# Run axe inside the page
result = page.evaluate(
"""
async () => {
if (!window.axe || !axe.run) {
return {error: 'axe not loaded'}
}
const res = await axe.run(document, {
resultTypes: ['violations', 'incomplete'],
runOnly: {
type: 'tag',
values: ['wcag2a','wcag2aa','wcag22aa']
}
});
return res;
}
"""
)
browser.close()
# Normalize result
if isinstance(result, str):
try:
result = json.loads(result)
except Exception:
result = {"error": "unexpected axe result"}
return result




def summarize_axe(result: Dict[str, Any]) -> Dict[str, Any]:
if result.get("error"):
return {"score": 0, "violations": [], "incomplete": [], "error": result["error"]}
violations = result.get("violations", [])
incomplete = result.get("incomplete", [])
# Simple scoring: 100 - (unique rules * 4) - (nodes * 1)
unique_rules = len(violations)
nodes = sum(len(v.get("nodes", [])) for v in violations)
score = max(0, 100 - unique_rules * 4 - nodes * 1)
return {
"score": score,
"violations": violations,
"incomplete": incomplete,
}
