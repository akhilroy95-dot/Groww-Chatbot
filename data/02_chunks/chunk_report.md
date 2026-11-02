# Chunk report

- strategy: `structure_aware_fact_cards_v1`
- configured max tokens: fact cards 250, narrative 300, hard 300
- token estimate: `ceil(characters / 4)` (deterministic, no tokenizer download)
- fetchable sources: 14
- sources with chunks: 14
- chunks: 54
- tokens: min 37, max 292, mean 106.9

## Counts by source

| source_id | role | priority | chunks | missing expected |
|---|---|---:|---:|---|
| `amfi_cas` | support | 2 | 1 | none |
| `amfi_expense_ratio` | support | 2 | 2 | none |
| `amfi_ter` | support | 2 | 2 | none |
| `groww_amc_hdfc` | core | 1 | 3 | none |
| `groww_elss` | core | 1 | 8 | none |
| `groww_flexi_cap` | core | 1 | 7 | none |
| `groww_help_cas_cams` | support | 2 | 1 | none |
| `groww_help_cas_what` | support | 2 | 1 | none |
| `groww_help_elss_statement` | support | 2 | 1 | none |
| `groww_help_tax_proof` | support | 2 | 1 | none |
| `groww_large_cap` | core | 1 | 8 | none |
| `groww_mid_cap` | core | 1 | 8 | none |
| `sebi_elss` | support | 2 | 6 | none |
| `sebi_riskometer` | support | 2 | 5 | none |

## Counts by fact type

| fact_type | chunks |
|---|---:|
| `amc_overview` | 3 |
| `aum` | 4 |
| `benchmark` | 4 |
| `definition` | 6 |
| `exit_load` | 4 |
| `expense_ratio` | 8 |
| `lock_in` | 2 |
| `minimum_sip` | 4 |
| `riskometer` | 9 |
| `scheme_basics` | 6 |
| `statement_download` | 4 |

## Token buckets

| bucket | chunks |
|---|---:|
| 1-50 | 1 |
| 51-100 | 34 |
| 101-200 | 13 |
| 201-300 | 6 |
| 301+ | 0 |

## Missing expected facts

- core: none
- support: none
