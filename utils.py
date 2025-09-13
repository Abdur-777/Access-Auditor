import os, pathlib, requests


ASSETS_DIR = pathlib.Path("assets")
ASSETS_DIR.mkdir(exist_ok=True)
AXE_PATH = ASSETS_DIR / "axe.min.js"
AXE_CDN = "https://cdnjs.cloudflare.com/ajax/libs/axe-core/4.9.1/axe.min.js"




def ensure_axe_js() -> str:
"""Ensure axe.min.js exists locally, download from CDN if missing."""
if AXE_PATH.exists() and AXE_PATH.stat().st_size > 0:
return str(AXE_PATH)
try:
r = requests.get(AXE_CDN, timeout=20)
r.raise_for_status()
AXE_PATH.write_bytes(r.content)
except Exception as e:
# Last‑ditch: minimal inline axe stub (won't scan), keeps app from crashing
AXE_PATH.write_text("window.axe=window.axe||{};")
return str(AXE_PATH)




def safe_filename(name: str) -> str:
from slugify import slugify
return slugify(name or "report")
