
import os, re, time, json, tempfile, io, csv
from urllib.parse import urljoin, urlparse, urldefrag
from collections import deque
from typing import Dict, Any, List
import streamlit as st

# --- Playwright Cloud Guard (install to writable dir) ---
import os, sys, subprocess, shutil, pathlib

# Force install into the repo dir (writable on Streamlit Cloud)
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "0"                  # -> ./.local-browsers
os.environ.setdefault("HOME", str(pathlib.Path.cwd()))        # avoid writes in /home/adminuser/venv



from playwright.sync_api import sync_playwright
# --- End guard ---

import streamlit as st, pathlib
st.write("PLAYWRIGHT_BROWSERS_PATH:", os.getenv("PLAYWRIGHT_BROWSERS_PATH"))
st.write("Local browsers dir exists:", pathlib.Path('.local-browsers').exists())
st.write("ms-playwright cache exists:", (pathlib.Path.home()/'.cache/ms-playwright').exists())


# Optional token counting for future
try:
    import tiktoken  # noqa
except Exception:
    tiktoken = None

st.set_page_config(page_title="Transformation Discovery Assistant", page_icon="🧭", layout="wide")
st.title("🧭 Transformation Discovery Assistant — Discovery + Q&A")

with st.expander("Read me (Security, Legal, Limits)"):
    st.markdown("""
- Use only on sites you are authorized to access.
- Credentials are used in memory only; never stored.
- Scans are capped by page/time to avoid abuse.
- This is not a penetration tool; basic form login only.
- SSO/MFA not supported by default.
""")

# ------------------ Selector Profile Support ------------------
def load_selector_profile(file_bytes: bytes) -> Dict[str, Any]:
    try:
        prof = json.loads(file_bytes.decode("utf-8"))
        # normalize keys
        keys = {k.lower(): k for k in prof.keys()}
        def get(k): return prof.get(keys.get(k, k))
        return {
            "login_url": get("login_url") or "",
            "username_sel": get("username") or get("username_sel") or "",
            "password_sel": get("password") or get("password_sel") or "",
            "submit_sel": get("submit") or get("submit_sel") or "",
            "post_login_indicator": get("post_login_indicator") or get("post_login_selector") or "",
        }
    except Exception as e:
        st.error(f"Invalid selector profile JSON: {e}")
        return {}

# ------------------ Helpers ------------------
def normalize_link(current_url, href):
    if not href: return None
    href = href.strip()
    href, _ = urldefrag(href)
    return urljoin(current_url, href)

def same_origin(u1, u2):
    p1, p2 = urlparse(u1), urlparse(u2)
    def norm_port(p):
        if p.port: return p.port
        return 80 if p.scheme == "http" else 443
    return (p1.scheme, p1.hostname, norm_port(p1)) == (p2.scheme, p2.hostname, norm_port(p2))

def path_prefix(u):
    p = urlparse(u)
    return p.path if p.path.endswith("/") else (p.path.rsplit("/",1)[0] + "/")

def navigate_with_retries(page, url, wait_ms:int, retries:int=2, nav_timeout:int=15000):
    last_err = None
    for attempt in range(retries+1):
        try:
            page.set_default_navigation_timeout(nav_timeout)
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_timeout(wait_ms)
            return True, None
        except Exception as e:
            last_err = e
            time.sleep(0.5 * (attempt+1))
    return False, str(last_err)[:300]

USER_HEUR = [
    'input[name="username"]','input[id="username"]','input[name*="user"]','input[id*="user"]',
    'input[type="email"]','input[name="email"]','input[id="email"]','input[name*="mail"]','input[type="text"]'
]
PASS_HEUR = ['input[type="password"]','input[name*="pass"]','input[id*="pass"]']
SUBM_HEUR = ['button[type="submit"]','input[type="submit"]','button[name*="login"]','button[id*="login"]']

