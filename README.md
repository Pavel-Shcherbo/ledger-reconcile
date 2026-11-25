# Ledger Reconcile

Compare two normalized payment CSV exports and retain a deterministic exception report with hashes of the actual input bytes. Open-source prototype with synthetic examples. Validated with Python 3.14.6.

```sh
python3 reconcile.py examples/ledger.csv examples/provider.csv --output report.json
python3 -m unittest -v
```

Render the JSON as an offline HTML report (no server or dependencies):

```sh
python3 report.py examples/report.json --output exceptions.html
node test_filters.cjs exceptions.html
```

Open `exceptions.html` in a browser. The report includes status/search filters,
side-by-side records, explanations, full-export currency totals and input hashes.
Search covers the visible row text. Filters never change full-export totals.
CSV values are escaped as text; no external assets or network requests are used.
Existing HTML files are never overwritten. The generated demonstration is in
`examples/report.html`; it contains only synthetic data.
The renderer requires JSON objects for the report, input provenance and currency
totals. Wrong container types exit with code 2 and a short error before an output
file is created.

Report tests cover the import-to-HTML pipeline and hostile text escaping. Node
controls exercise the actual generated filtering script with a minimal DOM
stand-in; native browser layout and accessibility have not yet been verified.

The example exits **1** because it intentionally contains exceptions: two matched references, one amount mismatch, one missing record on each side, and one ambiguous split settlement. Exit **0** means no exceptions in the supplied exports; **2** means an input/output error. Reports are created exclusively, never overwritten.

CSV columns, in order: `record_id,reference,currency,amount_minor`. IDs are unique within each source. Exact repeated IDs with identical content are ignored and counted; conflicting versions are rejected. References and currencies must match exactly. Amounts are signed integers in the currency's minor units, already normalized by the caller. Refunds need distinct references. Currency codes are checked for shape only; the tool does not infer currency exponents or convert currencies.

Multiple records sharing a reference and currency are **ambiguous**, even if sums agree. The program does not silently net fees, auto-match installments, accept tolerances, or repair source records. Totals remain separated by currency; equal totals do not establish transaction-level correctness. Reports contain record identifiers and amounts: use synthetic inputs for public demonstrations.

## Run the correction demo

### Export exceptions for manual review

```sh
python3 exceptions_csv.py examples/report.json --output exceptions.csv
```

The UTF-8 CSV has one row per non-matched group, including ambiguous groups.
Record ID lists are JSON arrays inside CSV cells. Left/right sums and deltas use
signed integer minor units, with currencies kept separate. The supplied example
produces four rows; a report with no exceptions produces only the header.
The exporter checks displayed integer amounts and deltas, but does not rerun
matching or validate every aspect of the supplied report. Retain the original
JSON for input hashes and full-export totals; the CSV is a review view, not a
replacement for that evidence or an import format.

Formula-like reference/currency text (leading `=`, `+`, `-`, `@`, including
preceding whitespace, or an initial tab/newline) is prefixed with an apostrophe.
This changes the display value; use the original JSON for exact identifiers.
CSV quoting handles commas, quotes and newlines. Spreadsheet import behavior
varies, so treat identifier columns as text and do not enable formulas from
untrusted data. Existing output files are refused; invalid input exits 2.

### Reproduce a correction

From a fresh checkout, use a new output directory:

```sh
mkdir demo-output
python3 store.py import examples/provider-v1.csv --db demo-output/demo.sqlite --stream provider --basis gross
python3 store.py check-refunds --db demo-output/demo.sqlite --stream provider
python3 store.py export --db demo-output/demo.sqlite --stream provider --output demo-output/initial.csv
python3 store.py import examples/correction.csv --db demo-output/demo.sqlite --stream provider --basis gross --correct evt1:1
python3 store.py batches --db demo-output/demo.sqlite --stream provider
python3 store.py history evt1 --db demo-output/demo.sqlite --stream provider
python3 store.py export --db demo-output/demo.sqlite --stream provider --as-of-batch 1 --output demo-output/before.csv
python3 store.py check-refunds --db demo-output/demo.sqlite --stream provider --as-of-batch 1
python3 store.py export --db demo-output/demo.sqlite --stream provider --output demo-output/after.csv
python3 reconcile.py demo-output/before.csv demo-output/after.csv --output demo-output/comparison.json
# The previous command exits 1 because one amount differs.
python3 report.py demo-output/comparison.json --output demo-output/comparison.html
```

