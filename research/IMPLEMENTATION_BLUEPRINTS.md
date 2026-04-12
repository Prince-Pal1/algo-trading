# Strategy Implementation Blueprints

**For use by coding session agents. Each blueprint maps research to concrete code.**

---

## Blueprint 1: Walk-Forward EMA Momentum (PRIORITY -- hits Phase 2 Sharpe target)

### Why First
- Your `ema_crossover.py` already exists but is UNPROFITABLE (static params, wrong timeframe)
- Walk-forward optimization + 60-min timeframe should achieve Sharpe 1.252 (above >1.0 target)
- Fully open-source with companion code
- Fits existing `walk_forward.py` validator

### Files to Create/Modify

```
src/strategies/scalping/ema_crossover_wf.py    # NEW: walk-forward variant
src/backtest/walk_forward.py                    # MODIFY: add dynamic param optimization
config/strategies.toml                          # MODIFY: add wf_ema config section
```

### Code Pattern (ema_crossover_wf.py)

```python
"""Walk-Forward EMA Momentum Strategy.

Reference: arXiv:2602.10785
GitHub: tmr-crypto/wf_optim_crypto_analysis

CRITICAL: Only 60-min timeframe is viable. Sub-30-min all have negative Sharpe.
"""
from src.strategies.base import BaseStrategy, Signal
import pandas as pd
import numpy as np

class WalkForwardEMA(BaseStrategy):
    """Dynamic EMA crossover with rolling walk-forward parameter optimization."""

    def __init__(self, config):
        super().__init__(config)
        # Walk-forward window parameters (from paper's optimal config)
        self.train_window_days = 7      # 7-day training window
        self.test_window_days = 28      # 28-day testing window
        self.ema_fast_range = range(5, 50, 5)    # Fast EMA search space
        self.ema_slow_range = range(20, 200, 10)  # Slow EMA search space
        self.current_fast_period = 12   # Default until first optimization
        self.current_slow_period = 26   # Default until first optimization
        self.fee_per_trade = 0.001      # 0.1% conservative
        self.last_optimization = None

    def _optimize_params(self, historical_data: pd.DataFrame):
        """Rolling walk-forward optimization.

        Selects EMA lengths that maximize Robust Sharpe Ratio
        over the training window.
        """
        best_sharpe = -np.inf
        best_fast = self.current_fast_period
        best_slow = self.current_slow_period

        for fast in self.ema_fast_range:
            for slow in self.ema_slow_range:
                if fast >= slow:
                    continue
                # Calculate EMA crossover returns on training window
                ema_fast = historical_data['close'].ewm(span=fast).mean()
                ema_slow = historical_data['close'].ewm(span=slow).mean()
                signal = (ema_fast > ema_slow).astype(int) * 2 - 1  # +1/-1
                returns = historical_data['close'].pct_change() * signal.shift(1)
                # Subtract fee on every signal change
                flips = signal.diff().abs() > 0
                returns[flips] -= self.fee_per_trade
                # Robust Sharpe Ratio
                if returns.std() > 0:
                    sharpe = returns.mean() / returns.std() * np.sqrt(252 * 24)  # 60-min annualization
                    if sharpe > best_sharpe:
                        best_sharpe = sharpe
                        best_fast = fast
                        best_slow = slow

        self.current_fast_period = best_fast
        self.current_slow_period = best_slow
        return best_sharpe

    def on_candle(self, candle, features) -> Signal | None:
        """Generate signal using dynamically optimized EMA lengths.

        IMPORTANT: This is a Stop-and-Reverse (SAR) strategy.
        Position flips on crossover, requiring two executions per flip.
        """
        # Check if re-optimization is needed (every test_window_days)
        # ... [implement time-based trigger]

        ema_fast = features.get(f'ema_{self.current_fast_period}')
        ema_slow = features.get(f'ema_{self.current_slow_period}')

        if ema_fast is None or ema_slow is None:
            return None

        if ema_fast > ema_slow:
            return Signal(direction='long', strength=1.0,
                         metadata={'fast': self.current_fast_period,
                                  'slow': self.current_slow_period})
        elif ema_fast < ema_slow:
            return Signal(direction='short', strength=1.0,
                         metadata={'fast': self.current_fast_period,
                                  'slow': self.current_slow_period})
        return None
```

