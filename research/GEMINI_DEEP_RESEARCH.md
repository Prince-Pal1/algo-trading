# Gemini Deep Research: Quantitative Microstructure & HF Scalping in Crypto

**Source:** Google Gemini Deep Research (April 2026)
**Scope:** BTC, ETH, SOL scalping with institutional-grade verification

---

## 1. Epistemology of Crypto HFT

### Verification Requirements for Any Scalping Strategy

A robust backtest must incorporate a **double out-of-sample** framework:
1. **Training set** -- parameter estimation
2. **Validation set** -- hyperparameter tuning
3. **Strictly unseen test set** -- executed exactly ONCE

Strategies that iteratively test on OOS data introduce data mining bias.

### Mandatory Friction Modeling
- Exchange taker fees: 0.02%-0.1% per leg
- Bid-ask slippage (especially during volatility)
- Queue position dynamics (limit order book)
- Passive vs. aggressive order semantics
- **Funding rates** (perpetual futures) -- NO public strategy models this

---

## 2. Asset Microstructure Profiles

### Bitcoin (BTC)
- Deepest LOBs, tightest spreads, highest institutional market maker concentration
- Inefficiencies resolve in **milliseconds**
- Scalping requires: extreme execution speed OR cross-exchange stat arb
- Price action dictates entire crypto market beta
- **Verdict:** Too efficient for simple scalping. Multiple papers confirm.

### Ethereum (ETH)
- Massive spot + perp liquidity on CEX
- L1 gas fees make on-chain HFT mathematically unviable during congestion
- Scalping confined to CEX perpetual contracts (off-chain matching engines)
- ETH/USDT perps have predictable maker-taker fee structures

### Solana (SOL)
- Sub-cent transaction fees + ~400ms block times
- Enables dozens/hundreds of trades per day on-chain
- Micro-PnL extraction viable because friction is orders of magnitude lower than ETH
- On-chain OFI detection and liquidity sweep fading possible
- **Best asset for on-chain HFT/scalping**

---

## 3. Verified Strategy: Alpha-AS (Deep RL Avellaneda-Stoikov Market Making)

### Core Architecture
- **Type:** Non-directional market making (spread capture)
- **Foundation:** Avellaneda-Stoikov (2008) reservation price model
- **Enhancement:** Double Deep Q-Network replaces static parameters
- **Action Cycle:** Every 5 seconds, RL agent adjusts risk aversion parameter
- **State Space:** L2 tick data -- price levels, cumulative notional, OFI, spread dynamics

### Entry Logic
- Continuous placement of simultaneous limit orders on both bid and ask
- RL agent outputs continuous variable adjusting risk aversion in A-S equations
- New reservation price + optimal spread calculated each cycle

### Exit Logic
- Inherent to quote repositioning
- Net-long inventory: lower ask price to attract buyers (compressed spread / micro-loss)
- Goal: return to delta-neutral state
- Reward function heavily penalizes directional inventory during vol spikes

### Position Sizing
- Hard inventory limits
- Dynamic scaling based on current inventory levels

### Performance

| Metric | Value |
|--------|-------|
| Sharpe Ratio | 2.5-3.0+ (substantially outperformed Gen-AS baseline) |
| Test Data | 30 days continuous L2 tick data, BTC/USD |
| Action Cycle | 5-second evaluations |
| Latency Modeling | YES (real-time execution latencies simulated) |
| Fee Structure | Maker rebate captured, taker fee avoided (post-only) |

### Market Conditions
- **Thrives:** High-volume, ranging markets (rapid oscillation around mean)
- **Fails:** Violent unidirectional breakouts, flash crashes
- **Best Session:** EU/US overlap (volume tightens spread, increases fill rate)

### Verification
- **Paper:** PLOS ONE (peer-reviewed)
- **Code:** GitHub -- javifalces/HFTFramework (290 stars)
- **Alternative Code:** TimCaron/Market_Maker_Strategies (WIP, 9 stars)
- **Framework:** hftbacktest (nkaz001, 3,927 stars) -- models queue position + latency

