"""
Daily social post bot.

Reads your recent GitHub activity and today's tech news (Hacker News), asks a
Groq-hosted LLM for three post options (my work, a news take, a meme), and
delivers them: one-tap links in a daily GitHub issue, or auto-posting via the APIs.
Keeps a copy under posts/.

Run:  python bot.py            # generate and deliver today's options
      python bot.py --dry-run  # generate and print only, never post or save
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import re
import sys
from pathlib import Path

import requests

try:  # optional: load a local .env when running on your own machine
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

ROOT = Path(__file__).parent
POSTS_DIR = ROOT / "posts"
HISTORY_FILE = ROOT / "history.json"
STYLE_FILE = ROOT / "style.md"

# ---------- config ----------
GITHUB_USERNAME = os.getenv("GH_USERNAME", "")
GITHUB_TOKEN = os.getenv("GH_TOKEN", "")  # optional, but includes private activity + higher rate limit
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL") or ""  # blank = pick the best available model automatically
LOOKBACK_HOURS = int(os.getenv("LOOKBACK_HOURS") or 24)
# private repos need GH_ACTIVITY_TOKEN (a personal token). they're never named in posts:
# the model only gets the kind of work, so client/company names stay out.
# which option api modes post automatically: work, news, or meme (falls back to the first one)
AUTO_OPTION = (os.getenv("AUTO_OPTION") or "work").lower()
INCLUDE_PRIVATE_REPOS = (os.getenv("INCLUDE_PRIVATE_REPOS") or "true").lower() == "true"
# repos to ignore completely, e.g. EXCLUDE_REPOS=me/client-app,acme/secret
EXCLUDE_REPOS = {r.strip().lower() for r in (os.getenv("EXCLUDE_REPOS") or "").split(",") if r.strip()}
# skip any repo whose name or description contains one of these, e.g. EXCLUDE_KEYWORDS=timber,ptm
EXCLUDE_KEYWORDS = [k.strip().lower() for k in (os.getenv("EXCLUDE_KEYWORDS") or "").split(",") if k.strip()]


def excluded(full_name: str, description: str = "") -> bool:
    name = full_name.lower()
    text = f"{name.split('/')[-1]} {description.lower()}"
    return name in EXCLUDE_REPOS or any(k in text for k in EXCLUDE_KEYWORDS)
AUTHOR_BIO = os.getenv("AUTHOR_BIO") or "a software developer who builds things in public"
# Auto-posting is ON by default. A platform is skipped (with a warning) if its keys aren't set.
AUTO_POST_X = (os.getenv("AUTO_POST_X") or "true").lower() == "true"
AUTO_POST_LINKEDIN = (os.getenv("AUTO_POST_LINKEDIN") or "true").lower() == "true"
# X_MODE / LINKEDIN_MODE:
#   "api"    posts automatically through the platform's API (x costs ~$0.015/post)
#   "manual" is free and needs no platform keys: opens one GitHub issue a day with
#            one-tap links that pre-fill each post, and you just hit post.
X_MODE = (os.getenv("X_MODE") or "api").lower()
LINKEDIN_MODE = (os.getenv("LINKEDIN_MODE") or "api").lower()
ISSUE_KEYS = ["GITHUB_REPOSITORY", "ISSUE_TOKEN"]
LOWERCASE = (os.getenv("LOWERCASE") or "true").lower() == "true"
X_KEYS = ["X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET"]
LINKEDIN_KEYS = ["LINKEDIN_ACCESS_TOKEN"]


def has_keys(names: list[str]) -> bool:
    missing = [n for n in names if not os.getenv(n)]
    if missing:
        print(f"warning: skipping, missing {', '.join(missing)}", file=sys.stderr)
    return not missing

X_LIMIT = 280


# ---------- GitHub ----------
def gh_get(url: str, **params):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "daily-post-bot"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    r = requests.get(url, headers=headers, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def gh_text(url: str) -> str:
    headers = {"Accept": "application/vnd.github.raw+json", "User-Agent": "daily-post-bot"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    r = requests.get(url, headers=headers, timeout=15)
    r.raise_for_status()
    return r.text


MAX_REPOS = 8
README_CHARS = 1500
BOT_REPO = os.getenv("GITHUB_REPOSITORY", "")  # this bot's own repo; its daily commits aren't news


def clean_readme(md: str) -> str:
    md = re.sub(r"<[^>]+>", " ", md)  # html tags
    md = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", md)  # images / badges
    md = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", md)  # links -> text
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip()[:README_CHARS]


def repo_context(full_name: str, since: dt.datetime) -> dict | None:
    """what the repo is (description, language, readme) + my commits in the window."""
    try:
        info = gh_get(f"https://api.github.com/repos/{full_name}")
    except requests.RequestException:
        return None
    if info.get("private") and not INCLUDE_PRIVATE_REPOS:
        return None
    if excluded(full_name, info.get("description") or ""):
        return None
    try:
        commits = gh_get(
            f"https://api.github.com/repos/{full_name}/commits",
            since=since.isoformat(),
            author=GITHUB_USERNAME,
            per_page=30,
        )
        messages = [c["commit"]["message"].splitlines()[0] for c in commits]
    except requests.RequestException:
        messages = []
    private = bool(info.get("private"))
    readme = ""
    if not private:  # private readmes can hold client details; don't send them anywhere
        try:
            readme = clean_readme(gh_text(f"https://api.github.com/repos/{full_name}/readme"))
        except requests.RequestException:
            pass
    return {
        "name": full_name,
        "private": private,
        "mine": info["owner"]["login"].lower() == GITHUB_USERNAME.lower(),
        "description": info.get("description") or "",
        "language": info.get("language") or "",
        "topics": info.get("topics") or [],
        "url": info.get("html_url", ""),
        "commits": messages,
        "readme": readme,
    }


def fetch_github_activity() -> dict:
    """
    returns {
      "repos":  [repo_context...]   repos i worked on (or my own repos i touched), with real context
      "events": [str...]            prs, issues, releases, new repos
      "starred": [str...]           other people's repos i starred/forked (names only)
    }
    """
    if not GITHUB_USERNAME:
        sys.exit("GH_USERNAME is not set")
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=LOOKBACK_HOURS)
    me = GITHUB_USERNAME.lower()
    events = gh_get(f"https://api.github.com/users/{GITHUB_USERNAME}/events", per_page=100)

    touched: list[str] = []
    lines: list[str] = []
    starred: list[str] = []
    for ev in events:
        created = dt.datetime.fromisoformat(ev["created_at"].replace("Z", "+00:00"))
        if created < since:
            continue
        if not ev.get("public", True) and not INCLUDE_PRIVATE_REPOS:
            continue
        repo = ev["repo"]["name"]
        p = ev.get("payload", {})
        t = ev["type"]
        mine = repo.split("/")[0].lower() == me

        if repo == BOT_REPO and t != "PushEvent":
            continue  # the bot's own daily issues
        if t in ("WatchEvent", "ForkEvent"):
            if mine:
                touched.append(repo)  # starring your own repo = it's your project
            else:
                starred.append(repo)
            continue
        touched.append(repo)
        if t == "PullRequestEvent":
            pr = p.get("pull_request", {})
            action = "merged" if p.get("action") == "closed" and pr.get("merged") else p.get("action")
            lines.append(f"PR {action} in {repo}: {pr.get('title', '')}")
        elif t == "IssuesEvent":
            lines.append(f"Issue {p.get('action')} in {repo}: {p.get('issue', {}).get('title', '')}")
        elif t == "CreateEvent" and p.get("ref_type") == "repository":
            lines.append(f"Created a new repo: {repo}")
        elif t == "CreateEvent" and p.get("ref_type") == "tag":
            lines.append(f"Tagged {p.get('ref')} in {repo}")
        elif t == "ReleaseEvent":
            rel = p.get("release", {})
            lines.append(f"Released {rel.get('tag_name')} of {repo}: {rel.get('name') or ''}")
        elif t == "PullRequestReviewEvent":
            lines.append(f"Reviewed a PR in {repo}: {p.get('pull_request', {}).get('title', '')}")
        elif t == "PublicEvent":
            lines.append(f"Open-sourced {repo}")

    # the events feed can miss pushes, so also check repos pushed in the window.
    # with a personal token this includes private repos and org repos too.
    try:
        try:
            listing = gh_get(
                "https://api.github.com/user/repos",
                sort="pushed",
                per_page=30,
                affiliation="owner,collaborator,organization_member",
            )
        except requests.RequestException:  # actions' built-in token can't use /user/repos
            listing = gh_get(f"https://api.github.com/users/{GITHUB_USERNAME}/repos", sort="pushed", per_page=30)
        for r in listing:
            pushed = dt.datetime.fromisoformat(r["pushed_at"].replace("Z", "+00:00"))
            if pushed >= since:
                touched.append(r["full_name"])
    except requests.RequestException as e:
        print(f"warning: could not list repos: {e}", file=sys.stderr)

    repos = []
    for name in list(dict.fromkeys(touched)):
        if len(repos) >= MAX_REPOS:
            break
        if excluded(name):
            continue
        ctx = repo_context(name, since)
        if not ctx:
            continue
        if ctx["private"] and not ctx["commits"]:
            continue  # private + no commits from me = nothing to say
        if name == BOT_REPO and not ctx["commits"]:
            continue
        repos.append(ctx)

    # keep event lines only for repos we kept, and never show private repo names
    kept = []
    for line in dict.fromkeys(lines):
        repo = next((r for r in repos if r["name"] in line), None)
        if repo:
            kept.append(line.replace(repo["name"], "a private repo") if repo["private"] else line)
    return {"repos": repos, "events": kept, "starred": list(dict.fromkeys(starred))}


def has_real_work(activity: dict) -> bool:
    return bool(activity["events"] or any(r["commits"] for r in activity["repos"]))


def describe_repo(r: dict) -> str:
    lang = f" ({r['language']})" if r["language"] else ""
    if r["private"]:
        out = [
            f"### a PRIVATE project{lang}",
            "rules for this one: never name the repo, the company, the client, or the product. "
            "describe the kind of work only (e.g. 'migrating a legacy php app to laravel'). no link",
            f"private description (for your understanding only, don't quote it): {r['description'] or '(none)'}",
        ]
    else:
        out = [f"### {r['name']}{lang}", f"what it is: {r['description'] or '(no description)'}"]
        if r["topics"]:
            out.append("topics: " + ", ".join(r["topics"]))
        out.append(f"url: {r['url']}")
    if r["commits"]:
        out.append(f"my commits in the last day ({len(r['commits'])}):\n- " + "\n- ".join(r["commits"][:15]))
    else:
        out.append("no new commits from me in the last day")
    if r["readme"]:
        out.append(f"readme excerpt:\n{r['readme']}")
    return "\n".join(out)


# ---------- Tech news (Hacker News, no key needed) ----------
def fetch_tech_news(limit: int = 8) -> list[str]:
    try:
        ids = requests.get(
            "https://hacker-news.firebaseio.com/v0/topstories.json", timeout=20
        ).json()[:30]
        stories = []
        for i in ids:
            s = requests.get(
                f"https://hacker-news.firebaseio.com/v0/item/{i}.json", timeout=20
            ).json()
            if s and s.get("type") == "story" and s.get("url") and s.get("score", 0) > 100:
                stories.append(f"{s['title']} ({s['url']}) [{s['score']} points]")
            if len(stories) >= limit:
                break
        return stories
    except Exception as e:  # news is optional; never fail the run over it
        print(f"warning: could not fetch news: {e}", file=sys.stderr)
        return []


# ---------- history (avoid repeating yourself) ----------
def load_history() -> list[dict]:
    if HISTORY_FILE.exists():
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    return []


def save_history(history: list[dict]) -> None:
    HISTORY_FILE.write_text(json.dumps(history[-30:], indent=2, ensure_ascii=False), encoding="utf-8")


# ---------- Groq ----------
SYSTEM_PROMPT = """You ghostwrite social media posts for {bio}. The posts must sound
exactly like them, not like an AI and not like a typical linkedin influencer.

