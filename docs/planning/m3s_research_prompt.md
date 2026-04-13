# M3S — Research + Architecture Prompt

> **Purpose:** This prompt frames the research and design work for M3S (Master Money Management System), Phase 3b part 2 of the algo-trading project. It is meant to be fed into a planning session that blends internet research with deep system design. The output is a reviewable plan, **not** code — Prince reviews the plan before any execution.

---

## Identity

You are two people at once. Both opinions must show up in the plan.

**Persona 1 — Principal Engineer.** You have shipped production trading systems. You care about: interfaces, state machines, failure modes, observability, testability, rollback, versioned state, no-surprise integration with existing components. You refuse to build a "clever" module that cannot be unit-tested or meta-backtested. You will push back on anything that couples M3S tightly to a single broker, a single strategy, or an unstructured config.

**Persona 2 — Senior Hedge Fund Portfolio Manager.** You have allocated capital across dozens of strategies at a multi-strategy fund. You care about: Kelly sizing (and its failure modes), volatility targeting, risk parity, correlation-aware construction, drawdown management, capacity decay, regime detection, anti-martingale logic, high-water marks, and the politics of "why is strategy X underweight this month." You refuse to use naive fixed-% allocation because you have seen it blow up every time a regime flips.

Write the plan in the voice of **both** — the engineer section drives *how*, the PM section drives *what/why*. Where they disagree, surface the disagreement explicitly so Prince can arbitrate.

---

## What M3S is (as currently sketched)

M3S = Master Money Management System. Sits *on top of* the existing `RiskManager` (separate ZMQ process, cannot be bypassed). M3S does not replace risk management — it is the **capital allocator, compounder, and meta-advisor** above the per-trade risk gates.

Existing context it must respect:
- 3 live strategies in paper trading: `bb_rsi_mr_opt` (40%), `donchian_ensemble_adx` (30%), `vol_momentum` (30%). Backtest Sharpe 2.318. Live since Session 16.
- Existing `RiskManager` implements 6 pre-trade checks, 4 risk modes (Fortress/Balanced/Assault/AI Adaptive), circuit breakers, kill switch, drawdown scaler, Kelly sizer. **M3S must not duplicate these** — it sits above them and sets their inputs.
- `BaseStrategy.on_candle() → Signal | None` contract is sacred. Same code path in backtest and live. M3S cannot change it.
- Paper engine is running. M3S must be buildable and testable without disrupting the running engine.
- Python 3.11, async-first, msgspec for data types, structlog, TOML config, SQLite+Parquet storage, pandas DataFrames, `ta` for indicators (not pandas-ta).
- `src/risk/modes.py` already has 4 risk modes. M3S "modes" are a layer above and may redefine or extend these — decide and justify.

Referenced docs (read during research):
- `ROADMAP.md` — current phase status
- `docs/M3S_SPEC.md` — the existing sketch (4 modes, Allocation Engine, Compounding Engine, AI Advisor "The PhD", Correlation, Telegram). This is the *starting* proposal, not the final design. Treat it as one data point.
- `ARCHITECTURE.md` — live module registry + RiskManager internals
- `MASTER_PLAN.md` — decision rationale and constraints
- `project_portfolio_composition.md` memory — current weights + Sharpe
- `project_risk_manager_semantics.md` memory — daily loss scales size (not rejects), DD CB fires before monthly CB

---

## Research tasks (use the internet)

Do not write the plan from first principles alone. Use WebSearch + WebFetch to ground the design in real hedge-fund practice. For each topic below, capture: what the technique *is*, when it works, when it fails, and whether it fits a solo retail paper-trading context with 3 strategies and ~$10k nominal equity.

### A. Capital allocation methods at real funds

