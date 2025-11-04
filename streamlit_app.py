import os
import re
import time
import json
import tempfile
import io
import csv
import zipfile
import pathlib
from urllib.parse import urljoin, urlparse, urldefrag
from collections import deque
from typing import Dict, Any, List
import streamlit as st
import requests
import asyncio
from pyppeteer import launch

# -------------------------------------------------
# 1. Pyppeteer browser (started once per session)
# -------------------------------------------------
if "browser" not in st.session_state:
    st.write("**Starting Pyppeteer browser…**")
    async def _start_browser():
        return await launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--single-process",
            ],
            handleSIGINT=False,
            handleSIGTERM=False,
            handleSIGHUP=False,
        )

    try:
        st.session_state.browser = asyncio.run(_start_browser())
        st.session_state.page = asyncio.run(st.session_state.browser.newPage())
        st.success("Pyppeteer ready!")
    except Exception as e:
        st.error(f"Failed to start browser: {e}")
        st.stop()
else:
    st.success("Pyppeteer ready!")

page = st.session_state.page
browser = st.session_state.browser

# -------------------------------------------------
# 2. Helper functions (same logic as before)
# -------------------------------------------------
def normalize_link(current_url: str, href: str) -> str | None:
    if not href:
        return None
    href = href.strip()
    href, _ = urldefrag(href)
    return urljoin(current_url, href)


def same_origin(u1: str, u2: str) -> bool:
    p1 = urlparse(u1)
    p2 = urlparse(u2)
    return (p1.scheme, p1.hostname, p1.port or (443 if p1.scheme == "https" else 80)) == (
        p2.scheme,
        p2.hostname,
        p2.port or (443 if p2.scheme == "https" else 80),
    )


def path_prefix(u: str) -> str:
    p = urlparse(u)
    path = p.path
    return path if path.endswith("/") else (path.rsplit("/", 1)[0] + "/")


async def navigate_with_retries(url: str, wait_ms: int, retries: int = 2, timeout: int = 15000) -> tuple[bool, str | None]:
    last_err = None
    for attempt in range(retries + 1):
        try:
            await page.goto(url, waitUntil="domcontentloaded", timeout=timeout)
            await page.waitForTimeout(wait_ms)
            return True, None
        except Exception as e:
            last_err = str(e)
            await asyncio.sleep(0.5 * (attempt + 1))
    return False, last_err


USER_HEUR = [
    'input[name="username"]',
    'input[id="username"]',
    'input[name*="user"]',
    'input[id*="user"]',
    'input[type="email"]',
    'input[name="email"]',
    'input[id="email"]',
]
PASS_HEUR = ['input[type="password"]', 'input[name*="pass"]', 'input[id*="pass"]']
SUBM_HEUR = ['button[type="submit"]', 'input[type="submit"]', 'button[name*="login"]']


async def try_login(
    start_url: str,
    username: str,
    password: str,
    user_sel: str | None = None,
    pass_sel: str | None = None,
    submit_sel: str | None = None,
    indicator: str | None = None,
    timeout: int = 10000,
) -> tuple[bool, str]:
    ok, err = await navigate_with_retries(start_url, wait_ms=400, timeout=timeout)
    if not ok:
        return False, page.url

    if not (username and password):
        return False, page.url

    async def safe_fill(sel: str, val: str) -> bool:
        try:
            await page.focus(sel)
            await page.keyboard.type(val)
            return True
        except Exception:
            return False

    filled_u = filled_p = False

    if user_sel and pass_sel:
        filled_u = await safe_fill(user_sel, username)
        filled_p = await safe_fill(pass_sel, password)
        if submit_sel:
            try:
                await page.click(submit_sel, timeout=timeout)
            except Exception:
                await page.keyboard.press("Enter")
    else:
        for sel in USER_HEUR:
            el = await page.querySelector(sel)
            if el:
                await el.focus()
                await page.keyboard.type(username)
                filled_u = True
                break
        for sel in PASS_HEUR:
            el = await page.querySelector(sel)
            if el:
                await el.focus()
                await page.keyboard.type(password)
                filled_p = True
                break
        if filled_u and filled_p:
            for sel in SUBM_HEUR:
                el = await page.querySelector(sel)
                if el:
                    try:
                        await el.click()
                        break
                    except Exception:
                        pass
            else:
                await page.keyboard.press("Enter")

    if indicator:
        try:
            await page.waitForSelector(indicator, timeout=timeout)
        except Exception:
            pass
    await page.waitForTimeout(800)
    return True, page.url


async def clean_visible_text() -> str:
    # Remove boilerplate
    boiler = ["nav", "footer", "header", "aside", "menu"]
    for sel in boiler:
        try:
            els = await page.querySelectorAll(sel)
            for el in els:
                await el.evaluate("n => n.remove()")
        except Exception:
            pass

    # Headings
    heads = []
    for tag in ["h1", "h2", "h3", "h4", "h5", "h6"]:
        try:
            els = await page.querySelectorAll(tag)
            for el in els:
                t = await el.evaluate("e => e.innerText?.trim()")
                if t:
                    heads.append(t)
        except Exception:
            pass

    body = await page.evaluate("() => document.body.innerText?.trim()") or ""
    body = re.sub(r"[ \t]+", " ", body)
    body = re.sub(r"\n{2,}", "\n", body)
    return "\n".join(heads) + "\n\n" + body