### Config Pattern (strategies.toml addition)

```toml
[strategies.wf_ema]
name = "Walk-Forward EMA Momentum"
module = "src.strategies.scalping.ema_crossover_wf"
class = "WalkForwardEMA"
enabled = true
timeframe = "60m"  # ONLY viable timeframe per paper
symbols = ["BTCUSDT", "ETHUSDT", "BNBUSDT"]

[strategies.wf_ema.params]
train_window_days = 7
test_window_days = 28
ema_fast_range_start = 5
ema_fast_range_end = 50
ema_fast_range_step = 5
ema_slow_range_start = 20
ema_slow_range_end = 200
ema_slow_range_step = 10
fee_per_trade = 0.001

[strategies.wf_ema.risk]
max_position_pct = 0.25
stop_loss_pct = 0.05   # 5% hard stop
break_even_fee = 0.004  # Strategy fails above 0.4% fee
```

### Validation Steps
1. Download 60-min BTC/ETH/BNB data (40+ months from Binance)
2. Split: 19 months training / 21 months OOS
3. Run walk-forward with 81 window combinations
4. Verify Sharpe > 1.0 on OOS period
5. Test fee sensitivity from 0.05% to 0.4%
6. Compare pure strategy vs blended (strategy + B&H for 50% DD reduction)

---

## Blueprint 2: MacroHFT (RL Regime-Aware Trading)

### Files to Create

```
src/strategies/rl/macro_hft.py             # Strategy wrapper
src/strategies/rl/sub_agents.py            # Low-level regime-specific agents
src/strategies/rl/meta_agent.py            # High-level regime selector
src/data/decomposition.py                  # Trend/volatility decomposition
src/data/macro_context.py                  # Cross-asset correlation features
requirements-rl.txt                        # PyTorch, gymnasium deps
```

### Architecture

```
                    +------------------+
                    |   Meta-Agent     |  <- selects sub-agent based on regime
                    | (Memory-Aug RL)  |
                    +--------+---------+
                             |
              +--------------+---------------+
              |              |               |
        +-----+----+  +-----+----+  +-------+------+
        | Trend    |  | Vol      |  | Mean-Rev     |
        | Sub-Agent|  | Sub-Agent|  | Sub-Agent    |
        +----------+  +----------+  +--------------+
              |              |               |
        +-----+----+  +-----+----+  +-------+------+
        | Trend    |  | Vol      |  | Calm Market  |
        | Features |  | Features |  | Features     |
        +----------+  +----------+  +--------------+
```

### Data Decomposition (from paper)

```python
"""Trend/Volatility decomposition for regime detection.

Reference: MacroHFT (KDD 2024) Section 3.2
"""
import pandas as pd
import numpy as np

def decompose_regime(ohlcv: pd.DataFrame, window: int = 60) -> dict:
    """Decompose price into trend and volatility components.

    Returns regime label: 'trend_up', 'trend_down', 'high_vol', 'low_vol'
    """
    returns = ohlcv['close'].pct_change()

    # Trend component: direction and strength
    sma = ohlcv['close'].rolling(window).mean()
    trend_strength = (ohlcv['close'] - sma) / sma

    # Volatility component: realized vol vs. historical
    realized_vol = returns.rolling(window).std() * np.sqrt(252 * 24 * 60)  # 1-min annualized
    vol_ma = realized_vol.rolling(window * 5).mean()
    vol_ratio = realized_vol / vol_ma

    # Regime classification
    regime = 'low_vol'
    if abs(trend_strength.iloc[-1]) > 0.02:
        regime = 'trend_up' if trend_strength.iloc[-1] > 0 else 'trend_down'
    elif vol_ratio.iloc[-1] > 1.5:
        regime = 'high_vol'

    return {
        'regime': regime,
        'trend_strength': trend_strength.iloc[-1],
        'vol_ratio': vol_ratio.iloc[-1],
        'realized_vol': realized_vol.iloc[-1]
    }
```

