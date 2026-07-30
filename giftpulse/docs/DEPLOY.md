# Deploying GiftPulse

A runbook for taking this from a mock-data demo to a live alerts channel earning
referral revenue. The order matters: each step de-risks the next one.

## 0. Before touching a server

Three of these are paperwork, not engineering, and they gate revenue more directly
than any code here.

- **Referral programs.** Sign up with Portals, Tonnel, and MRKT. Until
  `GIFTPULSE_PORTALS_REFERRAL` and friends hold real codes, every routed trade earns
  nothing. This is the entire day-one revenue line. The plumbing is built and
  tested; the accounts are not.
- **A bot.** `@BotFather` → `/newbot`. Keep the token out of git.
- **A channel.** Create it, add the bot as an administrator with permission to post,
  and note the numeric id (`-100…`).
- **A domain.** Telegram will not open a Mini App over plain HTTP, and a self-signed
  certificate fails the same way.

## 1. Validate the adapters against live APIs

**Do this before anything else.** The venue adapters have never been run against the
real APIs — every field name in `giftpulse/venues/*.py` comes from documented and
community-reported shapes, not from a response anyone has seen. They are built to
degrade quietly, which is right in production and unhelpful here: a wrong field name
produces an empty list, not an error.

Start with Tonnel, which needs no credentials:

```bash
export GIFTPULSE_MOCK=false
export GIFTPULSE_ENABLED_VENUES=tonnel
giftpulse indexer --once -v
```

You are looking for a non-zero snapshot count. If it reports `0 snapshots`, turn on
debug logging and compare the actual JSON against `TonnelAdapter.fetch_listings`.
Expect to correct field names on first contact. Then repeat for MRKT, and for
Portals once you have an `authData` blob.

Budget roughly a day per venue. Nothing below is meaningful until this passes.

## 2. First deploy

```bash
cp .env.example .env && chmod 600 .env
# fill in: bot token, channel, admin chat, referral codes, domain, POSTGRES_PASSWORD
docker compose --profile tls up -d --build
```

Then point `@BotFather` → *Bot Settings* → *Menu Button* at
`https://your-domain`, matching `GIFTPULSE_PUBLIC_URL` exactly.

Check it came up cleanly:

```bash
docker compose logs giftpulse | grep startup:   # misconfiguration warnings
curl -s https://your-domain/api/health | jq
```

`status` should reach `ok` within a couple of poll intervals. `starting` means no
cycle has completed yet; `degraded` means the indexer has stalled or is failing.

## 3. Tune the alert thresholds before going public

Run with `GIFTPULSE_ALERT_CHANNEL` unset for a few days and watch what *would* have
been posted:

```bash
docker compose logs giftpulse | grep "no delivery target"
```

The defaults — 5% floor break, 8% spread, 500 TON whale — are guesses calibrated
against synthetic prices. Real thresholds depend on real volatility, and the channel
is the top of the entire funnel: one spammy week costs subscribers permanently.

`GIFTPULSE_ALERT_MAX_PER_HOUR` (default 12) is the backstop. Cooldown prevents the
*same* event repeating; the hourly cap prevents thirty *distinct* events from one
market-wide move arriving back to back. When the cap binds, the most actionable
alerts win — spreads and floor breaks before whale prints and supply bursts.

Only once the shape looks right, set `GIFTPULSE_ALERT_CHANNEL` and restart.

## 4. Operating it

**Monitoring.** Point an uptime monitor at `/api/health`. It returns 503 when the
indexer is stalled, so a plain status-code check is enough. This is the failure that
otherwise hides: a dead indexer and a quiet market look identical from outside.

**Portals credentials expire in hours.** When they do, that venue silently returns
nothing and you are quietly serving two venues instead of three. The admin chat gets
one message when it happens; refresh `GIFTPULSE_PORTALS_AUTH_DATA` and restart.

**Backups.** `docker compose exec db pg_dump -U giftpulse giftpulse | gzip > backup.sql.gz`.
The snapshot history is the only thing here that cannot be rebuilt — floors are not
retrievable after the fact, and every alert rule is a delta question against them.

**Retention.** Pruning runs hourly and keeps `GIFTPULSE_RETENTION_DAYS` (45) of
snapshots, sales, and alert history. At eight collections across three venues that
is roughly 1.5M snapshot rows steady-state.

**Schema changes.** Edit `models.py`, then `make revision m="what changed"`, then
commit the generated file. CI fails if the two drift apart. Migrations apply
automatically on startup unless `GIFTPULSE_AUTO_MIGRATE=false`.

## 5. Turning on the execution fee

Referral routing earns from day one and needs no fee. When you do enable one:

```bash
GIFTPULSE_EXECUTION_FEE_BPS=50      # 0.50%
GIFTPULSE_FEE_WALLET=EQ...          # a public address, never a key
```

The fee becomes an extra leg in the TON Connect transaction the user signs. Note
that `build_payment_request` currently refuses to build a request with no fee leg,
because a wallet rejects an empty message list — with the fee at zero, buys complete
through the referral deep link instead.

## Scaling notes

- **SQLite is a single-process story.** `giftpulse all` runs the indexer and API
  together and both write. WAL and a 30-second busy timeout make that survivable,
  not advisable. Move to Postgres before real traffic.
- **The API rate limit is per worker.** `GIFTPULSE_QUOTE_RATE_LIMIT_PER_MINUTE` is
  enforced in process memory, so running four workers means four times the limit.
  Fine at this size; revisit alongside Redis if the alert queue arrives.
- **Venue politeness.** `GIFTPULSE_VENUE_MAX_RPS` caps sustained request rate per
  venue and polls are jittered. Marketplaces tolerate bots that bring them volume —
  staying obviously well-behaved is what keeps that true.

## Not built yet

Deliberately out of scope for the alerts-and-referral phase:

- TON Connect handshake in the Mini App (buys currently go through the referral deep
  link, which is what earns day-one revenue)
- In-bot sell and listing flows
- Telegram Stars subscription handling for the VIP tier
- Per-venue on-chain sale payload construction