def try_login(page, start_url, username, password, user_sel=None, pass_sel=None, submit_sel=None, indicator=None, nav_timeout:int=10000):
    # go to login or start_url
    target = start_url
    ok, err = navigate_with_retries(page, target, wait_ms=400, retries=1, nav_timeout=nav_timeout)
    if not ok: return False, page.url

    if not (username and password):
        return False, page.url

    # use selectors if provided
    def safe_fill(sel, val):
        try:
            page.fill(sel, val); return True
        except Exception: return False

    filled_u = filled_p = False
    if user_sel and pass_sel:
        filled_u = safe_fill(user_sel, username)
        filled_p = safe_fill(pass_sel, password)
        if submit_sel:
            try:
                with page.expect_navigation(timeout=nav_timeout) as _:
                    page.click(submit_sel)
            except Exception:
                # fallback: press enter
                try: page.press(pass_sel, "Enter")
                except Exception: pass
    else:
        # heuristics
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
                        with page.expect_navigation(timeout=nav_timeout) as _:
                            el.click()
                        clicked = True; break
                    except Exception: pass
            if not clicked:
                try:
                    for ps in PASS_HEUR:
                        el = page.query_selector(ps)
                        if el: el.press("Enter"); break
                except Exception: pass

    # wait for indicator if provided
    if indicator:
        try:
            page.wait_for_selector(indicator, timeout=nav_timeout)
        except Exception:
            pass

    page.wait_for_timeout(800)
    return True, page.url

def clean_visible_text(page):
    BOILER = ["nav","footer","header","aside","menu"]
    for sel in BOILER:
        try:
            for el in page.query_selector_all(sel):
                page.evaluate("(n)=>n.remove()", el)
        except Exception:
            pass
    heads = []
    for tag in ["h1","h2","h3","h4","h5","h6"]:
        try:
            for el in page.query_selector_all(tag):
                t = (el.inner_text() or "").strip()
                if t: heads.append(t)
        except Exception: pass
    try:
        body = (page.inner_text("body") or "").strip()
    except Exception:
        body = ""
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
        shot = None
        text = ""
        if not ok:
            results.append({"url": cur, "status": status, "title": None, "screenshot": None, "error": err, "text": ""})
            count += 1
            continue
        try:
            title = page.title()
            text = clean_visible_text(page)[:200000]
            if capture_screens:
                tmp = tempfile.gettempdir()
                safe = re.sub(r'[^a-zA-Z0-9_\-]+','_', urlparse(cur).path or "home")[:50] or "page"
                shot = os.path.join(tmp, f"shot_{int(time.time()*1000)}_{safe}.png")
                page.screenshot(path=shot, full_page=True)
        except Exception as e:
            pass
        results.append({"url": cur, "status": status, "title": title, "screenshot": shot, "error": None, "text": text})
        count += 1

        # enqueue links
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
    text = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "[REDACTED_EMAIL]", text)
    text = re.sub(r"(?:api|secret|token|key)[=:]\s*[A-Za-z0-9_\-]{12,}", r"\1=[REDACTED]", text, flags=re.I)
    text = re.sub(r"\b[0-9]{12,}\b", "[REDACTED_LONG_ID]", text)
    return text

def build_prompt(page_url: str, page_title: str, chunk_text: str, mode: str = "both") -> str:
    instr = f"""
You create high-value questions for modernization planning.

Page URL: {page_url}
Title: {page_title or "Untitled"}

Content snippet:
>>> {chunk_text[:4000]} <<<

Tasks:
1) DOMAIN QUESTIONS — purpose, actors, rules, data fields, KPIs, edge cases.
2) TECHNICAL QUESTIONS — UI behavior, validations, accessibility, state, performance, APIs, roles, i18n, security.

Output JSON:
{{
  "domain_questions": [{{"q":"...","answer_hint":"...","difficulty":"Easy|Medium|Hard","tags":["business","rules"]}}],
  "technical_questions": [{{"q":"...","answer_hint":"...","difficulty":"Easy|Medium|Hard","tags":["api","validation"]}}]
}}

Rules:
- 8–12 concise questions per section (skip if little signal).
- Prefer why/how/what-if questions that influence migration decisions.
"""
    if mode == "domain": instr += "\nOnly domain_questions; set technical_questions to []."
    if mode == "technical": instr += "\nOnly technical_questions; set domain_questions to []."
    return instr

