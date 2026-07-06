# Trend strategy backtest report — In-sample

Data: Coinbase daily BTC-USD and ETH-USD, through 2024-06-24.

- Ending equity: $616,866.87
- Net return: 516.8669%
- Sharpe: 1.281
- Maximum drawdown: -25.4804%
- Turnover: 55.4074
- Total fees: $87,980.91
- Fee drag: 14.55% of gross
- Coinbase maker fee supplied from API: 0.004
- ±50% perturbation profitability requirement: **PASS**

Sizing constraints: 10% volatility-target contribution, 2x per-instrument leverage cap, 25% allocation cap, and 25% rebalance buffer. Maker limits fill only on strict trade-through.
