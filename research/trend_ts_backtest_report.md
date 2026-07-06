# Trend strategy backtest report — In-sample

Data: Coinbase daily BTC-USD and ETH-USD, through 2024-06-24.

- Ending equity: $553,770.02
- Net return: 453.7700%
- Sharpe: 1.209
- Maximum drawdown: -27.4944%
- Turnover: 55.1301
- Total fees: $123,774.32
- Fee drag: 21.43% of gross
- Coinbase maker fee supplied from API: 0.006
- ±50% perturbation profitability requirement: **PASS**

Sizing constraints: 10% volatility-target contribution, 2x per-instrument leverage cap, 25% allocation cap, and 25% rebalance buffer. Maker limits fill only on strict trade-through.
