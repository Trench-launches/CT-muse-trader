# Security

This bot can hold and spend real money, so security problems are taken seriously.

## Reporting a vulnerability

Please **don't open a public issue** for a security problem. Report it privately through GitHub:
the repository's **Security** tab, then **Report a vulnerability**. Include what you found, how to reproduce
it, and what an attacker could do with it. You'll get a reply as soon as possible; please give us a reasonable
time to fix it before you publish anything.

Useful reports include: ways to make the bot trade, post or launch without the owner's settings allowing it;
bypasses of the risk limits, kill switch or admin token; ways to get secrets out of the API, logs or posts;
prompt injection that gets past the hard gates; and anything that would let a stranger spend the owner's funds.

Out of scope: losing money on trades the bot was configured to make, and rate limits on public read endpoints.

If you find a credential committed to this repository or its history, report it the same way. Don't use it.

## Running your own copy safely

### Keep secrets out of git

- Secrets go in `.env` (gitignored) or your host's secret store (Railway Variables, GitHub Actions secrets).
  Never in code, `.env.example`, `web/config.js`, issues, screenshots, logs or chats with anyone, AI assistants
  included.
- Everything in `web/` is public. The site needs no secrets, so never put one there.
- Install the pre-commit hooks once per clone (`pip install pre-commit && pre-commit install`). They run
  [gitleaks](https://github.com/gitleaks/gitleaks) and refuse commits that contain keys or `.env` files.
  CI scans every commit of the history again (`.github/workflows/ci.yml`).
- On GitHub, turn on **Secret scanning** and **Push protection** (Settings, Code security). They're free for
  public repositories.
- A secret that was committed is compromised even if a later commit deletes it: the history keeps it, and
  public repositories are scraped for keys within minutes. Revoke it and make a new one. Rewriting history
  afterwards is optional clean-up, not a fix.

### Wallets

- Give the bot its **own new wallet**, used for nothing else, holding only what you can afford to lose.
  Top it up in small amounts. Never give it your main wallet, a treasury, or a token deployer's key.
- The launchpad (`CT_LAUNCHER_WALLET_PRIVATE_KEY`) needs a **second** separate wallet. The bot refuses to start
  if both settings hold the same key.
- Don't set a wallet key while you're in paper mode. The bot warns if you do: an unused key is just one more
  thing to leak.
- Keep the risk limits small (`CT_MAX_POSITION_ETH`, `CT_MAX_TOTAL_EXPOSURE_ETH`, `CT_MAX_DAILY_LOSS_ETH`).
  They are the most the bot can lose in a bad stretch, not targets.

### The API

- `/admin/*` endpoints are disabled until you set `CT_API_ADMIN_TOKEN`. Use at least 32 random characters
  (`python -c "import secrets; print(secrets.token_urlsafe(32))"`). In live mode the bot refuses to start
  without one, or with a shorter one.
- Only list your own site in `CT_SITE_ORIGINS`. Browsers can never send the admin token cross-site.
- The public chat (`CT_CHAT_ENABLED`) spends Grok credit on every question. It is off by default and capped per
  visitor and per day (`CT_CHAT_GLOBAL_DAY` is the hard ceiling).
- Put production behind HTTPS. Logs leave RPC URLs out on purpose, since provider URLs carry keys.

### Hosting

- Rotate keys on a schedule, and immediately after any incident at a provider that held them. Hosting
  platforms do get breached: in October 2026 a major deploy platform reported a security incident, and every
  credential stored there had to be treated as exposed. In that situation, move wallet funds to new wallets
  first, then rotate API keys (Grok, X, RPC, Supabase, GMGN), the admin token and deploy tokens, and rebuild
  the service with fresh values.
- Use project-scoped deploy tokens (a Railway *project* token, not an account token) and give CI the least
  access it needs.
- Keep `data/` (the database and journal) on a private volume and out of backups you share. It holds no
  wallet keys, but it does hold the X OAuth tokens the bot refreshes at runtime, and your trading history.

### If a key leaks

1. Wallet key: move the funds to a new wallet right away, then retire the old one. Nothing else helps, since a
   private key can't be revoked.
2. API keys and tokens: revoke them at the provider (console.x.ai, developer.x.com, your RPC provider,
   Supabase, GMGN, Railway, GitHub) and set new ones.
3. `CT_API_ADMIN_TOKEN`: set a new one and redeploy.
4. Check the journal (`ctagent verify-journal`) and the X account for anything you didn't do.
