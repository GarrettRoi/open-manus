# Indices API Reference

Index values and data for S&P, Nasdaq, Dow Jones, and other major indices.

## Endpoint Categories

### 1. Aggregate Bars (OHLC)

#### Custom Bars
**Endpoint**: `GET /v2/aggs/ticker/{indexTicker}/range/{multiplier}/{timespan}/{from}/{to}`

**Path Parameters**:
- `indexTicker` (string): Index ticker (e.g., "I:SPX", "I:NDX", "I:DJI")
- `multiplier` (integer): Timespan multiplier
- `timespan` (string): "minute", "hour", "day", "week", "month"
- `from`, `to` (string): Date range

**Query Parameters**: `adjusted`, `sort`, `limit`

**Example**:
```python
import os
import requests

api_key = os.environ['MASSIVE_API']
index_ticker = "I:SPX"  # S&P 500
url = f"https://api.massive.com/v2/aggs/ticker/{index_ticker}/range/1/day/2024-01-01/2024-12-31?apiKey={api_key}"
response = requests.get(url)
data = response.json()

for bar in data['results']:
    print(f"Date: {bar['t']}, Close: {bar['c']}")
```

#### Daily Ticker Summary
**Endpoint**: `GET /v1/open-close/{indexTicker}/{date}`

#### Previous Day Bar
**Endpoint**: `GET /v2/aggs/ticker/{indexTicker}/prev`

### 2. Tickers

#### All Tickers
**Endpoint**: `GET /v3/reference/tickers?market=indices`

#### Ticker Overview
**Endpoint**: `GET /v3/reference/tickers/{indexTicker}`

### 3. Snapshots

#### Indices Snapshot
**Endpoint**: `GET /v3/snapshot/indices`

**Query Parameters**:
- `ticker.any_of` (string): Comma-separated index tickers

**Example**:
```python
import os
import requests

api_key = os.environ['MASSIVE_API']
indices = "I:SPX,I:NDX,I:DJI"
url = f"https://api.massive.com/v3/snapshot/indices?ticker.any_of={indices}&apiKey={api_key}"
response = requests.get(url)
data = response.json()

for index in data['results']:
    print(f"{index['ticker']}: {index['value']} ({index['session']['change_percent']:.2f}%)")
```

#### Unified Snapshot
**Endpoint**: `GET /v3/snapshot?ticker.any_of={indexTickers}`

### 4. Technical Indicators

- `GET /v1/indicators/sma/{indexTicker}`
- `GET /v1/indicators/ema/{indexTicker}`
- `GET /v1/indicators/macd/{indexTicker}`
- `GET /v1/indicators/rsi/{indexTicker}`

### 5. Market Operations

#### Market Status
**Endpoint**: `GET /v1/marketstatus/now`
