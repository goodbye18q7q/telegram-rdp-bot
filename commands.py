"""Convert user commands into RDP GUI actions.

Both slash commands (/open chrome) and plain text (open chrome) are parsed here.
Connection and screenshot commands live in bot.py.
"""
from __future__ import annotations

import urllib.parse

# ---------------------------------------------------------------------------
# Presets — ready-made action sequences
# ---------------------------------------------------------------------------

def _clear_field() -> list:
    """Select all + delete — purana text (jaise 'chrome') hata deta hai."""
    return [
        ("key", "ctrl+a"),
        ("wait", 0.2),
        ("key", "backspace"),
        ("wait", 0.2),
    ]


def a_open_app(app: str) -> list:
    """Open an app through the Windows Run dialog (Win+R)."""
    return [
        ("key", "win+r"),
        ("wait", 0.6),
        *_clear_field(),
        ("text", app),
        ("wait", 0.3),
        ("key", "enter"),
        ("wait", 1.2),
    ]


def a_win_search(word: str) -> list:
    """Open Win+R and type a word — WITHOUT pressing Enter.

    Used by /win so a screenshot can be taken before and after Enter to tell
    whether the app was found ("opened") or not ("not found").
    """
    return [
        ("key", "win+r"),
        ("wait", 0.6),
        *_clear_field(),
        ("text", word),
        ("wait", 0.4),
    ]


def a_chrome() -> list:
    return a_open_app("chrome") + [("wait", 2.0)]


def a_search(query: str) -> list:
    """Open Chrome and search on Google."""
    return a_chrome() + [
        ("key", "ctrl+l"),
        ("wait", 0.4),
        ("text", query),
        ("wait", 0.3),
        ("key", "enter"),
        ("wait", 2.5),
    ]


def a_goto(url: str) -> list:
    """Open a URL in Chrome."""
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    return a_chrome() + [
        ("key", "ctrl+l"),
        ("wait", 0.4),
        ("text", url),
        ("wait", 0.3),
        ("key", "enter"),
        ("wait", 2.5),
    ]


def a_webstore(name: str) -> list:
    """Search an extension in the Chrome Web Store."""
    url = "https://chromewebstore.google.com/search/" + urllib.parse.quote(name)
    return a_goto(url) + [("wait", 1.5)]


def a_run_cmd(command: str) -> list:
    """Open PowerShell and run a command."""
    return a_open_app("powershell") + [
        ("wait", 1.2),
        ("text", command),
        ("wait", 0.3),
        ("key", "enter"),
        ("wait", 1.5),
    ]


# ---------------------------------------------------------------------------
# Aliases
# ---------------------------------------------------------------------------

APP_ALIASES = {
    "chrome": "chrome",
    "google chrome": "chrome",
    "brave": "brave",
    "firefox": "firefox",
    "edge": "msedge",
    "msedge": "msedge",
    "microsoft edge": "msedge",
    "notepad": "notepad",
    "cmd": "cmd",
    "command prompt": "cmd",
    "powershell": "powershell",
    "terminal": "powershell",
    "calculator": "calc",
    "calc": "calc",
    "paint": "mspaint",
    "mspaint": "mspaint",
    "taskmanager": "taskmgr",
    "task manager": "taskmgr",
    "explorer": "explorer",
    "file explorer": "explorer",
    "vscode": "code",
    "vs code": "code",
    "code": "code",
    "word": "winword",
    "excel": "excel",
    "vlc": "vlc",
}

WEB_ALIASES = {
    "instagram": "https://www.instagram.com",
    "insta": "https://www.instagram.com",
    "facebook": "https://www.facebook.com",
    "fb": "https://www.facebook.com",
    "youtube": "https://www.youtube.com",
    "yt": "https://www.youtube.com",
    "gmail": "https://mail.google.com",
    "google": "https://www.google.com",
    "whatsapp": "https://web.whatsapp.com",
    "twitter": "https://x.com",
}

# ---------------------------------------------------------------------------
# Command words (English + Hinglish aliases)
# ---------------------------------------------------------------------------

OPEN_WORDS = {"open", "khol", "kholo", "khola", "launch", "start"}
SEARCH_WORDS = {"search", "dhundo", "dhund", "khoj", "find"}
GOTO_WORDS = {"goto", "go", "jao", "jaao", "navigate", "website", "url"}
TYPE_WORDS = {"type", "likh", "likho", "write"}
KEY_WORDS = {"key", "press", "dabao", "daba"}
CLICK_WORDS = {"click", "tika", "tap"}
SCROLL_WORDS = {"scroll"}
INSTALL_WORDS = {"install", "webstore", "extension"}
CMD_WORDS = {"cmd", "command", "execute", "run", "chala", "chalao", "chalana"}
EDGE_WORDS = {"edge", "msedge", "microsoft edge"}
WAIT_WORDS = {"wait", "ruko"}
SEQ_WORDS = {"seq", "sequence", "macro"}
MOVE_WORDS = {"move"}

