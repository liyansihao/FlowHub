# SQLite transaction boundary (phase 1)

## Problem

FlowHub has several workers and modules that open the same SQLite database and
independently start `BEGIN IMMEDIATE` transactions. SQLite still serializes the
file, but each caller races to become the writer and a long or repeated claim
can make unrelated work wait or fail with `database is locked`.

## Scope of this phase

This phase introduces one explicit `Database.write_transaction()` boundary and
migrates only the high-frequency claim/update paths used by publication,
admission, and source acquisition. It does not change listing decisions,
retry policy, schema shape, or data. Reads keep using `Database.connect()`.

The boundary uses a per-database advisory lock file plus the existing SQLite
`BEGIN IMMEDIATE`. The advisory lock covers separate `Database` instances and
worker processes; the in-process reentrant lock avoids duplicate acquisition
from threads sharing an instance. The transaction body remains responsible for
being short and must not nest another `write_transaction()` call.

## Rollback

The change is code-only and reversible by reverting its single commit. It does
not migrate or delete rows. The new `*.writer.lock` file is disposable runtime
coordination state and is outside the SQLite database.

## Not covered yet

Other write paths still use ordinary `connect()` contexts or their own
transactions. They will be migrated in separate, measured phases after this
boundary is validated; this branch is not a production deployment.
