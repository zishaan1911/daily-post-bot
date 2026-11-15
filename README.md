# Daily Post Bot

Every day this bot:

1. Reads your GitHub activity from the last 24h (commits, PRs, releases, new repos, stars…)
2. Sometimes (30% of days by default, and always when you had no activity) picks a trending tech story from Hacker News
3. Asks a Groq model to write **one LinkedIn post and one X post** in your voice
4. **Posts both automatically**, and saves a copy to `posts/YYYY-MM-DD.md` (it also keeps `history.json` so it doesn't repeat the same angle)

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
| `GH_ACTIVITY_TOKEN` (optional) | GitHub → Settings → Developer settings → Fine-grained token with read-only access. Gives private activity and higher rate limits. |
| `X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_TOKEN_SECRET` | https://developer.x.com → create a Project + App → under *User authentication settings* set permissions to **Read and write**, **then** generate the Access Token & Secret (tokens made before switching to write access can't post). Check X's current API pricing; posting may need paid credits. |
| `LINKEDIN_ACCESS_TOKEN` | https://www.linkedin.com/developers/apps → create an app → *Products*: add **Share on LinkedIn** and **Sign In with LinkedIn using OpenID Connect** → *Auth* tab → OAuth 2.0 tools → generate a token with scopes `openid profile w_member_social`. **The token expires after 60 days**, so set a reminder to regenerate it. |

### 3. Add them to the repo
Repo → Settings → Secrets and variables → Actions:

- **Secrets:** `GROQ_API_KEY`, `GH_ACTIVITY_TOKEN`, `X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_TOKEN_SECRET`, `LINKEDIN_ACCESS_TOKEN`
- **Variables** (all optional):
  - `GH_USERNAME`: defaults to the repo owner
  - `AUTHOR_BIO`: e.g. `a full-stack dev building AI tools in public`. This shapes the voice a lot.
  - `NEWS_PROBABILITY`: `0.3` by default
  - `GROQ_MODEL`: default `llama-3.3-70b-versatile`. See https://console.groq.com/docs/models
  - `INCLUDE_PRIVATE_REPOS`: `false` by default
  - `X_MODE`: `api` (auto, paid, about $0.45 a month) or `manual` (free one-tap link). With `manual` you don't need any X keys.
  - `LINKEDIN_MODE`: `api` (auto, free, but the token expires every 60 days) or `manual` (one-tap link, no token needed)
  - `LOWERCASE`: `true` by default
  - `AUTO_POST_X` / `AUTO_POST_LINKEDIN`: set to `false` to pause one platform

If a platform's keys are missing, the bot skips that platform with a warning and still posts to the other one.

### 4. Test it
Actions tab → **Daily post** → *Run workflow*. Tick "Force a tech-news angle" to try the news mode.

### 5. Schedule
It runs daily at **09:00 IST (03:30 UTC)**. Change the `cron` line in `.github/workflows/daily-post.yml` for a different time. GitHub can run scheduled jobs a few minutes late.

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

`--dry-run` prints the posts without publishing or saving them, which is useful for tuning `AUTHOR_BIO` and the prompt. `python bot.py` without the flag posts for real. Add `--news` to force a tech-news angle.

## Customising
- **Voice:** `style.md`. Length and format rules are in `SYSTEM_PROMPT` in `bot.py`.
- **Which GitHub events count:** `fetch_github_activity()`
- **News source:** `fetch_tech_news()`. It uses the Hacker News API, which needs no key, and keeps stories with more than 100 points.

> Note: GitHub turns off scheduled workflows in repos with no activity for 60 days. The bot's daily commit of `posts/` counts as activity, so this won't happen while it's running.
