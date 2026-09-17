# Windows compute execution v3

## Implemented

- Separate ERP process and persistent compareBot compute process. Model failure
  restarts compute only; ERP remains available. Shared local locks reject duplicate
  instances. Credentials travel in private authenticated requests, live only in
  worker memory, and are encrypted in the Mac command store.
- Same vendored compareBot search, DINO model revision and screening thresholds.
  Rank, screen and dossier extraction are fixed data operations, not shell commands.
- Compute jobs are bound to the authorized device, input digest and lease. Expired
  or conflicting results cannot overwrite a later review. Mac rechecks product,
  supplier and screening policy before accepting a publication approval.
- One compute slot per ready device; ERP can run alongside it. One additional
  review lane initially becomes available when an authorized v3 device is online.
  Existing Mac workers continue if no suitable idle Windows worker is available.
- Known platform wait phases release the Windows ERP device only after its current
  pipeline step and ERP command finish. Existing journal/readback continues on Mac.
  Unknown manual-review outcomes are not automatically replayed or reassigned.
- Dossier extraction is supported and source-bound. Default dispatch targets rank
  and screen, because shipping tiny field extractions can cost more time than local
  work. Set `kinds` in `compute-policy.json` to include dossier only after measurement.

## Remains on Mac

Central selection, store routing, deduplication, profit calculation/confirmation,
credential management, persistence, final publication authorization and journal.
This upgrade does not make a Windows copy of the production database. It does not
implement autonomous multi-master scheduling or new category/sales-based routing.

## Delivery and validation

- `output/FlowHub-Windows-Full-Agent-v3.zip`: installer, portable source, pinned model
  as ordinary Windows-compatible files, dependency constraints and startup scripts.
- Model weights match the existing Mac revision
  `ed25f3a31f01632728cabb09d1542f84ab7b0056`.
- Verified Windows cp312 wheels exist for the pinned torch/numpy/Pillow versions;
  transformers universal wheel exists. Installation itself still requires Windows.
- Automated tests cover compute fencing, encryption, capacity, product/supplier
  binding, source/variant checks, stage handoff and existing publication guards.
- Real packaged worker on Mac loaded the bundled model offline and returned a
  dossier result. Actual screen CLI rejected a low-score fixture and returned
  manual review when Qwen credentials were absent. No real product was approved
  by these fixture tests.

## Windows handoff

Close the old production window, extract the v3 package, run Upgrade-Full.cmd.
Python 3.12 required. Dependencies are installed in a separate compute-venv.
No new enrollment is necessary. The model is included to avoid a model download.
Wait for `FlowHub v3 ready`, then check live compute registration and real rank /
screen results from the Mac. Windows v3 live validation is not yet complete.
The existing v2 production connection remains usable until the user upgrades.
