# Windows production transport preparation

## Implemented

- `cluster_erp.py`: encrypted command store, device-bound lease, one-time begin,
  no lease reassignment/replay, result idempotency, endpoint allowlist.
- `cluster-flowb.mjs`: preserves the existing FlowB bridge and global account
  pacing; delegates only the final ERP network hop.
- `cluster_routing.py`: exact `(owner, sku, seller)` routing. Existing Mac SKU/store
  locks, journal, approval, Feishu exclusions and stock readback remain in control.
- Windows `production_agent.py`: shared single-instance lock, durable dispatch
  intent, no ERP retries, no redirects/proxy environment, TLS verification.
- `route_windows_canary.py`: local-only route for one existing approved product,
  refuses enablement until the registered version-2 device is online.

## Deployment boundary

Phase-1 clients cannot execute ERP tasks and cannot install their own upgrade.
User must run Upgrade-Production.cmd on Windows; existing device token is reused.
Coordinator can expose v2 endpoints while remaining acceptance_only: no ERP
commands are queued until an explicit local route is configured.
Production worker code is safely reloaded using scripts/reload_pipeline.py.

## Verified live on 2026-09-15

- Registered Windows device `windows-02` is running protocol 2.
- An ERP shops read through the Windows bridge returned HTTP 200 / ERP code 1.
- Source `1846192547` was rejected by the platform with
  `ML_INCORRECT_VOLUME_WEIGHT`; no stock was written to bypass this error.
- Source `1844527669`, offer `flowef-live99-3beaa18ae86f896e0613`, shop `120213`,
  product `6346667384`, Ozon SKU `5804496524` reached `stock_verified` and
  `selling`, stock 99, without platform issues at 2026-09-15 23:28:18 CST.
  Mac had submitted its initial import; Windows handled subsequent processing.
- Continuous dispatch added after this canary: one active product per Windows
  device, new assignments only from approved/prepared pipeline states, release
  after selling/needs_review/failed/rejected. Original Mac workers handle other
  products. Unresolved remote assignments are not automatically reassigned.
- `production-routing.json` enables continuous mode for the registered device.
- Tests: 41 passed covering relay fencing, authorization, lost responses,
  concurrent assignment, offline behavior and existing publication guards.

## Limits

The Mac stays the authoritative production engine. This is network execution
offload, not a replicated production database. One worker accepts one ERP command
at a time. All PCs still share the original account pacing budget. Automatic
category/sales-based multi-shop allocation is outside this implementation.
On unknown network outcomes, central publication recovery owns the decision;
Windows never resubmits a command. Device revocation prevents new commands but
cannot retract a request already accepted by ERP.