### Training Pipeline (Reference Only)

```bash
# Step 1: Data decomposition
python MacroHFT/scripts/decompose.sh

# Step 2: Train low-level sub-agents (one per regime)
python MacroHFT/scripts/train_low_level.sh

# Step 3: Train high-level meta-policy
python MacroHFT/scripts/train_high_level.sh

# Pre-trained checkpoints available in repo (Google Drive)
```

---

## Blueprint 3: VPIN Regime Filter (Overlay for ALL Strategies)

### Files to Create

```
src/data/vpin.py                           # VPIN calculator
src/strategies/filters/regime_filter.py    # Pause trading when VPIN > threshold
```

### Code Pattern

```python
"""Volume-Synchronized Probability of Informed Trading (VPIN).

Reference: ScienceDirect (Research in International Business and Finance, 2025)
URL: https://www.sciencedirect.com/science/article/pii/S0275531925004192

VPIN > 0.7 indicates imminent price jump. Pause mean-reversion strategies.
Serial correlation in VPIN makes it predictive (not just reactive).
"""
import numpy as np
import pandas as pd
from collections import deque

class VPINCalculator:
    """Calculate VPIN from trade flow data."""

    def __init__(self, bucket_size: float, n_buckets: int = 50):
        """
        Args:
            bucket_size: Volume per bucket (e.g., 10 BTC)
            n_buckets: Number of buckets for VPIN calculation (default 50)
        """
        self.bucket_size = bucket_size
        self.n_buckets = n_buckets
        self.buckets = deque(maxlen=n_buckets)
        self.current_bucket_volume = 0.0
        self.current_bucket_buy_volume = 0.0

    def update(self, price: float, volume: float, side: str) -> float | None:
        """Process a trade and return VPIN if bucket completes.

        Args:
            price: Trade price
            volume: Trade volume
            side: 'buy' or 'sell' (from trade aggressor side)

        Returns:
            VPIN value (0-1) when bucket completes, None otherwise
        """
        remaining = volume
        while remaining > 0:
            space = self.bucket_size - self.current_bucket_volume
            fill = min(remaining, space)

            self.current_bucket_volume += fill
            if side == 'buy':
                self.current_bucket_buy_volume += fill
            remaining -= fill

            if self.current_bucket_volume >= self.bucket_size:
                # Bucket complete
                buy_pct = self.current_bucket_buy_volume / self.bucket_size
                order_imbalance = abs(buy_pct - 0.5) * 2  # Normalize to 0-1
                self.buckets.append(order_imbalance)

                # Reset bucket
                self.current_bucket_volume = 0.0
                self.current_bucket_buy_volume = 0.0

                # Calculate VPIN if enough buckets
                if len(self.buckets) >= self.n_buckets:
                    vpin = np.mean(self.buckets)
                    return vpin

        return None

    @property
    def current_vpin(self) -> float:
        """Get latest VPIN value."""
        if len(self.buckets) < self.n_buckets:
            return 0.0
        return np.mean(self.buckets)


class RegimeFilter:
    """Pause strategies during high-toxicity regimes.

    Usage:
        filter = RegimeFilter(vpin_threshold=0.7)
        if filter.should_trade(current_vpin):
            # Execute strategy signal
        else:
            # Reduce position or go flat
    """

    def __init__(self, vpin_threshold: float = 0.7):
        self.vpin_threshold = vpin_threshold

    def should_trade(self, vpin: float) -> bool:
        """Returns True if safe to trade, False if VPIN indicates imminent jump."""
        return vpin < self.vpin_threshold

    def position_scalar(self, vpin: float) -> float:
        """Scale position size inversely with VPIN.

        Returns 1.0 when VPIN=0 (full size), 0.0 when VPIN >= threshold.
        """
        if vpin >= self.vpin_threshold:
            return 0.0
        return 1.0 - (vpin / self.vpin_threshold)
```

