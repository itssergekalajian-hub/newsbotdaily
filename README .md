# MidWorld Daily

Reads everything @midworldnews posted during one calendar day (00:00–23:59
Beirut time), rewrites it into one explained bulletin, narrates it with an AI
voice, and posts text + voice note + presenter video to @midworlddaily just
after local midnight.

Cost: **$0** — Gemini Flash free tier + edge-tts (no key) + GitHub Actions free minutes.

---

## Setup (about 20 minutes, one time)

### 1. Create your channel and bot
1. Telegram → New Channel → make it public, pick a username.
2. Talk to **@BotFather** → `/newbot` → copy the **bot token**.
3. Add the bot to your channel as an **administrator** with "Post messages" on.
4. Your `TARGET_CHAT_ID` is just `@yourchannelname`.

### 2. Get a free Gemini key
Go to `aistudio.google.com/apikey` → Create API key. No credit card.
Free tier is ~1,500 requests/day on Flash models; this bot uses **one per day**.

### 3. Make the presenter
Generate one portrait of your AI anchor (any free image generator), save it as
`presenter.png` (1280×720 or larger) in the repo root. Delete the file or set
`MAKE_VIDEO=0` if you only want the voice message.

### 4. Push to GitHub
```
git init && git add . && git commit -m "news bot" && git push
```
Then in the repo → **Settings → Secrets and variables → Actions**:

| Type     | Name             | Value                       |
|----------|------------------|-----------------------------|
| Secret   | `BOT_TOKEN`      | from BotFather              |
| Secret   | `TARGET_CHAT_ID` | `@yourchannel`              |
| Secret   | `GEMINI_API_KEY` | from AI Studio              |
| Variable | `SOURCE_CHANNEL` | source channel username, no `@` |

### 5. Test
Actions tab → "Daily news brief" → **Run workflow**. Check your channel.

---

## Local test

```bash
pip install -r requirements.txt
sudo apt install ffmpeg
export BOT_TOKEN=... TARGET_CHAT_ID=@yourchannel GEMINI_API_KEY=... SOURCE_CHANNEL=...
python daily_news.py
```

## Knobs

| Env var | Default | Notes |
|---|---|---|
| `TIMEZONE` | `Asia/Beirut` | EET/EEST; DST handled automatically |
| `TARGET_DATE` | *(empty)* | `2030-02-01` to rebuild one specific day |
| `VOICE` | `en-US-AndrewMultilingualNeural` | `edge-tts --list-voices` for 300+ others |
| `MAKE_VIDEO` | `1` | `0` = voice note only |
| `GEMINI_MODEL` | `gemini-2.5-flash` | any free-tier Flash model |
| `PRESENTER_IMAGE` | `presenter.png` | background/anchor still |

Arabic bulletin: set `VOICE=ar-LB-LaylaNeural` (or `ar-EG-SalmaNeural`) and add
"Write the bulletin in Arabic." to the rules inside `PROMPT`.

## Notes

- The collector reads `https://t.me/s/<channel>`, the public web preview. It only
  works if the source channel is **public**. For a private one you'd need
  Telethon with a user session instead.
- GitHub disables scheduled workflows in repos with no commits for 60 days —
  push anything occasionally to keep it alive.
- Two crons are scheduled, 21:00 and 22:00 UTC. Whichever one lands in the
  first hour of the local day does the work; the other exits. That keeps
  midnight delivery correct across both DST switches with no maintenance.
- GitHub cron can fire a few minutes late; that's harmless. If a run is missed
  entirely, use Actions → Run workflow and type the date to rebuild it.
- If you post the brief back into the **same** channel it reads from, that's
  handled: posts containing the `🗞 Daily brief` marker are skipped when
  collecting, so the bot never summarizes its own summary.