### Failure Modes
1. **May 2021 crash:** MMs pulled liquidity; stale quotes filled by panic sellers
2. **WebSocket outages:** Algorithm goes blind during peak vol
3. **Spoofing:** Institutional HFT creates false OFI signals
4. **Infrastructure:** Without co-location, latency kills the edge

### Execution Requirements
- Strictly post-only limit orders
- Millisecond latency (co-located VPS required)
- $100k+ capital minimum
- Level 2 tick data feed via WebSocket

---

## 4. Verified Strategy: Walk-Forward EMA Momentum

### Core Architecture
- **Type:** Directional momentum with dynamic parameter optimization
- **Foundation:** EMA crossover -- but lengths are NEVER fixed
- **Enhancement:** Rolling walk-forward optimization continuously adapts EMA lengths
- **Optimization Target:** Maximize Robust Sharpe Ratio per micro-epoch

### Entry Logic
- Fast EMA crosses above slow EMA = long entry
- EMA lengths selected from 81 walk-forward window combinations
- Best config: **7-day training / 28-day testing window**

### Exit Logic
- **Stop-and-Reverse (SAR):** Fast EMA crosses below slow EMA
- Long closes + short opens simultaneously
- Requires TWO executions per flip

### Position Sizing
- Fixed fractional allocation
- Risk bounded by validation-window drawdown limits

### Performance

| Metric | Value |
|--------|-------|
| Sharpe Ratio (best OOS) | 1.252 |
| Annualized Return (best) | 94.83% |
| Max Drawdown (best) | 35.16% |
| Training Period | 19 months |
| OOS Test Period | 21 months (strictly unseen) |
| Walk-Forward Combos | 81 (all beat B&H in training) |
| Fee Per Trade | 0.1% (conservative) |
| Break-Even Fee | 0.4% per trade |
| Assets | BTC, ETH, BNB |

### CRITICAL: Timeframe Viability

| Timeframe | Mean Sharpe (OOS) | Verdict |
|-----------|------------------|---------|
| 1-min | **-12.71** | DEAD |
| 5-min | Negative | DEAD |
| 15-min | Negative | DEAD |
| 30-min | Negative | DEAD |
| **60-min** | **Positive (1.252)** | **ONLY viable TF** |

**All sub-30-min EMA scalping is mathematically unviable after fees.**

### Market Conditions
- **Thrives:** Sustained intraday directional trends, vol expansions
- **Fails:** Sideways choppy markets (whipsaw = continuous fee drain)
- **Best Session:** US open / EU close overlap

### Verification
- **Paper:** arXiv:2602.10785
- **Code:** GitHub tmr-crypto/wf_optim_crypto_analysis (R + Python)
- **Companion:** GitHub tmr-crypto/wf_optim_crypto (strategy code)
- **Data:** Google Drive 76MB (linked from repo)

### Portfolio Enhancement
- Combining strategy + Buy-and-Hold reduces drawdown by **50%**
- Pure strategy: 35% MDD; blended portfolio: ~17% MDD

### Failure Modes
1. **Optimization lag:** 14-day training window lags sudden regime changes by days
2. **Fee attrition terminal** if exchange fee > 0.15% or slippage > 0.25%
3. **Constant flipping** erodes edge in ranging markets

---

## 5. Verified Strategy: LSTM-RNN with Microstructure Filter

### Core Architecture
- **Type:** Deep learning directional bias + deterministic confirmation
- **Timeframe:** 1-minute OHLCV
- **Neural Network:** 120-node bidirectional LSTM + 60-node standard LSTM
- **Input:** 180-minute price sequences
- **Key Innovation:** NN output must be confirmed by classical indicators

### Entry Logic
```
ENTRY CONDITION: get_rnn_result() == get_indicators_result()
```
- Neural network computes directional bias from 180 bars
- Deterministic layer: fast MA, slow MA, rolling standard deviation
- Trade ONLY when both agree (strict Boolean AND)

