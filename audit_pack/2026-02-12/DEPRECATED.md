# Deprecated Reports

The following reports in this directory are **superseded** and should NOT be used
for any downstream processing, governance decisions, or claim-vault construction.

## Superseded Files

| File | Reason | Replaced By |
|------|--------|-------------|
| `merge_valuation.json` | FLUX at 6 decimals (1e15); polluted NFT supply counts | `merge_valuation_cmatra.json` |
| `allocations_flux.csv` | Based on polluted valuation; wrong output token | `allocations_cmatra.csv` |
| `allocations_summary.json` | 6,645 claimants with inflated NFT holder counts | `allocations_cmatra_summary.json` |

## What changed

1. **Output token**: FLUX (6 decimals, 1e15 supply) -> cMATRA (12 decimals, 1e21 supply)
2. **NFT supply filtering**: Assets with quantity > 1 under NFT policies are now
   correctly excluded. This reduced supply counts for T1_ADAM_PASS (79->43),
   T2_ADAM_PASS (119->95), and FLUX_PASS (804->802).
3. **Holder count correction**: Excluding fungible-under-NFT-policy assets reduced
   unique claimants from 6,645 to 6,619.

## Current (authoritative) reports

- `merge_valuation_cmatra.json` — 7-asset valuation with corrected NFT supplies
- `allocations_cmatra.csv` — per-claimant allocations (6,619 rows)
- `allocations_cmatra_summary.json` — allocation summary with invariant checks
- `twap_report.json` — 7-asset TWAP (shared by both old and new; unchanged)
- `ELIGIBILITY_RULES.md` — plain-English eligibility policy

## Retained for audit trail

The deprecated files are kept in this directory (not deleted) so that the
before/after difference is auditable. They must not be used as inputs to any
downstream pipeline stage.
