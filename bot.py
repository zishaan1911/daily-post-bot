"""
Daily social post bot.

Reads your recent GitHub activity (and sometimes tech news from Hacker News),
asks a Groq-hosted LLM to write one LinkedIn post and one X post, publishes them
automatically, and keeps a copy under posts/.

Run:  python bot.py            # generate and publish today's posts
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
NEWS_PROBABILITY = float(os.getenv("NEWS_PROBABILITY") or 0.3)  # chance to weave in tech news
INCLUDE_PRIVATE_REPOS = os.getenv("INCLUDE_PRIVATE_REPOS", "false").lower() == "true"
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


MAX_COMPARE_CALLS = 12
_compare_calls = 0


def commit_messages_for_push(repo: str, payload: dict) -> list[str]:
    # GitHub's events feed no longer includes commit messages, so look them up
    # with the compare API (capped, to stay fast and inside rate limits).
    global _compare_calls
    commits = payload.get("commits")
    if commits:
        return [c["message"].splitlines()[0] for c in commits]
    before, head = payload.get("before"), payload.get("head")
    if not (before and head) or set(before) == {"0"} or _compare_calls >= MAX_COMPARE_CALLS:
        return []
    _compare_calls += 1
    try:
        data = gh_get(f"https://api.github.com/repos/{repo}/compare/{before}...{head}")
        return [c["commit"]["message"].splitlines()[0] for c in data.get("commits", [])]
    except requests.RequestException:
        return []


def fetch_github_activity() -> list[str]:
    if not GITHUB_USERNAME:
        sys.exit("GH_USERNAME is not set")
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=LOOKBACK_HOURS)
    events = gh_get(f"https://api.github.com/users/{GITHUB_USERNAME}/events", per_page=100)

    lines: list[str] = []
    for ev in events:
        created = dt.datetime.fromisoformat(ev["created_at"].replace("Z", "+00:00"))
        if created < since:
            continue
        if not ev.get("public", True) and not INCLUDE_PRIVATE_REPOS:
            continue
        repo = ev["repo"]["name"]
        p = ev.get("payload", {})
        t = ev["type"]

        if t == "PushEvent":
            msgs = commit_messages_for_push(repo, p)
            branch = p.get("ref", "").removeprefix("refs/heads/")
            if msgs:
                lines.append(f"Pushed {len(msgs)} commit(s) to {repo} ({branch}): " + "; ".join(msgs[:8]))
            else:
                lines.append(f"Pushed to {repo} ({branch})")
        elif t == "PullRequestEvent":
            pr = p.get("pull_request", {})
            action = "merged" if p.get("action") == "closed" and pr.get("merged") else p.get("action")
            lines.append(f"PR {action} in {repo}: {pr.get('title', '')}")
        elif t == "IssuesEvent":
            lines.append(f"Issue {p.get('action')} in {repo}: {p.get('issue', {}).get('title', '')}")
        elif t == "CreateEvent":
            if p.get("ref_type") == "repository":
                desc = p.get("description") or ""
                lines.append(f"Created new repo {repo}. {desc}".strip())
            elif p.get("ref_type") == "tag":
                lines.append(f"Tagged {p.get('ref')} in {repo}")
        elif t == "ReleaseEvent":
            rel = p.get("release", {})
            lines.append(f"Released {rel.get('tag_name')} of {repo}: {rel.get('name') or ''}")
        elif t == "WatchEvent":
            lines.append(f"Starred {repo}")
        elif t == "ForkEvent":
            lines.append(f"Forked {repo}")
        elif t == "PullRequestReviewEvent":
            lines.append(f"Reviewed a PR in {repo}: {p.get('pull_request', {}).get('title', '')}")
        elif t == "PublicEvent":
            lines.append(f"Open-sourced {repo}")

    # de-dupe, keep order
    return list(dict.fromkeys(lines))


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

Hard rules:
- first person. never invent work, numbers, or results that aren't in the provided activity
- don't mention private repo names unless they appear in the activity list
- the x post must NOT contain any links or urls (links cost extra on x). linkedin may have one link

Return ONLY valid JSON with exactly these keys:
{{
  "linkedin": "linkedin post, 400-1000 characters, short lines with blank lines between, first line makes people want to read on, ends with a thought or question, 2-4 hashtags at the end",
  "x": "x post, MUST be under 260 characters, no links, 0-1 hashtags",
  "topic": "3-8 word summary of what this post is about"
}}"""


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


def build_user_prompt(activity: list[str], news: list[str], history: list[dict]) -> str:
    parts = [f"Today is {dt.date.today():%A, %B %d, %Y}."]
    if activity:
        parts.append("My GitHub activity in the last day:\n- " + "\n- ".join(activity[:25]))
    else:
        parts.append("I had no public GitHub activity in the last day.")
    if news:
        parts.append(
            "Trending tech news (pick at most ONE that genuinely relates to my work or that a "
            "developer would have a real opinion on; tie it back to what I'm building if possible):\n- "
            + "\n- ".join(news)
        )
    if history:
        recent = "\n- ".join(h["topic"] for h in history[-10:])
        parts.append(f"Recent post topics (do NOT repeat these angles or openings):\n- {recent}")
    if not activity and not news:
        parts.append("Write a reflective post about a lesson from building software recently.")
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