# Union of all verbs — used by the Hinglish fallback
KNOWN_VERBS = (
    OPEN_WORDS
    | SEARCH_WORDS
    | GOTO_WORDS
    | TYPE_WORDS
    | KEY_WORDS
    | CLICK_WORDS
    | SCROLL_WORDS
    | INSTALL_WORDS
    | CMD_WORDS
    | WAIT_WORDS
    | SEQ_WORDS
    | MOVE_WORDS
    | EDGE_WORDS
    | {
        "close", "band", "bandh", "lock", "taskmanager", "fullscreen", "full",
        "desktop", "esc", "escape", "enter", "return",
    }
)

# Bare key steps in a sequence
KEY_NAMES = {
    "enter", "return", "esc", "escape", "tab", "space", "backspace", "delete", "del",
    "insert", "home", "end", "pageup", "pgup", "pagedown", "pgdn", "up", "down",
    "left", "right", "win", "lwin", "rwin", "ctrl", "alt", "shift", "capslock",
    "numlock", "scrolllock", "printscreen", "prtsc", "pause", "menu", "apps",
    "f1", "f2", "f3", "f4", "f5", "f6", "f7", "f8", "f9", "f10", "f11", "f12",
}


def looks_like_key(token: str) -> bool:
    return "+" in token or token.lower() in KEY_NAMES


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def parse_command(text: str) -> tuple[list, str]:
    """Text -> (actions, description). ValueError means "did not understand"."""
    raw = text.strip()
    if not raw:
        raise ValueError("Please type a command.")

    if raw.startswith("/"):
        tokens = raw.split()
        cmd = tokens[0][1:].lower()
        if "@" in cmd:  # /open@MyBot chrome
            cmd = cmd.split("@", 1)[0]
        arg = raw[len(tokens[0]):].strip()
        return _dispatch(cmd, arg)

    parts = raw.split()
    first = parts[0].lower()

    # phrases like "go to instagram"
    if first in GOTO_WORDS and len(parts) > 1 and parts[1].lower() == "to":
        return _dispatch("goto", " ".join(parts[2:]))

    # Hinglish: the verb may come at the end — "chrome kholo", "instagram search"
    if first not in KNOWN_VERBS and len(parts) > 1 and parts[-1].lower() in KNOWN_VERBS:
        verb = parts[-1].lower()
        return _dispatch(verb, " ".join(parts[:-1]))

    arg = raw[len(parts[0]):].strip()
    return _dispatch(first, arg)


def _dispatch(word: str, arg: str) -> tuple[list, str]:
    if word in WEB_ALIASES:
        # /instagram, /youtube, /gmail — works without arguments too
        return a_goto(WEB_ALIASES[word]), f"Opening {word}"

    if word in EDGE_WORDS:
        return a_open_app("msedge") + [("wait", 2.0)], "Opening Microsoft Edge"

    if word in OPEN_WORDS:
        if not arg:
            raise ValueError("What should I open? e.g. /open chrome")
        return _open(arg)

    if word in SEARCH_WORDS:
        if not arg:
            raise ValueError("What should I search? e.g. /search instagram")
        return a_search(arg), f"Search: {arg}"

    if word in GOTO_WORDS:
        if not arg:
            raise ValueError("Which URL? e.g. /goto instagram.com")
        return a_goto(arg), f"Opening: {arg}"

    if word in TYPE_WORDS:
        if not arg:
            raise ValueError("What should I type? e.g. /type hello")
        return [("text", arg)], f"Typing: {arg}"

    if word in KEY_WORDS:
        if not arg:
            raise ValueError("Which key? e.g. /key ctrl+shift+esc")
        return [("key", arg)], f"Key: {arg}"

    if word in CLICK_WORDS:
        return _click(arg)

    if word in SCROLL_WORDS:
        return _scroll(arg)

    if word in INSTALL_WORDS:
        if not arg:
            raise ValueError("Which extension? e.g. /install adblock")
        return (
            a_webstore(arg),
            f"Web Store opened: {arg} — check /screen, then /click x y on 'Add to Chrome'",
        )

    if word in CMD_WORDS:
        if not arg:
            raise ValueError("Which command? e.g. /cmd ipconfig")
        return a_run_cmd(arg), f"PowerShell: {arg}"

    if word in WAIT_WORDS:
        return [("wait", float(arg or 1))], f"Wait {arg or 1}s"

    if word in SEQ_WORDS:
        if not arg:
            raise ValueError("Empty sequence. e.g. /seq win+r; type chrome; press enter")
        return parse_sequence(arg), "Running sequence"

    if word in MOVE_WORDS:
        return _move(arg)

    if word in ("close", "band", "bandh"):
        return [("key", "alt+f4")], "Alt+F4 (close window)"

    if word in ("lock",):
        return [("key", "win+l")], "Lock (Win+L)"

    if word in ("taskmanager",):
        return [("key", "ctrl+shift+esc")], "Task Manager"

    if word in ("fullscreen", "full screen", "full"):
        return [("key", "f11")], "F11 (fullscreen)"

    if word in ("desktop",):
        return [("key", "win+d")], "Show Desktop (Win+D)"

    if word in ("esc", "escape"):
        return [("key", "escape")], "Escape"

    if word in ("enter", "return"):
        return [("key", "enter")], "Enter"

    raise ValueError(f"Unknown command: {word}")


