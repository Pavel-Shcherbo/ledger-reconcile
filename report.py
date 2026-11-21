"""Render a local reconciliation JSON report as a self-contained HTML file."""

import argparse
import html
import json
from pathlib import Path

REASONS = {
    "matched": "One record on each side, with equal signed amounts.",
    "amount_mismatch": "Same reference and currency, different signed amounts.",
    "left_only": "No record with this reference and currency in the right export.",
    "right_only": "No record with this reference and currency in the left export.",
    "ambiguous": "Multiple records on a side; no automatic split or netting rule.",
}


def escape(value):
    return html.escape(str(value), quote=True)


def require_object(value, name):
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a JSON object")
    return value


def render(report):
    require_object(report, "Report")
    if report.get("schema_version") != 1:
        raise ValueError("Unsupported report schema")
    input_proofs = require_object(report.get("inputs", {}), "Input provenance")
    currency_totals = require_object(report["totals_by_currency"], "Currency totals")
    rows = []
    counts = {status: 0 for status in REASONS}
    for item in report["items"]:
        status = item["status"]
        if status not in REASONS:
            raise ValueError("Unknown reconciliation status")
        counts[status] += 1
        sides = []
        for side in ("left", "right"):
            records = item[side]
            sides.append(
                "<ul>"
                + "".join(
                    f"<li><code>{escape(r['record_id'])}</code>: "
                    f"{escape(r['amount_minor'])}</li>"
                    for r in records
                )
                + "</ul>"
                if records
                else "—"
            )
        rows.append(
            f'<tr data-status="{status}"><th scope="row">'
            f"{escape(item['reference'])}</th><td>{escape(item['currency'])}</td>"
            f"<td>{status.replace('_', ' ')}</td><td>{sides[0]}</td>"
            f"<td>{sides[1]}</td><td>{escape(item['delta_minor'])}</td>"
            f"<td>{REASONS[status]}</td></tr>"
        )
    options = '<option value="all">All statuses</option>' + "".join(
        f'<option value="{s}">{s.replace("_", " ")} ({n})</option>'
        for s, n in counts.items()
    )
    inputs = "".join(
        f"<dt>{escape(side)}</dt><dd><code>{escape(proof['sha256'])}</code>"
        f" — {escape(proof['unique_records'])} unique records; "
        f"{escape(proof['exact_duplicates_ignored'])} exact duplicates ignored</dd>"
        for side, proof in input_proofs.items()
    )
    totals = "".join(
        f"<li>{escape(currency)}: left {escape(t['left_minor'])}, "
        f"right {escape(t['right_minor'])}, delta {escape(t['delta_minor'])}</li>"
        for currency, t in currency_totals.items()
    )
    return (
        """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ledger Reconcile — exception report</title>
<style>
:root {font-family:system-ui,sans-serif;color:#172b36;background:#f3f5f4}
body {max-width:1440px;margin:auto;padding:32px} h1 {margin-bottom:8px}
.note {max-width:90ch;color:#44565f}
label {display:inline-block;margin:12px 20px 12px 0}
input,select {font:inherit;padding:8px;border:1px solid #768a91;border-radius:4px}
.scroll {overflow:auto;background:white;border:1px solid #b6c3c5}
table {border-collapse:collapse;width:100%;text-align:left}
th,td {padding:12px;vertical-align:top;border-bottom:1px solid #dce3e4}
thead {background:#dfe9e8} tbody th {min-width:150px;overflow-wrap:anywhere}
tr[data-status="amount_mismatch"],tr[data-status="ambiguous"] {background:#fff4dd}
ul {margin:0;padding-left:20px} dd {overflow-wrap:anywhere;margin-bottom:12px}
[hidden] {display:none!important} #empty {padding:16px;background:#fff4dd}
@media print {label {display:none} body {padding:0}.scroll {overflow:visible}}
</style></head><body><header><p>LOCAL RECONCILIATION / SCHEMA 1</p>
<h1>Payment exception report</h1>
<p class="note">Exact reference and currency matching. Signed amounts are shown in
minor units, without currency conversion. A zero total delta does not prove that
individual payments match. This report describes only the supplied exports;
it does not establish settlement finality or completeness.</p></header>
<section aria-label="Filters"><label>Status <select id="status">"""
        + options
        + """</select></label>
<label>Reference, currency or record ID <input id="query" type="search"></label>
<p id="count" role="status" aria-live="polite"></p></section>
<div class="scroll"><table><caption>All values are signed integer minor units</caption>
<thead><tr><th scope="col">Reference</th><th scope="col">Currency</th>
<th scope="col">Status</th><th scope="col">Left records</th>
<th scope="col">Right records</th><th scope="col">Left − right</th>
<th scope="col">Explanation</th></tr></thead><tbody>"""
        + "".join(rows)
        + """</tbody></table></div>
<p id="empty" hidden>No rows match the selected filters.</p>
<noscript>Filters require JavaScript. All rows are visible without it.</noscript>
<section><h2>Full-export totals (unaffected by filters)</h2><ul>"""
        + totals
        + """</ul></section>
<section><h2>Input provenance</h2><dl>"""
        + inputs
        + """</dl>
<p>SHA-256 identifies the bytes read by the importer.
It does not authenticate their origin.</p>
</section><script>
const rows = Array.from(document.querySelectorAll('tbody tr'));
const statusFilter = document.getElementById('status');
const query = document.getElementById('query');
function filterRows() {
  const text = query.value.trim().toLowerCase();
  let visible = 0;
  for (const row of rows) {
    const match = (statusFilter.value === 'all'
      || row.dataset.status === statusFilter.value)
      && row.textContent.toLowerCase().includes(text);
    row.hidden = !match;
    if (match) visible++;
  }
  document.getElementById('count').textContent =
    `${visible} of ${rows.length} groups shown`;
  document.getElementById('empty').hidden = visible !== 0;
}
statusFilter.addEventListener('change', filterRows);
query.addEventListener('input', filterRows);
filterRows();
</script></body></html>"""
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        rendered = render(json.loads(args.report.read_text(encoding="utf-8")))
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(rendered)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(2, f"{error}\n")


if __name__ == "__main__":
    main()
