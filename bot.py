"""Telegram bot — controls remote Windows machines over RDP.

Supports multiple saved RDP profiles (RDP1, RDP2, ...). Every command acts on
the ACTIVE profile; use /rdp <name> (or just /rdp1) to switch between them.
"""
from __future__ import annotations

import asyncio
import functools
import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs
from urllib.parse import urlparse
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer

# Logging pehle set karo — rdp_manager import karte hi patch log hona chahiye
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)

from PIL import ImageChops
from PIL import ImageOps
from telegram import InlineKeyboardButton
from telegram import InlineKeyboardMarkup
from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import commands
import automa_features
from config import load_config
from rdp_manager import RDPManager

log = logging.getLogger("rdpbot")

cfg = load_config()

PROFILES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "profiles.json")

# name -> {"host": ..., "port": ..., "user": ..., "password": ...}
profiles: dict[str, dict] = {}
# The connection target every command acts on
current: dict = {"name": None, "host": "", "port": 3389, "user": "", "password": ""}
# One live RDP connection per profile
managers: dict[str, RDPManager] = {}
# chat_id -> {"step": "ip"|"user"|"pass", "host": ..., "port": ...}
connect_sessions: dict[int, dict] = {}
# chat_id -> [(title, url), ...] last search results
search_sessions: dict[int, list[tuple[str, str]]] = {}
# One-shot Instagram login state; passwords are never stored in this dict.
ig_login_sessions: dict[int, dict[str, str]] = {}

OPENED_RATIO = 0.05
FREERDP_LOCK = asyncio.Lock()

HELP_TEXT = """🤖 RDP Control Bot — multi profile

🔌 Profiles:
/add RDP1 1.2.3.4:3389 user pass — save a profile (host may include :port)
/connect RDP1                 — connect to a saved profile (always a fresh connection)
/connect                      — step by step (IP → username → password), saves as RDP1, RDP2...
/rdp RDP1   /rdp1   switch1   — switch the active profile
/list                         — all profiles (active + connected)
/remove RDP1                  — delete a profile
/disconnect                   — close the active connection
/status                       — active profile + connection status
/keepalive                    — send one small pointer nudge now; automatic idle keepalive is already on
/cancel                       — cancel the connect flow
/screen                       — photo of the active desktop
/fix                          — black screen fix (disconnect + reconnect)
/ui /keyboard /mouse           — virtual keyboard + mouse (web UI)

⌨️ Commands (they act on the ACTIVE profile):
/win chrome — Start menu search (auto fallback to Win+R on RemoteApp servers like 103.76.214.2)
/run calc — direct Win+R -> type app (fastest on RemoteApp: calc, notepad, mspaint, TallyPrime)
/ctrl hello — Ctrl+A then type text into the focused app
/edge                         — open or focus Microsoft Edge
/minimize                     — minimize the focused RDP window
/maximize                     — maximize the focused RDP window
/close                        — close visible app windows until desktop; keep RDP connected
/split                        — open Edge Split Screen for the current/next tab
/open chrome — open any app (notepad, cmd, powershell, calc, mspaint...)
/open instagram — open a website
/search wallpaper 4k — search + numbered list (1,2,3...) → reply 1 to open
/search instagram — shows 1) insta 2) etc → tap 1 to open in RDP Chrome
/goto instagram.com — open a URL directly
/instagram — restore/maximize the existing browser and open Instagram; then choose login or Messages.
/igdm — show the Open Messages button (use when Instagram is loaded and signed in).
Instagram messaging: Open Messages → /fetchgc → select chats → /done; review/confirm before any workflow run.
/fetchgc — show up to five recent Instagram inbox chats for selection.
/done — finalize chosen groups and prepare one browser tab per group.
/workflow — show the first five saved Automa workflows to select.
/setautoma — request and import a new workflow JSON in this private chat.
/start — run the selected/imported workflow in the selected tab(s).
/stop — stop the active workflow on selected tabs.
After /connect — tap 📸 Open Instagram, then follow the private login prompts; DMs open after OTP.
/install adblock — open the Chrome Web Store
/cmd ipconfig — run a command in PowerShell
/type hello — type text (unicode works too)
/key ctrl+shift+esc — shortcut (win+r, alt+f4, win+l...)
/click 640 360 — mouse click (right/double also supported)
/scroll down 3
/seq win+r; type chrome; press enter; wait 2; type instagram.com; press enter

Plain examples: "open chrome", "search instagram", "win chrome", "edge", "rdp2", "switch1"
"""


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------