The historical export matches initial.csv byte for byte. The comparison contains
three matching groups and one amount mismatch. Open comparison.html locally.
The event history retains both revisions and their source CSV hashes.

## Validation and scope

Run `python3 -m unittest -v` for Python checks and
`node test_filters.cjs examples/report.html` for the generated filter script.
Node.js is needed only for this additional check. Runtime commands use the
Python standard library.

`test_cli.py` runs the public commands as separate processes in a temporary
directory. Its synthetic scenario checks an amount correction, a historical
export, restored backup history and SHA-256, an expected reconciliation
exception, HTML generation, and refusal to overwrite an existing backup.
Run it alone with `python3 -m unittest test_cli -v`.

The `Checks` GitHub Actions workflow runs the Python suite, Ruff E/F/I and Node
filter controls on Ubuntu with Python 3.14 and Node 22 for pushes to main and
pull requests. Actions are pinned to commit hashes and the job has read-only
repository permissions. A green run covers these automated checks; it does not
establish native browser accessibility, other operating systems or production
recovery readiness.

The HTML source, escaping and filter logic have automated checks. Visual browser
review remains pending because the browser automation tool blocks local files.
The prototype uses synthetic inputs. Real vendor adapters, backup retention and recovery procedures,
migrations and production accounting validation remain future work.

## Explicit normalization adapter

`synthetic-provider-v1` is a documented demonstration format, not an adapter for
Stripe, a bank, or any other real provider. Header order:

```csv
event_id,reference,kind,parent_reference,currency,gross_minor,fee_minor,net_minor
```

```sh
python3 adapter.py examples/provider-v1.csv --basis gross --output-dir gross-batch
python3 adapter.py examples/provider-v1.csv --basis net --output-dir net-batch
```

Each new batch directory contains `normalized.csv` readable by `reconcile.py`,
and `audit.json` retaining the source hash, generated CSV hash, comparison basis,
gross/fee/net values and refund parent references. Existing directories are
rejected. Writes are not transactional; an I/O error can leave an incomplete
directory, which must not be treated as a completed import.

The format uses signed integer minor units: payments have positive gross,
refunds have negative gross and their own distinct reference. Fees are
nonnegative charges, and `net_minor = gross_minor - fee_minor`. A charged refund
fee makes its net amount more negative. Fee reversals and other event kinds are
not supported. Exact duplicate events count once; conflicting IDs are rejected.

Choose the **same comparison basis on both sides**. The reconciliation core sees
only normalized amounts and cannot detect a gross-versus-net mismatch itself.
The audit retains the choice; do not discard it. The example USD events total
7000 gross, 275 fees and 6725 net. Separate EUR totals are not added to USD.

Refund parent references are retained without inventing links. The adapter does
not check whether a parent exists in another batch, matches the currency, or
has already been fully refunded. Distinct partial refund references stay
separate; repeated references remain ambiguous in reconciliation. Provider
normalization rules must be verified before adapting this to real exports.

## Persistent imports and corrections

```sh
python3 store.py import examples/provider-v1.csv --db demo.sqlite --stream provider --basis gross
python3 store.py export --db demo.sqlite --stream provider --output current.csv
```

The stream name separates independent sources/accounts; each stream fixes its
gross/net basis at its first import. Event IDs are unique within that stream.
Exactly repeated input bytes return the original batch summary with
`replayed_batch: true` and perform no event updates. Those summary counts describe
the original batch, not new changes on replay. Overlapping new exports skip
identical known events. Omission of an event does not delete it: exports contain
the accumulated latest state, an accumulated state. Use --as-of-batch to select earlier stored revisions.

Conflicting event content rejects the whole batch. To apply a reviewed amount
correction, provide the current revision explicitly:

