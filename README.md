# Daily Post Bot

Every day this bot:

1. Reads your GitHub activity from the last ~day: commits in public, private and org repos, plus PRs, releases and new repos. It also reads each project's description and README.
2. Grabs today's top tech stories from Hacker News
3. Asks a Groq model for **three options**, each with an X post and a LinkedIn post in your voice:
   1. **my work**: a story about what you built
   2. **news take**: your hot take on one of today's stories
   3. **meme**: a meme image plus a short caption
4. Opens one GitHub issue with all three and one-tap links (or auto-posts one in API mode), and saves a copy to `posts/YYYY-MM-DD.md`. It keeps `history.json` so it doesn't repeat itself.

It runs on GitHub Actions. You don't need a server.

## Cost

| Part | Cost |
|---|---|
| Groq, GitHub Actions, Hacker News, LinkedIn | free |
| X with `X_MODE=api` (default) | about $0.015 per post, roughly $0.45 a month. The bot strips links from X posts because posts with links cost about $0.20. |
| `X_MODE=manual` / `LINKEDIN_MODE=manual` | **free, and you don't need any X or LinkedIn keys.** Each morning the bot opens one GitHub issue with a "tap here to post" link for each platform. Tap it, the app opens with the post filled in, and you hit post. The LinkedIn link fills in the text on the website; if the mobile app opens it empty, copy the text from the issue. Set the repo to Watch → All activity, and install the GitHub mobile app to get a phone notification. |

## Your voice

Everything about how the posts sound lives in [`style.md`](style.md): lowercase, short, no corporate tone, CAPS only for screaming. **Paste 3–5 of your real posts into the "sample posts" section.** This is what improves the voice the most.

The bot also lowercases the final text (`LOWERCASE=true`). It leaves links and words in ALL CAPS of 4+ letters as they are, so "it FINALLY works" stays loud.

## Setup (about 15 minutes)

### 1. Put the code in a GitHub repo
Make a new repo (private is fine) and push these files to it.

### 2. Get the keys

| Key | Where to get it |
|---|---|
| `GROQ_API_KEY` | https://console.groq.com/keys |
| `GH_ACTIVITY_TOKEN` (needed for private work) | See **Private repos** below. Without it the bot only sees public repos. |
| `X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_TOKEN_SECRET` | https://developer.x.com → create a Project + App → under *User authentication settings* set permissions to **Read and write**, **then** generate the Access Token & Secret (tokens made before switching to write access can't post). Check X's current API pricing; posting may need paid credits. |
| `LINKEDIN_ACCESS_TOKEN` | https://www.linkedin.com/developers/apps → create an app → *Products*: add **Share on LinkedIn** and **Sign In with LinkedIn using OpenID Connect** → *Auth* tab → OAuth 2.0 tools → generate a token with scopes `openid profile w_member_social`. **The token expires after 60 days**, so set a reminder to regenerate it. |

### 3. Add them to the repo
Repo → Settings → Secrets and variables → Actions:

- **Secrets:** `GROQ_API_KEY`, `GH_ACTIVITY_TOKEN`, `X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_TOKEN_SECRET`, `LINKEDIN_ACCESS_TOKEN`
- **Variables** (all optional):
  - `GH_USERNAME`: defaults to the repo owner
  - `AUTHOR_BIO`: e.g. `a full-stack dev building AI tools in public`. This shapes the voice a lot.
  - `GROQ_MODEL`: leave blank and the bot picks the best model Groq currently offers (Groq retires models over time). Set it to force a specific one from https://console.groq.com/docs/models
  - `INCLUDE_PRIVATE_REPOS`: `true` by default (needs `GH_ACTIVITY_TOKEN`)
  - `EXCLUDE_KEYWORDS`: skip repos whose name or description contains any of these, e.g. `timber,ptm`
  - `EXCLUDE_REPOS`: skip exact repos, e.g. `owner/name,owner/other`
  - `AUTO_OPTION`: which option API modes post automatically: `work` (default), `news` or `meme`. Falls back to the first option available.
  - `LOOKBACK_HOURS`: `26` by default, a little over a day so late-night work is never missed
  - `X_MODE`: `api` (auto, paid, about $0.45 a month) or `manual` (free one-tap link). With `manual` you don't need any X keys.
  - `LINKEDIN_MODE`: `api` (auto, free, but the token expires every 60 days) or `manual` (one-tap link, no token needed)
  - `LOWERCASE`: `true` by default
  - `AUTO_POST_X` / `AUTO_POST_LINKEDIN`: set to `false` to pause one platform

If a platform's keys are missing, the bot skips that platform with a warning and still posts to the other one.

### 4. Test it
Actions tab → **Daily post** → *Run workflow*.

### 5. Schedule
It runs daily at **08:45 Malaysia time (00:45 UTC)**, which usually arrives around 9:00 because GitHub starts scheduled jobs late. GitHub sometimes drops scheduled runs entirely, so there is a backup run at **10:15** that skips itself if the posts already exist. Change the `cron` line in `.github/workflows/daily-post.yml` for a different time; it uses UTC.

## Private repos

To read private and organization repos, the bot needs a personal token saved as the `GH_ACTIVITY_TOKEN` secret. The built-in Actions token can only see public repos.

- **Easiest:** GitHub → Settings → Developer settings → Personal access tokens → **Tokens (classic)** → scope `repo`. It covers your repos and every org you're in, but it can also write, so keep it only in the secret.
- **Read-only:** a **fine-grained** token with *Contents: read* and *Metadata: read*. These only cover one owner (you **or** one org), so org repos need a classic token.

Private work is used, but the bot never names private repos, companies, clients or products in posts. It describes the kind of work only, and it never reads private READMEs. To leave a project out completely, use `EXCLUDE_KEYWORDS` or `EXCLUDE_REPOS`.

## Memes

The meme option uses one of ~25 classic templates (drake, this is fine, gru's plan, galaxy brain…). The image is generated for free by [memegen.link](https://memegen.link), no key needed.

- **manual mode:** the meme shows in its option in the issue. Save it and attach it when you post (the one-tap links can't attach images).
- **`X_MODE=api`:** if the auto-posted option is the meme, the image is uploaded with it. If the upload fails, it posts the text alone.
- **`LINKEDIN_MODE=api`:** text only for now.

## Running locally

```bash
pip install -r requirements.txt
```

```bash
cp .env.example .env
```

```bash
python bot.py --dry-run
```

`--dry-run` prints the three options without publishing or saving them, which is useful for tuning `AUTHOR_BIO` and the prompt. `python bot.py` without the flag delivers them for real.

## Customising
- **Voice:** `style.md`. Length and format rules are in `SYSTEM_PROMPT` in `bot.py`.
- **Which GitHub events count:** `fetch_github_activity()`
- **News source:** `fetch_tech_news()`. It uses the Hacker News API, which needs no key, and keeps stories with more than 100 points.

> Note: GitHub turns off scheduled workflows in repos with no activity for 60 days. The bot's daily commit of `posts/` counts as activity, so this won't happen while it's running.
