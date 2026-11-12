# Cyber Chiefs XP

Levels and badges for WCC Cyber Chiefs members. Earn XP by showing up, solving club CTF challenges,
writing writeups, building challenges and helping others. Everything runs on free GitHub Actions + Pages.

**Leaderboard:** https://wcc-cyber-chiefs.github.io/xp

## For members

1. Open a **[Join XP](../../issues/new?template=join-xp.yml)** issue. A bot adds you in about a minute.
2. Earn XP (see the "how to earn" list on the leaderboard).
3. Your profile page has a copy-paste snippet that puts your live level badge and earned badges
   on your GitHub profile README.

Joining puts your GitHub username and XP activity on a public page. Want off the board? Ask an officer.

## For officers

### Awarding XP

Comment on any issue in this repo (pin one called **XP desk**):

```
/award @alex @sam attendance 2026-11-18              # session attendance, date = session date
/award @alex writeup https://github.com/.../pull/12  # sources with a default XP amount
/award @alex award 50 ran the IR tabletop            # free-form award, you pick the amount
/award @alex correction -25 duplicate solve          # fix a mistake
```

The bot replies with what it did. Only people listed under `officers:` in `xp-config.yml` can award.
Sources marked `unique` can't be given twice for the same ref, so re-running a command is safe.

### Automatic XP from other repos

Other club repos (CTF board, quests, writeups) can award XP by sending a `repository_dispatch`:

```bash
gh api repos/wcc-cyber-chiefs/xp/dispatches \
  -f event_type=xp-event \
  -F 'client_payload={"user":"alexdoe","source":"ctf_medium","ref":"web-02"}'
```

The caller needs a token with write access to this repo, stored as a secret in the calling repo.
Use a fine-grained token scoped to just this repo, or a GitHub App.

### Tuning

Everything lives in `xp-config.yml`: point values, levels, badge thresholds, colors, semesters and
the officer list. Change it, push, and the whole board is recomputed from the ledger.

## How it works

- `data/ledger.jsonl` is the source of truth: one line per XP event. Totals are never stored, only computed.
  To undo something, add a `correction` instead of editing history.
- `data/members.json` lists who has joined. Events for non-members are ignored.
- `scripts/xp.py` computes totals, levels and badges, then writes the static site and badge SVGs.
- Workflows:
  - `build.yml` rebuilds and deploys Pages, then posts level-ups and new badges to Discord
  - `award.yml` handles `/award` comments
  - `join.yml` handles the Join XP form
  - `dispatch.yml` handles events from other repos

### Badge URLs

- Level: `https://wcc-cyber-chiefs.github.io/xp/badges/<user>/level.svg`
- Achievement: `https://wcc-cyber-chiefs.github.io/xp/badges/<user>/<badge>.svg`
  (shows as locked until earned, so embeds never break)

## Setup (one time)

1. Create the repo `wcc-cyber-chiefs/xp` (public) and push this folder to `main`.
2. **Settings → Pages:** set Source to **GitHub Actions**.
3. **Settings → Actions → General → Workflow permissions:** select **Read and write permissions**.
4. **Settings → Secrets and variables → Actions:** add `DISCORD_WEBHOOK_XP`, a webhook for the
   channel where level-ups should post. Optional: without it, announcements are just logged.
5. **Issues → Labels:** create a label named `xp-join`.
6. Open an issue titled **XP desk**, pin it, and use it for `/award` commands.
7. Edit `officers:` in `xp-config.yml` so at least two people (including the club advisor) can award.
8. Run the **build-and-deploy** workflow once from the Actions tab.

If you add branch protection to `main`, allow GitHub Actions to push (or the bot can't write the ledger).

## Local testing

```bash
pip install pyyaml
python scripts/xp.py --data-dir examples build --out _site --today 2026-11-11
python -m http.server -d _site 8000
```

`examples/` has fake members and events for previewing the site.

## Handoff

Nothing here is tied to one person. When leadership changes:
- update `officers:` in `xp-config.yml`
- add the new semester under `semesters:`
- make sure the new officers are owners or maintainers of the `wcc-cyber-chiefs` org
