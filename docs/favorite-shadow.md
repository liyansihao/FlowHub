# Favorite dependency shadow implementation

Branch based on main ac08a4d. No remote deletion capability in shadow module or probe.

`favorite_shadow.py` stores a fail-closed dependency ledger, consumer evidence history and expiring shadow queue. Unknown maps to favorite_required=true. A release needs complete consumer coverage, durable snapshot proof, independent import proof, and evidence for every released consumer. A new dependency atomically withdraws a queued candidate; stale/conflicting versions cannot restore it. Queue certificates expire after 60 seconds.

Three narrowly scoped business save hooks record publication, legacy source and native acquisition stages in the caller's transaction. They record observations, not release certificates. The additive table can remain on code rollback. These hooks do not change payloads, stock, routing or retry rules. They have NOT been deployed to production in this phase.

`favorite_shadow_probe.py` reads the production database with mode=ro/query_only and uses only GET favorite/lists. It projects current consumer states into a separate shadow database. It is a bounded one-shot worker, not a continuously deployed observer. Reads are single-concurrency, 25 second request timeout, 180 second account budget and at most 50 pages. No historical item is released by the projector: lifecycle coverage and durable release evidence are incomplete. Snapshot projection is not an atomic online deletion fence and must never be used to execute real deletion.

Run:

    python -m flowhub.pipeline_modules.favorite_shadow_probe --data /path/to/production/data --output /separate/shadow/output

Output: timestamped evidence, latest.json, would-delete.json, shadow.sqlite3. It includes observed collection count, required/unknown counts, overlapping dependency reasons, and candidate evidence. Remote pagination remains a moving observation, not a server snapshot; an incomplete/failed enumeration is explicitly reported and must not be replaced by old counts.

The 24h candidate metric only describes candidates recorded by this ledger; starting it now cannot reconstruct 24 hours of historical releases. Account identity currently uses a token hash; credential rotation or alternate credentials for one ERP account are not certified. Unmatched or unsupported consumers remain unknown. No actual release producer is enabled until those coverage gaps and permanent snapshot/reprocessing contracts are proved. Official publication and other old paths are not falsely treated as covered by the three observation hooks.

Tests cover fail-closed defaults, individual missing proofs, shared consumers, new dependency withdrawal, out-of-order/conflicting events, expiry/restart, atomic observation rollback, 1000 candidate/500 withdrawal replay and malformed/moving pagination. Existing full regression passed. This is shadow instrumentation and evidence, NOT acceptance of real bulk deletion.
