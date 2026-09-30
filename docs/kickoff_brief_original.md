> PRESERVED VERBATIM. This is the original kickoff brief, exactly as supplied at
> project start on 2026-08-23, before any code existed. `BRIEFING.md` has since
> been rewritten as the working handover document; this file is kept because the
> brief is the *provenance* of the project's binding constraints (its section 5
> "no live capital, no real order placement", and its section 2 non-negotiable
> data-labelling requirement), and because nothing in this repo is committed yet,
> so overwriting it in place would have made it unrecoverable. Nothing below has
> been edited. Where it has been superseded -- the broker is Dhan, not Paytm
> Money; the quoted instrument is options, not futures -- `BRIEFING.md` says so
> and says why.
# Claude Code Kickoff Brief: Market Making Simulation on BANKNIFTY

*Paste this as your first message to Claude Code, or save it in the repo (e.g. as `PROJECT_BRIEF.md`) and point Claude Code to it at kickoff.*

## How to use this document

This is a context and scoping brief, not a locked spec. It captures the thinking from a planning conversation, not a checklist to execute mechanically. A few things that follow from that:

- Wherever this brief proposes an approach, treat it as the current best guess, not a final decision. If a smarter way to reach the same underlying goal exists, propose it — with the trade-off made explicit — before defaulting to what's written here.
- Ask clarifying questions before writing substantial code if anything is ambiguous, or if a decision here would meaningfully change downstream architecture.
- Section 4 lists explicit questions/decisions this brief wants your independent view on before implementation starts in earnest.
- The end goal is a project that's genuinely strong for quant/finance and data-focused placement applications — evaluate choices not just for technical correctness, but for how defensible and impressive they'd read to a quant recruiter or interviewer.

---

## 1. The idea, its ambition, and its practicality

**Original ambition:** a project titled "Market Making on BANKNIFTY" — using quantitative finance concepts alongside BANKNIFTY spot and derivatives price/order-book data to simulate market making across its options, futures, stocks, and potentially other related instruments. The goal is a project that reads credibly on both a quant/finance CV and a data-science/data-engineering CV.

**Why the idea is strong:** market making sits at the intersection of market microstructure, stochastic control, and applied engineering. It's underrepresented in the typical student portfolio (which skews heavily toward "predict price direction with ML"), and it forces real engagement with concepts quant recruiters specifically screen for — inventory risk, adverse selection, spread/skew optimization, and the mechanics of order queues and fills, not just an alpha signal.

**Why the full ambition, as originally stated, is hard to execute as a single project:** market-making simultaneously across options, futures, and stocks is close to a full trading desk's mandate:
- Options market making alone needs a live, consistent implied-volatility surface plus delta/vega hedging — enough scope for an entire project by itself.
- Separate quoting logic across multiple instrument classes multiplies both the data-access burden and the modeling burden.
- Historical NSE order-book (L2/L3) data isn't freely available, which caps how much can be validated historically regardless of instrument scope (see Section 2).

**Core technical pillars that make this project "count," regardless of exact final scope:**
1. A fair-value / mid-price estimator — ideally a microprice or book-imbalance-weighted mid, not just last-traded price.
2. An inventory-aware quoting model — a reference point like the Avellaneda-Stoikov (2008) framework, where the reservation price shifts with inventory and the spread widens with volatility and time-to-close.
3. A queue-position-aware fill simulator — likely the single highest-value, most differentiating piece. Naive backtests assume instant fills at the quoted price; real market making requires modeling where an order sits in the queue at a price level and only filling it probabilistically once observed trade volume clears what's ahead of it. Skipping this is the most common flaw in student projects, and the first thing an interviewer who knows market making will look for.
4. Inventory risk controls — position limits and asymmetric quote skewing to unwind inventory, rather than naive symmetric quoting.
5. PnL decomposition — spread-capture PnL, inventory/directional PnL, and adverse-selection cost (price movement immediately after a fill). This decomposition is what separates "a strategy that made money in a backtest" from an actual market microstructure study.

**A scoped-down direction that's been considered (not mandatory):**
- Quote two-sided prices on BANKNIFTY futures only — a single, continuous instrument with clean inventory dynamics.
- Use the options chain not as a separate market-making target but as a source of a derived fair-value signal — e.g., a synthetic futures price via put-call parity across a few strikes, or IV skew — feeding into the futures quoting model.
- This keeps the "derivatives"/"options" story relevant to the CV without requiring a full vol-surface-and-Greeks engine built from scratch.

**Explicit invitation:** the scoping above is current best thinking, not a mandate. If there's a better instrument choice, a cleaner way to fold in options exposure, a different architecture, or even a good reason to keep broader multi-instrument scope after all — propose it, with trade-offs stated, rather than defaulting to what's written here.

---

## 2. Data: what's needed, and room for a smarter approach

**The core problem:** NSE does not provide free historical tick-by-tick / order-book (L2/L3) data. Official channels give end-of-day bhavcopy or limited snapshots at best. Real market-making research needs at least top-5 depth at high frequency (ideally millisecond-level) — which rules out doing this from free historical downloads alone.

**Broad strategies considered:**
1. Buy historical tick/depth data from a vendor — workable in principle, but the cost is a real blocker on a student budget.
2. Self-capture live depth going forward via a broker API — free, but the usable backtest window is gated by how long data is collected before analysis can begin, and by construction can't reach back before capture started.

