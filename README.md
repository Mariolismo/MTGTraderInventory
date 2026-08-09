# MTGTraderInventory

Keep CardTrader Magic: The Gathering listings priced in line with the market — safely, and on a schedule if you want.

The tool reads your inventory, looks at comparable Zero offers, proposes updates (floors, clamps, dead-band), and can apply them to CardTrader. You can run that loop on your machine or on AWS.

## Architecture

```text
                    ┌─────────────┐
   CardTrader API ←─┤  Plan       │  export + marketplace comps → proposed prices
                    │  (DRY_RUN)  │
                    └──────┬──────┘
                           │ plan.jsonl + summary
                           ▼
                    ┌─────────────┐
   CardTrader API ←─┤  Apply      │  only if safety_ok (LIVE)
                    │  (LIVE)     │  bulk_update + stale checks
                    └─────────────┘

Optional AWS (same idea, orchestrated):

  EventBridge ──► Step Functions
                    Prepare → Map(PlanChunk) → Merge → Apply
                         │         │            │
                         └──── S3 artifacts ────┘
                              CloudWatch metrics / dashboard
```

- **Local:** Python scripts under `scripts/`.
- **AWS:** SAM template (`template.yaml`) — chunked planning so large catalogs stay under Lambda time limits; apply still uses DynamoDB for batch idempotency.

Extra helpers: ManaBox CSV import and ManaBox ↔ inventory compare.

## Docs

Setup, deploy, and day-to-day commands: **[docs/runbook.md](docs/runbook.md)**

## Note

Most of this codebase was produced with AI assistance (in less than 4 hours, geez I am going to be unemployed xD). A proper human code review is overdue, as are broader hygiene passes (structure, tests, CI/CD). That work is intentional backlog — not a claim that the stack is “done” from an engineering-process standpoint.

## Possible later

- **Improved CI/CD** — Automated unit tests on push, optional gated `sam build`/`deploy`, and clearer release habits once a human review pass has landed.
- **UI** — A small website or app to follow runs (AWS or local artifacts) without the AWS console; enrich cards with Scryfall (art / set / rarity); and support batch import plus one-off manual adds from the same place.
- **Manual pricing overrides** — Pin or force a price (or a floor) on specific cards instead of always accepting the algorithm.
- **Custom pricing statistics** — Choose which KPIs you care about (e.g. € delta, increases vs decreases, skip reasons, top movers) and surface them in the UI or reports.
- **Other games** — Extend beyond MTG if you sell more on CardTrader and the same plan/apply loop still fits.
- **More importers** — Ingest from sources beyond ManaBox (other collection apps / CSV layouts) into CardTrader.
