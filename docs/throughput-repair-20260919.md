# Publication scheduler repair

The user's ordered repair is: safely reclaim parked Windows ERP slots, remove blocking database units from the event loop, then evaluate a bounded pacing trial.

- A parked `same_product_confirmed`, `not_listed`, `quarantined` or `delisted` product can release its transport assignment only after its pipeline lease ends and all associated ERP commands settle. Running commands and unknown write outcomes keep the original assignment. This never requeues the parked product or changes its offer, review or publication journal.
- Queue claiming, lease renewal, final persistence, remote transport routing and Ozon API reservation/accounting run complete SQLite units in background threads. Cancellation drains a transaction before returning; a cancelled claim explicitly releases its own token. Existing fences, priorities, final verified criteria and remote backoff remain unchanged.
- A `(state,due)` queue index and a `(device,created)` ERP command index support the affected lookups. Runtime module checks and worker heartbeat DB writes also leave the event loop.
- Tests hold an actual SQLite writer lock while asserting other coroutines continue, compete for a single task and cancel an in-flight claim. Remote-slot tests cover parked states, active leases, live requests, unknown writes and acknowledged responses. Existing publication and official API regressions remain required.

Other synchronous DB work remains; this patch does not claim every blocking site has been removed. Runtime observations determine whether a pacing trial is appropriate. The existing global 2500ms policy is unchanged by this code; any 2000ms trial must have an expiry, failure/429 baseline, automatic rollback and a recorded result. Never retry an unknown publication to manufacture throughput.

Post-deployment follow-up: a read-only production measurement of review snapshot
construction took 31.313 seconds for 14,375 cards. It was still synchronous inside
the worker event loop. Move snapshot construction and application of incoming
review decisions to the same cancellation-draining database helper without
changing decision semantics or outgoing protocol. Move the generic worker claim
transaction off-loop as well. Temporary SQLite busy/locked errors at idle claim
or heartbeat now defer instead of terminating the worker and interrupting other
operations; other database errors continue to surface. Regression covers lock
competition, preservation of the review-sync lock during cancellation, and
non-lock error propagation.

The shared source loop also synchronously reads all verified publication bodies
and source backlog on every cycle. Read-only measurements found 3,018 publication
records / 199,587,211 JSON bytes in 2.883 seconds, plus backlog evaluation in
3.008 seconds. Move full store synchronization, backlog calculation, synchronous
other-seller discovery preparation, selection, and event recording off-loop.
Keep existing per-source file locks held until cancellation-drained work finishes;
leave all source selection rules and publication authorization unchanged.

A stack-only loop observer located remaining >5 second stalls in compareBot
review reservation and result persistence, listing-control queue claim, source
claim, favorite cleanup candidate/receipt reads, and publication record saves.
Move these exact synchronous units off-loop without changing their decisions.
Cancellation drains claims and releases only the matching lease. Project only
required publication identity/product fields for source enrollment and finish
its read cursor before seed inserts, avoiding read-to-write cursor contention
and loading large historical request logs into the enrollment pass.
