# M3S — Master Money Management System (Design Spec)

> **Role:** This is the design spec for the M3S module (Phase 3b part 2, see `ROADMAP.md`). Extracted from the original `FULL_PLAN.md` during Session 21 doc consolidation. This is the **authoritative reference** for M3S implementation. When the design evolves, update this file (and note the deviation in `MASTER_PLAN.md`'s decision log).

---

## Why M3S

This is the single most valuable component in the trading system. No retail system has this. Hedge funds have entire teams doing this manually. We're building it as an AI-powered engine.

---

## Money Management Modes

### MODE 1: FORTRESS (Ultra-Safe)
- Max 0.5% risk per trade
- Quarter Kelly (0.25×) sizing
- Only SAFE-tagged strategies active
- No compounding (profits go to reserve)
- Target: 2-3%/mo, <2% drawdown
- Analog: Apex Drawdown Zero

### MODE 2: BALANCED
- Max 1% risk per trade
- Half Kelly (0.5×) sizing
- SAFE + MODERATE strategies active
- Compound 30% of weekly profit
- Target: 5-8%/mo, <10% drawdown

### MODE 3: ASSAULT (Aggressive)
- Max 2% risk per trade
- Full Kelly (capped at 2%) sizing
- ALL strategies active including AGGRESSIVE
- Compound 70% of profits immediately
- Target: 10-15%/mo, <25% drawdown

### MODE 4: AI ADAPTIVE (The Innovation)
- Claude agent analyzes ALL inputs in real-time
- Dynamically blends Mode 1-3 based on conditions
- **This is what hedge funds do** — automated

---

## Capital Allocation Engine

**Step 1 — Score each strategy:**
```
score = rolling_sharpe * (1 - correlation_penalty) * regime_fit_bonus * recent_performance_weight
```

**Step 2 — Kelly-weighted allocation:**
```
raw_alloc[i] = kelly_fraction * score[i] / sum(scores)
alloc[i]     = min(raw_alloc[i], max_per_strategy)
# remainder goes to cash reserve
```

**Step 3 — Correlation constraint:**
```
if corr(strategy_A, strategy_B) > 0.7:
    combined_alloc(A + B) <= 40% of capital
```

**Step 4 — Apply mode caps:**
| Mode | Max single-strategy cap |
|---|---|
| FORTRESS | 15% of capital |
| BALANCED | 25% of capital |
| ASSAULT  | 35% of capital |
| ADAPTIVE | Claude decides caps based on conditions |

---

## Compounding Engine

Tracks two pools: **BASE CAPITAL** (trading principal) and **PROFIT POOL** (accumulated net profit).

| Mode | Compounding rule |
|---|---|
| FORTRESS | Compound 0% (linear growth, maximum safety) |
| BALANCED | Compound 30% of **weekly** profit to base capital. Rebalance every Monday. |
| ASSAULT  | Compound 70% of **daily** profit to base capital. Rebalance after each profitable trade. |
| AI ADAPTIVE | Claude decides compounding % based on context (see below) |

**AI Adaptive compounding heuristics:**
- Winning streak (3+ consecutive) → compound 80%
- In drawdown → compound 0%, preserve capital
- Big single win (>2% in one trade) → compound 50%
- Daily target already hit → stop trading, bank it
- Week already +5% → shift to Fortress for rest of week

---

## AI Advisor ("The PhD")

**System prompt:**
> *"You are a PhD-level quantitative portfolio manager with expertise in Kelly criterion, risk parity, dynamic allocation, and compounding."*

**Runs every:**
- 30 minutes during market hours
- After every trade that changes P&L by >0.5%
- End of day (mandatory daily review)

**Output (structured JSON):**
```json
{
  "mode": "ADAPTIVE",
  "mode_blend": {"fortress": 0.2, "balanced": 0.6, "assault": 0.2},
  "allocation": {
    "asian_breakout": 0.25,
    "smc_liquidity": 0.20,
    "ema_crossover": 0.15,
    "iron_condor": 0.10,
    "cash_reserve": 0.30
  },
  "compound_pct": 0.50,
  "reasoning": "Strong week (+4.2%), but FOMC tomorrow. Reduce aggressive exposure, keep cash buffer high. Compound 50% of profit, save rest as event hedge."
}
```

**Model routing:** Claude Haiku for routine 30-min checks; Claude Sonnet for daily review. Expected cost: ~$2-4/day.

---

## Expected Impact on Returns

| Allocation Method | Expected Sharpe | Expected Monthly | Max Drawdown |
|---|---|---|---|
| Equal weight (naive) | 1.0-1.3 | 3-5% | 15-20% |
| Static Kelly | 1.3-1.6 | 4-7% | 12-18% |
| **Dynamic AI allocation (M3S)** | **1.6-2.2** | **5-10%** | **8-15%** |
| **+ AI compounding on top** | **1.8-2.5** | **6-12%** | **10-18%** |

---

## M3S vs Competition

| Feature | Commercial Bots | Forex EAs | Hedge Funds | **Our M3S** |
|---|---|---|---|---|
| Multiple strategies | 1-3 (preset) | 1 per EA | 50+ | **5-10 (flexible)** |
| Dynamic allocation | None | None | Human team | **AI-powered (Claude)** |
| Mode switching | Manual (user) | None | Manual (CIO) | **Auto + Manual override** |
| Compounding | Basic auto-compound | None | Sophisticated | **AI-adaptive compounding** |
| Correlation-aware | No | No | Yes | **Yes (auto-penalizes)** |
| Regime-adaptive | No | No | Yes | **Yes (meta-strategist agent)** |
| Risk budgeting | Basic % | Fixed lot | Kelly variants | **Fractional Kelly + AI** |
| Cost | $40-100/mo | $500-700 one-time | Millions | **~$150 build + $200/mo** |

---

## Build Notes for Implementation (Phase 3b-2)

- **Hooks into existing RiskManager** (`src/risk/manager.py`): M3S sits *in front of* RiskManager — it decides allocation and mode, RiskManager enforces hard limits. The existing 4 adaptive risk modes (AGGRESSIVE/BALANCED/DEFENSIVE/CUSTOM from Session 13) map onto M3S modes, but M3S adds the dynamic blending, compounding, and AI advisor layer.
- **Non-negotiables preserved:** max_drawdown (15%), max_monthly_loss (10%), kill_switch — never scaled by M3S mode.
- **Module location:** `src/m3s/` — new top-level package. Sub-modules: `allocator.py`, `compounder.py`, `advisor.py`, `correlation.py`, `telegram.py`.
- **Persistence:** Add M3S state (current mode, BASE/PROFIT pool balances, last advisor decision, advisor cost-to-date) to SQLite via existing `storage.py` patterns.
- **Telemetry:** Structured-log every advisor call with run_id for post-hoc replay.
