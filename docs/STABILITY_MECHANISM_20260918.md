# Stability controls, 2026-09-18

This release adds opt-in account-level collection capacity admission and persistent reservations, bounded zero-yield cleanup backoff, original-publication source-draft lineage recovery after credential rotation, repair lifecycle tracking, deduplicated local monitoring, and a login supervisor service that waits for any existing supervisor lock.

Cleanup additionally limits remote candidate examinations to 20 per batch and persists a round-robin draft cursor. Failed validation cannot cause an unbounded scan or permanently starve later candidates. Both successful deletes and examined candidates are separately counted.

The draft cleanup scope remains backed-up, currently verified selling products only. Fallback snapshots must match the original publication's owner, SKU, seller, store and offer. When the publication holds a repair reference rather than a full snapshot, its exact draft ID must resolve to one complete local source snapshot. Remote source identity, sale/stock status, active-task guards and deletion receipts remain mandatory. Unknown deletion results are not retried.

`data/collection-capacity.json`: enabled defaults false, stop_ratio .95, resume_ratio .80, max_age 60 seconds. Enable only on the production controller that owns creation. All new collector draft writes reserve space durably. Existing source reads and recovery continue when capacity is blocked. Unknown writes retain their reservations; confirmed reservations clear only after a later capacity observation. A missing/invalid observation fails closed for new writes. This gate covers SourceCollector, not unrelated clients writing to the same ERP account.

`data/stability-policy.json`: enabled defaults false, max_stall_seconds 7200, max_reentries 3. New lifecycle observations preserve total repair attempts and reentries across stage changes without refreshing approval timestamps. Lifecycle enforcement uses the existing isolation lane and respects active leases. Existing isolation rules remain in place. Old request ages are reported but do not instantly become lifecycle stalls on first observation.

`python -m flowhub.stability_monitor --data ... --output ...` samples read-only local databases and appends a compact time series and alert transitions. It never attempts remote writes or restarts. Repeated unchanged alerts are not repeated, resolved alerts receive one transition, and failed sampling preserves existing alerts. Low-output alerts are suppressed during an explicit publication pause.

Initial low-output threshold: fewer than 10 first completions in 60 minutes with at least eight missing-field tasks. Once opened, this alert remains until at least 15 completions/hour or the repair backlog falls below eight, preventing a few completions from falsely declaring recovery. These are operating thresholds, not promised throughput. Pending work with zero completions in 30 minutes is separately reported.

`scripts/install_stability_services.py --root ...` installs two Mac login agents: five-minute sampling, and recovery of the worker-only production supervisor. The recovery entry waits on the existing supervisor lock; it does not start a duplicate writer. The setup is login-based, not a guarantee of pre-login service availability. Existing production must have its database and Python environment. The worker-only setting is deliberate for the current deployment; do not use this installer for a different topology without adapting it.

Validation: 223 targeted tests passed (capacity, source collection, cleanup, lifecycle, isolation, publication recovery, cluster compatibility and runtime identity). F checks and whitespace checks passed. A stale baseline test expecting review deletion was reproduced on the original production commit and corrected to assert preserved review history plus forced revaluation, matching the previously shipped behavior.

Remaining acceptance: actual safe capacity release, process/lock identity after controlled deployment, Windows version reporting, host reboot/login recovery, 24/72-hour sustained output. No stable-throughput claim follows from the unit tests.

Rollback: disable the two opt-in policy files to stop new capacity/lifecycle enforcement; do not erase reservations or journals. Existing quarantined items require original-intent review, not deletion/requeue. Preserve the monitoring history and deletion receipts. Roll back code only through a drained deployment; never restore an older business database over current publications.
