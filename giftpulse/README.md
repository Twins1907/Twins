# GiftPulse

Cross-venue execution and alerts for the Telegram Gifts market.

GiftPulse watches Portals, Tonnel and MRKT together, tells a trader which venue is
cheapest right now, and hands them a one-tap buy link with a referral code attached.
It is **non-custodial by construction**: the bot never holds funds, gifts, or private
keys, and there is no code path in this repository that accepts one.

```
[Alerts channel]  →  [Bot / Mini App]  →  [Routing engine]  →  [Venue adapters]
    funnel             aiogram + TMA       best execution       Portals/Tonnel/MRKT
                            │                     │
                     [TON Connect]          [Indexer → Postgres]
                     user signs             floors, sales, deltas
```

## Quick start

Runs with no credentials and no network — mock mode serves a deterministic synthetic
market so you can see the whole pipeline work before touching a marketplace API.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env

giftpulse indexer --once --dry-run   # one poll cycle, alerts evaluated but not sent
giftpulse api                        # Mini App + API on :8080
```

```
$ giftpulse indexer --once --dry-run
[dry-run] ⚡ Astral Shard 9.35% cross-venue spread
[dry-run] 🐋 Plush Pepe 14416 TON buy
Cycle complete: 24 snapshots, 24 new sales, 9 alerts found, 3 venues responding
```

Then `curl localhost:8080/api/collections` to see cheapest-venue-per-collection, or
open `http://localhost:8080/` for the Mini App shell.

## Going live

1. **Bot** — create one with [@BotFather](https://t.me/botfather), set
   `GIFTPULSE_BOT_TOKEN`.
2. **Channel** — create the alerts channel, add the bot as an admin, set
   `GIFTPULSE_ALERT_CHANNEL`.
3. **Mini App** — serve the API over HTTPS and register that URL with BotFather.
   Set `GIFTPULSE_PUBLIC_URL` to match; Telegram refuses to open a Mini App over
   plain HTTP, and the bot hides the button rather than showing a broken one.
4. **Referrals** — set `GIFTPULSE_PORTALS_REFERRAL` and friends. This is the day-one
   revenue line and needs nothing else.
5. **Turn off mock** — `GIFTPULSE_MOCK=false`, and set `GIFTPULSE_PORTALS_AUTH_DATA`
   if you want Portals (see below).

```bash
giftpulse all      # indexer + bot + API in one process, fine for a single VPS
```

## Processes

| Command | Does |
|---|---|
| `giftpulse indexer` | Polls venues on a schedule, writes snapshots, fires alerts |
| `giftpulse bot` | Telegram command surface |
| `giftpulse api` | Mini App backend and static shell |
| `giftpulse all` | All three in one process |

Separate processes because they scale differently and because a wedged indexer
should not take the bot down with it. `indexer --once` and `--dry-run` exist so you
can inspect what the rules would fire before pointing them at a live channel.

## Design notes

**Venue adapters absorb breakage.** Every marketplace sits behind one class in
`giftpulse/venues/`. MRKT quotes nanoton, Tonnel quotes TON floats, Portals needs an
auth blob — none of that leaks past the adapter. Adapters never raise on transport
failure: a venue being down returns empty and the quote proceeds with the venues that
answered. That behaviour is tested (`test_a_broken_venue_does_not_break_the_quote`).

**Portals auth expires, deliberately manually.** Portals wants a short-lived
`authData` captured from the Telegram Web client. Automating the refresh would mean
holding Telegram account credentials, so this process doesn't: it treats 401 as
weather, logs once, and keeps serving the other venues.

**Snapshots, not current state.** The indexer appends rows rather than updating a
"current price". Every alert rule is a delta question, and you cannot ask a delta
question of a table that only remembers now.

**Alert rules are pure functions.** `giftpulse/alerts/rules.py` takes snapshots and
thresholds and returns alerts. No database, no network, no clock reads. That is why
they can be unit-tested directly and replayed over history.

**Floor breaks fire downward only.** A floor rising is not an execution opportunity.
Collapsing both directions into one "floor moved" alert is how a signal channel turns
into noise and users mute it.

**Whales need two conditions.** A sale must be large in absolute terms *and* well
above floor. Size alone makes every Plush Pepe trade an alert; multiple-of-floor
alone makes a 3 TON sticker sale look like conviction.

**On-chain volume is separated from reported volume.** Venues settle through internal
balance ledgers, so headline volume figures are inflated. `SaleEvent.onchain` records
which is which and `onchain_volume_ton()` counts only settled trades.

**Quotes are honest about the fee.** When the execution fee is large enough to erase
the routing advantage, `BuyQuote.beats_runner_up` is False rather than the quote
claiming a saving the user won't get.

## Security

The custody boundary is the product's main risk surface, so it is enforced in code
rather than by convention:

- No setting, endpoint, or model field accepts a private key, seed phrase, or
  mnemonic. `/api/wallet` stores a public address and nothing else.
- `build_payment_request()` constructs a TON Connect request for the *user's* wallet
  to sign. This process cannot sign it.
- Mini App `initData` is verified server-side on every authenticated request with a
  constant-time HMAC comparison, an expiry window, and fail-closed behaviour when no
  bot token is configured. `tests/test_auth.py` covers forged signatures, swapped
  user ids, wrong-token signing, and expiry.
- Alert text is HTML-escaped before it reaches Telegram.

## Tests

```bash
pytest          # 54 tests, no network required
ruff check .
```

The suite runs entirely against mock venues and in-memory SQLite, so it is
deterministic and offline. Coverage is weighted toward the parts that are expensive
to get wrong: alert rule thresholds, fee arithmetic, referral attachment, venue
failure handling, and initData verification.

## Roadmap

See [`docs/ROADMAP.md`](docs/ROADMAP.md) for what's built and what's next.

## Licence

MIT