1. **Kelly criterion** — full Kelly, fractional Kelly (1/4, 1/2), ensemble Kelly for multiple strategies. Failure modes: estimation error on edge, overbetting on correlated strategies, catastrophic sensitivity to mis-estimated win rate. Sources: Thorp's "The Kelly Criterion in Blackjack, Sports Betting, and the Stock Market," MacLean/Thorp/Ziemba "Good and Bad Properties of the Kelly Criterion."
2. **Risk parity / Equal Risk Contribution (ERC)** — allocate so each strategy contributes equal variance to the portfolio. Used at Bridgewater All Weather, AQR. Contrast with naive equal-weight. When does ERC break? (High-correlation regimes.)
3. **Volatility targeting** — scale notional so portfolio realizes a target annual vol (10%, 15%, 20%). Managed futures CTAs (Man AHL, Winton). Rebalance cadence (daily? weekly?). Lookback window for vol estimate.
4. **Hierarchical Risk Parity (HRP)** — Lopez de Prado's method. Clusters correlated strategies, allocates top-down. Robust to singular covariance. Search: "Building Diversified Portfolios that Outperform Out-of-Sample."
5. **Mean-variance optimization** — Markowitz. Why it fails for multi-strategy allocation (ill-conditioned covariance, extreme weights). Shrinkage estimators (Ledoit-Wolf).
6. **Meta-labeling** — Lopez de Prado "Advances in Financial Machine Learning." Strategies output signals; a meta-model sizes them conditional on regime.
7. **Regime-conditional allocation** — detecting market regimes (trending vs mean-reverting vs chaotic) and shifting weights accordingly. Hidden Markov Models, rolling volatility regimes, Hurst exponent.
8. **Drawdown-controlled allocation** — reducing exposure as drawdown grows (anti-martingale). Compare with adding on drawdown (martingale — why this blows up).

### B. Compounding policies — the core question Prince asked

The question: *"Given $X and $Y profit today, do I trade $X tomorrow (no compounding) or $X+$Y tomorrow (full compounding)?"* Research real answers used in practice:

1. **Full geometric compounding** — every trade sized as a % of current equity. Classic retail default. Risk: a bad week compounds losses just as fast as a good week compounded gains.
2. **Fixed-fractional with periodic resync** — trade on the same base for N days/weeks, then rebase. Common at discretionary prop desks.
3. **Profit-pool separation (BASE vs PROFIT)** — keep original capital in a "BASE pool" that never compounds, route profits into a "PROFIT pool" that does. Prince's intuition. Search for real analogs: "profit center accounting" in trading firms, "high-water mark" mechanics in hedge funds.
4. **Mode-dependent compounding** — aggressive strategies compound slower (or not at all) because variance estimation is less reliable; safe strategies compound faster. **This is Prince's specific design instinct — validate or refute with research.** Look for how CTAs treat strategy-level reinvestment.
5. **Drawdown-gated compounding** — stop compounding (or de-compound, i.e., scale down) during drawdown. Resume only after new high-water mark. Used at managed futures shops.
6. **Volatility-adjusted compounding** — compound at a rate proportional to realized Sharpe or inverse realized vol. Converts "I had a good month" into "my edge-to-variance ratio is improving" rather than "I got lucky."
7. **Withdrawal policies** — when and how to take profit *out* of the trading system (pay self, pay taxes, reduce risk). Often overlooked; as important as the in-flow rule. Search: "fund distribution policy," "pay yourself first algo trading."

Produce a **taxonomy table** in the plan of compounding policies, with: name, formula, when it works, when it fails, compute cost, and which M3S mode (if any) should use it as default.

### C. AI / LLM as portfolio advisor — what's actually useful

Current spec proposes Claude Haiku for routine + Sonnet for daily review ("The PhD"). Research:

1. **What can an LLM actually contribute** that a numerical optimizer cannot? (Regime commentary, news integration, narrative consistency checks, detecting "the setup that broke this strategy in 2008.")
2. **What should an LLM never touch?** (Real-time position sizing, execution, stop placement — anything latency-critical or where a hallucination could move real money.)
3. **Advisory vs authoritative** — in real funds, is there an AI that *decides* or only one that *recommends*? Search: "AI portfolio manager production," "LLM in quant trading 2024 2025," "RenTech AI stack anecdotes."
4. **Failure modes** — LLM cost spikes, API outages, silent prompt injection from news data, stale recommendations. How do real systems degrade gracefully?
5. **Prompt structure** — what is a *good* system prompt for a portfolio advisor LLM? (Structured JSON output, explicit refusal when data is insufficient, versioned prompts for reproducibility.)
6. **Cost modeling** — $2–4/day is cited in the spec. Verify: what volume of Haiku + Sonnet calls does that buy, and is it enough for meaningful oversight?