Their voice guide and writing samples:
-----
{style}
-----

Every day you write THREE options, and the author picks which one to post:
1. "work": about what they built. tell ONE story: what the thing is, what they did to it,
   what's funny or hard about it. never walk through commits one by one, 20 commits =
   "spent the night doing X". if the context has no work at all, set "work" to null
2. "news": their hot take on ONE of today's news stories. pick the one a developer would
   have the funniest or strongest opinion on, ideally one connected to what they build.
   say what happened in a few words so people get it. if there's no news, set "news" to null
3. "meme": a meme about their work or the news (whichever is funnier) with a short caption

How to write a good post:
- if people may not know the project, say what it is in one plain line early on
  (e.g. "it's a gpu i designed from scratch that draws triangles on a $10 fpga")
- funny. dry, deadpan, self-aware, short punchlines, like the best tech twitter posts:
  understatement, absurd comparisons, roasting my own code or decisions. be bold, not cringe
- no "thrilled to share", no inspirational lessons, no "here's what i learned"
- the three options should feel different from each other, not the same joke three times

Hard rules (facts):
- first person. every fact about a project must come from the context given (description,
  readme, commits). never invent features, numbers, results, time saved, or tech it uses.
  jokes and exaggeration are fine as long as they're obviously jokes, not fake facts
