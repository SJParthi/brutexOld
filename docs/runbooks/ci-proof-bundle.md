# CI Proof Bundle — Operator Runbook

> **What:** Every CI run on BruteX uploads a single `proof-bundle`
> artifact containing the coverage report, O(1) benchmark results,
> Trivy filesystem scan, and CycloneDX SBOM for that exact commit.
> It's retained for 90 days.
>
> **Why:** To give the operator a single auditable artifact per merge
> proving the state of the codebase at merge time — not trust-us,
> but show-you-the-receipts.

## Contents

Every proof bundle is a zip with this layout:

```
proof/
├── MANIFEST.txt           ← run metadata + contents table + verify steps
├── coverage/
│   └── coverage.xml       ← pytest-cov XML (100% line+branch)
├── bench/
│   └── bench-results.json ← pytest-benchmark SLA report
├── trivy/
│   └── trivy-results.sarif ← filesystem vuln scan (SARIF v2)
└── sbom/
    └── sbom-python.json    ← CycloneDX v1.5 supply-chain inventory
```

## How to download

### Via the GitHub UI

1. Go to any completed CI run: `https://github.com/SJParthi/bruteX/actions`
2. Scroll to the "Artifacts" section at the bottom
3. Click `proof-bundle` → downloads `proof-bundle.zip`

### Via the `gh` CLI

```bash
gh run list --workflow=ci.yml --branch=main --limit 5
gh run download <RUN_ID> --name proof-bundle -D ./proof
```

### Via the GitHub API

```bash
curl -sSL -H "Authorization: Bearer $GH_TOKEN" \
  "https://api.github.com/repos/sjparthi/brutex/actions/runs/<RUN_ID>/artifacts" \
  | jq '.artifacts[] | select(.name == "proof-bundle") | .archive_download_url'
```

## How to verify each piece

### `coverage.xml` — 100% coverage gate

```bash
# Python parser
python -c "
import xml.etree.ElementTree as ET
root = ET.parse('proof/coverage/coverage.xml').getroot()
line_rate = float(root.attrib['line-rate'])
branch_rate = float(root.attrib['branch-rate'])
print(f'line rate:   {line_rate:.2%}')
print(f'branch rate: {branch_rate:.2%}')
assert line_rate >= 0.90, f'line coverage {line_rate} below 90% floor'
assert branch_rate >= 0.85, f'branch coverage {branch_rate} below 85% floor'
print('✅ coverage floors met')
"
```

### `bench-results.json` — O(1) SLA gate

```bash
# Check O(1) ratio assertions
python -c "
import json
data = json.load(open('proof/bench/bench-results.json'))
for b in data['benchmarks']:
    name = b['name']
    mean_ns = b['stats']['mean'] * 1e9
    print(f'{name}: {mean_ns:.0f} ns')
"
```

The actual O(1) ratio assertions live inside the test code
(`test_get_ratio_below_3x`, `test_acquire_ratio_constant`, etc.)
and run as normal pytest tests — if they fail, `bench-results.json`
reflects a failed run and CI blocks the merge at the bench job.

### `trivy-results.sarif` — filesystem vuln scan

```bash
# Check no CRITICAL or HIGH findings
jq '.runs[].results | length' proof/trivy/trivy-results.sarif
# should be 0 or only warnings; any HIGH/CRITICAL blocked merge
# at the trivy job (it's a blocking gate in ci-ok).
```

### `sbom-python.json` — supply-chain inventory

```bash
# List every declared + transitive dep by name
jq -r '.components[] | "\(.name) \(.version)"' proof/sbom/sbom-python.json
```

Feed the SBOM to any vulnerability DB (OSV, GHSA, Snyk) to get a
post-hoc audit of what the codebase was exposed to at merge time.

## Retention

* **Artifact retention:** 90 days from CI run completion
* **Manifest retention:** inside each artifact, forever (until the
  artifact itself expires)

After 90 days, GitHub deletes the artifact. For longer-term audit
trails, the operator should periodically download and archive the
proof bundles to cold storage (S3, Backblaze, etc.).

## When the proof bundle is incomplete

If a CI gate fails AND produces no artifact, the proof bundle's
manifest reflects it but the corresponding sub-directory is empty.
This is intentional — a failed run's partial proof is still proof
of which gates ran and which didn't. Use the absent artifact as
a debugging signal, not a completeness failure.

## Adding new artifacts to the proof bundle

To add a new gate's artifact to the bundle:

1. Make sure the producing job uploads via `actions/upload-artifact@v5`
   with `if: always()` so partial proofs still flow through.
2. Add a `Download <name> artifact` step in the `proof-bundle` job
   in `ci.yml`.
3. Add the new file to the `Contents` section of `MANIFEST.txt`
   generation.
4. Document it in this runbook under "How to verify each piece".
5. Add `<new-job>` to `needs:` in the `proof-bundle` job.