---

## Blueprint 4: Alpha-AS Market Making

### Files to Create

```
src/data/order_book.py                     # L2 order book processor (already planned)
src/strategies/market_making/avellaneda_stoikov.py  # Base A-S model
src/strategies/market_making/alpha_as.py   # RL-enhanced A-S
src/strategies/market_making/inventory_manager.py  # Delta-neutral inventory control
```

### Core A-S Equations

```python
"""Avellaneda-Stoikov reservation price and optimal spread.

Reference: PLOS ONE (journals.plos.org/plosone/article?id=10.1371/journal.pone.0277042)

WARNING: DolphinDB tutorial default params LOSE $27k in 2 days.
Must train RL agent to optimize gamma (risk aversion).
"""
import numpy as np

def reservation_price(mid_price: float, inventory: float,
                      gamma: float, sigma: float, T_remaining: float) -> float:
    """Calculate the price at which MM is indifferent to holding.

    r(s, t) = s - q * gamma * sigma^2 * (T - t)

    Args:
        mid_price: Current mid price (s)
        inventory: Current inventory in units (q), positive=long
        gamma: Risk aversion parameter (RL-optimized)
        sigma: Estimated volatility
        T_remaining: Time remaining in session (T - t)
    """
    return mid_price - inventory * gamma * (sigma ** 2) * T_remaining


def optimal_spread(gamma: float, sigma: float,
                   T_remaining: float, kappa: float) -> float:
    """Calculate optimal bid-ask spread.

    delta = gamma * sigma^2 * (T - t) + (2/gamma) * ln(1 + gamma/kappa)

    Args:
        gamma: Risk aversion parameter
        sigma: Estimated volatility
        T_remaining: Time remaining
        kappa: Order arrival intensity parameter
    """
    return gamma * (sigma ** 2) * T_remaining + (2 / gamma) * np.log(1 + gamma / kappa)


def compute_quotes(mid_price: float, inventory: float,
                   gamma: float, sigma: float,
                   T_remaining: float, kappa: float) -> tuple[float, float]:
    """Compute bid and ask prices.

    Returns:
        (bid_price, ask_price)
    """
    r = reservation_price(mid_price, inventory, gamma, sigma, T_remaining)
    spread = optimal_spread(gamma, sigma, T_remaining, kappa)
    half_spread = spread / 2
    return (r - half_spread, r + half_spread)
```

---

## Blueprint 5: Pairs Trading (Statistical Arbitrage)

### Files to Create

```
src/strategies/stat_arb/cointegration.py   # Cointegration testing
src/strategies/stat_arb/pairs_trading.py   # Pairs trading strategy
src/strategies/stat_arb/copula_signals.py  # Copula-based signal enhancement
```

### References
- **Optimized Cointegration:** https://onlinelibrary.wiley.com/doi/full/10.1002/fut.70018
  - Sharpe 3.97, Calmar reported, MDD 7.94%, Jan 2019-May 2024
  - Walk-forward validation with overfitting ratio analysis
- **Copula Enhancement:** https://arxiv.org/abs/2305.06961
  - Nonlinear dependence via copula functions
  - Published in Financial Innovation (Springer, 2025)
- **Erasmus Thesis:** https://thesis.eur.nl/pub/47732
  - Johansen method: 6.81% weekly return (incl. transaction costs)
  - Granger causality for pair pre-filtering
