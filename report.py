from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm
from reportlab.lib import colors
from typing import List, Dict, Any
from utils import safe_filename




def draw_wrapped(c, text, x, y, max_width, leading=14):
from reportlab.pdfbase.pdfmetrics import stringWidth
words = text.split()
line = ""
while words and y > 20:
w = words.pop(0)
trial = (line + " " + w).strip()
if stringWidth(trial, "Helvetica", 11) <= max_width:
line = trial
else:
c.drawString(x, y, line)
y -= leading
line = w
if y > 20 and line:
c.drawString(x, y, line)
y -= leading
return y




def export_report(path: str, council_name: str, url: str, web_summary: Dict[str, Any], pdf_summaries: List[Dict[str, Any]]):
c = canvas.Canvas(path, pagesize=A4)
W, H = A4


# Header
c.setFillColor(colors.HexColor("#0B5ED7"))
c.rect(0, H - 30, W, 30, fill=True, stroke=False)
c.setFillColor(colors.white)
c.setFont("Helvetica-Bold", 14)
c.drawString(20, H - 22, f"Accessibility Audit — {council_name}")


c.setFillColor(colors.black)
c.setFont("Helvetica", 11)
y = H - 50
c.drawString(20, y, f"Scanned URL: {url}")
y -= 16
c.drawString(20, y, f"Overall Web Score (heuristic): {web_summary.get('score', 0)} / 100")
y -= 24
c.setFont("Helvetica-Bold", 12)
c.drawString(20, y, "Top Web Violations:")
y -= 16
c.setFont("Helvetica", 11)
for v in web_summary.get("violations", [])[:8]:
rule = v.get("id")
impact = v.get("impact")
help_text = v.get("help")
y = draw_wrapped(c, f"• [{impact}] {rule}: {help_text}", 26, y, W - 46)
if y < 60:
c.showPage(); y = H - 40


if not web_summary.get("violations"):
c.drawString(26, y, "• No blocking violations detected by axe.")
y -= 16


# PDF Section
c.setFont("Helvetica-Bold", 12)
c.drawString(20, y, "PDF Checks:")
y -= 16
c.setFont("Helvetica", 11)
if not pdf_summaries:
c.drawString(26, y, "• No PDFs found on the scanned page.")
y -= 16
else:
for p in pdf_summaries[:8]:
title = p.get("url", "PDF")
issues = p.get("issues", [])
y = draw_wrapped(c, f"• {title}", 26, y, W - 46)
if issues:
for iss in issues:
y = draw_wrapped(c, f" - {iss}", 36, y, W - 56)
else:
y = draw_wrapped(c, " - No obvious issues from heuristics.", 36, y, W - 56)
if y < 60:
c.showPage(); y = H - 40


# Footer note
c.setFont("Helvetica-Oblique", 9)
c.setFillColor(colors.gray)
c.drawString(20, 20, "Note: Web score is heuristic; PDFs use quick checks and are not a full WCAG PDF/UA audit.")


c.save()