**What's available:** API access to Paytm Money, which exposes a Python SDK (`pyPMClient`) with a WebSocket client capable of streaming full market depth. Relevant specifics already gathered:
- Auth is a daily token-exchange flow (OTP-based login → `request_token` → `access_token` / `public_access_token`); tokens expire daily. This refresh should not be automated around the OTP step — assume a human supplies fresh tokens each morning.
- `modeType='FULL'` on subscription is expected to return multi-level depth rather than just LTP/top-of-book, though the exact depth-level count and field structure isn't confirmed from public docs and needs a first-run check against live data.
- The `on_message` payload may arrive as a raw object/Blob rather than parsed JSON in some reports — needs defensive parsing rather than an assumption either way.
- Only one active WebSocket connection should run at a time, per the SDK's own guidance.
- If used, instrument scope would be: BANKNIFTY current-month futures, plus the nearest **monthly**-expiry options chain (explicitly not weekly, since BANKNIFTY may list both simultaneously) across a strike band around spot (e.g., ATM ± 3, both CE and PE).
- A more detailed draft technical plan for this exact pipeline (scaffold → instrument resolution → websocket ingestion with buffered Parquet writes → daily QA script) already exists from an earlier planning pass and can be supplied as supplementary reference if useful — treat it as a draft to critique, not a locked spec.

**Explicit invitation — is live self-capture even the right call?** It's the default mostly because it's free, not necessarily because it's optimal. Before sinking significant effort into building and babysitting a live collector, it's worth evaluating honestly whether a smarter path reaches a credible, demonstrable prototype faster. Some directions to react to (not prescriptive):
- A shorter, high-quality real capture window used specifically to calibrate and validate the fill simulator's realism, paired with a longer synthetic/simulated order-book process for broader backtesting and robustness analysis.
- Using an existing public limit-order-book dataset (e.g., LOBSTER, even though it's US equities, not BANKNIFTY) purely to validate the queue-simulation *methodology*, decoupled from BANKNIFTY-specific results, while a smaller real BANKNIFTY capture runs in parallel at lower stakes.
- Any hybrid that gets to a working, defensible prototype sooner — as long as what's real versus synthetic/proxy is never conflated in the final results.

**Non-negotiable constraint:** whatever data strategy is chosen, the final writeup must clearly and honestly label what is real captured market data versus synthetic, simulated, or proxy data. Overstating this is exactly the kind of thing that damages credibility badly if probed in an interview.

---

## 3. The quant / placement lens

**Why this is worth doing, if scoped and executed well:** compared to the median student quant project (price-direction prediction via ML, sentiment-driven trading, generic portfolio optimization), market making is a meaningfully stronger signal for both quant/finance and data-focused roles — it demonstrates comfort with market microstructure, stochastic-control-style framing, and the applied engineering needed to simulate a live system rather than just fit a model to a static dataset.

**What actually makes it count in an interview, not just a checkbox:**
- A working queue-position-aware fill simulator, with an explicit before/after showing how much a naive "instant fill at quoted price" backtest overstates market-making profitability. Likely the single strongest, most specific talking point the project can produce.
- A clean PnL decomposition (spread capture vs. inventory/directional vs. adverse selection) — shows the difference between "a strategy that made money" and actually understanding where the money came from.
- Walk-forward or out-of-sample validation of any fitted parameters (risk-aversion, spread widths, etc.), rather than a single in-sample fit — avoids the most common overfitting critique.
- An honest, explicit statement of limitations: this is a simulation, not live-traded with real capital, and not co-located, so latency and adverse-selection dynamics are approximations. Reviewers tend to respect a candidate who states this plainly far more than one who implicitly oversells profitability.

**The single biggest risk to the project's credibility:** a backtest that assumes instant fills at the quoted price, with PnL reported from that assumption. This is the first thing a market-making-literate interviewer will probe, and it's a well-known, easy-to-spot flaw. Whatever else changes about scope or implementation under time pressure, this should not be quietly skipped or hand-waved.

**Keep this lens active throughout, not just at kickoff:** when making implementation trade-offs — simplifying the fill model, cutting a validation step to save time, narrowing scope further — periodically sanity-check the decision against how it would read to a quant interviewer who knows this space, and flag it explicitly if a shortcut risks undermining the project's core credibility, rather than making that call silently.

---

## 4. What I want from you at kickoff

**Before writing substantial code:**
1. Read this brief in full and ask clarifying questions on anything ambiguous, or anywhere a decision would meaningfully change downstream architecture.
2. Propose a concrete technical plan and repo structure — matching the direction above, improving on it, or diverging from it with justification — and get sign-off before implementation begins in earnest.
3. Flag explicitly, and early, if the underlying idea itself (a market-making simulation on BANKNIFTY) doesn't look like the strongest use of the available time, data, and effort for a placements-focused CV project — better to hear that now than discover it later.

**Ongoing:**
4. Structure the repo as something presentable directly to recruiters and interviewers: a clear README explaining the idea, methodology, and honest limitations; a sensible module structure; tests for anything with non-obvious logic, especially the fill simulator and any fair-value/parity calculations.
5. Where a non-obvious scoping or modeling decision gets made, note it and the reasoning briefly (README, commit messages, or a running decisions log) so the eventual writeup can explain *why* choices were made, not just *what* was built — this matters for how the project holds up under interview questions.

---

## 5. Known constraints — quick reference

- **Market:** NSE BANKNIFTY — futures + nearest monthly-expiry options (not weekly).
- **Data:** no free historical L2/L3 order-book data exists; live capture via the Paytm Money API (`pyPMClient`) is the leading option, requires daily manual token refresh, and the exact `on_message` payload format needs first-run verification against live data.
- **No live capital, no real order placement** — this is a research/simulation project only. Do not wire up trading/order-placement endpoints even where the SDK makes it convenient.
- **Audience:** this is a placements-focused portfolio project for a student, not a production system — bias toward a smaller, well-validated, honestly-presented result over a maximal-scope, shakier one.