- **Dynamic Cointegration:** https://www.frontiersin.org/journals/applied-mathematics-and-statistics/articles/10.3389/fams.2026.1749337/full
  - Dynamic Johansen (not static) + DNN/LSTM ensemble

### Code Pattern

```python
"""Cointegration-based pairs trading.

Pairs: BTC-ETH, BTC-SOL, ETH-SOL
"""
from statsmodels.tsa.vector_ar.vecm import coint_johansen
from statsmodels.tsa.stattools import adfuller
import numpy as np
import pandas as pd

def test_cointegration(price_a: pd.Series, price_b: pd.Series,
                       significance: float = 0.05) -> dict:
    """Johansen cointegration test for a crypto pair.

    Returns:
        dict with 'cointegrated' (bool), 'hedge_ratio', 'spread'
    """
    data = pd.concat([price_a, price_b], axis=1).dropna()
    result = coint_johansen(data, det_order=0, k_ar_diff=1)

    # Check trace statistic at 5% level
    trace_stat = result.lr1[0]
    critical_value = result.cvt[0, 1]  # 5% critical value
    is_cointegrated = trace_stat > critical_value

    # Hedge ratio from eigenvector
    hedge_ratio = result.evec[1, 0] / result.evec[0, 0]

    # Spread
    spread = price_a - hedge_ratio * price_b

    return {
        'cointegrated': is_cointegrated,
        'hedge_ratio': hedge_ratio,
        'spread': spread,
        'trace_stat': trace_stat,
        'critical_value': critical_value,
        'adf_pvalue': adfuller(spread.dropna())[1]
    }


class PairsTrader:
    """Mean-reversion on cointegrated spread.

    Entry: spread deviates > entry_z standard deviations
    Exit: spread returns to < exit_z standard deviations
    """

    def __init__(self, entry_z: float = 2.0, exit_z: float = 0.5,
                 lookback: int = 60, hedge_ratio: float = 1.0):
        self.entry_z = entry_z
        self.exit_z = exit_z
        self.lookback = lookback
        self.hedge_ratio = hedge_ratio

    def get_signal(self, spread: pd.Series) -> str:
        """Generate signal from spread z-score.

        Returns: 'long_spread', 'short_spread', 'close', or 'hold'
        """
        mean = spread.rolling(self.lookback).mean().iloc[-1]
        std = spread.rolling(self.lookback).std().iloc[-1]

        if std == 0:
            return 'hold'

        z_score = (spread.iloc[-1] - mean) / std

        if z_score > self.entry_z:
            return 'short_spread'   # Spread will revert down
        elif z_score < -self.entry_z:
            return 'long_spread'    # Spread will revert up
        elif abs(z_score) < self.exit_z:
            return 'close'          # Mean reverted
        return 'hold'
```

---

## Data Download Commands

```bash
# 60-min BTC/ETH/SOL for Walk-Forward EMA (Strategy 5)
python src/data/downloader.py --symbol BTCUSDT --interval 1h --start 2022-01-01 --end 2026-04-01
python src/data/downloader.py --symbol ETHUSDT --interval 1h --start 2022-01-01 --end 2026-04-01
python src/data/downloader.py --symbol SOLUSDT --interval 1h --start 2022-01-01 --end 2026-04-01

# 1-min BTC/ETH for MacroHFT and LSTM strategies
python src/data/downloader.py --symbol BTCUSDT --interval 1m --start 2023-01-01 --end 2024-01-01
python src/data/downloader.py --symbol ETHUSDT --interval 1m --start 2023-01-01 --end 2024-01-01

# Clone reference repos
git clone https://github.com/ZONG0004/MacroHFT.git research/repos/MacroHFT
git clone https://github.com/tmr-crypto/wf_optim_crypto_analysis.git research/repos/wf_ema
git clone https://github.com/javifalces/HFTFramework.git research/repos/HFTFramework
git clone https://github.com/nkaz001/hftbacktest.git research/repos/hftbacktest
```
