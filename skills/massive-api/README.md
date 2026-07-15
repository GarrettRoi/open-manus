# Massive API Skill

Comprehensive access to Massive.com API (evolution of polygon.io) for financial market data across all asset classes.

## Overview

This skill provides complete coverage of the Massive.com REST API, enabling access to:

- **Stocks**: OHLC bars, tickers, snapshots, trades, quotes, technical indicators, corporate actions, fundamentals, SEC filings
- **Options**: Contracts, option chains, OHLC bars, snapshots, trades, quotes, Greeks, implied volatility
- **Futures**: Contracts, products, schedules, aggregates, snapshots, trades, quotes
- **Indices**: Index values, OHLC bars, snapshots, technical indicators
- **Forex**: 1,750+ currency pairs, OHLC bars, quotes, currency conversion, snapshots
- **Crypto**: Cryptocurrency market data, OHLC bars, trades, snapshots, technical indicators
- **Economy**: Macroeconomic indicators (inflation, labor market, treasury yields)
- **Partners**: Benzinga news, analyst ratings, ETF analytics, corporate events

## Structure

```
massive-api/
├── SKILL.md                    # Main skill file with core concepts and navigation
├── README.md                   # This file
├── scripts/
│   └── fetch_stock_data.py    # Helper script for fetching stock data
└── references/
    ├── stocks.md              # Comprehensive stocks API reference
    ├── options.md             # Options API reference
    ├── futures.md             # Futures API reference
    ├── indices.md             # Indices API reference
    ├── forex.md               # Forex API reference
    ├── crypto.md              # Crypto API reference
    ├── economy.md             # Economy indicators reference
    └── partners.md            # Third-party data reference
```

## Usage

The skill uses progressive disclosure - start with `SKILL.md` for core concepts and authentication, then read the appropriate reference file based on the asset class needed.

### Authentication

The skill uses the `MASSIVE_API` environment variable for authentication. All examples use this variable.

### Example Usage

**Get historical stock prices**:
```python
import os
import requests

api_key = os.environ['MASSIVE_API']
ticker = "AAPL"
url = f"https://api.massive.com/v2/aggs/ticker/{ticker}/range/1/day/2024-01-01/2024-12-31?apiKey={api_key}"
response = requests.get(url)
data = response.json()
```

**Get options chain with Greeks**:
```python
import os
import requests

api_key = os.environ['MASSIVE_API']
underlying = "AAPL"
url = f"https://api.massive.com/v3/snapshot/options/{underlying}?apiKey={api_key}"
response = requests.get(url)
data = response.json()

for contract in data['results']:
    greeks = contract.get('greeks', {})
    print(f"{contract['details']['ticker']}: Delta={greeks.get('delta')}, IV={contract.get('implied_volatility')}")
```

## Coverage

- **196 REST endpoints** across 8 asset classes
- **Real-time and historical data** from all major U.S. exchanges
- **Comprehensive fundamentals** including balance sheets, income statements, cash flows
- **Corporate actions** including dividends, splits, IPOs
- **Third-party data** including news, analyst ratings, ETF analytics
- **Economic indicators** from Federal Reserve

## Line Count

- Main SKILL.md: 280 lines
- Reference files: 2,038 lines total
- Helper script: 61 lines
- **Total: 2,318 lines**

## When to Use

Use this skill when you need to:
- Fetch stock prices, options data, or other market data
- Analyze company fundamentals or financial statements
- Track corporate actions (dividends, splits, IPOs)
- Monitor analyst ratings and consensus
- Access economic indicators
- Retrieve news and corporate events
- Build financial analysis tools or trading systems

## API Documentation

Official documentation: https://massive.com/docs