def load_profiles() -> dict[str, dict]:
    try:
        with open(PROFILES_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_profiles() -> None:
    try:
        with open(PROFILES_FILE, "w", encoding="utf-8") as f:
            json.dump(profiles, f, indent=2)
    except Exception as e:
        log.warning("could not save profiles: %s", e)


def next_profile_name() -> str:
    i = 1
    while f"rdp{i}" in profiles:
        i += 1
    return f"rdp{i}"


def profile_cfg(name: str) -> dict:
    p = profiles.get(name, {})
    c = dict(cfg)
    c["rdp_host"] = p.get("host", "")
    c["rdp_port"] = int(p.get("port", 3389))
    c["rdp_user"] = p.get("user", "")
    c["rdp_pass"] = p.get("password", "")
    return c


def mgr_key(name: str | None = None) -> str:
    return (name or current["name"] or "adhoc").lower()


def get_mgr(name: str | None = None) -> RDPManager:
    """Return (creating if needed) the RDP manager for a profile."""
    key = mgr_key(name)
    mgr = managers.get(key)
    if mgr is None:
        if key == "adhoc" and not current["host"]:
            raise ConnectionError("No RDP selected — use /add or /connect first")
        mgr = RDPManager(profile_cfg(key) if key != "adhoc" else _adhoc_cfg())
        managers[key] = mgr
    return mgr


def _adhoc_cfg() -> dict:
    c = dict(cfg)
    c["rdp_host"] = current["host"]
    c["rdp_port"] = current["port"]
    c["rdp_user"] = current["user"]
    c["rdp_pass"] = current["password"]
    return c


def split_host_port(text: str, default_port: int = 3389) -> tuple[str, int]:
    """'1.2.3.4:65430' -> ('1.2.3.4', 65430); '1.2.3.4' -> ('1.2.3.4', 3389)"""
    text = text.strip()
    if text.startswith("["):  # IPv6: [2001:db8::1]:3389
        host, _, rest = text.partition("]")
        port_s = rest.lstrip(":") or str(default_port)
        return host.lstrip("["), int(port_s)
    if ":" in text:
        host, _, port_s = text.rpartition(":")
        try:
            return host.strip(), int(port_s)
        except ValueError:
            pass
    return text, default_port


_HOST_RE = re.compile(r"^[A-Za-z0-9._\[\]:-]+$")


def validate_host(text: str) -> str | None:
    """IP/hostname sahi hai? Garbage (prompt copy, emoji, spaces) reject karo."""
    t = (text or "").strip()
    if not t or len(t) > 253:
        return None
    if not _HOST_RE.match(t):  # space/newline/emoji/bhasha -> reject
        return None
    # kam se kam ek alphanumeric character chahiye
    if not re.search(r"[A-Za-z0-9]", t):
        return None
    return t


def diff_ratio(img_a, img_b) -> float:
    """Fraction of pixels that differ by more than 40 (grayscale)."""
    a = ImageOps.grayscale(img_a)
    b = ImageOps.grayscale(img_b)
    if a.size != b.size:
        b = b.resize(a.size)
    hist = ImageChops.difference(a, b).histogram()
    total = a.size[0] * a.size[1]
    return sum(hist[40:]) / total if total else 0.0


def mean_brightness(img) -> float:
    """0..255 — 0 ka matlab screen bilkul black (diagnostic ke liye)."""
    hist = ImageOps.grayscale(img).histogram()
    total = sum(hist)
    if not total:
        return 0.0
    return sum(i * c for i, c in enumerate(hist)) / total


def fetch_search_results(query: str, count: int = 5) -> list[tuple[str, str]]:
    """DuckDuckGo HTML se top results — 1,2,3 selection ke liye."""
    import html as htmlmod
    import re
    import urllib.parse
    import urllib.request
    try:
        q = urllib.parse.quote(query)
        url = f"https://html.duckduckgo.com/html/?q={q}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        data = urllib.request.urlopen(req, timeout=8).read().decode("utf-8", errors="ignore")
        # DuckDuckGo result__url and result__title
        titles = re.findall(r'class="result__title".*?<a[^>]*>(.*?)</a>', data, re.S)
        hrefs = re.findall(r'class="result__url"[^>]*href="([^"]+)"', data)
        # fallback hrefs from result__a
        if not hrefs:
            hrefs = re.findall(r'class="result__a"[^>]*href="([^"]+)"', data)
        results = []
        for i in range(min(len(titles), len(hrefs), count)):
            title = re.sub(r"<.*?>", "", titles[i]).strip()
            title = htmlmod.unescape(title) or query
            href = hrefs[i].strip()
            # DuckDuckGo wraps with /l/?kh=...&uddg=https://...
            if "uddg=" in href:
                try:
                    href = urllib.parse.unquote(href.split("uddg=")[1].split("&")[0])
                except: pass
            if href.startswith("//"):
                href = "https:" + href
            if not href.startswith("http"):
                href = "https://" + href
            results.append((title[:70], href))
        if results:
            return results
    except Exception as e:
        log.warning("search fetch fail %r: %s", query, e)
    # Known sites direct — instagram etc ke liye seedha link
    low = query.lower().strip()
    try:
        from commands import WEB_ALIASES
        if low in WEB_ALIASES:
            import urllib.parse as up2
            q = up2.quote(query)
            return [
                (low.title(), WEB_ALIASES[low]),
                (f"Google: {query}", f"https://www.google.com/search?q={q}"),
                (f"Bing: {query}", f"https://www.bing.com/search?q={q}"),
            ]
    except: pass
    # fallback: direct google search url + bing
    import urllib.parse as up
    q = up.quote(query)
    return [
        (f"Google: {query}", f"https://www.google.com/search?q={q}"),
        (f"Bing: {query}", f"https://www.bing.com/search?q={q}"),
        (f"DuckDuckGo: {query}", f"https://duckduckgo.com/?q={q}"),
    ]


REMOTEAPP_PORTS = {65430, 8250}
REMOTEAPP_HOSTS = {"103.76.214.2", "202.140.136.146"}


def _is_remoteapp(mgr, host: str | None = None) -> bool:
    cfg = getattr(mgr, "cfg", {}) or {}
    host = host or cfg.get("rdp_host", "")
    try:
        port = int(cfg.get("rdp_port", 3389))
    except (TypeError, ValueError):
        port = 3389
    return host in REMOTEAPP_HOSTS or port in REMOTEAPP_PORTS


BROWSER_CANDIDATES = [
    "chrome",
    "chrome.exe",
    "msedge",
    "msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "iexplore",
    "firefox",
]


def _looks_like_edge_window(image) -> bool:
    """Check for browser tab/address-bar chrome, not just a bright page."""
    width, height = image.size
    band = image.convert("RGB").crop((int(width*.06), int(height*.04),
                                      int(width*.80), int(height*.12)))
    pixels = list(band.get_flattened_data())
    if not pixels:
        return False
    # Chrome and Edge both expose dark, low-saturation text/icons here. The
    # Automa dashboard canvas is mostly white and is not sufficient evidence.
    chrome_ink = sum(max(rgb) < 175 and max(rgb)-min(rgb) < 45 for rgb in pixels)
    return chrome_ink / len(pixels) > 0.012


async def _maximize_foreground_window(mgr) -> None:
    """Restore/maximize the foreground app using Windows' system menu."""
    await mgr.execute([("key", "alt+space"), ("wait", 0.4), ("key", "x"),
                       ("wait", 0.8), ("key", "escape"), ("wait", 0.2)])


async def _focus_edge_window(mgr) -> bool:
    """Focus/restore a browser without toggling an already-active taskbar window.

    First inspect the current screen: clicking an active taskbar button minimizes
    its window, which caused the old helper to intermittently leave Edge hidden.
    If it is not already foregrounded, try Alt+Tab before the pinned taskbar
    slot (which may toggle/minimize if it is already selected). Every path is
    screenshot-verified.
    """
    width, height = mgr.client.width, mgr.client.height
    initial = await mgr.screenshot_nowait()
    if _looks_like_edge_window(initial):
        await _maximize_foreground_window(mgr)
        return _looks_like_edge_window(await mgr.screenshot_nowait())

    attempts = [
        [("key", "alt+tab"), ("wait", 0.7)],
        [("key", "alt+tab"), ("wait", 0.7)],
    ]
    # RemoteApp sessions do not expose the normal Windows taskbar layout, so
    # never click the old desktop's pinned-browser coordinates there.
    if not _is_remoteapp(mgr):
        x, y = int(width * 0.341), int(height * 0.974)
        attempts.append([("click", x, y, 1, False), ("wait", 1.0)])
    for actions in attempts:
        await mgr.execute(actions)
        await _maximize_foreground_window(mgr)
        if _looks_like_edge_window(await mgr.screenshot_nowait()):
            return True
    return False


async def _navigate_browser_addressbar(mgr, url: str) -> None:
    """Navigate the foreground browser without layout-specific coordinate clicks."""
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    # Ctrl+L targets the browser address bar across Chrome/Edge layouts and
    # resolutions; a fixed click near the old toolbar/search field is brittle.
    await mgr.execute([
        ("key", "escape"), ("wait", 0.2),
        ("key", "ctrl+l"), ("key", "ctrl+a"), ("key", "backspace"),
    ])
    for char in url:
        await mgr.execute([("text", char)])
        await asyncio.sleep(0.12)
    await mgr.execute([("key", "enter"), ("wait", 5.0)])


async def _open_url_for_profile(mgr, url: str, host: str | None = None) -> str | None:
    """Open a URL in the existing browser profile, restoring/maximizing first."""
    host = host or current.get("host", "")
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url

    # Reuse the already-open browser first so its existing login/profile is kept.
    # Do not launch another browser window unless focus/restore could not be verified.
    focused = await _focus_edge_window(mgr)
    if not focused:
        if _is_remoteapp(mgr, host):
            opened = await _open_browser_via_run(mgr, None)  # Chrome is installed on RemoteApp.
        else:
            opened = await _win_via_freerdp(mgr, "msedge")
            if not opened or "Not found" in opened:
                opened = await _open_browser_via_run(mgr, None)
        if not opened or "Not found" in opened:
            return opened or "Not found: browser"
        await asyncio.sleep(2.0)
        await _maximize_foreground_window(mgr)
        if not _looks_like_edge_window(await mgr.screenshot_nowait()):
            return "Browser launch reported, but foreground browser could not be verified"

    # _focus_edge_window already restored, maximized, and verified the foreground
    # browser. Verify browser chrome remains on-screen after navigation; this is
    # not OCR/DOM verification of Instagram content or the account's login state.
    await _navigate_browser_addressbar(mgr, url)
    if not _looks_like_edge_window(await mgr.screenshot_nowait()):
        raise RuntimeError("Navigation was sent, but browser chrome was not visible afterward; check /screen")
    return f"Navigation sent to {url}; maximized browser remained visible"


async def _open_browser_via_run(mgr, url: str | None = None) -> str | None:
    """Tally RemoteApp pe chrome kholne ka robust tareeka — multiple candidates try."""
    # Tally host is a RemoteApp shell: Win+R/Start keys can leak a stray "r"
    # into an already-open task dialog. Chrome is confirmed installed there, so
    # run exactly one command through the mouse-driven Task Manager path.
    if _is_remoteapp(mgr):
        app = "chrome" if not url else f"chrome {url}"
        log.info("RemoteApp browser launch: %r", app)
        return await _win_via_freerdp(mgr, app)
    # Other hosts: retain candidate fallback.
    for cand in BROWSER_CANDIDATES:
        app = cand if url is None else f'{cand} {url}' if "://" not in cand else cand
        if url and " " in cand:
            app = f'"{cand}" {url}'
        log.info("try browser candidate %r", app)
        res = await _win_via_freerdp(mgr, app)
        if res and "Opened" in res:
            return res
        await asyncio.sleep(0.8)
    return None


UI_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>RDP Virtual Control</title>
<style>
*{box-sizing:border-box;font-family:system-ui,Segoe UI,Roboto,Arial,sans-serif}
body{margin:0;background:#0f1115;color:#e6e6e6}
header{padding:12px 16px;background:#161a22;border-bottom:1px solid #222;display:flex;flex-wrap:wrap;gap:8px;align-items:center}
header h1{margin:0;font-size:16px;flex:1}
select,button,input{border-radius:8px;border:1px solid #333;background:#1e2330;color:#e6e6e6;padding:8px 10px;font-size:13px}
button{cursor:pointer;background:#2a56ff;border-color:#2a56ff}
button:active{opacity:0.8}
button.secondary{background:#1e2330}
button.small{padding:6px 8px;font-size:12px}
.layout{max-width:1100px;margin:auto;padding:12px;display:grid;gap:12px}
.card{background:#161a22;border:1px solid #232836;border-radius:12px;padding:12px}
.card h3{margin:0 0 8px 0;font-size:13px;opacity:0.9}
.screen-wrap{position:relative;background:#000;border-radius:10px;overflow:hidden;border:1px solid #333;touch-action:none}
.screen-wrap img{width:100%;height:auto;display:block}
#pad{height:170px;background:radial-gradient(circle at 30% 30%,#1a2035,#0f1115);border:1px dashed #333;border-radius:10px;display:flex;align-items:center;justify-content:center;user-select:none;touch-action:none;position:relative;overflow:hidden}
#pad::after{content:"◉ drag / tap = move + click";position:absolute;bottom:6px;font-size:11px;opacity:0.6}
.kbd{display:grid;gap:4px}
.kbd .row{display:flex;gap:4px;flex-wrap:wrap}
.kbd button{flex:1;min-width:28px;padding:10px 4px;font-size:13px}
.kbd button.extra{background:#212837}
.row-actions{display:flex;gap:6px;flex-wrap:wrap}
input[type=text]{flex:1;min-width:160px}
.dot{width:8px;height:8px;border-radius:50%;background:#ff3b30;display:inline-block}
.dot.ok{background:#30d158}
.help{font-size:12px;opacity:0.7;line-height:1.4}
</style></head><body>
<header><h1>🖥️ RDP Virtual — <span id="profLabel">-</span> <span id="dot" class="dot"></span></h1>
<select id="profile"></select>
<button id="btnScreen" class="secondary">📸 Refresh</button>
<button id="btnFix" class="secondary">🛠️ Fix Black</button>
<a href="/health" target="_blank" style="font-size:12px;opacity:0.6;color:#8ab4ff;text-decoration:none">health</a>
</header>
<div class="layout">
<div class="card">
<h3>Screen (tap to click, drag to move)</h3>
<div class="screen-wrap" id="screenWrap"><img id="screen" alt="screen"><div id="loading" style="position:absolute;inset:0;display:none;align-items:center;justify-content:center;background:rgba(0,0,0,0.6)">Loading…</div></div>
<div class="row-actions" style="margin-top:8px">
<button class="secondary small" onclick="sendClick('left')">Left Click</button>
<button class="secondary small" onclick="sendClick('right')">Right Click</button>
<button class="secondary small" onclick="sendClick('double')">Double</button>
<button class="secondary small" onclick="doScroll('up')">Scroll ↑</button>
<button class="secondary small" onclick="doScroll('down')">Scroll ↓</button>
<span id="pos" style="font-size:11px;opacity:0.6;margin-left:6px"></span>
</div>
</div>
<div class="card">
<h3>Mouse Pad (trackpad jaisa)</h3>
<div id="pad"></div>
<div class="row-actions" style="margin-top:8px">
<button class="secondary small" onclick="sendKey('escape')">Esc</button>
<button class="secondary small" onclick="sendKey('alt+f4')">Alt+F4</button>
<button class="secondary small" onclick="sendKey('win+r')">Win+R</button>
<button class="secondary small" onclick="sendKey('ctrl+esc')">Ctrl+Esc</button>
<button class="secondary small" onclick="sendKey('alt+tab')">Alt+Tab</button>
</div>
</div>
<div class="card">
<h3>Virtual Keyboard + Type</h3>
<div style="display:flex;gap:6px;margin-bottom:8px">
<input id="typeBox" type="text" placeholder="Yahan type karo">
<button onclick="sendType()">Type ↵</button>
<button class="secondary" onclick="sendKey('ctrl+a')">Ctrl+A</button>
<button class="secondary" onclick="sendKey('backspace')">⌫</button>
<button class="secondary" onclick="sendKey('enter')">Enter</button>
</div>
<div class="kbd" id="kbd"></div>
<div class="row-actions" style="margin-top:8px">
<button class="secondary small" onclick="quickRun('calc')">Run calc</button>
<button class="secondary small" onclick="quickRun('notepad')">Run notepad</button>
<button class="secondary small" onclick="quickRun('mspaint')">Run mspaint</button>
<button class="secondary small" onclick="quickRun('explorer')">Explorer</button>
</div>
<p class="help">RemoteApp (103.76…) pe Run calc/notepad hi kaam karega — Chrome install nahi hai. Black screen aaye to Fix Black dabao.</p>
</div>
</div>
<script>
const $=s=>document.querySelector(s);
const profileSel=$("#profile"), profLabel=$("#profLabel"), dot=$("#dot"), screen=$("#screen"), wrap=$("#screenWrap"), loading=$("#loading"), pos=$("#pos"), pad=$("#pad"), typeBox=$("#typeBox");
let prof=""; let w=1366,h=768;
async function api(path, opts){ const r=await fetch(path, opts); const t=await r.text(); try{return JSON.parse(t)}catch{return t} }
async function loadProfiles(){ const j=await api("/api/status"); const sel=j.profiles||[]; const cur=j.current||{}; profileSel.innerHTML=sel.map(p=>`<option value="${p.name}">${p.name} — ${p.host}:${p.port} ${p.connected?"🟢":"⚪"}</option>`).join("")||`<option value="">(no profile)</option>`; if(cur.name) profileSel.value=cur.name; prof=profileSel.value||cur.name||sel[0]?.name||""; profLabel.textContent=prof||"-"; dot.className="dot "+(sel.find(x=>x.name===prof)?.connected?"ok":""); w=cur.width||1366; h=cur.height||768; if(prof) refresh(); }
profileSel.onchange=()=>{ prof=profileSel.value; profLabel.textContent=prof; refresh(); };
async function refresh(){ if(!prof) return; loading.style.display="flex"; try{ const r=await fetch(`/api/screen?profile=${encodeURIComponent(prof)}&t=${Date.now()}`); if(r.ok){ const b=await r.blob(); screen.src=URL.createObjectURL(b); } }catch(e){} loading.style.display="none"; }
$("#btnScreen").onclick=refresh; $("#btnFix").onclick=async()=>{ if(!prof) return; $("#btnFix").textContent="Fixing…"; await api("/api/fix?profile="+encodeURIComponent(prof),{method:"POST"}); $("#btnFix").textContent="🛠️ Fix Black"; setTimeout(refresh,1200); };
async function exec(actions){ if(!prof) return alert("profile select karo"); await api("/api/exec",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({profile:prof,actions})}); setTimeout(refresh,600); }
function sendKey(combo){ exec([{key:combo}]); }
function sendType(){ const v=typeBox.value; if(!v) return; exec([{text:v}]); typeBox.value=""; }
typeBox.addEventListener("keydown",e=>{ if(e.key==="Enter"){ e.preventDefault(); sendType(); }});
function sendClick(which){ exec([{move:[Math.floor(w/2),Math.floor(h/2)]},{click:which}]); }
function doScroll(dir){ exec([{scroll:[dir,3]}]); }
function quickRun(app){ exec([{key:"win+r"},{wait:0.8},{text:app},{key:"enter"}]); }
wrap.addEventListener("click", e=>{
  const imgRect=screen.getBoundingClientRect();
  const x = Math.round((e.clientX - imgRect.left) / imgRect.width * w);
  const y = Math.round((e.clientY - imgRect.top) / imgRect.height * h);
  pos.textContent = `click ${x},${y}`;
  exec([{move:[x,y]},{click:"left"}]);
});
let dragging=false, lastX=0,lastY=0, curX=w/2, curY=h/2;
function padPos(e){ const r=pad.getBoundingClientRect(); const t=e.touches?e.touches[0]:e; return {x: t.clientX-r.left, y: t.clientY-r.top}; }
pad.addEventListener("mousedown", e=>{ dragging=true; const p=padPos(e); lastX=p.x; lastY=p.y; e.preventDefault(); });
pad.addEventListener("touchstart", e=>{ dragging=true; const p=padPos(e); lastX=p.x; lastY=p.y; e.preventDefault(); }, {passive:false});
window.addEventListener("mouseup", ()=>dragging=false);
window.addEventListener("touchend", ()=>dragging=false);
pad.addEventListener("mousemove", e=>{ if(!dragging) return; const p=padPos(e); const dx=p.x-lastX, dy=p.y-lastY; lastX=p.x; lastY=p.y; curX=Math.max(0,Math.min(w,curX+dx*(w/pad.clientWidth)*1.8)); curY=Math.max(0,Math.min(h,curY+dy*(h/pad.clientHeight)*1.8)); exec([{move:[Math.round(curX),Math.round(curY)}]); pos.textContent=`pad ${Math.round(curX)},${Math.round(curY)}`; });
pad.addEventListener("touchmove", e=>{ if(!dragging) return; const p=padPos(e); const dx=p.x-lastX, dy=p.y-lastY; lastX=p.x; lastY=p.y; curX=Math.max(0,Math.min(w,curX+dx*(w/pad.clientWidth)*1.8)); curY=Math.max(0,Math.min(h,curY+dy*(h/pad.clientHeight)*1.8)); exec([{move:[Math.round(curX),Math.round(curY)}]); e.preventDefault(); }, {passive:false});
const rows=[ ["q","w","e","r","t","y","u","i","o","p"], ["a","s","d","f","g","h","j","k","l"], ["z","x","c","v","b","n","m"], ["1","2","3","4","5","6","7","8","9","0"] ];
const kbd=$("#kbd");
rows.forEach(r=>{
  const div=document.createElement("div"); div.className="row";
  r.forEach(k=>{ const b=document.createElement("button"); b.textContent=k; b.onclick=()=>exec([{text:k}]); div.appendChild(b); });
  kbd.appendChild(div);
});
const special=["Escape","Tab","Backspace","Enter","Space","Shift","Ctrl","Alt","Win","ArrowUp","ArrowDown","ArrowLeft","ArrowRight","F1","F2","F5","Delete"];
const div=document.createElement("div"); div.className="row";
special.forEach(k=>{ const b=document.createElement("button"); b.textContent=k; b.className="extra"; b.style.fontSize="11px"; b.onclick=()=>{ const map={Space:"space",Escape:"escape",Delete:"delete"}; sendKey(map[k]||k.toLowerCase()); }; div.appendChild(b); });
kbd.appendChild(div);
loadProfiles(); setInterval(()=>{ if(document.visibilityState==="visible") loadProfiles(); }, 8000);
</script></body></html>
"""

def start_health_server() -> None:
    port = int(os.environ.get("PORT", "10000"))
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                parsed = urlparse(self.path)
                path = parsed.path
                qs = parse_qs(parsed.query)
                if path.startswith("/automa-file/"):
                    token = path.rsplit("/", 1)[-1]
                    transfer = automa_features.get_download(token)
                    if not transfer:
                        self.send_response(404); self.send_header("Content-Type", "text/plain"); self.end_headers(); self.wfile.write(b"expired or missing"); return
                    entry, filename = transfer
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(entry.content)))
                    self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers(); self.wfile.write(entry.content); return
                if path in ("/health", "/ok"):
                    self.send_response(200); self.send_header("Content-Type","text/plain"); self.end_headers(); self.wfile.write(b"ok"); return
                if path == "/ui" or path == "/":
                    if path == "/" and "uptimerobot" in self.headers.get("User-Agent","").lower():
                        self.send_response(200); self.send_header("Content-Type","text/plain"); self.end_headers(); self.wfile.write(b"ok"); return
                    self.send_response(200); self.send_header("Content-Type","text/html; charset=utf-8"); self.end_headers(); self.wfile.write(UI_HTML.encode()); return
                if path == "/api/status":
                    data = {"profiles": [{"name":k,"host":v.get("host"),"port":v.get("port"),"user":v.get("user"),"connected": (managers.get(k) and managers[k].is_connected)} for k,v in profiles.items()], "current": {"name": current.get("name"), "host": current.get("host"), "port": current.get("port"), "width": cfg.get("rdp_width",1366), "height": cfg.get("rdp_height",768)}}
                    self.send_response(200); self.send_header("Content-Type","application/json"); self.send_header("Access-Control-Allow-Origin","*"); self.end_headers(); self.wfile.write(json.dumps(data).encode()); return
                if path == "/api/screen":
                    prof = (qs.get("profile") or [None])[0]
                    key = (prof or current.get("name") or "").lower()
                    mgr = managers.get(key)
                    if not mgr or not mgr.is_connected:
                        try: mgr = get_mgr(prof) if prof else get_mgr()
                        except Exception as e: self.send_response(404); self.send_header("Content-Type","application/json"); self.end_headers(); self.wfile.write(json.dumps({"error":str(e)}).encode()); return
                    try:
                        loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
                        img = loop.run_until_complete(mgr.screenshot())
                        buf = io.BytesIO(); img.save(buf, format="PNG"); data = buf.getvalue()
                        self.send_response(200); self.send_header("Content-Type","image/png"); self.send_header("Cache-Control","no-store"); self.end_headers(); self.wfile.write(data)
                        loop.close()
                    except Exception as e: self.send_response(500); self.send_header("Content-Type","application/json"); self.end_headers(); self.wfile.write(json.dumps({"error":str(e)}).encode())
                    return
                if path == "/api/tools":
                    import shutil, glob
                    data = {
                        "xvfb": shutil.which("Xvfb"),
                        "xfreerdp3": shutil.which("xfreerdp3"),
                        "xfreerdp": shutil.which("xfreerdp"),
                        "xdotool": shutil.which("xdotool"),
                        "import": shutil.which("import"),
                        "freerdp_bin": _freerdp_bin(),
                        "glob": glob.glob("/usr/bin/*freerdp*") + glob.glob("/usr/bin/*xvfb*") + glob.glob("/usr/bin/xdotool*"),
                        "path": os.environ.get("PATH"),
                    }
                    self.send_response(200); self.send_header("Content-Type","application/json"); self.send_header("Access-Control-Allow-Origin","*"); self.end_headers(); self.wfile.write(json.dumps(data).encode()); return
                if path == "/api/run":
                    prof = (qs.get("profile") or [None])[0]
                    appname = (qs.get("app") or ["calc"])[0]
                    async def _run():
                        m = get_mgr(prof) if prof else get_mgr()
                        return await _win_via_freerdp(m, appname)
                    try:
                        loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
                        res = loop.run_until_complete(_run()); loop.close()
                        self.send_response(200); self.send_header("Content-Type","application/json"); self.send_header("Access-Control-Allow-Origin","*"); self.end_headers(); self.wfile.write(json.dumps({"result":res}).encode())
                    except Exception as e: self.send_response(500); self.send_header("Content-Type","application/json"); self.end_headers(); self.wfile.write(json.dumps({"error":str(e)}).encode())
                    return
                self.send_response(200); self.send_header("Content-Type","text/plain"); self.end_headers(); self.wfile.write(b"ok")
            except Exception as e:
                try: self.send_response(500); self.send_header("Content-Type","text/plain"); self.end_headers(); self.wfile.write(str(e).encode())
                except: pass
        def do_POST(self):
            try:
                parsed = urlparse(self.path)
                path = parsed.path
                length = int(self.headers.get("Content-Length",0))
                body = self.rfile.read(length) if length else b""
                if path == "/api/exec":
                    try: data = json.loads(body.decode() or "{}")
                    except: data = {}
                    prof = data.get("profile")
                    actions = data.get("actions") or []
                    norm = []
                    for a in actions:
                        if "key" in a: norm.append(("key", a["key"]))
                        elif "text" in a: norm.append(("text", a["text"]))
                        elif "move" in a: norm.append(("move", tuple(a["move"])))
                        elif "click" in a:
                            c = a["click"]
                            if c=="right": norm.append(("click","right"))
                            elif c=="double": norm.append(("click","double"))
                            else: norm.append(("click","left"))
                        elif "scroll" in a: norm.append(("scroll", tuple(a["scroll"])))
                        elif "wait" in a: norm.append(("wait", float(a["wait"])))
                    async def _exec():
                        m = get_mgr(prof) if prof else get_mgr()
                        await m.execute(norm)
                        return True
                    loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
                    loop.run_until_complete(_exec()); loop.close()
                    self.send_response(200); self.send_header("Content-Type","application/json"); self.send_header("Access-Control-Allow-Origin","*"); self.end_headers(); self.wfile.write(b'{"ok":true}')
                    return
                if path == "/api/fix":
                    qs = parse_qs(parsed.query)
                    prof = (qs.get("profile") or [None])[0]
                    async def _fix():
                        key = (prof or current.get("name") or "").lower()
                        m = managers.get(key)
                        if m:
                            try: await m.disconnect()
                            except: pass
                            await asyncio.sleep(1.0)
                            await m.connect()
                            await asyncio.sleep(1.0)
                            return True
                        return False
                    loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
                    loop.run_until_complete(_fix()); loop.close()
                    self.send_response(200); self.send_header("Content-Type","application/json"); self.send_header("Access-Control-Allow-Origin","*"); self.end_headers(); self.wfile.write(b'{"ok":true}')
                    return
                self.send_response(404); self.end_headers()
            except Exception as e:
                try: self.send_response(500); self.send_header("Content-Type","text/plain"); self.end_headers(); self.wfile.write(str(e).encode())
                except: pass
        def do_OPTIONS(self):
            self.send_response(200); self.send_header("Access-Control-Allow-Origin","*"); self.send_header("Access-Control-Allow-Methods","GET, POST, OPTIONS"); self.send_header("Access-Control-Allow-Headers","Content-Type"); self.end_headers()
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("Health+UI server listening on :%s ( /ui virtual keyboard/mouse )", port)



# Load saved profiles at startup
profiles = load_profiles()


def guarded(handler):
    """Only ALLOWED_USERS may use the bot (empty = allow everyone, with a warning)."""

    @functools.wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id if update.effective_user else 0
        uname = update.effective_user.username if update.effective_user else None
        log.info(
            "Message from user id=%s username=%s chat=%s",
            uid,
            uname,
            update.effective_chat.id if update.effective_chat else None,
        )
        if not cfg["allowed_users"]:
            log.warning("ALLOWED_USERS is not set — everyone is allowed!")
        elif uid not in cfg["allowed_users"]:
            if update.message:
                await update.message.reply_text("⛔ You are not authorized.")
            return
        return await handler(update, context)

    return wrapper


# ---------------------------------------------------------------------------
# Connection handlers
# ---------------------------------------------------------------------------

async def _do_connect(update: Update, name: str | None = None) -> None:
    """Hamesha FRESH connection — purani (agar hai) band karke naya khole."""
    try:
        key = mgr_key(name)
        old = managers.pop(key, None)
        if old is not None:
            try:
                await old.disconnect()
            except Exception:
                pass

        mgr = get_mgr(name)
        await mgr.connect()
        p = profiles.get(key, {})
        host = p.get("host", current["host"])
        port = p.get("port", current["port"])
        edge_opened = None
        # On ordinary Windows desktops, restore/launch the pinned Edge taskbar
        # icon. Do not run an unverified shell command: that previously left
        # Explorer in the foreground. Keep Tally RemoteApp untouched.
        if not _is_remoteapp(mgr, host):
            try:
                edge_opened = await _focus_edge_window(mgr)
            except Exception:
                log.exception("Edge auto-focus after RDP connect failed")
        open_instagram = InlineKeyboardMarkup([[
            InlineKeyboardButton("📸 Open Instagram", callback_data="igflow:open")
        ]])
        edge_status = (
            "Edge focused/open kiya."
            if edge_opened is True else
            "Tally RemoteApp ko nahi chheda; browser button se khol sakte ho."
            if _is_remoteapp(mgr, host) else
            "Edge taskbar se open/focus confirm nahi hua; /screen check karo."
        )
        await update.message.reply_text(
            f"✅ Connected: {key} — {host}:{port}\n{edge_status}\n🛡 RDP keepalive on: 2 minute idle hone par cursor 1px nudge karke wapas aayega (no click/key). /keepalive se ek nudge abhi bhejo. Instagram ke liye button dabao.",
            reply_markup=open_instagram,
        )
    except Exception as e:
        log.exception("connect failed")
        await update.message.reply_text(f"❌ Connection failed:\n{e}")


@guarded
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT)


@guarded
async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Save a profile: /add <name> <host[:port]> <user> <password>"""
    args = context.args or []
    if len(args) < 4:
        await update.message.reply_text(
            "Usage: /add <name> <host[:port]> <username> <password>\n"
            "Example: /add RDP1 1.2.3.4:3389 Administrator MyPass"
        )
        return
    name = args[0].lower()
    if validate_host(args[1]) is None:
        await update.message.reply_text(f"⚠️ Host sahi nahi lag raha: {args[1]}")
        return
    host, port = split_host_port(args[1])
    profiles[name] = {"host": host, "port": port, "user": args[2], "password": args[3]}
    save_profiles()
    await update.message.reply_text(f"💾 Saved profile: {name} — {host}:{port}\nConnect with /connect {name} or /rdp {name}")


@guarded
async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    name = (context.args or [""])[0].lower()
    if name not in profiles:
        await update.message.reply_text(f"Profile not found: {name}")
        return
    profiles.pop(name, None)
    mgr = managers.pop(name, None)
    if mgr is not None:
        try:
            await mgr.disconnect()
        except Exception:
            pass
    if current["name"] == name:
        current.update(name=None, host="", port=3389, user="", password="")
    save_profiles()
    await update.message.reply_text(f"🗑️ Removed profile: {name}")


@guarded
async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not profiles:
        await update.message.reply_text("No profiles saved yet.\nUse /add <name> <host> <user> <pass>")
        return
    lines = []
    for name in sorted(profiles):
        p = profiles[name]
        mgr = managers.get(name)
        state = "🟢 connected" if (mgr and mgr.is_connected) else "⚪ offline"
        marker = "👉 " if current["name"] == name else "   "
        lines.append(f"{marker}{name} — {p.get('host')}:{p.get('port')} ({p.get('user')}) {state}")
    await update.message.reply_text("\n".join(lines))


@guarded
async def cmd_switch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/rdp <name> — make a profile active and connect to it."""
    name = (context.args or [""])[0].lower()
    if name.isdigit():
        name = f"rdp{name}"
    if name not in profiles:
        await update.message.reply_text(
            f"Profile not found: {name}\nSaved: {', '.join(sorted(profiles)) or 'none'}\nUse /add to save one."
        )
        return
    current.update(
        name=name,
        host=profiles[name]["host"],
        port=profiles[name]["port"],
        user=profiles[name]["user"],
        password=profiles[name]["password"],
    )
    await _do_connect(update, name)


@guarded
async def cmd_connect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    chat_id = update.effective_chat.id

    # /connect <saved profile>
    if len(args) == 1 and args[0].lower() in profiles:
        await cmd_switch(update, context)
        return

    # /connect <host> <user> <pass>  — ya —  /connect <host> <port> <user> <pass>
    if len(args) >= 3:
        if len(args) >= 4 and args[1].isdigit():
            host, _ = split_host_port(args[0])
            port = int(args[1])
            user, pw = args[2], args[3]
        else:
            host, port = split_host_port(args[0])
            user, pw = args[1], args[2]
        if not validate_host(host):
            await update.message.reply_text(f"⚠️ Host sahi nahi lag raha: {args[0]}")
            return
        name = next_profile_name()
        profiles[name] = {"host": host, "port": port, "user": user, "password": pw}
        save_profiles()
        current.update(name=name, host=host, port=port, user=user, password=pw)
        connect_sessions.pop(chat_id, None)
        await _do_connect(update)
        return

    # Step-by-step flow
    session = {"step": "ip"}
    if len(args) == 1:
        host = validate_host(args[0])
        if host is None:
            await update.message.reply_text(f"⚠️ Host sahi nahi lag raha: {args[0]}")
            return
        session["host"], session["port"] = split_host_port(host)
        session["step"] = "user"
    elif len(args) == 2:
        host = validate_host(args[0])
        if host is None:
            await update.message.reply_text(f"⚠️ Host sahi nahi lag raha: {args[0]}")
            return
        session["host"], session["port"] = split_host_port(host)
        if args[1].isdigit():          # /connect 1.2.3.4 3389  -> port, ab username maango
            session["port"] = int(args[1])
            session["step"] = "user"
        else:                          # /connect 1.2.3.4 admin  -> username diya
            session["user"] = args[1]
            session["step"] = "pass"

    connect_sessions[chat_id] = session

    if session["step"] == "ip":
        await update.message.reply_text(
            "🔌 RDP connect — pehle IP address bhejo.\n"
            "• Port 3389 hai to sirf IP:  1.2.3.4\n"
            "• Alag port hai to:  1.2.3.4:65430\n"
            "• Ya dono alag-alag:  /connect 1.2.3.4 65430\n"
            "/cancel bhejo to ruk jayega."
        )
    elif session["step"] == "user":
        await update.message.reply_text(
            f"🌐 Host: {session['host']}  Port: {session['port']}\n👤 Ab username bhejo:"
        )
    else:
        await update.message.reply_text(
            f"🌐 Host: {session['host']}  👤 {session['user']}\n🔑 Ab password bhejo:"
        )


async def _handle_connect_step(update: Update, session: dict) -> None:
    chat_id = update.effective_chat.id
    text = update.message.text.strip()
    step = session["step"]

    if step == "ip":
        host = validate_host(text)
        if host is None:
            await update.message.reply_text(
                f"⚠️ Ye IP nahi lag raha: {text[:60]}\n"
                "Sirf IP/hostname bhejo — masalan 1.2.3.4 ya 1.2.3.4:65430"
            )
            return
        session["host"], session["port"] = split_host_port(host)
        session["step"] = "user"
        await update.message.reply_text(
            f"🌐 Host: {session['host']}  Port: {session['port']}\n👤 Ab username bhejo:"
        )
    elif step == "user":
        session["user"] = text
        session["step"] = "pass"
        await update.message.reply_text(
            "🔑 Now send the password:\n"
            "⚠️ The password will be saved in the chat history — delete this message after connecting."
        )
    else:
        host = session.get("host", "")
        port = session.get("port", 3389)
        name = next_profile_name()
        profiles[name] = {"host": host, "port": port, "user": session.get("user", ""), "password": text}
        save_profiles()
        current.update(name=name, host=host, port=port, user=session.get("user", ""), password=text)
        connect_sessions.pop(chat_id, None)
        await _do_connect(update)
        await update.message.reply_text(f"💾 Saved as profile: {name} — switch anytime with /rdp {name}")


@guarded
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    cancelled_ig = ig_login_sessions.pop(chat_id, None)
    cancelled_connect = connect_sessions.pop(chat_id, None)
    if cancelled_ig:
        await update.message.reply_text("❌ Instagram login flow cancelled; no Instagram password was retained.")
    elif cancelled_connect:
        await update.message.reply_text("❌ Connect flow cancelled.")
    else:
        await update.message.reply_text("No connect or Instagram login flow is running.")


@guarded
async def cmd_disconnect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await get_mgr().disconnect()
        await update.message.reply_text(f"🔌 Disconnected from {mgr_key()}.")
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")


@guarded
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not current["name"] and not current["host"]:
        await update.message.reply_text("No RDP selected. Use /connect or /rdp <name>.")
        return
    mgr = managers.get(mgr_key())
    state = "🟢 connected" if (mgr and mgr.is_connected) else "🔴 disconnected"
    keepalive = " | 🛡 keepalive active" if mgr and mgr.keepalive_active else ""
    await update.message.reply_text(
        f"👉 Active: {mgr_key()} — {current['host']}:{current['port']} ({current['user']}) {state}{keepalive}"
    )


@guarded
async def cmd_keepalive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send an immediate one-pixel cursor nudge; automatic idle nudges continue."""
    try:
        mgr = get_mgr()
        await mgr.keepalive_now()
        await update.message.reply_text(
            "🛡 RDP keepalive nudge bheja: cursor 1px gaya aur wapas aaya; click/keypress nahi hua. "
            "Connected session par automatic idle nudges bhi active hain."
        )
    except Exception as exc:
        log.exception("manual RDP keepalive failed")
        await update.message.reply_text(f"❌ RDP keepalive nahi bhej paya: {exc}")


@guarded
async def cmd_screen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        img = await get_mgr().screenshot()
        log.info(
            "screenshot %s -> %dx%d, brightness %.0f",
            mgr_key(),
            img.width,
            img.height,
            mean_brightness(img),
        )
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        await update.message.reply_photo(
            photo=buf, caption=f"🖥 {mgr_key()} — {current['host']} ({img.width}x{img.height})"
        )
    except Exception as e:
        log.exception("screenshot failed")
        await update.message.reply_text(f"❌ Screenshot failed: {e}")


# ---------------------------------------------------------------------------
# Command handlers (act on the ACTIVE profile)
# ---------------------------------------------------------------------------

def _freerdp_bin():
    import shutil
    return shutil.which("xfreerdp3") or shutil.which("xfreerdp") or shutil.which("freerdp3") or None

async def _ensure_xvfb() -> bool:
    """Xvfb :99 chal raha hai ya nahi — agar nahi to start karo (fallback ke liye)."""
    import shutil, subprocess
    if shutil.which("Xvfb") is None or _freerdp_bin() is None:
        return False
    try:
        # check if :99 already listening
        import socket
        s = socket.socket()
        s.settimeout(0.5)
        try:
            s.connect(("127.0.0.1", 6099))  # X11 port 6000+display
            s.close()
            return True
        except Exception:
            pass
        # also check via pgrep
        subprocess.Popen(
            ["Xvfb", ":99", "-screen", "0", "1366x768x24"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        await asyncio.sleep(1.5)
        return True
    except Exception as e:
        log.warning("Xvfb start fail: %s", e)
        return False


async def _win_via_freerdp(mgr, word: str) -> str | None:
    """FreeRDP + Xvfb se Run dialog khol kar app launch — Start menu blocked servers ke liye.

    Returns 'Opened: word' / 'Not found: word' / None (fallback not available).
    """
    import shutil, subprocess, os, time
    freerdp = _freerdp_bin()
    xd = shutil.which("xdotool")
    imp = shutil.which("import")
    xv = shutil.which("Xvfb")
    if freerdp is None or xd is None or imp is None:
        log.warning("FreeRDP fallback missing: freerdp=%s xdotool=%s import=%s Xvfb=%s PATH=%s", freerdp, xd, imp, xv, os.environ.get("PATH"))
        try:
            import glob
            freerdps = glob.glob("/usr/bin/*freerdp*")
            log.warning("glob /usr/bin/*freerdp*: %s", freerdps)
        except: pass
        # Fallback without FreeRDP: try simple-rdp methods (Win+R and Run app)
        # Take base screenshot first
        try:
            desktop_simple = await mgr.screenshot()
            if _is_remoteapp(mgr):
                # Recover cleanly if a prior attempt left Create new task open.
                await mgr.execute([("key", "escape"), ("wait", 0.4)])
                desktop_simple = await mgr.screenshot_nowait()
        except Exception as e:
            log.warning("fallback base screenshot fail: %s", e)
            return f"Not found: {word}"
        # Tally's RemoteApp swallows Win+R; go straight to the verified mouse-driven
        # Task Manager flow instead of typing a stray "r" into an existing dialog.
        methods = [] if _is_remoteapp(mgr) else ["win+r", "run"]
        for method in methods:
            try:
                log.info("Trying simple-rdp fallback method %s for %r", method, word)
                base = desktop_simple
                if method == "win+r":
                    # First verify Win+R actually opened a dialog. On RemoteApp hosts
                    # the shortcut can be swallowed; never type/click into the app behind it.
                    await mgr.execute([("key", "win+r"), ("wait", 1.2)])
                    run_img = await mgr.screenshot_nowait()
                    run_diff = diff_ratio(base, run_img)
                    log.info("Run dialog probe for %r -> diff %.3f", word, run_diff)
                    if run_diff < 0.025:
                        log.info("Run dialog did not appear for %r; skip typing/click", word)
                        continue
                    await mgr.execute([("key", "ctrl+a"), ("wait", 0.2), ("key", "backspace"), ("wait", 0.2)])
                    await mgr.execute([("text", word), ("wait", 0.5)])
                    typed_img = await mgr.screenshot_nowait()
                    # Try Enter, then physically click the Run dialog's OK button.
                    # Mouse clicks work on this RemoteApp host while Enter is swallowed.
                    await mgr.execute([("key", "enter"), ("wait", 2.0)])
                    after_img = await mgr.screenshot_nowait()
                    if diff_ratio(typed_img, after_img) < 0.01:
                        sw, sh = mgr.client.width, mgr.client.height
                        ok_x, ok_y = int(sw * 0.43), int(sh * 0.57)
                        log.info("Run dialog unchanged after Enter for %r; clicking OK at %d,%d (%dx%d)", word, ok_x, ok_y, sw, sh)
                        await mgr.execute([("click", ok_x, ok_y, 1, False), ("wait", 4.0)])
                        after_img = await mgr.screenshot_nowait()
                    if diff_ratio(typed_img, after_img) < 0.01:
                        log.info("Run dialog still open after button click for %r; trying Alt+O", word)
                        await mgr.execute([("key", "alt+o"), ("wait", 3.0)])
                        after_img = await mgr.screenshot_nowait()
                    if diff_ratio(typed_img, after_img) < 0.01:
                        log.info("Run dialog still open for %r; not reporting success", word)
                        continue
                else:
                    # Start -> Run, but verify each window before typing so text never
                    # leaks into the app behind a blocked Start/Run surface.
                    await mgr.execute([("key", "ctrl+esc"), ("wait", 0.8)])
                    start_img = await mgr.screenshot_nowait()
                    if diff_ratio(base, start_img) < 0.025:
                        log.info("Start menu did not appear for %r; skip Start->Run", word)
                        continue
                    await mgr.execute([("key", "ctrl+a"), ("wait", 0.2), ("key", "backspace"), ("wait", 0.2)])
                    await mgr.execute([("text", "Run"), ("wait", 0.5), ("key", "enter"), ("wait", 1.5)])
                    run_img = await mgr.screenshot_nowait()
                    if diff_ratio(start_img, run_img) < 0.01:
                        log.info("Run dialog did not appear from Start for %r; skip typing", word)
                        continue
                    await mgr.execute([("key", "ctrl+a"), ("text", word), ("wait", 0.4)])
                    typed_img = await mgr.screenshot_nowait()
                    await mgr.execute([("key", "enter"), ("wait", 2.0)])
                    after_img = await mgr.screenshot_nowait()
                    if diff_ratio(typed_img, after_img) < 0.01:
                        await mgr.execute([("click", int(600*mgr.client.width/1366), int(450*mgr.client.height/768), 1, False), ("wait", 4.0)])
                        after_img = await mgr.screenshot_nowait()
                r = diff_ratio(base, after_img)
                from PIL import ImageChops, ImageOps
                a = ImageOps.grayscale(base); b = ImageOps.grayscale(after_img)
                if a.size != b.size: b = b.resize(a.size)
                hist = ImageChops.difference(a,b).histogram()
                r10 = sum(hist[10:])/(a.size[0]*a.size[1]) if a.size[0]*a.size[1] else 0
                log.info("simple fallback %s %r -> diff %.3f thr10 %.3f", method, word, r, r10)
                if r > 0.02 or r10 > 0.03:
                    return f"Opened: {word}"
                # if not opened, try next method
            except Exception as e:
                log.warning("simple fallback %s failed: %s", method, e)
                continue
        # Try 3: Task Manager -> File -> Run new task using mouse clicks.
        # On this host Task Manager opens in compact mode (no File menu); Alt+F/N
        # silently fails. Expand it, choose File > Run new task, and click its OK button.
        try:
            log.info("Trying mouse-driven Task Manager fallback for %r", word)
            base2 = desktop_simple
            await mgr.execute([("key", "ctrl+shift+esc"), ("wait", 2.0)])
            tm = await mgr.screenshot_nowait()
            r_tm = diff_ratio(base2, tm)
            sw, sh = mgr.client.width, mgr.client.height
            sx, sy = sw / 1366.0, sh / 768.0
            if _is_remoteapp(mgr):
                # RemoteApp Task Manager may reopen in a small window at different
                # coordinates. Maximize it with the standard system-menu shortcut,
                # then use the stable File > Run new task positions.
                await mgr.execute([("key", "alt+space"), ("wait", 0.25), ("key", "x"), ("wait", 0.7),
                                   ("click", int(16*sx), int(32*sy), 1, False), ("wait", 0.35),
                                   ("click", int(68*sx), int(55*sy), 1, False), ("wait", 1.0)])
                field_x, field_y, ok_x, ok_y = int(230*sx), int(155*sy), int(160*sx), int(218*sy)
            else:
                # On ordinary Windows desktops, try the restored Task Manager menu
                # only when its title text is present at the known window origin.
                rgb = tm.convert("RGB")
                title_crop = rgb.crop((int(120*sx), int(108*sy), int(250*sx), int(132*sy)))
                title_dark = sum(1 for px in title_crop.getdata() if max(px) < 140)
                is_taskmgr = title_dark > 20
                log.info("taskmgr probe diff %.3f title_dark %d at %dx%d", r_tm, title_dark, sw, sh)
                if is_taskmgr:
                    await mgr.execute([("click", int(300*sx), int(118*sy), 1, False), ("wait", 0.3),
                                       ("click", int(128*sx), int(143*sy), 1, False), ("wait", 0.4),
                                       ("click", int(185*sx), int(167*sy), 1, False), ("wait", 1.0)])
                    field_x, field_y, ok_x, ok_y = int(340*sx), int(266*sy), int(273*sx), int(330*sy)
                else:
                    log.info("Task Manager not detected; skip mouse-driven fallback")
                    return f"Not found: {word}"
            # Let the modal settle, then type only after focusing the known Open field.
            await asyncio.sleep(0.6)
            await mgr.execute([("click", field_x, field_y, 1, False), ("wait", 0.4),
                               ("key", "ctrl+a"), ("wait", 0.3)])
            # This RemoteApp drops bulk text (often leaving only 'm'/'ch').
            # Send one character at a time with a pause so every key reaches the Run field.
            for char in word:
                # Separate execute calls matter on this RemoteApp: batching
                # chars into one action list drops keys even with wait actions.
                await mgr.execute([("text", char)])
                await asyncio.sleep(0.5)
            await asyncio.sleep(0.8)
            typed = await mgr.screenshot_nowait()
            await asyncio.sleep(0.3)
            await mgr.execute([("click", ok_x, ok_y, 1, False)])
            await asyncio.sleep(2.0)
            # RemoteApp sometimes acknowledges the click before the updated
            # desktop frame arrives. Poll for a changed screen instead of
            # treating the first stale frame as a failed launch.
            final = await mgr.screenshot_nowait()
            r_final = diff_ratio(typed, final)
            for _ in range(10):
                if r_final > 0.02:
                    break
                await asyncio.sleep(1.0)
                final = await mgr.screenshot_nowait()
                r_final = diff_ratio(typed, final)
            log.info("taskmgr Run new task click %r -> diff from typed dialog %.3f", word, r_final)
            # Keep the launched app in front; do not Alt+F4 the active window.
            if r_final > 0.02:
                from PIL import ImageOps
                bright = sum(ImageOps.grayscale(final).histogram()[10:]) / (final.size[0]*final.size[1])
                if bright > 0.05:
                    return f"Opened: {word}"
        except Exception as e:
            log.warning("mouse-driven Task Manager fallback failed: %s", e)
        return f"Not found: {word}"
    if not await _ensure_xvfb():
        return None
    cfg = mgr.cfg
    # simple-rdp base
    try:
        desktop = await mgr.screenshot()
    except Exception as e:
        log.warning("freerdp fallback base screenshot fail: %s", e)
        return None
    # disconnect simple-rdp to free the session (server allows only one)
    try:
        await mgr.disconnect()
    except Exception:
        pass
    await asyncio.sleep(1.0)
    env = os.environ.copy()
    env["DISPLAY"] = ":99"
    proc = None
    # clean stale FreeRDP instances (consecutive /win calls)
    try:
        subprocess.run(["pkill", "-x", "xfreerdp3"], timeout=3, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["pkill", "-x", "xfreerdp"], timeout=3, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        await asyncio.sleep(1.2)
    except Exception:
        pass
    try:
        # start FreeRDP with RDP security (Win+R works there) — use whichever binary exists
        freerdp_cmd = freerdp  # from above
        proc = subprocess.Popen(
            [freerdp_cmd, f"/v:{cfg['rdp_host']}:{cfg['rdp_port']}", f"/u:{cfg['rdp_user']}", f"/p:{cfg['rdp_pass']}",
             "/cert:ignore", "/sec:rdp", "/f"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
        )
        await asyncio.sleep(7)
        # find window
        wid = None
        for _ in range(8):
            try:
                out = subprocess.check_output(["xwininfo", "-root", "-tree"], env=env, text=True, timeout=3)
                for line in out.splitlines():
                    if "FreeRDP" in line:
                        wid = line.split()[0]
                        break
                if wid:
                    # ensure window is viewable (import gives >1k)
                    try:
                        subprocess.run(["import", "-window", wid, "/tmp/_fb_check.png"], env=env, timeout=5)
                        if os.path.getsize("/tmp/_fb_check.png") > 5000:
                            break
                    except Exception:
                        pass
                wid = None
            except Exception:
                pass
            await asyncio.sleep(1)
        if not wid:
            log.warning("FreeRDP window not found / not ready")
            return None
        # focus window (windowactivate may fail without WM, try windowfocus)
        try:
            subprocess.run(["xdotool", "windowfocus", "--sync", wid], env=env, timeout=5)
        except Exception:
            pass
        await asyncio.sleep(0.5)
        # Win+R
        subprocess.run(["xdotool", "key", "super+r"], env=env, timeout=5)
        await asyncio.sleep(1.5)
        # verify Run dialog appeared (import should be >20k and diff vs base)
        # click inside Run input (682,380), clear, type
        subprocess.run(["xdotool", "mousemove", "--window", wid, "682", "380"], env=env, timeout=5)
        await asyncio.sleep(0.2)
        subprocess.run(["xdotool", "click", "--window", wid, "1"], env=env, timeout=5)
        await asyncio.sleep(0.5)
        subprocess.run(["xdotool", "key", "ctrl+a"], env=env, timeout=5)
        await asyncio.sleep(0.2)
        subprocess.run(["xdotool", "key", "BackSpace"], env=env, timeout=5)
        await asyncio.sleep(0.3)
        subprocess.run(["xdotool", "type", word], env=env, timeout=5)
        await asyncio.sleep(min(10.0, max(2.0, len(word) * 0.08)))
        # Snapshot the populated Run dialog before submitting. On the Tally host,
        # Return can be swallowed; in that case physically click its OK button.
        from PIL import Image
        typed_run = None
        try:
            subprocess.run(["import", "-window", wid, "/tmp/_fb_typed.png"], env=env, timeout=5)
            if os.path.getsize("/tmp/_fb_typed.png") > 5000:
                typed_run = Image.open("/tmp/_fb_typed.png").copy()
        except Exception as e:
            log.warning("FreeRDP typed-dialog capture failed: %s", e)
        subprocess.run(["xdotool", "key", "Return"], env=env, timeout=5)
        await asyncio.sleep(2.0)
        return_img = None
        try:
            subprocess.run(["import", "-window", wid, "/tmp/_fb_return.png"], env=env, timeout=5)
            if os.path.getsize("/tmp/_fb_return.png") > 5000:
                return_img = Image.open("/tmp/_fb_return.png").copy()
        except Exception:
            pass
        if typed_run is not None and return_img is not None and diff_ratio(typed_run, return_img) < 0.01:
            # 1366x768 Run dialog OK button is centered around (647,436).
            log.info("Run dialog still unchanged after Return for %r; clicking OK", word)
            subprocess.run(["xdotool", "mousemove", "--window", wid, "647", "436"], env=env, timeout=5)
            await asyncio.sleep(0.2)
            subprocess.run(["xdotool", "click", "--window", wid, "1"], env=env, timeout=5)
        await asyncio.sleep(5.0)
        # Capture inside FreeRDP before closing — this is the reliable check
        # (simple-rdp reconnect loses the window, inside import keeps it)
        r_after = 0.0
        r_after_10 = 0.0
        try:
            from PIL import Image, ImageChops, ImageOps
            fb_after = None
            for imp_try in range(3):
                try:
                    subprocess.run(["import", "-window", wid, "/tmp/_fb_after.png"], env=env, timeout=5)
                    if os.path.getsize("/tmp/_fb_after.png") > 5000:
                        fb_after = Image.open("/tmp/_fb_after.png")
                        break
                except Exception as ie:
                    log.warning("import try %d fail: %s", imp_try+1, ie)
                    await asyncio.sleep(0.8)
            if fb_after is not None:
                # desktop is 1366x768, fb_after also 1366x768 (FreeRDP window)
                a = ImageOps.grayscale(desktop)
                b = ImageOps.grayscale(fb_after)
                if a.size != b.size:
                    b = b.resize(a.size)
                hist = ImageChops.difference(a, b).histogram()
                total = a.size[0] * a.size[1]
                r_after = sum(hist[40:]) / total if total else 0
                r_after_10 = sum(hist[10:]) / total if total else 0
                log.info("freerdp fallback %r -> inside %.3f (thr10 %.3f), brightness %.0f", word, r_after, r_after_10, mean_brightness(fb_after))
            else:
                log.warning("freerdp fallback import all tries failed")
        except Exception as e:
            log.warning("freerdp fallback import compare fail: %s", e)
        # Do NOT Alt+F4 here: that used to close the application immediately after
        # launching it, so /screen showed only the desktop. Let the app remain open;
        # terminating the RDP client below disconnects the viewer without closing it.
    except Exception as e:
        log.warning("FreeRDP fallback error: %s", e)
        return None
    finally:
        if proc:
            try:
                proc.terminate()
                await asyncio.sleep(1)
                proc.kill()
            except Exception:
                pass
            await asyncio.sleep(1.5)
    # reconnect simple-rdp
    try:
        await mgr.connect()
        await asyncio.sleep(1.0)
    except Exception as e:
        log.warning("freerdp fallback reconnect fail: %s", e)
        # Even if reconnect fails, we already have inside diff
        pass
    # Decide based on inside diff — calc 0.07, notepad 0.5 = Opened; zzz 0.005, chrome-not-installed 0.0006 = Not found
    # Whitelist for known apps with lenient thr10 check
    known = {"calc","calculator","notepad","mspaint","paint","chrome","msedge","edge","brave","firefox","explorer","cmd","powershell","code","vscode","word","excel","vlc","taskmgr","tallyprime","tally"}
    low = word.lower().strip()
    # Use inside diff: thr40 >0.02 or thr10 >0.03 = Opened
    is_open = r_after > 0.02 or r_after_10 > 0.03
    # For calibration: calc 0.070/0.075, notepad 0.503/0.506 => open; zzz 0.005/0.006, chrome 0.0006/0.0009 => not open
    if low in known and is_open:
        return f"Opened: {word}"
    if is_open:
        # Unknown app but visible change -> treat as Opened (could be error dialog? but error dialog on this RemoteApp server is invisible — experiment showed zzz no dialog, so safe)
        # To be safe, require larger diff for unknown: >0.05
        if r_after > 0.05 or r_after_10 > 0.06:
            return f"Opened: {word}"
        # small diff for unknown -> Not found
        return f"Not found: {word}"
    return f"Not found: {word}" if not is_open else f"Opened: {word}"


async def _win_flow(update: Update, word: str) -> None:
    """Start menu me type karke app kholna, phir Opened / Not found report.

    Server par Windows key kaam nahi karta (blocked), isliye Win+R ki jagah
    ctrl+esc se Start menu khulta hai aur usme app ka naam type hota hai.
    Agar Start menu blocked ho (jaise 103.76.214.2), to FreeRDP fallback se Run dialog use hota hai.
    """
    if not word:
        await update.message.reply_text("Not found: no search word given (/win <word>)")
        return
    try:
        mgr = get_mgr()
        desktop = await mgr.screenshot()               # menu se pehle
        dialog = desktop
        r_dialog = 0.0

        # Start menu kabhi-kabhi nahi khulta / turant band ho jata hai — retry.
        for attempt in range(1, 4):
            await mgr.execute(commands.a_win_search(word))
            dialog = await mgr.screenshot()
            r_dialog = diff_ratio(desktop, dialog)
            if r_dialog >= 0.01:
                break
            log.info("win %r: Start menu nahi khula (%.3f), retry %d", word, r_dialog, attempt)
            await mgr.execute([("key", "escape"), ("wait", 0.8)])
            await asyncio.sleep(0.5)

        await mgr.execute([("key", "enter"), ("wait", 2.5)])
        after = await mgr.screenshot()
        # Start menu abhi khula ho sakta hai — Escape daba kar screen saaf karo,
        # phir desktop se compare karo (chhoti window bhi detect ho).
        await mgr.execute([("key", "escape"), ("wait", 1.2)])
        closed = await mgr.screenshot()

        r_after = diff_ratio(desktop, closed)
        # also thr10 for small windows like calc (0.033 thr40 but thr10 0.05+)
        from PIL import ImageChops, ImageOps
        a10 = ImageOps.grayscale(desktop)
        b10 = ImageOps.grayscale(closed)
        if a10.size != b10.size:
            b10 = b10.resize(a10.size)
        hist10 = ImageChops.difference(a10, b10).histogram()
        r_after_10 = sum(hist10[10:]) / (a10.size[0]*a10.size[1]) if a10.size[0]*a10.size[1] else 0
        # Menu abhi bhi khula hua hai = Enter ne kuch nahi kiya.
        menu_open = diff_ratio(dialog, closed) < 0.05
        log.info(
            "win %r -> dialog %.3f, after %.3f (thr10 %.3f), menu_open %s, brightness %.0f",
            word,
            r_dialog,
            r_after,
            r_after_10,
            menu_open,
            mean_brightness(closed),
        )

        if r_dialog < 0.01:
            # Start menu blocked — Try FreeRDP fallback (Run dialog via Win+R) for this server
            log.info("Start menu blocked for %r, trying FreeRDP fallback", word)
            fb = await _win_via_freerdp(mgr, word)
            if fb is not None:
                await update.message.reply_text(fb + " (via Run dialog)")
                return
            await update.message.reply_text(
                f"⚠️ Start menu nahi khula (screen change {r_dialog:.3f}).\n"
                f"Is server par Start menu blocked hai — /screen bhej kar desktop check karo.\n"
                f"FreeRDP fallback bhi available nahi hai."
            )
            return
        # Check if Start menu path says Opened: not menu_open and diff big enough
        # Use lenient threshold for small windows: thr10 >0.04 counts as opened
        is_opened_start = (not menu_open) and (r_after >= OPENED_RATIO or r_after_10 >= 0.04)
        if is_opened_start:
            await update.message.reply_text(f"Opened: {word}")
            return
        # Normal path says Not found — but on RemoteApp servers Start menu is flaky, try Run fallback as second chance
        log.info("win %r: Not found via Start menu (after %.3f thr10 %.3f), trying Run fallback", word, r_after, r_after_10)
        fb = await _win_via_freerdp(mgr, word)
        if fb is not None:
            await update.message.reply_text(fb + " (via Run dialog)")
            return
        await update.message.reply_text(f"Not found: {word}")
    except Exception as e:
        log.exception("win search failed")
        await update.message.reply_text(f"Error: {e}")


@guarded
async def cmd_win(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _win_flow(update, " ".join(context.args or []).strip())


@guarded
async def cmd_run(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Direct Win+R -> type -> Enter (RemoteApp servers ke liye). /run calc, /run notepad"""
    word = " ".join(context.args or []).strip()
    if not word:
        await update.message.reply_text("Usage: /run <app>  (e.g. /run calc, /run notepad, /run mspaint)")
        return
    try:
        mgr = get_mgr()
        log.info("cmd_run %r via FreeRDP/simple", word)
        fb = await _win_via_freerdp(mgr, word)
        if fb is not None:
            if "Opened" in fb:
                await update.message.reply_text(fb + " (via Run dialog)")
                return
            # Not found for chrome -> try msedge/candidates automatically (tally has no chrome)
            if word.lower() in ("chrome","chrome.exe","google chrome"):
                for cand in ["msedge","msedge.exe","explorer"]:
                    log.info("chrome not found, trying fallback cand %r", cand)
                    fb2 = await _win_via_freerdp(mgr, cand)
                    if fb2 and "Opened" in fb2:
                        await update.message.reply_text(fb2 + f" (tried {cand} for chrome)" )
                        return
            # Tally RemoteApp blocks browsers - give clear hint
            if current.get("host")=="103.76.214.2":
                await update.message.reply_text(fb + " (via Run dialog)\n⚠️ Ye Tally RemoteApp server browsers ko block karta hai. Sirf TallyPrime allowed hai. Chrome/Edge yahan install hi nahi ya policy blocked hai.\nTry: /run calc, /run notepad — ye bhi blocked hai (tested). Browser ke liye dusra RDP 103.127.30.214:3330 use karo — wahan /search aur chrome pura working hai.")
            else:
                await update.message.reply_text(fb + " (via Run dialog) — try /run msedge or /run explorer if chrome not installed")
            return
        # Last resort: try simple-rdp direct open (no FreeRDP) — works for many apps
        try:
            await mgr.execute(commands.a_open_app(word) + [("wait", 2.0)])
            await asyncio.sleep(1.2)
            # check via screenshot diff
            # we don't have base, so just reply generic
            await update.message.reply_text(f"✅ Trying to open: {word} — /screen bhejo check karo")
        except Exception as e2:
            await update.message.reply_text(f"❌ Open failed: {e2}")
    except Exception as e:
        log.exception("run failed")
        await update.message.reply_text(f"Error: {e}")


@guarded
async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Search in Chrome with numbered selection: /search wallpaper 4k, /search instagram"""
    query = " ".join(context.args or []).strip()
    if not query:
        await update.message.reply_text("Kya search karna hai? e.g. /search wallpaper 4k  ya  /search instagram")
        return
    chat_id = update.effective_chat.id
    try:
        await update.message.reply_text(f"🔍 Searching: {query}...")
        # Fetch results via DuckDuckGo (numbered)
        results = await asyncio.to_thread(fetch_search_results, query, 5)
        search_sessions[chat_id] = results
        # Build message
        lines = [f"🔍 *{query}* — select karo (1-{len(results)}):\n"]
        for i, (title, url) in enumerate(results, 1):
            # shorten url
            disp = url[:55] + ("..." if len(url) > 55 else "")
            lines.append(f"*{i}.* {title}\n   {disp}\n")
        lines.append("\nReply with number `1`, `2`... ya button dabao — RDP ke Chrome me khulega.")
        # Inline keyboard 1-5 + Open Google
        buttons = []
        row = []
        for i in range(1, len(results)+1):
            row.append(InlineKeyboardButton(str(i), callback_data=f"search:{i}"))
            if len(row) == 3:
                buttons.append(row); row=[]
        if row: buttons.append(row)
        # Add direct open google search as fallback (especially for tally where results may not open)
        buttons.append([InlineKeyboardButton("🔎 Open Google Search", callback_data="search:0")])
        markup = InlineKeyboardMarkup(buttons)
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=markup)
    except Exception as e:
        log.exception("search failed")
        await update.message.reply_text(f"❌ Search failed: {e}")


async def on_search_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    chat_id = update.effective_chat.id
    results = search_sessions.get(chat_id, [])
    try:
        if data == "search:0":
            # open google search directly
            # need original query — reconstruct from last search? For now open google.com
            # Find last query from results title?
            # Use first result's google url if available, else google
            url = results[0][1] if results and "google.com/search" in results[0][1] else "https://www.google.com"
            # If results empty, try to get query from message text
            # Fallback: open google
        else:
            idx = int(data.split(":")[1]) - 1
            if idx < 0 or idx >= len(results):
                await query.edit_message_text("❌ Invalid selection")
                return
            title, url = results[idx]
        mgr = get_mgr()
        await query.edit_message_text(f"⏳ Opening in RDP: {url[:80]}...")
        host = current.get("host", "")
        res = await _open_url_for_profile(mgr, url, host)
        if res and "Not found" not in res:
            prefix = "✅" if host == "103.76.214.2" else "⏳"
            await query.message.reply_text(f"{prefix} {res} — /screen bhejo confirm karo")
        else:
            await query.message.reply_text(f"❌ URL open confirm nahi hua: {url} — /screen bhejo")
    except Exception as e:
        log.exception("search callback failed")
        try: await query.message.reply_text(f"❌ Error: {e}")
        except: pass


def _instagram_after_open_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔐 Start login", callback_data="igflow:login")],
        [InlineKeyboardButton("📩 Open Messages", callback_data="igflow:messages")],
    ])


async def _open_instagram_messages(mgr) -> str | None:
    """Open Instagram inbox directly, avoiding UI-version-specific icon clicks."""
    return await _open_url_for_profile(
        mgr, "https://www.instagram.com/direct/inbox/", current.get("host", "")
    )


@guarded
async def cmd_igdm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show a private-chat button that only navigates to Instagram Messages."""
    if not update.effective_chat or update.effective_chat.type != "private":
        await update.message.reply_text("Instagram Messages button sirf bot ke private DM mein use karo.")
        return
    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("📩 Open Instagram Messages", callback_data="igflow:messages")
    ]])
    await update.message.reply_text(
        "Instagram pehle se loaded aur signed in ho tab button dabao. "
        "Yeh sirf Messages section kholega; koi DM send nahi karega.",
        reply_markup=markup,
    )


@guarded
async def cmd_instagram(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Launch the right browser for this RDP type, then open Instagram."""
    if not update.effective_chat or update.effective_chat.type != "private":
        await update.message.reply_text("Instagram flow sirf bot ke private DM mein use karo.")
        return
    try:
        mgr = get_mgr()
        await mgr.ensure()
        result = await _open_url_for_profile(
            mgr, "https://www.instagram.com/", current.get("host", "")
        )
        if not result or "Not found" in result:
            await update.message.reply_text(
                f"❌ Browser launch/open confirm nahi hua ({result or 'unknown'}). RDP screen check karo."
            )
            return
        await update.message.reply_text(
            "✅ Instagram maximized browser mein open kiya (RemoteApp par Chrome, otherwise Edge). "
            "Agar browser profile mein pehle se login hai to Open Messages dabao; warna Start login. "
            "Existing browser profile hi reuse kiya hai—desktop login ko main independently verify nahi kar sakta.",
            reply_markup=_instagram_after_open_keyboard(),
        )
    except Exception as e:
        log.exception("/instagram failed")
        await update.message.reply_text(f"❌ Instagram nahi khul paya: {e}")


async def on_igflow_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Private guided Instagram flow; the same navigation helper covers both RDP types."""
    query = update.callback_query
    uid = update.effective_user.id if update.effective_user else 0
    if cfg["allowed_users"] and uid not in cfg["allowed_users"]:
        await query.answer("Not authorized", show_alert=True)
        return
    if not update.effective_chat or update.effective_chat.type != "private":
        await query.answer("Use this flow in the bot's private chat", show_alert=True)
        return
    chat_id = update.effective_chat.id
    action = (query.data or "").split(":", 1)[-1]
    await query.answer("Opening…")
    try:
        mgr = get_mgr()
        await mgr.ensure()
        if action == "open":
            res = await _open_url_for_profile(mgr, "https://www.instagram.com/", current.get("host", ""))
            await query.message.reply_text(
                "✅ Instagram maximized browser mein khola. Agar browser profile pehle se signed in hai to Open Messages dabao; warna Start login.\n"
                "Messages se chat list kholne ke baad /fetchgc → groups select → /done se tabs prepare karo. Workflow chalana ho tabhi /start use karo; pehle prompts/confirmation review karo. Koi DM apne aap send nahi hota.\n"
                f"{res or ''}",
                reply_markup=_instagram_after_open_keyboard(),
            )
        elif action == "login":
            res = await _open_url_for_profile(mgr, "https://www.instagram.com/accounts/login/", current.get("host", ""))
            if not res or "Not found" in res:
                await query.message.reply_text("❌ Login page open confirm nahi hua. /screen check karo.")
                return
            ig_login_sessions[chat_id] = {"step": "username"}
            await query.message.reply_text(
                "Username bhejo. Is private chat mein username ke baad password maangunga; password message delete hone ke baad hi RDP par type hoga."
            )
        elif action == "messages":
            res = await _open_instagram_messages(mgr)
            if not res or "Not found" in res:
                await query.message.reply_text(
                    "❌ Instagram inbox route open confirm nahi hua. /screen se browser state check karo."
                )
                return
            ig_login_sessions.pop(chat_id, None)
            await query.message.reply_text(
                "✅ Instagram Messages route khola. Koi DM send nahi hua.\n"
                "Next: /fetchgc se recent chats dikhao → desired chats select karo → /done se tabs prepare karo. "
                "Workflow choose/import karne ke baad hi /start chalao; /screen se verify karo.\n"
                f"{res}"
            )
        else:
            await query.message.reply_text("Unknown Instagram action.")
    except Exception as e:
        log.exception("Instagram guided flow failed")
        await query.message.reply_text(f"❌ Instagram flow failed: {e}")


@guarded
async def cmd_fix(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Black screen fix: disconnect + reconnect"""
    try:
        mgr = get_mgr()
        await update.message.reply_text("🔄 Fixing black screen — reconnecting...")
        try:
            await mgr.disconnect()
        except: pass
        await asyncio.sleep(1.2)
        await mgr.connect()
        await asyncio.sleep(1.0)
        img = await mgr.screenshot()
        await update.message.reply_text(f"✅ Reconnected — brightness {mean_brightness(img):.0f} — /screen bhejo")
    except Exception as e:
        await update.message.reply_text(f"❌ Fix failed: {e}")

@guarded
async def cmd_ui(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    host = os.environ.get("RENDER_EXTERNAL_HOSTNAME") or os.environ.get("RENDER_EXTERNAL_URL") or "telegram-rdp-bot.onrender.com"
    # Render gives https url
    url = f"https://{host}/ui" if not host.startswith("http") else f"{host}/ui"
    # fallback
    if "onrender.com" not in url:
        url = "https://telegram-rdp-bot.onrender.com/ui"
    await update.message.reply_text(f"🎮 Virtual Keyboard + Mouse:\n{url}\n\n• Screen pe tap = click\n• Pad drag = mouse move\n• Type box + keyboard\n• Fix Black button\n• Har RDP apne lock me — dusre ko block nahi karega")

async def _window_command(update: Update, actions: list, success: str) -> None:
    """Apply a system-level window shortcut to the focused RDP window."""
    try:
        mgr = get_mgr()
        await mgr.ensure()
        await mgr.execute(actions)
        await update.message.reply_text(success)
    except Exception as e:
        log.exception("window command failed")
        await update.message.reply_text(f"❌ Window command failed: {e}")


@guarded
async def cmd_minimize(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Open the active window's system menu, wait for it to render, then choose
    # Minimize by its English mnemonic. The delay is important over RDP.
    await _window_command(update, [("key", "alt+space"), ("wait", 0.5), ("key", "n"), ("wait", 0.5)],
                          "✅ Minimize command sent to the focused window (RDP session stays connected).")


@guarded
async def cmd_maximize(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Use the active window's system menu, not Win+Up (which may be intercepted
    # by the local RDP client). X is the standard Maximize mnemonic.
    await _window_command(update, [("key", "alt+space"), ("wait", 0.5), ("key", "x"), ("wait", 0.7)],
                          "✅ Maximize command sent to the focused window.")


def _looks_like_windows_desktop(image) -> bool:
    """Recognize the blue desktop used by this RDP profile before sending Alt+F4."""
    width, height = image.size
    crop = image.convert("RGB").crop((int(width*.12), int(height*.12), int(width*.88), int(height*.86)))
    pixels = list(crop.get_flattened_data())
    if not pixels:
        return False
    blue = sum(b > r*1.15 and b > g*1.05 and b > 80 for r, g, b in pixels)
    return blue / len(pixels) > 0.65


@guarded
async def cmd_close(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Close visible app windows in turn; stop at desktop, keep the RDP session."""
    try:
        mgr = get_mgr()
        await mgr.ensure()
        closed = 0
        for _ in range(15):
            before = await mgr.screenshot()
            if _looks_like_windows_desktop(before):
                break
            await mgr.execute([("key", "alt+f4"), ("wait", 0.8)])
            closed += 1
            after = await mgr.screenshot()
            if _looks_like_windows_desktop(after):
                break
            # If a dialog/window did not visibly go away, stop rather than
            # sending more close keystrokes that could discard unsaved work.
            if diff_ratio(before, after) < 0.004:
                await update.message.reply_text(
                    f"⚠️ {closed} close shortcut(s) sent, but a window did not visibly close. "
                    "Possible save/confirmation dialog left untouched; check /screen. RDP session remains connected."
                )
                return
        await update.message.reply_text(
            f"✅ Closed {closed} visible window(s) until the desktop appeared. RDP session remains connected. "
            "Unsaved-work dialogs are not auto-confirmed."
        )
    except Exception as e:
        log.exception("close-all failed")
        await update.message.reply_text(f"❌ Close-all failed: {e}")


@guarded
async def cmd_split(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Open Edge's native two-tab Split Screen without changing other windows."""
    try:
        mgr = get_mgr()
        await mgr.ensure()
        if not await _focus_edge_window(mgr):
            await update.message.reply_text("❌ Edge focus confirm nahi hua; split shortcut nahi bheja.")
            return
        await mgr.execute([("key", "ctrl+shift+2"), ("wait", 1.5)])
        image = await mgr.screenshot()
        buf = io.BytesIO(); image.save(buf, format="PNG"); buf.seek(0)
        await update.message.reply_photo(
            photo=buf,
            caption=("✅ Edge Split Screen shortcut bheja. Agar version mein horizontal/stacked layout available ho, "
                     "split-pane menu se top-bottom select karo. Kuch Edge builds sirf side-by-side dete hain; "
                     "ye command dusre RDP windows ko move nahi karti."),
        )
    except Exception as e:
        log.exception("Edge split failed")
        await update.message.reply_text(f"❌ Edge split open nahi hua: {e}")


@guarded
async def cmd_edge(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Launch/restore Edge only through its pinned taskbar icon; never type into Explorer."""
    try:
        mgr = get_mgr()
        await mgr.ensure()
        opened = await _focus_edge_window(mgr)
        if opened:
            await update.message.reply_text("✅ Microsoft Edge open/focus confirmed.")
        else:
            await update.message.reply_text("❌ Edge taskbar icon/window not confirmed. No shell command was sent; /screen check karo.")
    except Exception as e:
        log.exception("edge failed")
        await update.message.reply_text(f"❌ Edge open/focus failed: {e}")


async def _ctrl_flow(update: Update, word: str) -> None:
    """Ctrl+A then type — replaces the current selection in the focused app."""
    if not word:
        await update.message.reply_text("Usage: /ctrl <text>")
        return
    try:
        await get_mgr().execute([("key", "ctrl+a"), ("wait", 0.2), ("text", word)])
        await update.message.reply_text(f"Typed: {word}")
    except Exception as e:
        log.exception("ctrl type failed")
        await update.message.reply_text(f"Error: {e}")


@guarded
async def cmd_ctrl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _ctrl_flow(update, " ".join(context.args or []).strip())


@guarded
async def cmd_igotp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Submit an Instagram verification code once, after deleting its chat message."""
    chat_id = update.effective_chat.id if update.effective_chat else 0
    uid = update.effective_user.id if update.effective_user else 0
    if not cfg["allowed_users"] or uid not in cfg["allowed_users"]:
        await update.message.reply_text("🔒 OTP entry ke liye ALLOWED_USERS mein apna Telegram user ID set hona zaroori hai.")
        return
    if not update.effective_chat or update.effective_chat.type != "private":
        await update.message.reply_text("🔒 OTP sirf bot ke private DM mein bhejo.")
        return
    code = "".join(context.args or []).strip()
    if not re.fullmatch(r"[A-Za-z0-9-]{4,12}", code):
        await update.message.reply_text("Usage: /igotp <4–12 character OTP or backup code>")
        return
    try:
        await update.message.delete()
    except Exception:
        await update.message.reply_text("🔒 OTP message delete nahi hua, isliye code submit nahi kiya. Message khud delete karke /igotp dobara bhejo.")
        return

    ig_login_sessions.setdefault(chat_id, {"step": "otp"})
    try:
        mgr = get_mgr()
        await mgr.ensure()
        if not mgr.is_connected:
            await update.effective_chat.send_message("❌ RDP disconnected hai; OTP submit nahi hua. /connect karke dobara try karo.")
            return
        width, height = mgr.client.width, mgr.client.height
        # Instagram's email-code challenge places the Code field low in the
        # centered card, with Continue near the bottom; don't reuse login-page coords.
        await mgr.execute([
            ("click", int(width * 0.50), int(height * 0.80), 1, False),
            ("wait", 0.3), ("key", "ctrl+a"),
        ])
        # As with the Run dialog, bundled text actions can lose characters on this host.
        for char in code:
            await mgr.execute([("text", char)])
            await asyncio.sleep(0.25)
        await mgr.execute([
            ("wait", 0.4),
            ("click", int(width * 0.50), int(height * 0.955), 1, False), ("wait", 5.0),
        ])
        # Keep only the pending-step marker (never the code), so the user can
        # retry with a fresh code if Instagram leaves the challenge screen open.
        await update.effective_chat.send_message("✅ OTP entry/Continue click bheja. /screen se confirm karo; code prompt rahe to naya code dekar /igotp dobara bhejo, ya /cancel karo.")
    except Exception:
        log.exception("Instagram OTP automation failed")
        await update.effective_chat.send_message("❌ OTP type/click nahi hua. /screen check karo; code save nahi hua—prompt rahe to /igotp dobara try karo.")


@guarded
async def cmd_iglogin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Open Instagram and begin a private, one-shot credential entry flow."""
    uid = update.effective_user.id if update.effective_user else 0
    if not cfg["allowed_users"] or uid not in cfg["allowed_users"]:
        await update.message.reply_text("🔒 /iglogin ke liye ALLOWED_USERS mein apna Telegram user ID set hona zaroori hai.")
        return
    if update.effective_chat.type != "private":
        await update.message.reply_text("🔒 Instagram login sirf bot ke private DM mein start karo.")
        return
    chat_id = update.effective_chat.id
    try:
        mgr = get_mgr()
        host = current.get("host", "")
        opened = await _open_url_for_profile(mgr, "https://www.instagram.com/accounts/login/", host)
        if not opened or "Not found" in opened:
            await update.message.reply_text("❌ Instagram page khulna confirm nahi hua. /screen bhejo aur RDP browser check karo.")
            return
        ig_login_sessions[chat_id] = {"step": "username"}
        await update.message.reply_text(
            "Instagram login page par navigation command bhej diya. /screen se page check karo; phir isi private chat mein username bhejo, uske baad password. "
            "Password message ko bot enter karne se pehle delete karne ki koshish karega; agar delete fail hua to login submit nahi karega. "
            "Credentials save nahi honge. /cancel se flow rok sakte ho."
        )
    except Exception as e:
        ig_login_sessions.pop(chat_id, None)
        await update.message.reply_text(f"❌ Instagram page open nahi hua: {e}")


async def _type_slow(mgr, value: str, delay: float = 0.18) -> None:
    """Type separately per character; RemoteApp drops bundled text payloads."""
    for char in value:
        await mgr.execute([("text", char)])
        await asyncio.sleep(delay)


async def _handle_ig_login_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Consume private login/OTP messages without persisting secrets."""
    if not update.message or not update.message.text or not update.effective_chat:
        return False
    chat_id = update.effective_chat.id
    session = ig_login_sessions.get(chat_id)
    if not session:
        return False
    if update.effective_chat.type != "private":
        ig_login_sessions.pop(chat_id, None)
        return True

    step = session.get("step")
    if step == "username":
        username = update.message.text.strip()
        if not username or username.startswith("/"):
            await update.message.reply_text("Username bhejo, ya /cancel se flow rok do.")
            return True
        session["username"] = username
        session["step"] = "password"
        await update.message.reply_text("Ab password bhejo. Password message delete hone ke baad hi RDP par type karunga.")
        return True

    if step == "otp":
        code = update.message.text.strip()
        if code.startswith("/"):
            return False
        if not re.fullmatch(r"[A-Za-z0-9-]{4,12}", code):
            await update.message.reply_text("OTP/backup code 4–12 characters ka hona chahiye; ya /cancel karo.")
            return True
        try:
            await update.message.delete()
        except Exception:
            await update.message.reply_text("🔒 OTP message delete nahi hua, isliye code submit nahi kiya. Chat se delete karke dobara bhejo.")
            return True
        try:
            mgr = get_mgr()
            await mgr.ensure()
            width, height = mgr.client.width, mgr.client.height
            await mgr.execute([
                ("click", int(width * 0.50), int(height * 0.80), 1, False),
                ("wait", 0.3), ("key", "ctrl+a"),
            ])
            await _type_slow(mgr, code, 0.25)
            await mgr.execute([
                ("wait", 0.4),
                ("click", int(width * 0.50), int(height * 0.955), 1, False),
                ("wait", 5.0),
            ])
            # After OTP submission, open the Direct Messages sidebar. This is a
            # navigation click only; no chat is selected and no message is sent.
            await _click_instagram_messages(mgr)
            ig_login_sessions.pop(chat_id, None)
            await update.effective_chat.send_message("✅ OTP submit hua; Instagram Messages section kholne ka click bheja. /screen se verify karo.")
        except Exception:
            ig_login_sessions.pop(chat_id, None)
            log.exception("Instagram OTP flow failed")
            await update.effective_chat.send_message("❌ OTP flow fail hua. /screen check karo; code store nahi hua.")
        return True

    if step != "password":
        return False

    # Password is held only in this local variable and deleted before entering it.
    password = update.message.text
    username = session.get("username", "")
    try:
        await update.message.delete()
    except Exception:
        password = ""
        await update.message.reply_text("🔒 Password message delete nahi hua, isliye login submit nahi kiya. Message khud delete karke flow dobara start karo.")
        return True

    try:
        mgr = get_mgr()
        await mgr.ensure()
        width, height = mgr.client.width, mgr.client.height
        await mgr.execute([
            ("click", int(width * 0.75), int(height * 0.35), 1, False),
            ("wait", 0.3), ("key", "ctrl+a"),
        ])
        await _type_slow(mgr, username)
        await mgr.execute([("key", "tab")])
        await _type_slow(mgr, password)
        await mgr.execute([
            ("wait", 0.4),
            ("click", int(width * 0.75), int(height * 0.54), 1, False),
            ("wait", 5.0),
        ])
        password = ""
        ig_login_sessions[chat_id] = {"step": "otp"}
        await update.effective_chat.send_message(
            "✅ Login form submit hua. Agar OTP maange, code ko agle message mein bhejo—delete hone ke baad submit hoga aur Messages khulega. "
            "Agar OTP nahi aaya/already logged in ho, Open Messages button use karo; koi message send nahi hoga.",
            reply_markup=_instagram_after_open_keyboard(),
        )
    except Exception:
        password = ""
        log.exception("Instagram login automation failed")
        await update.effective_chat.send_message("❌ Login form fill nahi ho paya. /screen check karo; secret save nahi hua.")
    return True


@guarded
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    if await _handle_ig_login_message(update, context):
        return
    text = update.message.text.strip()

    # /connect step-by-step flow
    session = connect_sessions.get(update.effective_chat.id)
    if session:
        return await _handle_connect_step(update, session)

    low = text.lower()

    # Plain-text shortcuts
    if low in ("screen", "screenshot", "photo", "desktop photo"):
        return await cmd_screen(update, context)
    if low in ("fix", "fix black", "black screen"):
        return await cmd_fix(update, context)
    if low in ("ui", "keyboard", "mouse", "virtual", "vkbd"):
        return await cmd_ui(update, context)
    if low in ("connect", "login"):
        context.args = []
        return await cmd_connect(update, context)
    if low in ("disconnect", "logout"):
        return await cmd_disconnect(update, context)
    if low in ("status",):
        return await cmd_status(update, context)
    if low in ("list", "profiles"):
        return await cmd_list(update, context)
    if low == "win" or low.startswith("win "):
        return await _win_flow(update, text[3:].strip())
    if low == "run" or low.startswith("run "):
        context.args = text[3:].strip().split() if text[3:].strip() else []
        return await cmd_run(update, context)
    if low == "ctrl" or low.startswith("ctrl "):
        return await _ctrl_flow(update, text[4:].strip())
    if low.startswith("search ") or low == "search":
        q = text[6:].strip() if low.startswith("search ") else ""
        context.args = q.split() if q else []
        return await cmd_search(update, context)
    # Numbered search selection: user replies "1" or "2" after /search
    if low.isdigit() and update.effective_chat.id in search_sessions:
        results = search_sessions[update.effective_chat.id]
        idx = int(low) - 1
        if 0 <= idx < len(results):
            title, url = results[idx]
            try:
                mgr = get_mgr()
                await update.message.reply_text(f"⏳ Opening {idx+1}: {title}\n{url}")
                host = current.get("host", "")
                res = await _open_url_for_profile(mgr, url, host)
                if not res or "Not found" in res:
                    await update.message.reply_text(f"❌ Open confirm nahi hua: {title} — /screen bhejo")
                else:
                    prefix = "✅" if host == "103.76.214.2" else "⏳"
                    await update.message.reply_text(f"{prefix} {title}: {res} — /screen bhejo confirm karo")
            except Exception as e:
                await update.message.reply_text(f"❌ Error: {e}")
            return

    # Profile switching: "rdp2", "rdp 2", "/rdp2", "switch1", "switch 2", "/switch1"
    sw = low[1:] if low.startswith("/") else low
    for prefix in ("rdp", "switch"):
        if sw.startswith(prefix):
            rest = sw[len(prefix):].strip()
            if rest.isdigit():
                rest = f"rdp{rest}"
            if rest:
                context.args = [rest]
                return await cmd_switch(update, context)

    try:
        actions, desc = commands.parse_command(text)
    except ValueError as e:
        await update.message.reply_text(f"⚠️ {e}\n\nType /start to see the commands.")
        return

    # Website aliases/URLs should go straight to the current browser on full
    # Windows desktops, not into the Start-menu search box via Ctrl+Esc.
    target_url = None
    if desc.startswith("Opening:"):
        target_url = desc.split(":", 1)[1].strip()
    elif desc.startswith("Opening "):
        site = desc[len("Opening "):].strip().lower()
        target_url = commands.WEB_ALIASES.get(site)
    if target_url:
        try:
            host = current.get("host", "")
            res = await _open_url_for_profile(get_mgr(), target_url, host)
            if not res or "Not found" in res:
                await update.message.reply_text(f"❌ Open command fail hua: {target_url} — /screen bhejo")
            else:
                prefix = "✅" if host == "103.76.214.2" else "⏳"
                await update.message.reply_text(f"{prefix} {res} — /screen bhejo confirm karo")
        except Exception as e:
            log.exception("website navigation failed")
            await update.message.reply_text(f"❌ Website open failed: {e}")
        return

    try:
        await get_mgr().execute(actions)
        await update.message.reply_text(f"✅ {desc}")
    except Exception as e:
        log.exception("execute failed")
        await update.message.reply_text(f"❌ Error: {e}")


def main() -> None:
    if not cfg["telegram_token"]:
        raise SystemExit("TELEGRAM_TOKEN missing — set it in the environment")

    start_health_server()

    app = Application.builder().token(cfg["telegram_token"]).build()
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("rdp", cmd_switch))
    app.add_handler(CommandHandler("switch", cmd_switch))
    app.add_handler(CommandHandler("edge", cmd_edge))
    app.add_handler(CommandHandler("minimize", cmd_minimize))
    app.add_handler(CommandHandler("maximize", cmd_maximize))
    app.add_handler(CommandHandler("close", cmd_close))
    app.add_handler(CommandHandler("split", cmd_split))
    app.add_handler(CommandHandler("connect", cmd_connect))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("disconnect", cmd_disconnect))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("keepalive", cmd_keepalive))
    app.add_handler(CommandHandler("screen", cmd_screen))
    app.add_handler(CommandHandler("win", cmd_win))
    app.add_handler(CommandHandler("run", cmd_run))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("fix", cmd_fix))
    app.add_handler(CommandHandler("ui", cmd_ui))
    app.add_handler(CommandHandler("keyboard", cmd_ui))
    app.add_handler(CommandHandler("mouse", cmd_ui))
    app.add_handler(CommandHandler("ctrl", cmd_ctrl))
    app.add_handler(CommandHandler("instagram", cmd_instagram))
    app.add_handler(CommandHandler("igdm", cmd_igdm))
    app.add_handler(CallbackQueryHandler(on_search_callback, pattern=r"^search:"))
    app.add_handler(CallbackQueryHandler(on_igflow_callback, pattern=r"^igflow:"))
    automa_features.register(app, sys.modules[__name__])
    app.add_handler(MessageHandler(filters.TEXT, on_message))

    async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        log.error("Unhandled error: %s", context.error, exc_info=context.error)

    app.add_error_handler(on_error)
    log.info("Bot is starting... profiles: %s", sorted(profiles))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