async def crawl(
    seed_url: str,
    max_pages: int,
    wait_ms: int,
    same_path_only: bool,
    capture_screens: bool,
) -> List[Dict[str, Any]]:
    results = []
    seen = {seed_url}
    q = deque([seeds_url])
    start_prefix = path_prefix(seed_url)
    count = 0

    while q and count < max_pages:
        cur = q.popleft()
        ok, err = await navigate_with_retries(cur, wait_ms=wait_ms)
        title = None
        shot = None
        text = ""

        if not ok:
            results.append({"url": cur, "title": None, "screenshot": None, "error": err, "text": ""})
            count += 1
            continue

        try:
            title = await page.title()
            text = await clean_visible_text()
            if capture_screens:
                shot = await page.screenshot(fullPage=True)
        except Exception:
            pass

        results.append({"url": cur, "title": title, "screenshot": shot, "error": None, "text": text})
        count += 1

        try:
            anchors = await page.querySelectorAll("a[href]")
            for a in anchors:
                href = await a.evaluate("el => el.getAttribute('href')")
                nxt = normalize_link(cur, href)
                if not nxt or not same_origin(seed_url, nxt):
                    continue
                if same_path_only and not urlparse(nxt).path.startswith(start_prefix):
                    continue
                if nxt not in seen:
                    seen.add(nxt)
                    q.append(nxt)
        except Exception:
            pass

    return results


# -------------------------------------------------
# 3. UI – Discovery Tab
# -------------------------------------------------
st.set_page_config(page_title="Transformation Discovery Assistant", layout="wide")
st.title("Transformation Discovery Assistant (Pyppeteer)")

tab_discovery, tab_qna = st.tabs(["Discovery", "Q&A"])

with tab_discovery:
    st.subheader("Web Crawl & Export")

    with st.form("disc_form"):
        start_url = st.text_input("Start URL", value="https://httpbin.org/html")
        c1, c2 = st.columns(2)
        username = c1.text_input("Username")
        password = c2.text_input("Password", type="password")

        st.markdown("**Options**")
        o1, o2, o3 = st.columns(3)
        max_pages = o1.number_input("Max pages", 1, 50, 10)
        wait_ms = o2.number_input("Wait after load (ms)", 0, 5000, 500)
        screenshot = o3.checkbox("Screenshots", value=True)
        same_path_only = st.checkbox("Same path prefix only", value=False)
        consent = st.checkbox("I am authorized to scan this site.", value=False)

        run = st.form_submit_button("Run Discovery")

        if run:
            if not consent:
                st.error("Consent required.")
                st.stop()
            if not start_url.startswith(("http://", "https://")):
                st.error("Valid URL required.")
                st.stop()

            status = st.status("Starting…")
            try:
               # Login (optional)
if username and password:
    status.update(label="Logging in…")
    success, final_url = asyncio.run(try_login(start_url, username, password))
    if not success:
        st.warning(f"Login failed. Proceeding from {final_url}")
    else:
        st.success(f"Logged in! Starting from {final_url}")
else:
    final_url = start_url

                seed = page.url
                status.update(label="Craw crawler…")
                results = await crawl(
                    seed,
                    max_pages=int(max_pages),
                    wait_ms=int(wait_ms),
                    same_path_only=same_path_only,
                    capture_screens=screenshot,
                )

                # Export
                mem_zip = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
                with zipfile.ZipFile(mem_zip.name, "w", zipfile.ZIP_DEFLATED) as zf:
                    zf.writestr("results.json", json.dumps(results, indent=2))
                    buf = io.StringIO()
                    cw = csv.writer(buf)
                    cw.writerow(["url", "title", "screenshot", "error"])
                    for r in results:
                        cw.writerow([r["url"], r.get("title", ""), "Yes" if r.get("screenshot") else "No", r.get("error", "")])
                    zf.writestr("results.csv", buf.getvalue())
                    for i, r in enumerate(results):
                        if r.get("screenshot"):
                            safe = re.sub(r"[^a-zA-Z0-9_\-]", "_", urlparse(r["url"]).path or "home")[:40]
                            zf.writestr(f"screens/{i}_{safe}.png", r["screenshot"])

                status.update(label="Done", state="complete")
                st.success(f"Crawled {len(results)} pages")
                st.download_button("Download ZIP", data=open(mem_zip.name, "rb").read(), file_name="discovery.zip", mime="application/zip")
                st.session_state.last_results = results

            except Exception as e:
                status.update(label="Failed", state="error")
                st.error(f"Error: {e}")

# -------------------------------------------------
# 4. Q&A Tab (uses last crawl results)
# -------------------------------------------------
with tab_qna:
    st.subheader("Generate Q&A from crawled pages")
    if "last_results" not in st.session_state:
        st.info("Run Discovery first.")
    else:
        # (Same LLM logic as before – omitted for brevity)
        st.write("Q&A generation coming soon…")
