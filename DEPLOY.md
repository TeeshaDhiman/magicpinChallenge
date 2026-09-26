# Deploying the Vera bot

You need one public HTTPS base URL that serves `/v1/context`, `/v1/tick`, `/v1/reply`, `/v1/healthz` and `/v1/metadata`. Any host works. Two paths are given below. Whichever you choose, these four settings are what keep the bot from being disqualified:

1. **Exactly one instance, one worker.** All state lives in memory. A second worker or machine would never see the contexts pushed to the first one. The Dockerfile already pins `--workers 1`, and the host must also run a single instance.
2. **Never sleeps.** The judge polls `/v1/healthz` every 60s, and three misses in a row means disqualification. Free tiers that spin down on idle will fail this. Use an always-on instance.
3. **No restarts during the test window.** A restart wipes every pushed context. Don't redeploy while the judge is running.
4. **`VERA_IGNORE_EXPIRY` must NOT be set.** That flag exists only for local simulator runs.

Set these environment variables on the host so `/v1/metadata` identifies you:

| Variable | Example |
|---|---|
| `TEAM_NAME` | `Team Alpha` |
| `TEAM_MEMBERS` | `Alice, Bob` (comma-separated) |
| `CONTACT_EMAIL` | `team@example.com` |
| `SUBMITTED_AT` | `2026-09-26T10:00:00Z` |

## Option A: Render (simplest)

1. Push this folder to a GitHub repo.
2. In Render: **New → Blueprint**, then pick the repo. `render.yaml` sets up a Docker web service with the health check at `/v1/healthz`.
3. Keep the **Starter** plan (the free plan sleeps after 15 idle minutes).
4. Fill in the four env vars when prompted.
5. Your URL is `https://vera-bot-XXXX.onrender.com`.

## Option B: Fly.io (Mumbai region, closer to Indian users)

```bash
fly launch --no-deploy --copy-config      # rename the app in fly.toml when asked
fly secrets set TEAM_NAME="Team Alpha" TEAM_MEMBERS="Alice, Bob" CONTACT_EMAIL=team@example.com
fly deploy
fly scale count 1                          # IMPORTANT: Fly creates 2 machines by default
```

`fly.toml` already turns off auto-stop and keeps one machine running. Your URL is `https://<app-name>.fly.dev`.

## Any other host (Railway, a VM, Cloud Run, …)

Build the `Dockerfile`, or run `uvicorn bot:app --host 0.0.0.0 --port $PORT --workers 1` behind HTTPS. On Cloud Run, set min instances = max instances = 1 and turn off CPU throttling.

## Before you submit

```bash
python smoke_test.py https://your-url
```

For the full requirement-by-requirement check, run `python verify_requirements.py https://your-url` too. The smoke test checks all five endpoints, response shapes, idempotency, 409/400 handling, suppression, and latency. At the end it wipes its own test data. Submit only after it prints `ALL CHECKS PASSED`, and don't run it while the judge's window is live.

## Watching it during the test

Logs show one line per tick (`tick now=… triggers=… actions=…`). Set `LOG_LEVEL=DEBUG` to also see why each trigger was sent or skipped (expired, suppressed, in-flight, insufficient data).
