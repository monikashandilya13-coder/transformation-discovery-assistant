import os, re, time, json, tempfile, io, csv, zipfile, pathlib
from urllib.parse import urljoin, urlparse, urldefrag
from collections import deque
from typing import Dict, Any, List
import streamlit as st
import requests

# --- Playwright Setup (NO RUNTIME INSTALL) ---
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "0"
LOCAL_BROWSERS = pathlib.Path(".local-browsers")

if not LOCAL_BROWSERS.exists():
    st.error("Playwright browsers not found. Build failed.")
    st.stop()

st.success("Playwright ready")
from playwright.sync_api import sync_playwright

# Optional
try:
    import tiktoken
except Exception:
    tiktoken = None

st.set_page_config(page_title="Transformation Discovery Assistant", page_icon="Compass", layout="wide")
st.title("Transformation Discovery Assistant — Discovery + Q&A")

with st.expander("Read me (Security, Legal, Limits)"):
    st.markdown("""
- Use only on sites you are authorized to access.
- Credentials are used in memory only; never stored.
- Scans are capped by page/time to avoid abuse.
- This is not a penetration tool; basic form login only.
- SSO/MFA not supported by default.
""")

# ------------------ Helpers ------------------
def normalize_link(current_url, href):
    if not href: return None
    href = href.strip()
    href, _ = urldefrag(href)
    return urljoin(current_url, href)

def same_origin(u1, u2):
    p1, p2 = urlparse(u1), urlparse(u2)
    def norm_port(p): return p.port or (80 if p.scheme == "http" else 443)
    return (p1.scheme, p1.hostname, norm_port(p1)) == (p2.scheme, p2.hostname, norm_port(p2))

def path_prefix(u):
    p = urlparse(u)
    return p.path if p.path.endswith("/") else (p.path.rsplit("/",1)[0] + "/")

def navigate_with_retries(page, url, wait_ms: int, retries: int = 2, nav_timeout: int = 15000):
    last_err = None
    for attempt in range(retries + 1):
        try:
            page.set_default_navigation_timeout(nav_timeout)
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_timeout(wait_ms)
            return True, None
        except Exception as e:
            last_err = e
            time.sleep(0.5 * (attempt + 1))
    return False, str(last_err)[:300]

USER_HEUR = ['input[name="username"]','input[id="username"]','input[name*="user"]','input[id*="user"]',
             'input[type="email"]','input[name="email"]','input[id="email"]','input[name*="mail"]','input[type="text"]']
PASS_HEUR = ['input[type="password"]','input[name*="pass"]','input[id*="pass"]']
SUBM_HEUR = ['button[type="submit"]','input[type="submit"]','button[name*="login"]','button[id*="login"]']

def try_login(page, start_url, username, password, user_sel=None, pass_sel=None, submit_sel=None, indicator=None, nav_timeout=10000):
    ok, err = navigate_with_retries(page, start_url, wait_ms=400, retries=1, nav_timeout=nav_timeout)
    if not ok: return False, page.url
    if not (username and password): return False, page.url

    def safe_fill(sel, val):
        try: page.fill(sel, val); return True
        except Exception: return False

    filled_u = filled_p = False
    if user_sel and pass_sel:
        filled_u = safe_fill(user_sel, username)
        filled_p = safe_fill(pass_sel, password)
        if submit_sel:
            try:
                with page.expect_navigation(timeout=nav_timeout):
                    page.click(submit_sel)
            except Exception:
                try: page.press(pass_sel, "Enter")
                except Exception: pass
    else:
        for us in USER_HEUR:
            el = page.query_selector(us)
            if el:
                try: el.fill(username); filled_u = True; break
                except Exception: pass
        for ps in PASS_HEUR:
            el = page.query_selector(ps)
            if el:
                try: el.fill(password); filled_p = True; break
                except Exception: pass
        if filled_u and filled_p:
            clicked = False
            for ss in SUBM_HEUR:
                el = page.query_selector(ss)
                if el:
                    try:
                        with page.expect_navigation(timeout=nav_timeout):
                            el.click()
                        clicked = True; break
                    except Exception: pass
            if not clicked:
                try:
                    for ps in PASS_HEUR:
                        el = page.query_selector(ps)
                        if el: el.press("Enter"); break
                except Exception: pass

    if indicator:
        try: page.wait_for_selector(indicator, timeout=nav_timeout)
        except Exception: pass
    page.wait_for_timeout(800)
    return True, page.url

def clean_visible_text(page):
    BOILER = ["nav","footer","header","aside","menu"]
    for sel in BOILER:
        try:
            for el in page.query_selector_all(sel):
                page.evaluate("(n)=>n.remove()", el)
        except Exception: pass
    heads = []
    for tag in ["h1","h2","h3","h4","h5","h6"]:
        try:
            for el in page.query_selector_all(tag):
                t = (el.inner_text() or "").strip()
                if t: heads.append(t)
        except Exception: pass
    try:
        body = (page.inner_text("body") or "").strip()
    except Exception: body = ""
    body = re.sub(r"[ \t]+"," ", body)
    body = re.sub(r"\n{2,}", "\n", body)
    return "\n".join(heads) + "\n\n" + body

def crawl(context, seed_url, max_pages, wait_ms, same_path_only, capture_screens):
    results = []
    seen = set([seed_url])
    q = deque([seed_url])
    start_prefix = path_prefix(seed_url)
    page = context.new_page()
    url_status = {}
    def on_resp(r):
        try: url_status[r.url] = r.status
        except Exception: pass
    page.on("response", on_resp)
    count = 0
    while q and count < max_pages:
        cur = q.popleft()
        ok, err = navigate_with_retries(page, cur, wait_ms=wait_ms, retries=2)
        status = url_status.get(cur)
        title = None
        shot_bytes = None
        text = ""
        if not ok:
            results.append({"url": cur, "status": status, "title": None, "screenshot": None, "error": err, "text": ""})
            count += 1
            continue
        try:
            title = page.title()
            text = clean_visible_text(page)[:200000]
            if capture_screens:
                shot_bytes = page.screenshot(full_page=True)
        except Exception: pass
        results.append({"url": cur, "status": status, "title": title, "screenshot": shot_bytes, "error": None, "text": text})
        count += 1
        try:
            anchors = page.query_selector_all("a[href]")
            for a in anchors:
                href = a.get_attribute("href")
                nxt = normalize_link(cur, href)
                if not nxt: continue
                if not same_origin(seed_url, nxt): continue
                if same_path_only and not (urlparse(nxt).path or "/").startswith(start_prefix): continue
                if nxt not in seen:
                    seen.add(nxt); q.append(nxt)
        except Exception: pass
    return results

# ------------------ LLM Q&A ------------------
def redact(text: str) -> str:
    text = re.sub(r"[