def call_groq(model: str, user_prompt: str, json_mode: bool = True) -> dict:
    body = {
        "model": model,
        "temperature": 0.8,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT.format(bio=AUTHOR_BIO, style=load_style())},
            {"role": "user", "content": user_prompt},
        ],
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    r = requests.post(
        f"{GROQ_URL}/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
        json=body,
        timeout=120,
    )
    if r.status_code != 200:
        raise RuntimeError(f"{r.status_code}: {r.text[:300]}")
    data = parse_json(r.json()["choices"][0]["message"]["content"])
    if not (data.get("linkedin") and data.get("x")):
        raise RuntimeError("model reply was missing the linkedin or x post")
    return data


def generate_posts(user_prompt: str) -> dict:
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
    if LOWERCASE:
        data["linkedin"] = lowercase_voice(data["linkedin"])
        data["x"] = lowercase_voice(data["x"])
    data["x"] = fit_x(strip_links(data["x"]))
    return data


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
def post_to_x(text: str) -> str:
    import tweepy

    client = tweepy.Client(
        consumer_key=os.environ["X_API_KEY"],
        consumer_secret=os.environ["X_API_SECRET"],
        access_token=os.environ["X_ACCESS_TOKEN"],
        access_token_secret=os.environ["X_ACCESS_TOKEN_SECRET"],
    )
    resp = client.create_tweet(text=text)
    return f"https://x.com/i/web/status/{resp.data['id']}"


def x_manual_section(text: str) -> str:
    from urllib.parse import quote

    link = f"https://x.com/intent/post?text={quote(text)}"
    return f"## x\n\n**[tap here to post on x]({link})**\n\n{text}"


def linkedin_manual_section(text: str) -> str:
    from urllib.parse import quote

    link = f"https://www.linkedin.com/feed/?shareActive=true&text={quote(text)}"
    return (
        f"## linkedin\n\n**[tap here to post on linkedin]({link})**\n\n"
        "if the app opens without the text filled in, copy it from here:\n\n"
        f"```\n{text}\n```"
    )


def open_manual_issue(sections: list[str], platforms: list[str]) -> str:
    """free mode: one github issue a day with one-tap links for each platform."""
    repo = os.environ["GITHUB_REPOSITORY"]  # set automatically in github actions
    token = os.environ["ISSUE_TOKEN"]
    body = (
        "each link opens the app with the post filled in, you just hit post.\n\n"
        + "\n\n---\n\n".join(sections)
        + "\n\n_close this issue once posted_"
    )
    r = requests.post(
        f"https://api.github.com/repos/{repo}/issues",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        json={"title": f"post on {' + '.join(platforms)}: {dt.date.today():%b %d}", "body": body},
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
    ap.add_argument("--news", action="store_true", help="force a tech-news angle today")
    args = ap.parse_args()

    activity = fetch_github_activity()
    use_news = args.news or not activity or random.random() < NEWS_PROBABILITY
    news = fetch_tech_news() if use_news else []
    history = load_history()

    print(f"GitHub items: {len(activity)} | news angle: {bool(news)}")
    posts = generate_posts(build_user_prompt(activity, news, history))

    today = dt.date.today().isoformat()
    md = (
        f"# Posts for {today}\n\n_Topic: {posts.get('topic', '')}_\n\n"
        f"## LinkedIn\n\n{posts['linkedin']}\n\n"
        f"## X ({len(posts['x'])}/{X_LIMIT})\n\n{posts['x']}\n"
    )
    print("\n" + md)
    if args.dry_run:
        return

    POSTS_DIR.mkdir(exist_ok=True)
    (POSTS_DIR / f"{today}.md").write_text(md, encoding="utf-8")
    history.append({"date": today, "topic": posts.get("topic", ""), "x": posts["x"]})
    save_history(history)

    failures = 0
    manual_sections, manual_platforms = [], []

    if AUTO_POST_X and X_MODE == "manual":
        manual_sections.append(x_manual_section(posts["x"]))
        manual_platforms.append("x")
    elif AUTO_POST_X and X_MODE == "api" and has_keys(X_KEYS):
        try:
            print("X:", post_to_x(posts["x"]))
        except Exception as e:
            failures += 1
            print(f"X post failed: {e}", file=sys.stderr)

    if AUTO_POST_LINKEDIN and LINKEDIN_MODE == "manual":
        manual_sections.append(linkedin_manual_section(posts["linkedin"]))
        manual_platforms.append("linkedin")
    elif AUTO_POST_LINKEDIN and LINKEDIN_MODE == "api" and has_keys(LINKEDIN_KEYS):
        try:
            print("LinkedIn:", post_to_linkedin(posts["linkedin"]))
        except Exception as e:
            failures += 1
            print(f"LinkedIn post failed: {e}", file=sys.stderr)

    if manual_sections and has_keys(ISSUE_KEYS):
        try:
            print("One-tap links:", open_manual_issue(manual_sections, manual_platforms))
        except Exception as e:
            failures += 1
            print(f"Could not open the one-tap issue: {e}", file=sys.stderr)

    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
