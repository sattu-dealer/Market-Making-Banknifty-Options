# Holdout unlock log

Appended by `bnfmm.analysis.holdout.require_access` every time a holdout day is read. Never edit by hand. Protocol: `config/frozen/protocol.yaml`.

| When (UTC) | Days | Reason | Protocol sha256 | Command |
|---|---|---|---|---|
| 2026-09-29T21:21:58+00:00 | 2026-08-31, 2026-09-01, 2026-09-02, 2026-09-03, 2026-09-04 | mm_v1 one-shot out-of-sample evaluation; config frozen in config/frozen/mm_v1.yaml sha256 4f144314 | `0e865025ab99` | `scripts/mm.py --date 2026-08-31 --date 2026-09-01 --date 2026-09-02 --date 2026-09-03 --date 2026-09-04 --band 11 --buffer 3 --cost-split symmetric --vega-limit 3 --min-spread-bp 37.8 --per-leg --dump-fills --tag holdout-mm_v1 --unlock-holdout mm_v1 one-shot out-of-sample evaluation; config frozen in config/frozen/mm_v1.yaml sha256 4f144314` |
| 2026-09-30T14:22:57+00:00 | 2026-08-31, 2026-09-01, 2026-09-02, 2026-09-03, 2026-09-04 | latency sensitivity of frozen mm_v1 (strategy unchanged; simulator order-latency assumption varied); disclosure against the 0 ms headline | `0e865025ab99` | `scripts/mm.py --date 2026-08-31 --date 2026-09-01 --date 2026-09-02 --date 2026-09-03 --date 2026-09-04 --band 11 --buffer 3 --cost-split symmetric --vega-limit 3 --min-spread-bp 37.8 --tag holdout-mm_v1-latency --grid {"latency_ms":[500,1000]} --unlock-holdout latency sensitivity of frozen mm_v1 (strategy unchanged; simulator order-latency assumption varied); disclosure against the 0 ms headline` |