### D. Correlation handling at real multi-strategy funds

1. Rolling correlation estimation — window length, shrinkage, stability.
2. Hard caps vs soft penalties — if strategies A and B correlate > 0.7, do you reject the second signal, scale it down, or let it through with a warning?
3. Correlation during crises — "everything correlates to 1 in a crash." How do real funds prepare? Tail hedges, cross-asset diversification, forced degrossing.
4. The difference between **signal correlation** (strategies agree on direction) and **P&L correlation** (strategies make/lose money together) — which matters more for allocation?

### E. What real hedge funds actually publish about this

Look up and cite specific, recent sources where possible:
- AQR, Two Sigma, Man Group, Winton research papers on multi-strategy allocation
- Bridgewater's risk parity papers (Ray Dalio, Bob Prince)
- Lopez de Prado's work on HRP and meta-labeling
- Ernest Chan's books (algo trading retail perspective)
- Any recent (2023–2025) published work on LLMs in quant production

Prefer primary sources and dated material. Flag anything that is blog-post hype vs peer-reviewed / practitioner publication.

---

## Design questions the plan must answer

For each: state the decision, the alternatives considered, the reasoning, and which research finding supports it. Be opinionated. "It depends" is not an answer — if it genuinely depends, state on *what* and pick a default.

### Allocation layer
1. What allocation method(s) does M3S use? Single method or hybrid? If hybrid, how are they blended?
2. What inputs does the allocator consume? (Per-strategy rolling Sharpe? Rolling vol? Correlation matrix? Regime indicator? LLM opinion?)
3. What is the minimum history needed before M3S will trust an allocation? (Cold-start problem.)
4. How often does allocation recompute? (Every bar? Daily close? Weekly? On significant event?)
5. How does it handle a strategy that is "warming up" (not enough history) alongside mature strategies?
6. How does it detect a strategy that has **turned bad** (edge decayed, Sharpe collapsed) and reduce its weight automatically — without killing it prematurely on normal variance?

### Compounding layer
7. What **is** compounding in M3S? Define precisely. (The currency of compounding is... equity? notional? risk budget?)
8. Per-strategy compounding or portfolio-level compounding?
9. Cadence: tick / bar / daily / weekly / monthly? Default answer + mode-specific overrides.
10. BASE vs PROFIT pool: yes or no? If yes, what triggers profit rolling from PROFIT → BASE? (Time-based? Drawdown-based? Never?)
11. How does compounding interact with drawdown? (Stop compounding during DD? De-compound?)
12. How does compounding interact with mode (Fortress vs Assault)?
13. Withdrawal: does M3S support "take $X out as personal income"? If yes, how?

### Modes
14. How many modes? Current spec says 4 (Fortress/Balanced/Assault/AI Adaptive). Validate or redesign. Each mode must have a clear *thesis* (who uses it and why), not just "more aggressive numbers."
15. What changes between modes? Candidate dials: risk-per-trade, max daily DD, correlation cap, allocation method, compounding rate, LLM advisor weight, cold-start threshold, rebalance cadence.
16. Mode transitions: who changes the mode? (Prince manually? The AI Advisor? Auto on drawdown threshold?) How is the transition made safe (no whipsaw)?
17. The "AI Adaptive" mode — what does "adaptive" actually mean operationally? The LLM changes the mode? The LLM changes mode *parameters*? Something else?

### Per-strategy risk budgeting
18. Prince's core intuition: **risky strategy → bigger capital but slower compounding; safe strategy → smaller capital but faster compounding.** Validate against research. Is this actually a good policy or does it invert what Kelly would do?
19. What defines "risky" vs "safe"? Realized vol, max DD, tail ratio, Sharpe variance, something else?
20. Should each strategy have its own mode (its own risk profile) while the portfolio has a separate "meta-mode"? Or is mode global?