- repos listed under "my repos" are the author's OWN projects. talk about them as things they
  built ("i built", "i'm working on"), never as something they found or starred
- PRIVATE projects: never name the repo, company, client, or product. only the kind of work
- only say they did something if a commit, pr, or release shows it
- don't mention repo names that aren't in the context
- x posts must NOT contain any links or urls (links cost extra on x). linkedin may have one link

Lengths:
- x: under 200 characters is ideal, MUST be under 260. 0-1 hashtags
- linkedin: 400-900 characters. hook line first, short lines with blank lines between,
  end with a thought, question, or punchline. 2-4 hashtags at the end
- meme captions are shorter: x under 120 characters, linkedin 1-3 short lines + 1-2 hashtags

Meme templates (pick one id, follow its line format, max ~8 words per line):
{memes}

Return ONLY valid JSON in exactly this shape:
{{
  "work": {{"x": "...", "linkedin": "...", "topic": "3-8 word summary"}} or null,
  "news": {{"x": "...", "linkedin": "...", "topic": "3-8 word summary"}} or null,
  "meme": {{"template": "<id>", "lines": ["...", "..."], "x": "...", "linkedin": "...", "topic": "3-8 word summary"}}
}}"""

MEME_TEMPLATES = {
    "drake": "Drakeposting: line1 = thing rejected, line2 = thing preferred",
    "db": "Distracted Boyfriend: line1 = new shiny thing, line2 = me, line3 = what i should be doing",
    "fine": "This is Fine (dog in burning room): line1 = situation, line2 = 'this is fine' style reaction",
    "gru": "Gru's Plan: 4 lines, step 1, step 2, step 3 (goes wrong), step 4 (same as 3, realization)",
    "pigeon": "Is This a Pigeon: line1 = me, line2 = the thing, line3 = 'is this X?' misidentification",
    "astronaut": "Always Has Been: line1/2 = 'wait it's all X?', line3/4 = 'always has been'",
    "spongebob": "Mocking Spongebob: line1 = normal statement, line2 = mocking version",
    "buzz": "X, X Everywhere: line1 = 'X', line2 = 'X everywhere'",
    "rollsafe": "Roll Safe (tapping head): line1 = setup, line2 = galaxy-brained 'smart' solution",
    "harold": "Hide the Pain Harold: line1 = setup, line2 = smiling through pain",
    "both": "Why Not Both: line1 = choice question, line2 = 'why not both?'",
    "stonks": "Stonks: line1 = dumb decision, line2 = 'stonks'",
    "pooh": "Tuxedo Pooh: line1 = normal way to say it, line2 = fancy way",
    "ds": "Daily Struggle (two buttons, sweating): line1 = button 1, line2 = button 2, line3 = me",
    "gb": "Galaxy Brain: 4 lines, increasingly 'enlightened' (absurd) ideas",
    "mordor": "One Does Not Simply: line1 = 'one does not simply', line2 = the hard thing",
    "success": "Success Kid: line1 = setup, line2 = small win",
    "fry": "Futurama Fry (squinting): line1 = 'not sure if X', line2 = 'or just Y'",
    "same": "They're the Same Picture: line1 = thing A, line2 = thing B, line3 = 'they're the same picture'",
    "exit": "Left Exit 12: line1 = sensible road, line2 = chaotic exit, line3 = me swerving",
    "handshake": "Epic Handshake: line1 = group A, line2 = group B, line3 = what they agree on",
    "spiderman": "Spider-Man Pointing: line1 = thing A, line2 = thing B (they're the same)",
    "woman-cat": "Woman Yelling at Cat: line1 = angry accusation, line2 = smug cat reply",
    "bihw": "But It's Honest Work: line1 = tiny achievement, line2 = 'but it's honest work'",
    "wonka": "Condescending Wonka: line1 = 'oh you X?', line2 = 'tell me more about Y'",
    "millers": "You Guys Are Getting Paid?: 4 lines, setup between two characters, last = shocked question",
}

MEME_LIST = "\n".join(f"  {k}: {v}" for k, v in MEME_TEMPLATES.items())


def meme_url(meme: dict | None) -> str | None:
    """free meme images from memegen.link (no key needed)."""
    if not meme or meme.get("template") not in MEME_TEMPLATES or not meme.get("lines"):
        return None

    def enc(s: str) -> str:
        s = str(s).strip() or " "
        for a, b in [("_", "__"), ("-", "--"), (" ", "_"), ("?", "~q"), ("%", "~p"), ("#", "~h"),
                     ("/", "~s"), ("\\", "~b"), ("&", "~a"), ('"', "''"), ("\n", "~n")]:
            s = s.replace(a, b)
        return requests.utils.quote(s, safe="_~'")

    return f"https://api.memegen.link/images/{meme['template']}/" + "/".join(
        enc(l) for l in meme["lines"][:8]
    ) + ".png"


def load_style() -> str:
    return STYLE_FILE.read_text(encoding="utf-8") if STYLE_FILE.exists() else ""


def lowercase_voice(text: str) -> str:
    """lowercase everything except urls and ALL-CAPS 'screaming' words (4+ letters)."""
    keep = re.compile(r"(https?://\S+|\b[A-Z]{4,}\b)")
    parts = keep.split(text)
    return "".join(part if i % 2 else part.lower() for i, part in enumerate(parts))


def strip_links(text: str) -> str:
    text = re.sub(r"\s*https?://\S+", "", text)
    text = re.sub(r"\s*\b[\w-]+\.(?:com|io|dev|ai|org|net|app|co|sh)\b(?:/\S*)?", "", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def build_user_prompt(activity: dict, news: list[str], history: list[dict]) -> str:
    parts = [f"Today is {dt.date.today():%A, %B %d, %Y}."]
    repos = activity["repos"]
    if repos and has_real_work(activity):
        parts.append(
            "WORK: here's what I worked on in the last day. for the work option, pick the most "
            "interesting thread (usually the project with the most meaningful commits)."
        )
    elif repos:
        parts.append(
            "WORK: no new commits today. for the work option, spotlight one of my projects below: "
            "what it is, why it exists, or what's hard/interesting about it, using only the facts given."
        )
    else:
        parts.append("WORK: nothing on GitHub in the last day, so set \"work\" to null.")
    if repos:
        parts.append(
            "My repos (these are MY projects, I built them) with real context:\n\n"
            + "\n\n".join(describe_repo(r) for r in repos)
        )
    if activity["events"]:
        parts.append("Other GitHub activity:\n- " + "\n- ".join(activity["events"][:15]))
    if activity["starred"]:
        parts.append(
            "Other people's repos I starred (names only. you know nothing else about them, so never "
            "describe them or say I used them. skip unless it fits naturally):\n- "
            + "\n- ".join(activity["starred"][:10])
        )
    if news:
        parts.append("NEWS: today's top tech stories (from hacker news):\n- " + "\n- ".join(news))
    else:
        parts.append("NEWS: couldn't fetch news today, so set \"news\" to null.")
    if history:
        recent = "\n- ".join(h["topic"] for h in history[-10:])
        parts.append(
            "Recent post topics, ONLY so you don't repeat the same angle or opening. these are not "
            f"facts, and may be wrong:\n- {recent}"
        )
    return "\n\n".join(parts)


GROQ_URL = "https://api.groq.com/openai/v1"
# best first. groq retires models over time, so the bot checks what's actually
# available and uses the first match (or any general chat model as a last resort).
PREFERRED_MODELS = [
    "openai/gpt-oss-120b",
    "moonshotai/kimi-k2-instruct-0905",
    "moonshotai/kimi-k2-instruct",
    "llama-3.3-70b-versatile",
    "meta-llama/llama-4-maverick-17b-128e-instruct",
    "qwen/qwen3-32b",
    "openai/gpt-oss-20b",
]
NOT_CHAT = ("whisper", "tts", "guard", "playai", "orpheus", "distil", "compound", "prompt")


def available_models() -> list[str]:
    r = requests.get(
        f"{GROQ_URL}/models", headers={"Authorization": f"Bearer {GROQ_API_KEY}"}, timeout=30
    )
    r.raise_for_status()
    return [m["id"] for m in r.json().get("data", []) if m.get("active", True)]


def pick_models() -> list[str]:
    """models to try, in order: your GROQ_MODEL (if set), then the best available ones."""
    try:
        avail = available_models()
    except requests.RequestException as e:
        print(f"warning: could not list groq models: {e}", file=sys.stderr)
        avail = []
    ordered = [m for m in PREFERRED_MODELS if m in avail]
    ordered += [m for m in avail if m not in ordered and not any(k in m.lower() for k in NOT_CHAT)]
    if GROQ_MODEL:
        ordered = [GROQ_MODEL] + [m for m in ordered if m != GROQ_MODEL]
    return ordered or PREFERRED_MODELS


def parse_json(text: str) -> dict:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if not match:
            raise
        return json.loads(match.group(0))


OPTION_KEYS = ("work", "news", "meme")
OPTION_LABELS = {"work": "my work", "news": "news take", "meme": "meme"}


def valid_option(opt) -> bool:
    return isinstance(opt, dict) and bool(opt.get("x")) and bool(opt.get("linkedin"))


def call_groq(model: str, user_prompt: str, json_mode: bool = True) -> dict:
    body = {
        "model": model,
        "temperature": 0.9,
        "messages": [
            {
                "role": "system",
                "content": SYSTEM_PROMPT.format(bio=AUTHOR_BIO, style=load_style(), memes=MEME_LIST),
            },
            {"role": "user", "content": user_prompt},
        ],
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    r = requests.post(
        f"{GROQ_URL}/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
        json=body,
        timeout=180,
    )
    if r.status_code != 200:
        raise RuntimeError(f"{r.status_code}: {r.text[:300]}")
    data = parse_json(r.json()["choices"][0]["message"]["content"])
    if not any(valid_option(data.get(k)) for k in OPTION_KEYS):
        raise RuntimeError("model reply had no usable options")
    return data


def generate_options(user_prompt: str) -> dict:
    """returns {"work": {...}, "news": {...}, "meme": {...}}, leaving out any that came back empty."""
    if not GROQ_API_KEY:
        sys.exit("GROQ_API_KEY is not set")
    data, errors = None, []
    for model in pick_models()[:5]:
        for json_mode in (True, False):  # some models don't support json mode
            try:
                data = call_groq(model, user_prompt, json_mode)
                print(f"model: {model}")
                break
            except Exception as e:
                errors.append(f"{model} (json_mode={json_mode}): {e}")
        if data:
            break
    if not data:
        sys.exit("every groq model failed:\n" + "\n".join(errors))

    options = {}
    for key in OPTION_KEYS:
        opt = data.get(key)
        if not valid_option(opt):
            continue
        if LOWERCASE:
            opt["x"] = lowercase_voice(opt["x"])
            opt["linkedin"] = lowercase_voice(opt["linkedin"])
        opt["x"] = fit_x(strip_links(opt["x"]))
        if key == "meme":
            opt["meme_url"] = meme_url(opt)
            if not opt["meme_url"]:
                continue  # a meme option without a valid meme is pointless
        options[key] = opt
    return options


def fit_x(text: str) -> str:
    text = text.strip()
    if len(text) <= X_LIMIT:
        return text
    # drop hashtags first, then trim on a word boundary
    text = re.sub(r"\s#\w+", "", text).strip()
    if len(text) <= X_LIMIT:
        return text
    return text[: X_LIMIT - 1].rsplit(" ", 1)[0] + "…"


# ---------- publishing (optional) ----------
def post_to_x(text: str, image_url: str | None = None) -> str:
    import tempfile

    import tweepy

    keys = dict(
        consumer_key=os.environ["X_API_KEY"],
        consumer_secret=os.environ["X_API_SECRET"],
        access_token=os.environ["X_ACCESS_TOKEN"],
        access_token_secret=os.environ["X_ACCESS_TOKEN_SECRET"],
    )
    client = tweepy.Client(**keys)
    media_ids = None
    if image_url:
        try:
            img = requests.get(image_url, timeout=60)
            img.raise_for_status()
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                f.write(img.content)
            api = tweepy.API(tweepy.OAuth1UserHandler(*keys.values()))
            media_ids = [api.media_upload(f.name).media_id]
        except Exception as e:  # meme is a bonus; still post the text
            print(f"warning: meme upload failed, posting text only: {e}", file=sys.stderr)
    resp = client.create_tweet(text=text, media_ids=media_ids)
    return f"https://x.com/i/web/status/{resp.data['id']}"


def option_section(n: int, key: str, opt: dict, x: bool, linkedin: bool) -> str:
    from urllib.parse import quote

    out = [f"## option {n}: {OPTION_LABELS[key]}"]
    if opt.get("meme_url"):
        out.append(
            f"![meme]({opt['meme_url']})\n\n"
            "long-press (or right-click) to save the meme, then attach it when you post."
        )
    if x:
        link = f"https://x.com/intent/post?text={quote(opt['x'])}"
        out.append(f"**[tap here to post on x]({link})**\n\n{opt['x']}")
    if linkedin:
        link = f"https://www.linkedin.com/feed/?shareActive=true&text={quote(opt['linkedin'])}"
        out.append(
            f"**[tap here to post on linkedin]({link})**\n\n"
            "if the app opens without the text filled in, copy it from here:\n\n"
            f"```\n{opt['linkedin']}\n```"
        )
    return "\n\n".join(out)


def open_options_issue(options: dict, x: bool, linkedin: bool) -> str:
    """one github issue a day with every option and one-tap links for each platform."""
    repo = os.environ["GITHUB_REPOSITORY"]  # set automatically in github actions
    token = os.environ["ISSUE_TOKEN"]
    sections = [option_section(i, k, o, x, linkedin) for i, (k, o) in enumerate(options.items(), 1)]
    body = (
        "pick whichever you like (or post more than one). each link opens the app with the "
        "post filled in, you just hit post.\n\n---\n\n"
        + "\n\n---\n\n".join(sections)
        + "\n\n_close this issue once posted_"
    )
    labels = " / ".join(OPTION_LABELS[k] for k in options)
    r = requests.post(
        f"https://api.github.com/repos/{repo}/issues",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        json={
            "title": f"today's posts ({labels}): {dt.date.today():%b %d}",
            "body": body,
            # assigning the repo owner makes github always send a notification
            # (email + mobile push), even if they aren't watching the repo
            "assignees": [os.getenv("GITHUB_REPOSITORY_OWNER") or repo.split("/")[0]],
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["html_url"]


def post_to_linkedin(text: str) -> str:
    token = os.environ["LINKEDIN_ACCESS_TOKEN"]
    person = os.getenv("LINKEDIN_PERSON_URN")  # urn:li:person:xxxx; looked up if missing
    if not person:
        me = requests.get(
            "https://api.linkedin.com/v2/userinfo",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        me.raise_for_status()
        person = f"urn:li:person:{me.json()['sub']}"
    r = requests.post(
        "https://api.linkedin.com/v2/ugcPosts",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Restli-Protocol-Version": "2.0.0",
            "Content-Type": "application/json",
        },
        json={
            "author": person,
            "lifecycleState": "PUBLISHED",
            "specificContent": {
                "com.linkedin.ugc.ShareContent": {
                    "shareCommentary": {"text": text},
                    "shareMediaCategory": "NONE",
                }
            },
            "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"},
        },
        timeout=30,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"LinkedIn error {r.status_code}: {r.text}")
    return r.headers.get("x-restli-id", "posted")


# ---------- main ----------
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print only; don't save or post")
    args = ap.parse_args()

    activity = fetch_github_activity()
    news = fetch_tech_news()
    history = load_history()
    print(
        f"repos: {[('(private)' if r['private'] else r['name']) for r in activity['repos']]} | "
        f"real work: {has_real_work(activity)} | events: {len(activity['events'])} | news: {len(news)}"
    )
    options = generate_options(build_user_prompt(activity, news, history))

    today = dt.date.today().isoformat()
    md = f"# Posts for {today}\n"
    for i, (key, opt) in enumerate(options.items(), 1):
        md += f"\n## Option {i}: {OPTION_LABELS[key]} ({opt.get('topic', '')})\n\n"
        if opt.get("meme_url"):
            md += f"![meme]({opt['meme_url']})\n\n"
        md += f"### X ({len(opt['x'])}/{X_LIMIT})\n\n{opt['x']}\n\n### LinkedIn\n\n{opt['linkedin']}\n"
    print("\n" + md)
    if args.dry_run:
        return

    POSTS_DIR.mkdir(exist_ok=True)
    (POSTS_DIR / f"{today}.md").write_text(md, encoding="utf-8")
    for opt in options.values():
        history.append({"date": today, "topic": opt.get("topic", "")})
    save_history(history)

    failures = 0
    # api modes post one option automatically: AUTO_OPTION (default work), else the first one
    pick = options.get(AUTO_OPTION) or next(iter(options.values()))
    if AUTO_POST_X and X_MODE == "api" and has_keys(X_KEYS):
        try:
            print("X:", post_to_x(pick["x"], pick.get("meme_url")))
        except Exception as e:
            failures += 1
            print(f"X post failed: {e}", file=sys.stderr)
    if AUTO_POST_LINKEDIN and LINKEDIN_MODE == "api" and has_keys(LINKEDIN_KEYS):
        try:
            print("LinkedIn:", post_to_linkedin(pick["linkedin"]))
        except Exception as e:
            failures += 1
            print(f"LinkedIn post failed: {e}", file=sys.stderr)

    manual_x = AUTO_POST_X and X_MODE == "manual"
    manual_li = AUTO_POST_LINKEDIN and LINKEDIN_MODE == "manual"
    if (manual_x or manual_li) and has_keys(ISSUE_KEYS):
        try:
            print("Options issue:", open_options_issue(options, manual_x, manual_li))
        except Exception as e:
            failures += 1
            print(f"Could not open the options issue: {e}", file=sys.stderr)

    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