### Exit Logic
- Dynamic reversal of predictive conditions
- Hard-coded absolute stop-loss for tail risk
- Max 1 market order per minute (rate limit)

### Position Sizing
- Weighted by: MA spread distance, current volatility, free cash
- Minimum size threshold prevents micro-orders consumed by commission minimums

### Performance

| Metric | Value |
|--------|-------|
| ROI (6 months OOS) | 54.49% (vs 22.01% B&H) |
| Sharpe Ratio | >2.0 (annualized) |
| Max Drawdown | 33.78% |
| Trade Count | 1,289 (747 buys, 542 sells) |
| Avg Trades/Day | ~7 |
| Training Data | December 2020 |
| Test Period | Jan-Jun 2021 (strictly OOS) |
| Commission | Fully modeled |

### CRITICAL CAVEAT
- Tested exclusively during **Jan-Jun 2021 bull market**
- 33.78% drawdown aligned with May 2021 crash
- No bear market or sideways validation
- NN failed to conceptualize systemic liquidity drain

### Verification
- **Code:** GitHub liampgrichardson/Cryptocurrency_Trading_Bot
- **Framework:** Backtrader + TensorFlow/CUDA + TA-Lib
- **Runnable:** Yes (simplified version in Backtests/ folder)

### Failure Modes
1. **May 2021 crash:** NN viewed plunge as buyable deviation from mean
2. **Regime blindness:** DL predicts future resembles recent past
3. **GPU dependency:** Requires CUDA for real-time inference latency
4. **Needs kill switch:** Hard portfolio-level halt if aggregate DD exceeds threshold

---

## 6. Failed Strategies (Explicitly Excluded)

### A. Informer / Transformer Neural Networks
- Claim ~57% ROC-AUC on 5-30 min intervals
- **FATAL FLAW:** Omit 0.1% taker fee. 0.2% round-trip obliterates 57% edge.
- Predictions are smaller than bid-ask spread + commission combined
- **Verdict:** Mathematical phantoms. Unusable for live trading.

### B. NostalgiaForInfinity (Freqtrade)
- Hyper-complex indicator array with dozens of hard-coded magic numbers
- Parameters generated via Hyperopt on single historical dataset
- **FATAL FLAW:** Severe overfitting. Fails catastrophically OOS or in regime change.
- No academic-level double OOS validation.

### C. Pure Order Flow Imbalance (OFI) Linear Scalping
- Academic research confirms OFI correlates with sub-second price vectors
- **FATAL FLAW:** Standalone Sharpe ~0.12, R-squared drops to 3% OOS
- Signal is extremely noisy
- Institutional spoofing creates deliberate false imbalances
- **Use as COMPONENT, not standalone strategy**

---

## 7. Execution Infrastructure Requirements

### Latency & Co-location
- Alpha-AS and market making: MUST co-locate VPS near exchange data centers
- Network jitter causes adverse selection on resting limit orders
- Event-driven architecture required (not procedural loops)

### Tail Risk Events to Model
- **FTX collapse:** MMs pulled liquidity, creating massive LOB vacuums
- **May 2021 cascade:** Liquidation waterfalls, dozens of % slippage on market orders
- **API throttling:** CEXes throttle during peak panic -- algorithm goes blind
- **WebSocket disconnect:** Leveraged positions lose stop-loss protection

### Kill Switch Requirements
- Hard-coded exogenous kill switch overriding all strategy logic
- Alternative REST API fallback for emergency liquidation
- Mandatory use of exchange-hosted conditional orders (not local-only)

---

## 8. Risk-Adjusted Ranking (Gemini Analysis)

| Rank | Strategy | Asset | Style | Edge | Est. Sharpe | Max DD |
|------|----------|-------|-------|------|-------------|--------|
| 1 | Alpha-AS DRL MM | BTC, SOL | Passive Limit | Non-directional spread capture, RL risk aversion | 2.5-3.0+ | Moderate-High |
| 2 | LSTM-RNN + Filter | BTC, ETH | Aggressive Market | Sequence mapping + deterministic filter | >2.0 | High (33.78%) |
| 3 | Walk-Forward EMA | BTC, ETH, BNB | Market/Limit | Dynamic parameter shifting | ~1.5 | Low-Moderate |
| N/A | Informer NN | BTC | -- | Academic only | Negative | Catastrophic |
| N/A | Pure OFI | BTC, ETH | Limit | LOB volume imbalance only | ~0.12 | Unstable |