def _open(arg: str) -> tuple[list, str]:
    low = arg.lower()
    if low in WEB_ALIASES:
        return a_goto(WEB_ALIASES[low]), f"Opening {low}"
    app = APP_ALIASES.get(low, arg)
    return a_open_app(app), f"Opening {app}"


def _click(arg: str) -> tuple[list, str]:
    toks = arg.split()
    if len(toks) < 2:
        raise ValueError("Format: /click 640 360 [right|middle|double]")
    try:
        x, y = int(toks[0]), int(toks[1])
    except ValueError:
        raise ValueError("Coordinates must be numbers")
    button, double = 1, False
    for t in toks[2:]:
        tl = t.lower()
        if tl == "right":
            button = 2
        elif tl == "middle":
            button = 3
        elif tl in ("double", "2x"):
            double = True
    return [("click", x, y, button, double)], f"Click ({x}, {y})"


def _move(arg: str) -> tuple[list, str]:
    toks = arg.split()
    if len(toks) < 2:
        raise ValueError("Format: /move 640 360")
    try:
        x, y = int(toks[0]), int(toks[1])
    except ValueError:
        raise ValueError("Coordinates must be numbers")
    return [("move", x, y)], f"Mouse move ({x}, {y})"


def _scroll(arg: str) -> tuple[list, str]:
    toks = arg.split()
    direction = toks[0].lower() if toks else "down"
    try:
        n = int(toks[1]) if len(toks) > 1 else 3
    except ValueError:
        n = 3
    delta = 120 * n if direction in ("up", "upar") else -120 * n
    return [("scroll", delta)], f"Scroll {direction}"


def parse_sequence(seq: str) -> list:
    """/seq win+r; type chrome; press enter; wait 2 -> actions"""
    actions: list = []
    for step in seq.split(";"):
        step = step.strip()
        if not step:
            continue
        parts = step.split(None, 1)
        op = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if op in ("wait", "sleep"):
            actions.append(("wait", float(arg)))
        elif op in ("key", "press"):
            actions.append(("key", arg))
        elif looks_like_key(op):
            # Bare key step — "win+r", "ctrl+shift+esc", "enter"
            actions.append(("key", op))
        elif op == "type":
            actions.append(("text", arg))
        elif op == "click":
            toks = arg.split()
            if len(toks) < 2:
                raise ValueError(f"click step needs x y: {step}")
            button = 1
            double = False
            for t in toks[2:]:
                tl = t.lower()
                if tl == "right":
                    button = 2
                elif tl == "middle":
                    button = 3
                elif tl in ("double", "2x"):
                    double = True
            actions.append(("click", int(toks[0]), int(toks[1]), button, double))
        elif op == "move":
            toks = arg.split()
            if len(toks) < 2:
                raise ValueError(f"move step needs x y: {step}")
            actions.append(("move", int(toks[0]), int(toks[1])))
        elif op == "scroll":
            toks = arg.split()
            direction = toks[0] if toks else "down"
            n = int(toks[1]) if len(toks) > 1 else 3
            actions.append(("scroll", 120 * n if direction in ("up", "upar") else -120 * n))
        else:
            raise ValueError(f"Unknown sequence step: {op}")
    return actions
