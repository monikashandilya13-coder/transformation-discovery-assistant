# Transformation Discovery Assistant (v2)

Features:
- Selector profiles (JSON upload) for login.
- Retry/timeout hardened navigation.
- Discovery: crawl same-origin pages, screenshots, CSV/JSON export.
- Q&A: Generate domain + technical questions per page using Grok (xAI).

## Run
```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install --with-deps chromium
streamlit run streamlit_app.py
```