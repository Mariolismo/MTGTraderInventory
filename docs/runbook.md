# Runbook

Do not commit `CARDTRADER_JWT`, `.env`, `samconfig.toml`, or `artifacts/`.

## Local

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
$env:CARDTRADER_JWT = "your-token"
```

```powershell
# Plan only (no price writes)
python scripts/dry_run.py --out-dir artifacts -v
python scripts/dry_run_item.py --name "Bloom Tender"

# Apply a saved plan (mutates CardTrader)
python scripts/apply_plan.py --plan artifacts\<run>-plan.jsonl --confirm-live

# ManaBox
python scripts/import_manabox.py path\to\file.csv
python scripts/compare_manabox.py path\to\CardTrader.csv

python -m unittest discover -s tests -v
```

Defaults (env / `template.yaml`): marketplace RPS **6**, median of cheapest **5** Zero comps, max decrease **5%**/run, dead-band max(**5¢**, **1%**).

## AWS

Region and stack name come from `samconfig.toml` (`region`, `stack_name`). Copy from `samconfig.toml.example`.

| Resource | Name |
|----------|------|
| State machine | `${StackName}-reprice` |
| Lambdas | `${StackName}-{prepare,plan-chunk,merge,apply}` |
| Dashboard | `${StackName}-reprice` |
| Artifacts | `s3://…/runs/<pricing_run_id>/` |

Flow: EventBridge → Prepare → Map(PlanChunk, concurrency 1) → Merge → Apply (if `mode=LIVE` and `safety_ok`).

### Deploy

```powershell
sam build
sam deploy   # uses samconfig.toml (ScheduleEnabled, AlarmEmail, …)
```

Disable schedule: `ScheduleEnabled=false` in `parameter_overrides` (or pass it on the CLI).

### CardTrader JWT on AWS (easy to miss)

`$env:CARDTRADER_JWT` is **local only**. It does **not** update Lambda.

Set the same Bearer token on **all four** functions:

- `${StackName}-prepare`
- `${StackName}-plan-chunk`
- `${StackName}-merge`
- `${StackName}-apply`

**Preferred:** AWS Console → Lambda → each function → Configuration → Environment variables → `CARDTRADER_JWT`.

**One-shot via deploy** (do not commit the token; omit it from `samconfig.toml`):

```powershell
sam deploy --parameter-overrides `
  "ScheduleEnabled=true" `
  "AlarmEmail=you@example.com" `
  "CardTraderJwt=YOUR_TOKEN"
```

If you deploy **without** `CardTraderJwt`, the template sets `CARDTRADER_JWT=""` and can **wipe** a token you previously set in the console. After rotating a CT app token, update AWS explicitly (all four Lambdas) or redeploy with `CardTraderJwt=…`.

`application_disabled` / HTTP 403 from CardTrader means the API app behind the JWT was disabled — fix/recreate the app in CardTrader, then refresh AWS env vars.

### Start a run

```powershell
$Region = "…"
$StackName = "…"

$StateMachineArn = aws cloudformation describe-stacks `
  --stack-name $StackName --region $Region `
  --query "Stacks[0].Outputs[?OutputKey=='StateMachineArn'].OutputValue" `
  --output text

aws stepfunctions start-execution `
  --state-machine-arn $StateMachineArn `
  --input "{\"mode\":\"DRY_RUN\"}" --region $Region

aws stepfunctions start-execution `
  --state-machine-arn $StateMachineArn `
  --input "{\"mode\":\"LIVE\"}" --region $Region
```

Per run under `runs/<id>/`: `listings.jsonl`, `chunks/*`, `plan.jsonl`, `summary.json`, `apply.json` (LIVE).

### Alarms (email)

```powershell
sam deploy --parameter-overrides "ScheduleEnabled=true" "AlarmEmail=you@example.com"
# optional: "InventoryChangeAlarmPct=20" "LambdaDurationAlarmMs=720000"
```

Confirm the SNS subscription email AWS sends **before** you expect alarm mail. Until you click the confirm link, the topic subscription stays **Pending** and **no alarm emails are delivered**.

Alarm-only mail (no OK spam).

| Alarm | Fires when |
|-------|------------|
| `…-reprice-error` | `RepriceError` ≥ 1 |
| `…-sfn-failed` | Step Functions `ExecutionsFailed` ≥ 1 |
| `…-lambda-errors` | Any of the four Lambdas `Errors` ≥ 1 |
| `…-inventory-value-change` | \|`InventoryValue`\| hourly change ≥ **20%** (param) **and** `PriceUpdatesApplied` = 0 **and** \|`CardsInInventory`\| change &lt; 10% |
| `…-lambda-near-timeout` | Max Lambda `Duration` ≥ `LambdaDurationAlarmMs` (default **12 min** / 720000 ms) |

Inventory € swing with a big stock-count change (bulk add/remove) or with many LIVE applies is treated as expected and suppressed. A large € swing with **no** applies and a stable listing count is the worrisome case (bad export, market shock in the plan, etc.).

**Not alarmed (on purpose):** empty `CardsInInventory` (too arbitrary). S3 / log volume — `runs/` already expires at 90 days and log groups retain 90 days; CloudWatch `BucketSizeBytes` is daily and laggy, so skip until storage actually shows up on the bill.