def call_grok(prompt: str, api_key: str, model: str = "grok-2-latest", max_tokens: int = 1200, temperature: float = 0.3):
    url = os.getenv("XAI_API_BASE", "https://api.x.ai/v1/chat/completions")
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": model,
        "messages": [{"role":"system","content":"You are a precise analyst."},{"role":"user","content":prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    import requests
    r = requests.post(url, headers=headers, json=payload, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"Grok error {r.status_code}: {r.text[:200]}")
    data = r.json()
    text = data.get("choices",[{}])[0].get("message",{}).get("content","")
    m = re.search(r"\{[\s\S]*\}$", text.strip())
    if not m: return {"domain_questions": [], "technical_questions": []}
    try:
        return json.loads(m.group(0))
    except Exception:
        return {"domain_questions": [], "technical_questions": []}

def rough_chunk(text: str, max_chars: int = 8000, overlap: int = 800) -> List[str]:
    if len(text) <= max_chars: return [text]
    out = []; i = 0
    while i < len(text):
        out.append(text[i:i+max_chars])
        i += (max_chars - overlap)
    return out

def generate_qna_for_page(record: Dict[str, Any], api_key: str, model: str, mode: str):
    txt = redact(record.get("text",""))
    chunks = rough_chunk(txt, max_chars=8000, overlap=800)
    agg = {"domain_questions": [], "technical_questions": []}
    for ch in chunks:
        out = call_grok(build_prompt(record["url"], record.get("title",""), ch, mode), api_key=api_key, model=model)
        for k in ["domain_questions","technical_questions"]:
            if out.get(k): agg[k].extend(out[k])
        time.sleep(0.2)
    # dedup by q
    def dedup(lst):
        seen = set(); res = []
        for it in lst:
            q = (it.get("q","").strip()).lower()
            if q and q not in seen:
                seen.add(q); res.append(it)
        return res[:12]
    agg["domain_questions"] = dedup(agg["domain_questions"])
    agg["technical_questions"] = dedup(agg["technical_questions"])
    return agg

# ------------------ UI ------------------
tab_discovery, tab_qna = st.tabs(["🔎 Discovery", "🧠 Q&A Generator"])

with tab_discovery:
    st.subheader("Discovery (Crawl + Screenshots + CSV/JSON)")

    prof_file = st.file_uploader("Selector Profile (JSON)", type=["json"], help="Optional: upload a profile to prefill selectors & post-login indicator.")
    profile = {}
    if prof_file:
        profile = load_selector_profile(prof_file.read())

    with st.form("disc_form"):
        start_url = st.text_input("Start URL (login page or any page)", value=profile.get("login_url",""))
        c1, c2 = st.columns(2)
        with c1:
            username = st.text_input("Username / Email", value="")
        with c2:
            password = st.text_input("Password", type="password", value="")

        st.markdown("**Selectors (optional; profile or manual):**")
        s1, s2, s3, s4 = st.columns(4)
        with s1:
            user_sel = st.text_input("Username selector", value=profile.get("username_sel",""))
        with s2:
            pass_sel = st.text_input("Password selector", value=profile.get("password_sel",""))
        with s3:
            submit_sel = st.text_input("Submit selector", value=profile.get("submit_sel",""))
        with s4:
            post_login_indicator = st.text_input("Post-login indicator", value=profile.get("post_login_indicator",""), help="CSS selector that appears only after successful login.")

        st.markdown("**Scan Options**")
        o1, o2, o3 = st.columns(3)
        with o1:
            max_pages = st.number_input("Max pages", 1, 1000, 40, 1)
        with o2:
            wait_ms = st.number_input("Wait after nav (ms)", 0, 10000, 500, 100)
        with o3:
            nav_timeout = st.number_input("Nav timeout (ms)", 2000, 60000, 12000, 1000)
        screenshot = st.checkbox("Screenshots", value=True)
        same_path_only = st.checkbox("Restrict to same path prefix", value=False)

        consent = st.checkbox("I am authorized to scan this site.", value=False)
        run_disc = st.form_submit_button("Run Discovery")

    if run_disc:
        if not consent:
            st.error("Authorization is required.")
            st.stop()
        if not start_url or not start_url.lower().startswith(("http://","https://")):
            st.error("Provide a valid start URL.")
            st.stop()

        status = st.status("Starting headless browser…", expanded=False)
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
                context = browser.new_context()
                context.set_default_timeout(nav_timeout)

                page = context.new_page()
                status.update(label="Attempting login…", state="running")

                # Try login (works even if no creds)
                try_login(page, start_url, username, password, user_sel or None, pass_sel or None, submit_sel or None, post_login_indicator or None, nav_timeout=nav_timeout)

                seed = page.url
                status.update(label="Crawling…", state="running")
                results = crawl(context, seed, int(max_pages), int(wait_ms), bool(same_path_only), bool(screenshot))

                # Package results
                data = {
                    "start_url": start_url,
                    "crawl_seed": seed,
                    "pages_crawled": len(results),
                    "timestamp": int(time.time()),
                    "results": results,
                }
                mem_zip = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
                with zipfile.ZipFile(mem_zip.name, "w", zipfile.ZIP_DEFLATED) as zf:
                    zf.writestr("results.json", json.dumps(data, indent=2))
                    buf = io.StringIO()
                    cw = csv.writer(buf); cw.writerow(["url","status","title","screenshot","error"])
                    for r in results: cw.writerow([r["url"], r.get("status",""), r.get("title",""), r.get("screenshot",""), r.get("error","")])
                    zf.writestr("results.csv", buf.getvalue())
                    for r in results:
                        if r.get("screenshot"):
                            try: zf.write(r["screenshot"], arcname=f"screens/{os.path.basename(r['screenshot'])}")
                            except Exception: pass

                status.update(label="Done", state="complete")
                st.success(f"Discovery complete. Pages crawled: {len(results)}")
                st.download_button("⬇️ Download Discovery ZIP", data=open(mem_zip.name,"rb").read(), file_name="discovery_results.zip", mime="application/zip")

                st.session_state["last_results"] = results  # keep for Q&A tab
                try: context.close(); browser.close()
                except Exception: pass
        except Exception as e:
            status.update(label="Failed", state="error")
            st.error(f"Discovery failed: {e}")

with tab_qna:
    st.subheader("Generate Domain + Technical Q&A per Page")
    if "last_results" not in st.session_state:
        st.info("Run **Discovery** first to collect pages & text.")
    else:
        results = st.session_state["last_results"]
        with st.form("qna_form"):
            model = st.text_input("Grok model", value="grok-2-latest")
            api_key = st.text_input("xAI Grok API Key", type="password", value=os.getenv("XAI_API_KEY",""))
            mode = st.selectbox("Mode", ["both","domain","technical"])
            min_chars = st.number_input("Min text chars per page (skip below)", 0, 20000, 500, 50)
            submit_qna = st.form_submit_button("Generate Q&A")
        if submit_qna:
            if not api_key:
                st.error("API key required."); st.stop()
            st.info("Generating questions…")
            out_items = []
            prog = st.progress(0)
            for i, r in enumerate(results):
                if len(r.get("text","")) < int(min_chars):
                    out_items.append({"url": r["url"], "title": r.get("title",""), "domain_questions": [], "technical_questions": [], "skipped": True})
                    prog.progress(int((i+1)/max(1,len(results))*100)); continue
                try:
                    qna = generate_qna_for_page(r, api_key, model, mode)
                    out_items.append({"url": r["url"], "title": r.get("title",""), **qna})
                except Exception as e:
                    out_items.append({"url": r["url"], "title": r.get("title",""), "domain_questions": [], "technical_questions": [], "error": str(e)[:200]})
                prog.progress(int((i+1)/max(1,len(results))*100))

            qjson = {"generated_at": int(time.time()), "items": out_items}
            csv_buf = io.StringIO(); cw = csv.writer(csv_buf)
            cw.writerow(["url","title","section","question","answer_hint","difficulty","tags"])
            for item in out_items:
                for sec in ["domain_questions","technical_questions"]:
                    for q in item.get(sec, []):
                        cw.writerow([item["url"], item["title"], "domain" if sec=="domain_questions" else "technical",
                                     q.get("q",""), q.get("answer_hint",""), q.get("difficulty",""), ",".join(q.get("tags",[]))])
            qzip = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
            with zipfile.ZipFile(qzip.name, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("questions.json", json.dumps(qjson, indent=2))
                zf.writestr("questions.csv", csv_buf.getvalue())
            st.success("Q&A ready.")
            st.download_button("⬇️ Download Q&A ZIP", data=open(qzip.name,"rb").read(), file_name="page_questions.zip", mime="application/zip")
