# Accessibility Auditor — MVP


### Local dev
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install --with-deps chromium
streamlit run app.py


### Deploy to Render
- Push this repo to GitHub
- New > Web Service > Build from repo
- Render auto-runs the `render.yaml` build/start commands


### Notes
- The web score is a simple heuristic based on axe violations; you will refine it later.
- PDF checks are heuristics; upgrade later with a proper PDF/UA engine and auto-remediation (e.g., alt text suggestions via GPT).