```sh
python3 store.py import corrected.csv --db demo.sqlite --stream provider --basis gross --correct evt1:1
```

This creates revision 2 and retains revision 1. Incorrect expected revisions,
unchanged corrections, unknown events and identity changes are rejected. A
correction can change gross/fee/net amounts while preserving their arithmetic;
reference, currency, payment/refund kind and refund parent must stay unchanged.
Replaying an older already-imported batch cannot revert a correction. Failed
batches roll back their revisions and batch journal entry in one transaction.

`streams`, `batches` and `revisions` retain the basis, source hashes, timestamps,
explicit correction requests and versioned payloads. This is an audit trail,
not tamper-proof storage; a database owner can edit it outside the program.
Unknown/nonempty SQLite databases and unsupported schema IDs are rejected.
Export opens the database read-only and refuses to overwrite an output file.

Before using real data: add verified adapters, review refund findings,
define backups/retention/access controls and test actual concurrency/crash cases.

## Refund integrity across import batches

```sh
python3 store.py check-refunds --db demo.sqlite --stream provider
```

The command reads the latest stored revisions without modifying the database.
It reports missing payment parents, mismatched currencies, ambiguous payment
references and total gross refunds exceeding the matched payment gross amount.
Fees are excluded from this limit. Parent matching stays within the named source
stream and currency. Multiple same-reference payments are not silently summed.

Exit 0 means no findings in the stored events; exit 1 means integrity findings;
exit 2 means a read/input error. An incomplete export can produce a missing-parent
finding that disappears when the parent is imported later. Imports still retain
such events: this check is an explicit reporting step, not an import rejection
or an assertion that all payment history is present. Amount corrections are
included automatically through their latest revisions. Use `check-refunds --as-of-batch ID` with a committed batch ID from the named
stream to rerun the check against the revisions available through that import.
An unknown or foreign batch exits with code 2. Real provider fee/refund
policies remain outside this prototype's validation.

## Inspect an event

Run `python3 store.py history evt1 --db demo.sqlite --stream provider` to list revisions in order with their import batch IDs, source CSV SHA-256 hashes and event data. An unknown event returns exit code 2.

Run `python3 store.py batches --db demo.sqlite --stream provider` to list committed imports in batch order. The JSON includes UTC timestamps, source CSV SHA-256 hashes, requested revisions and import counts. An exact replay uses the existing batch. An unknown stream exits with code 2.

## Export a historical snapshot

Use `python3 store.py export --db demo.sqlite --stream provider --as-of-batch 1 --output before.csv` to export the latest revision of each event available through that batch. Choose an ID from the named stream’s `batches` output. Omit the flag for the current view. The command leaves SQLite unchanged and requires a new output file.

## Back up the local store

```sh
python3 store.py backup --db demo-output/demo.sqlite --output snapshot.sqlite
```

The command uses SQLite Backup API to include committed WAL data and copies all
streams, import batches and revisions. It checks the copy with SQLite quick_check
and foreign_key_check, then publishes the completed file without replacing an
existing output. JSON output includes the backup file SHA-256. The temporary copy
requires space on the output filesystem, which must support hard links.
On POSIX systems the completed backup has mode 0600 (owner read/write),
independent of the process umask. This does not encrypt the file or configure
access controls on other platforms.
Exit 2 indicates failure.

To inspect a restored copy, pass snapshot.sqlite as --db to the read-only export,
batches, history or check-refunds commands. Keep the backup in a protected
location and define retention and recovery procedures before using real data.

For an isolated recovery exercise, create a new working database from a snapshot:

```sh
python3 store.py backup --db snapshot.sqlite --output restored.sqlite
python3 store.py batches --db restored.sqlite --stream provider
python3 store.py history evt1 --db restored.sqlite --stream provider
```

Use the stream and event IDs from your snapshot. Keep the original snapshot
unchanged and run any subsequent imports against restored.sqlite. The recovery
regression removes a temporary synthetic source database, creates a working copy,
and checks that replay cannot revert a correction, stale revisions still fail,
and an explicit next correction retains all three revisions. This bounded check
does not establish recovery after arbitrary corruption or a host failure.
