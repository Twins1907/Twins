# Roadmap

Mapped against the build spec's phases. This tracks the *code*; the spec's growth
and revenue targets are tracked elsewhere.

## Shipped — MVP foundation

- Venue adapter layer with Portals, Tonnel and MRKT, plus a deterministic mock venue
  so the full pipeline runs offline.
- Indexer: scheduled polling, floor snapshots, sale recording with dedupe.
- Alert rules: floor break, cross-venue spread, whale buy, supply burst, watch hit.
- Dispatcher with cooldown dedupe and Telegram rate limiting.
- Routing engine: best execution across venues, referral attachment, fee arithmetic,
  TON Connect payment-request construction.
- Bot: `/scan`, `/floor`, `/watch`, `/portfolio`, inline callbacks, referral links.
- Mini App: cross-venue scanner, per-collection detail, 24h sparkline, quote flow.
- API with verified Mini App `initData` authentication.
- 54 tests, no network required.

## Next — v1 execution

- [ ] **TON Connect handshake in the Mini App.** The `/api/wallet` endpoint and the
      payment-request builder exist; the client-side connect flow is the missing
      piece. Deliberately left until the manifest is served from a real HTTPS origin,
      since TON Connect validates it.
- [ ] **Venue-specific on-chain sale payloads.** `build_payment_request()` currently
      emits the fee leg and routes the purchase itself through the referral deep
      link. Wiring each venue's sale contract turns that into a single signed
      transaction. One method per adapter; the interface is already in place.
- [ ] **Sell and list flows.** Adapters need `create_listing()` alongside the read
      methods.
- [ ] **Portfolio with cost basis.** `RoutedTrade` already records what was routed
      and at what price, which is the hard half.

## Later — v2 retention

- [ ] **Auto-snipe.** `Watch` and `watch_hit` deliver the alert today; pre-building
      the transaction on hit is the remaining step.
- [ ] **Whale wallet following.** `SaleEvent` stores buyer and seller, so this is a
      query plus a subscription table.
- [ ] **Scam and wash-trade detection.** The trust layer. Wash trading is detectable
      from `SaleEvent` cycles between the same address pair; fake collections need a
      verified-collection allowlist per venue.
- [ ] **VIP tier in Telegram Stars.** `User.vip_until` and `is_vip()` are in place;
      needs the Stars payment handler and gating on alert latency.

## Known limitations

- **Real endpoint shapes are unverified.** The Portals, Tonnel and MRKT adapters are
  written against documented and community-reported API shapes but have not been run
  against live credentials in this repository. Expect to adjust field names on first
  contact — that is precisely why parsing is isolated per adapter and why every field
  read is defensive.
- **Tracked collections are a hardcoded list.** `TRACKED_COLLECTIONS` is explicit
  because poll cost is linear in it and the long tail has no liquidity worth
  alerting on. Making it dynamic needs a per-collection liquidity filter first.
- **SQLite by default.** Fine for a single indexer. Concurrent writers need the
  Postgres extra (`pip install -e ".[postgres]"`).
- **No Redis queue yet.** The dispatcher throttles in-process, which is correct for
  one indexer and wrong for several.
