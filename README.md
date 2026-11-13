# MDAZ (FP 11) order alerter — GitHub Actions setup

Runs the FP 11 stuck-order alerter on GitHub's servers (Microsoft Azure IPs,
**not** Google), so it's always-on with nothing to keep powered on. It fetches
pending orders, flags ones stuck too long (no DA / first mile / last mile),
dedups so each order alerts once per stage, and posts to your Google Chat space.

**No secrets live in this code.** Your token, webhook, and internal API host are
all GitHub **Secrets** — encrypted, private, never shown in logs, even on a
public repo.

---

## Before you start — read this once

**Use a PUBLIC repo.** It's the only way to get fast checks for free:
- Public repo → **unlimited** Actions minutes → every-5-min is free forever.
- Private repo → only 2,000 free min/month → every-5-min would run out in ~a week
  and then cost money (~$50/mo). Not worth it.

Public is safe here because the three sensitive things (token, webhook, internal
host URL) are all **Secrets**, not code. The committed code is generic plumbing.

---

## Step 1 — create the repo

1. Go to <https://github.com/new>
2. Owner: **ghorab11**, name: `mdaz-alerter`, visibility: **Public**.
3. Create it (empty, no README).

## Step 2 — add the files

Easiest: on the new repo page → **Add file → Upload files** → drag in **all the
files from this bundle** (keep the `.github/workflows/` folder structure) →
**Commit changes**.

Files: `mdaz_alerter.py`, `.github/workflows/mdaz-alerter.yml`,
`requirements.txt`, `.gitignore`, `config.example.json`, `README.md`.

## Step 3 — add the 3 secrets

Repo → **Settings → Secrets and variables → Actions → New repository secret**.
Add these three (names must match exactly):

| Secret name | Value |
|---|---|
| `BREADFAST_BEARER_TOKEN` | your Control Room token (paste it **without** the word "Bearer") |
| `GOOGLE_CHAT_WEBHOOK_URL` | your Chat webhook URL (the full `https://chat.googleapis.com/...` link) |
| `CONTROL_ROOM_BASE` | your Control Room base, e.g. `https://<host>/control-room` |

> You already have the webhook and the host. The **token is the one thing only
> you can provide** — paste it straight into the secret; it never passes through
> anyone else.

## Step 4 — the gate test (the whole point)

This proves whether GitHub's IP gets past the 453 gate.

1. Repo → **Actions** tab → **MDAZ FP11 Alerter** → **Run workflow**.
2. Set **mode = `test`** → **Run workflow**.
3. Open the run → **Run alerter** step. You want:
   - **`Connectivity OK (HTTP 200)`** + a count of orders → **it works.** 🎉
     GitHub passes the gate. You're done — it'll now run automatically.
   - **`HTTP 453`** → Azure is blocked too. Stop here and tell me; we switch to
     an Oracle always-free VM (same files, just a different host).

Posts nothing in `test` mode, so it's safe to run as many times as you like.

## Step 5 — see real alerts, then go live

- **Run workflow → mode = `dry-run`** → prints the exact alerts it *would* post,
  still posts nothing. Good for sanity-checking the wording and thresholds.
- Once happy, do nothing more: the **schedule is already on**. Every ~5 minutes
  the workflow runs in `run` mode and posts real alerts to your Chat space.

To pause it: Actions tab → **MDAZ FP11 Alerter** → **⋯ → Disable workflow**.

---

## Tuning

Edit the `env:` block in `.github/workflows/mdaz-alerter.yml` (no code change):

| Setting | Meaning | Default |
|---|---|---|
| `UNASSIGNED_THRESHOLD_MIN` | mins pending with no DA before alerting | 10 |
| `FIRST_MILE_THRESHOLD_MIN` | mins assigned→restaurant before alerting | 20 |
| `LAST_MILE_THRESHOLD_MIN` | mins out-for-delivery before alerting | 20 |
| `REALERT_MIN` | re-remind about a still-stuck order every N mins | 30 |

## About "every minute" (this is already set up)

GitHub's scheduler can't launch more often than every 5 minutes, so this
workflow bridges the gap: every 5-minute launch, in `run` mode, **polls once a
minute for ~5 minutes** (`END=$((SECONDS+270))` loop), then the next launch
takes over. Net effect ≈ a check every minute, 24/7. A failed tick never kills
the window.

This only works for free because the repo is **public** (unlimited Actions
minutes). Honest caveat: a near-continuous loop leans on Actions harder than
its intended CI use — fine for a tiny poller, but if GitHub ever throttles it,
the proper tool for guaranteed 1-minute is an always-on VM with real `cron`
(Oracle always-free). Say the word and I'll port it; the script is identical.

To dial back to a calmer every-5-min (one check per launch), replace the loop
in the `*)` branch of the workflow with a single `python mdaz_alerter.py`.

## Note on the webhook you shared

The webhook URL contains a `key`+`token`. If you want to be safe after pasting it
around, regenerate it in the Chat space's **Manage webhooks**, then update the
`GOOGLE_CHAT_WEBHOOK_URL` secret. Everything keeps working with the new URL.

## Privacy

The alerter posts only: order number, restaurant, delivery associate, stage, and
minutes waiting. Never customer names, phones, addresses, coordinates, or
products.