---

## 9. Source Links (Gemini Research)

### Papers
| Source | URL |
|--------|-----|
| Walk-Forward EMA (arXiv) | https://arxiv.org/abs/2602.10785 |
| Walk-Forward EMA (ResearchGate) | https://www.researchgate.net/ (search: walk forward crypto EMA) |
| Alpha-AS RL (PLOS ONE) | https://journals.plos.org/plosone/article?id=10.1371/journal.pone.0277042 |
| Alpha-AS RL (Semantic Scholar) | https://pdfs.semanticscholar.org/ (search: Avellaneda-Stoikov RL) |
| Crypto Microstructure (arXiv) | https://arxiv.org/abs/2602.00776 |
| Multi-TF Feature Engineering | https://www.preprints.org/manuscript/202603.0994/v1 |
| Deep LOB Forecasting (NIH) | https://pmc.ncbi.nlm.nih.gov/ (search: deep limit order book) |
| Crypto Microstructure ML (Venice) | https://unitesi.unive.it/ (search: cryptocurrency microstructure binance) |
| Cointegration Pairs (Erasmus) | https://thesis.eur.nl/pub/47732 |
| LSTM Trading Bot (GitHub) | https://github.com/liampgrichardson/Cryptocurrency_Trading_Bot |

### Tutorials & Guides
| Source | URL |
|--------|-----|
| DolphinDB AS Tutorial | https://docs.dolphindb.com/en/Tutorials/market_making_strategies.html |
| DolphinDB AS (Medium) | https://medium.com/@dolphindb (search: Avellaneda-Stoikov) |
| HFT Backtesting Best Practices | https://medium.com/@dolphindb (search: HF backtesting market making) |
| Day Trading Crypto (QuantifiedStrategies) | https://quantifiedstrategies.com/ (search: day trading cryptocurrency) |
| Algo Trading Solana (Digiqt) | https://digiqt.com/ (search: algo trading solana) |
| Mastering HFT Crypto (HyroTrader) | https://hyrotrader.com/ (search: mastering HFT crypto) |
| Backtesting Fees Slippage (Paybis) | https://paybis.com/ (search: backtest crypto bot fees) |
| Retrospective Simulation (QuantInsti) | https://blog.quantinsti.com/ (search: retrospective simulation) |

### GitHub Repositories
| Repo | URL | Purpose |
|------|-----|---------|
| HFTFramework | https://github.com/javifalces/HFTFramework | Alpha-AS implementation (290 stars) |
| Market_Maker_Strategies | https://github.com/TimCaron/Market_Maker_Strategies | A-S backtesting framework (WIP) |
| hftbacktest | https://github.com/nkaz001/hftbacktest | HFT backtesting with queue position modeling (3,927 stars) |
| wf_optim_crypto_analysis | https://github.com/tmr-crypto/wf_optim_crypto_analysis | Walk-forward EMA analysis code |
| Cryptocurrency_Trading_Bot | https://github.com/liampgrichardson/Cryptocurrency_Trading_Bot | LSTM-RNN bot |
| MacroHFT | https://github.com/ZONG0004/MacroHFT | KDD 2024 RL trading (132 stars) |
| freqtrade-strategies | https://github.com/freqtrade/freqtrade-strategies | Community strategies (5,023 stars) |

### Reddit & Community
| Source | URL |
|--------|-----|
| BTC Scalping Algo Discussion | https://reddit.com/r/BitcoinBeginners (search: scalping algo backtest) |
| 86-day 98.84% WR System | https://reddit.com/ (search: 1161 trades 98.84% win rate) |
| Spot Algo Trading Guide (ForexVPS) | https://forexvps.net/ (search: spot algorithmic trading) |
