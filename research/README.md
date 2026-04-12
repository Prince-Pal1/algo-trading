# Research Index -- Crypto Scalping Strategies

**Created:** April 12, 2026
**Purpose:** Complete research package for building verified crypto scalping strategies.

---

## How to Use This Research

**Other Claude Code session:** Read these files in order:

1. `README.md` (this file) -- overview and reading order
2. `CRYPTO_SCALPING_TOP5_REPORT.md` -- top 5 strategies ranked by Sharpe, all links, agent build plans
3. `GEMINI_DEEP_RESEARCH.md` -- deep analysis of microstructure, asset profiles, full strategy details
4. `IMPLEMENTATION_BLUEPRINTS.md` -- code patterns, file structures, config templates, download commands

---

## File Contents

| File | What's Inside | Lines |
|------|--------------|-------|
| `CRYPTO_SCALPING_TOP5_REPORT.md` | Top 5 strategies ranked by risk-adjusted return. Entry/exit rules, metrics, verification, market conditions, failure modes, 8 supplementary strategies, implementation priority, all links. | ~500 |
| `GEMINI_DEEP_RESEARCH.md` | Google Gemini Deep Research output. Asset microstructure (BTC vs ETH vs SOL), Alpha-AS deep dive, Walk-Forward EMA deep dive, LSTM-RNN deep dive, failed strategies autopsy, execution infrastructure, tail risk modeling. | ~400 |
| `IMPLEMENTATION_BLUEPRINTS.md` | Ready-to-code blueprints. Walk-Forward EMA (full Python class), MacroHFT architecture, VPIN regime filter, Alpha-AS equations, Pairs Trading with cointegration. Config templates, data download commands, repo clone commands. | ~450 |

---

## Top 5 Strategies (Quick Reference)

| # | Strategy | Sharpe | Code Available | Effort | Phase 2 Target? |
|---|----------|--------|---------------|--------|-----------------|
| 1 | MacroHFT (RL) | 3.89 (ETH) | github.com/ZONG0004/MacroHFT | 1-2 weeks | Yes |
| 2 | DRL Learn-to-Rank | 2.85 | No | 2-3 weeks | Yes |
| 3 | AdaptiveTrend | 2.41 | No | 1-2 weeks | Yes |
| 4 | Alpha-AS Market Making | 2.5-3.0+ | github.com/javifalces/HFTFramework | 2-4 weeks | Yes |
| **5** | **Walk-Forward EMA** | **1.252** | **github.com/tmr-crypto/wf_optim_crypto_analysis** | **2-3 days** | **YES (fastest path)** |

---

## Implementation Priority

**Start with Strategy 5 (Walk-Forward EMA)** because:
- Achieves Sharpe 1.252 (above Phase 2 target of >1.0)
- Your `ema_crossover.py` already exists -- just needs walk-forward optimization
- Fully open-source with companion code
- Works on 60-min candles (existing data pipeline supports this)
- 2-3 day implementation time

**Then add VPIN filter** (1 day) to ALL strategies as regime detection layer.

---

## Critical Findings

1. **BTC is too efficient** for simple scalping (3 independent papers confirm)
2. **Sub-30-min EMA is DEAD** -- paper proves 1-min Sharpe = -12.71 after fees
3. **Only 60-min EMA works** -- Sharpe 1.252 with 0.1% fee
4. **Publication decay ~50%** -- treat all public strategies as degraded starting points
5. **No strategy modeled funding rates** -- hidden cost for leveraged perps
6. **VPIN > 0.7 predicts price jumps** -- use as kill switch for mean-reversion
7. **XGBoost matches DeepLOB** with faster inference for LOB prediction
8. **DEX funding rate arb has edge** (Sharpe 23.55 on Drift) but CEX is dead (Sharpe -7.34 on Binance)

---

## All Verified Links

### Papers (15)
- MacroHFT: https://arxiv.org/abs/2406.14537
- DRL Learn-to-Rank: https://papers.ssrn.com/sol3/papers.cfm?abstract_id=5494646
- AdaptiveTrend: https://arxiv.org/abs/2602.11708
- Alpha-AS: https://journals.plos.org/plosone/article?id=10.1371/journal.pone.0277042
- Walk-Forward EMA: https://arxiv.org/abs/2602.10785
- Crypto Microstructure: https://arxiv.org/abs/2602.00776
- Informer Bitcoin HFT: https://arxiv.org/abs/2503.18096
- EarnHFT: https://arxiv.org/abs/2309.12891
- Cointegration Pairs: https://onlinelibrary.wiley.com/doi/full/10.1002/fut.70018
- Copula Pairs: https://arxiv.org/abs/2305.06961
- Funding Rate Arb: https://www.sciencedirect.com/science/article/pii/S2096720925000818
- VPIN Jump Prediction: https://www.sciencedirect.com/science/article/pii/S0275531925004192
- Multi-TF Features: https://www.preprints.org/manuscript/202603.0994/v1
- LOB Simple vs Deep: https://arxiv.org/abs/2506.05764
- Erasmus Thesis: https://thesis.eur.nl/pub/47732

### GitHub Repos (8)
- MacroHFT: https://github.com/ZONG0004/MacroHFT (132 stars)
- hftbacktest: https://github.com/nkaz001/hftbacktest (3,927 stars)
- HFTFramework: https://github.com/javifalces/HFTFramework (290 stars)
- Walk-Forward Analysis: https://github.com/tmr-crypto/wf_optim_crypto_analysis
- Walk-Forward Strategy: https://github.com/tmr-crypto/wf_optim_crypto
- LSTM Trading Bot: https://github.com/liampgrichardson/Cryptocurrency_Trading_Bot
- Market Maker Strategies: https://github.com/TimCaron/Market_Maker_Strategies (WIP)
- Freqtrade Strategies: https://github.com/freqtrade/freqtrade-strategies (5,023 stars)