### AI Advisor (The PhD)
21. What is the advisor's actual scope? (Read-only commentary? Mode suggestion? Allocation tweak within bounds?)
22. How often does it run? (Every trade? Every candle? Daily close? On request?)
23. What inputs does it see? (Portfolio state, recent trades, recent news, recent macro? What's off-limits?)
24. What outputs does it produce? (Structured JSON recommendation? Free-text commentary? Both?)
25. What happens when the advisor is unavailable (API down, cost cap hit)? M3S must degrade gracefully to a deterministic fallback.
26. How do we detect bad advice? (Shadow-mode comparison? Post-hoc attribution?)
27. What cost envelope is acceptable? Daily cap + per-call cap.

### Integration
28. Where does M3S sit in the pipeline? Strategy → Signal → **M3S allocator** → RiskManager → Executor? Or Strategy → Signal → RiskManager → **M3S compounder updates equity** → Executor? Draw it precisely.
29. What does M3S *write to* — does it change `config/risk.toml` at runtime? Publish allocation via ZMQ? Set strategy-level risk budgets through a new channel?
30. How is M3S state persisted? (SQLite table for equity + pool state? Append-only event log for audit?)
31. How does M3S boot after a crash? (Read last state from SQLite, reconstruct if missing, fail safely if ambiguous.)

### Meta-backtesting M3S
32. How do we backtest M3S *itself*? Meta-backtest: given historical per-strategy trade logs, simulate M3S's allocation and compounding policy. Does the result beat naive fixed-weight?
33. What metrics define "M3S is working"? (Portfolio Sharpe uplift? Drawdown reduction? Capacity growth? All?)
34. What baseline does M3S need to beat before going live? (Fixed-weight portfolio Sharpe 2.318 — that's the bar.)

### Observability and ops
35. What does the M3S dashboard look like? (Current mode, current allocation, current compounding state, advisor's last recommendation, BASE vs PROFIT pool balances, recent mode transitions.)
36. What alerts fire? (Mode transition, advisor disagreement with allocator, DD cap approach, correlation cap hit.)
37. How does Telegram fit in? (Daily digest? Alerts only? On-demand "what's the state?"?)
38. What do we log for post-hoc attribution? (Every allocation decision with its inputs, every advisor call with prompt + response.)

### Failure modes and safety
39. What if two strategies disagree on regime? (Both long, both short, one long one short.)
40. What if the correlation estimate is unstable? (Insufficient history, regime break.)
41. What if the advisor goes silent for 24h? (Fall back to last known recommendation? Fall back to deterministic rule?)
42. What if equity crosses the DD cap mid-bar? (Halt all new entries? Close all positions? Narrow to safe mode?)
43. What does an M3S kill switch look like? (Revert to pre-M3S fixed-weight, keep existing positions, halt re-allocation.)
44. How is M3S **rolled out** without breaking live paper trading? Shadow mode first (read-only, logs what it *would* do) → advisory mode (suggests, Prince approves) → authoritative mode (decides).

### Testing
45. Unit tests: math of allocator, compounder, pool accounting — bit-exact against hand-calculated values per `feedback_backtesting_protocol` + `feedback_verify_infrastructure_first`.
46. Simulation tests: feed a synthetic trade log, verify allocation converges to expected weights.
47. Property tests: no mode can produce negative equity, no allocation > 100%, no allocation when DD cap hit.
48. Meta-backtest vs fixed-weight baseline (the "beats 2.318 Sharpe" bar).
49. Chaos tests: advisor offline, ZMQ disconnect, SQLite locked, corrupted state file.

---

## Constraints the plan must respect

- **Solo developer.** No feature that requires a dedicated MLOps team.
- **Paper first, then real money.** Nothing that can't be safely shadow-tested on the running paper engine.
- **Risk manager is sacred.** M3S feeds inputs into it; it does not replace it and cannot bypass it.
- **Strategy contract is sacred.** No changes to `BaseStrategy.on_candle() → Signal | None`.
- **No backwards-compat shims.** If a design change is right, make it cleanly.
- **No premature abstraction.** If M3S needs one allocator, build one, not a plugin framework.
- **Reversible rollout.** Any mode Prince turns on must have a one-command revert.
- **Per `feedback_never_settle_mediocre`:** if a design choice looks mediocre, escalate with options, don't ship the compromise quietly.

---

## Expected plan output

The plan (written in plan mode, reviewed before execution) must contain:

1. **Research findings summary** — 1 page max, cite sources, flag consensus vs controversial claims, say which findings changed the design vs which confirmed it.
2. **Compounding taxonomy table** — policies × when-they-work × when-they-fail × M3S default.
3. **Architecture diagram** — ASCII, showing how M3S sits between strategies, risk manager, executor, and the advisor.
4. **Module list** — every file under `src/m3s/`, one-line responsibility each. Minimum viable set, no speculative modules.
5. **Per-module interface sketches** — key types, function signatures, state transitions. Not full code, but enough that the engineer knows what to build.
6. **State and persistence design** — where BASE pool, PROFIT pool, last allocation, last advisor recommendation live. Schema or protobuf-style sketch.
7. **Mode definitions table** — one row per mode, columns = every dial that changes between modes. Must justify why each dial matters.
8. **Compounding policy per mode** — explicit rule.
9. **Integration hooks** — exact points in the existing codebase (`src/risk/*`, `src/main.py`, `src/execution/*`) that need to change, with line-level pointers.
10. **Config schema** — new `config/m3s.toml` or extensions to `config/risk.toml`. Show the TOML.
11. **Rollout phases** — shadow → advisory → authoritative, with the acceptance criteria for each promotion.
12. **Meta-backtest design** — how we validate M3S itself before going live.
13. **Test plan** — unit / simulation / property / meta-backtest / chaos, with test counts as an estimate.
14. **Observability plan** — what metrics, what dashboard panels, what alerts, what Telegram messages.
15. **Rollback plan** — one command to revert, what it does, what it preserves.
16. **Open questions for Prince** — things the plan deliberately leaves for him to decide, not things you dodged.
17. **Success criteria** — quantitative (meta-backtest Sharpe uplift target) and qualitative (Prince can explain M3S's decision for any given day in one sentence).
18. **Out of scope** — explicitly list what M3S will *not* do in v1. (Options. Cross-venue. Real money. Tax optimization. Portfolio margining.)

---

## Creative instruction

This is the part of the project where being clever actually pays off. Do not just codify `docs/M3S_SPEC.md` — it was written in a planning sprint before any of the strategies existed. Use it as one data point and be willing to reject pieces of it if research contradicts them.

Specifically, challenge:
- Whether "4 modes" is the right number (maybe it's 2, maybe it's 3 with the 4th as a parameter of the others)
- Whether Fortress/Balanced/Assault naming actually maps to anything risk theory would recognize
- Whether the LLM advisor belongs at all in v1, or should be a v2 feature after deterministic M3S proves itself
- Whether "AI Adaptive" is a meaningful mode or just a rebrand of "regime-conditional allocation"
- Whether BASE vs PROFIT pool is actually better than high-water-mark + drawdown-gated compounding (simpler, same intent)
- Whether `src/m3s/` should contain 5 modules or 2 — resist module bloat
- Whether Telegram is v1 or v2

Where real hedge fund practice differs from Prince's current intuition, **say so clearly and give him the better alternative** — per `feedback_challenge_choices`, he values honest correction over agreement.

The goal: an M3S that is not merely "nicer than fixed-weight allocation" but **structurally better than what a retail trader or even a small fund would build on first try**, because it is grounded in real multi-strategy fund practice and hardened against the specific failure modes that wreck retail compounders.

---

## How this prompt is used

1. Prince reviews this prompt, edits anything he wants to change.
2. I (Claude) execute it — WebSearch/WebFetch for research, then enter plan mode with the research findings loaded into context.
3. The resulting plan is presented via ExitPlanMode for Prince's review.
4. Only after Prince approves the plan does any code get written.

No code is written until the plan is approved. No research is done until this prompt is approved.
