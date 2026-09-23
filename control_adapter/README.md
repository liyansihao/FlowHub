# FlowHub Control Adapter 0.1

The adapter runs independently of the frozen `v1.0-stable` publisher. It does not import core Python modules, change business rules, expose a local HTTP listener, or submit products to ERP/Ozon. User login/identity checks are deliberately absent per the owner's decision. Anyone able to reach the public command API can issue valid controls; same-origin checks are not authentication. Device exchange uses a separate private bearer token.

## Layout and operation

- `flowhub_control/reader.py`: short read-only SQLite connections, bounded query time, rowid pagination, event/log cursors, allowlisted output. No database schema initialization.
- `storage.py`: separate local projection/outbox, first-success facts and durable command ledger.
- `controller.py`: existing seed switch with compare-and-set ownership, single production LaunchAgent, version checks. Never an alternate publisher.
- `agent.py`: exclusive file lock, outbound HTTPS, bounded network waits, replayable acknowledgements. Cloud failure does not stop the publisher.
- Website additions: `api/system.mjs`, `lib/system*.mjs`, one Vercel rewrite and three isolated `flowhub_system_*` tables. Existing review/operations code and UI stay byte-for-byte unchanged.

Run tests:

```
PYTHONPATH=control_adapter python3 -m unittest discover -s control_adapter/tests -v
npm test --prefix vercel-review
node --env-file=/private/path/cloud.env vercel-review/tests/system-database-integration.mjs
```

The database integration test creates/drops only its own synthetic test schema. All test DDL uses a transaction-local search path (Neon's pool is transaction-based). The live adapter never drops data.

## Private config and installation

Use a separate directory outside production, e.g. `/Users/mac/Desktop/ozon/FlowHub-control`. Deploy only `flowhub_control/` there, not the website worktree's older core. Store private `config.json` with mode 600:

```json
{
  "production": "/Users/mac/Desktop/ozon/FlowHub",
  "state_directory": "/Users/mac/Desktop/ozon/FlowHub-control/state",
  "url": "https://flowhub-review.vercel.app",
  "deployment": "flowhub-main",
  "token": "GENERATE_A_NEW_DEVICE_SECRET",
  "controls_enabled": false
}
```

Website environment: `SYSTEM_DEPLOYMENT_ID=flowhub-main`, `SYSTEM_PUBLIC_ORIGIN=https://flowhub-review.vercel.app`, `SYSTEM_AGENT_TOKEN` equals the new private token. Preserve existing DATABASE_URL and REVIEW_SYNC_TOKEN. Initialize the new schema with `systemSchema()` inside a database transaction before deploying API. Do not replace existing review tables.

Start the independent `com.flowhub.control-adapter` LaunchAgent with the adapter directory as working directory and `python3 -m flowhub_control.agent --config <private-file>`. Never call generic FlowHub `control start`. First validate read-only upload with controls disabled, then enable after test evidence. Production `com.flowhub.production` remains unchanged.

## API

Base `/api/system/v1/`:

| GET | Returns |
|---|---|
| `status` | worker heartbeat/process, source revision, instance, switches, disk, state version, queue counts |
| `activity` | live product leases (not a claim of active network work) |
| `metrics?date=YYYY-MM-DD` | Shanghai daily first verified successes, same-cohort ratio including pending, observed failure transitions, step attempts/durations |
| `throughput?date=YYYY-MM-DD` or `throughput?from=EPOCH&to=EPOCH` (up to 7 days) | same metrics plus 24 hourly buckets and rolling 60 minutes |
| `tasks?state=publishing&limit=50&cursor=...` | bounded product-state projection, live aggregate counts |
| `errors?limit=50&cursor=...` (newest first) | redacted error events and recent log alerts |
| `products/{sku}?seller_id=...` | separate owner/store identities and recent stage events |
| `commands/{id}` | command and execution receipt |

POST `commands` requires JSON, exact site Origin, `X-System-Request: 1` and:

```json
{"action":"pause","idempotency_key":"unique-request-id","expected_state_version":"from-status","target_revision":"from-status","expires_at":1799999999}
```

Expiry is epoch seconds, future and within 30 minutes. Actions: `start`, `resume`, `pause`, `stop`. `202` means queued only. Commands serialize per deployment. Reuse identical idempotency key/body to recover a lost HTTP response; do not generate a new key as an automatic retry. A stale/disconnected device rejects new commands. An active command remains tracked through agent outages and is reconciled locally.

POST `agent/exchange` is device-only; uses `Authorization: Bearer <SYSTEM_AGENT_TOKEN>`. Never put this token in browser JavaScript. Every observation includes observed/uploaded time, stale and coverage. Startup backfill is bounded and can take several minutes (3-second catch-up interval, 15-second steady interval, capped network backoff); inspect coverage and upload_pending_after_batch. No data means unknown/partial, not zero failures or confirmed healthy. Product projections include their observation times, and existing archived rows are not silently deleted.

## Exact control limitations

- `pause`: pauses seed/new source/admission and related repair work. Submitted products, queued reviews, reconciliation and listing controls can continue. It does not promise that immediately no new listing completes.
- `resume`: only restores an adapter-owned pause which has not subsequently been changed locally. It cannot override an existing manual pause.
- `start`: starts only the saved, enabled production plan through `com.flowhub.production`; a loaded but unhealthy service is reported, not restarted by the adapter. Source and disk checks apply.
- `stop`: requests drain and pauses new admission. The frozen core has independent reconciliation/manual listing lanes and **no global quiescence barrier**. This first version deliberately cannot automatically unload a running worker while proving all remote writes are settled. It returns `draining`, then bounded `needs_attention` with blockers and leaves readback alive. Already stopped is acknowledged. No force-kill or fake `stopped` success. A future automatic-stop implementation needs a separate narrowly reviewed core cooperation contract; it is not smuggled into this release.

## Statistics boundaries

Success means earliest stock_verified with verified=true/backend=maozi_follow, deduplicated by owner/source SKU/store. updated timestamps, repeated readbacks and unlisting/relisting do not create extra success. Day is Asia/Shanghai. Publication intent cohort started_at defines the ratio denominator; uncompleted intents remain included. Existing history is only as complete as the retained publication event journal; first-success facts persist separately thereafter.

Failure event counts are not failed product counts. `failures:null` explicitly marks unavailable exact historical failure-transition time; `observed_failures` records newly observed transitions since adapter startup and distinguishes failed/rejected/quarantined. Step attempts are observed attempts, not proof of a complete lifetime retry count. Partial internal-task liveness stays `not_fully_observable`.

## Rollback

1. Disable command acceptance by setting local controls_enabled=false and exchange a new snapshot. Check the outstanding command ledger first.
2. Unload only `com.flowhub.control-adapter`; it is not the publisher's LaunchAgent.
3. Promote the recorded previous website deployment if needed. Preserve new cloud tables/local ledger and production data; never roll back a database snapshot.
4. An adapter-owned pause is not silently resumed on uninstall; inspect it and explicitly choose resume before uninstalling when desired.

The core tag and main commit are not moved for this adapter release. Read-only status integration is not another long-run stability certification.
