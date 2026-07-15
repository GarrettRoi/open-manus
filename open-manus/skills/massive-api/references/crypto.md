# Crypto API Reference

Cryptocurrency market data with tick-level precision operating 24/7.

## Market Hours

Crypto markets operate 24/7. All timestamps are Unix milliseconds in UTC.

## Endpoint Categories

### 1. Aggregate Bars (OHLC)

#### Custom Bars
**Endpoint**: `GET /v2/aggs/ticker/{cryptoTicker}/range/{multiplier}/{timespan}/{from}/{to}`

**Path Parameters**:
- `cryptoTicker` (string): Crypto pair (e.g., "X:BTCUSD", "X:ETHUSD")

**Example**:
```python
import os
import requests

api_key = os.environ['MASSIVE_API']
pair = "X:BTCUSD"
url = f"https://api.massive.com/v2/aggs/ticker/{pair}/range/1/day/2024-01-01/2024-12-31?apiKey={api_key}"
response = requests.get(url)
data = response.json()

for bar in data['results']:
    print(f"Date: {bar['t']}, Close: ${bar['c']}, Volume: {bar['v']}")
```

#### Daily Market Summary
**Endpoint**: `GET /v2/aggs/grouped/locale/global/market/crypto/{date}`

#### Daily Ticker Summary
**Endpoint**: `GET /v1/open-close/crypto/{from}/{to}/{date}`

#### Previous Day Bar
**Endpoint**: `GET /v2/aggs/ticker/{cryptoTicker}/prev`

### 2. Tickers

#### All Tickers
**Endpoint**: `GET /v3/reference/tickers?market=crypto`

#### Ticker Overview
**Endpoint**: `GET /v3/reference/tickers/{cryptoTicker}`

### 3. Trades

#### Last Trade
**Endpoint**: `GET /v3/trades/{cryptoTicker}/last`

**Example**:
```python
import os
import requests

api_key = os.environ['MASSIVE_API']
pair = "X:BTCUSD"
url = f"https://api.massive.com/v3/trades/{pair}/last?apiKey={api_key}"
response = requests.get(url)
trade = response.json()['results']

print(f"Last Trade: ${trade['price']} x {trade['size']} at {trade['timestamp']}")
```

#### Trades
**Endpoint**: `GET /v3/trades/{cryptoTicker}`

### 4. Snapshots

#### Full Market Snapshot
**Endpoint**: `GET /v2/snapshot/locale/global/markets/crypto/tickers`

#### Single Ticker Snapshot
**Endpoint**: `GET /v2/snapshot/locale/global/markets/crypto/tickers/{cryptoTicker}`

#### Top Market Movers
**Endpoint**: `GET /v2/snapshot/locale/global/markets/crypto/{direction}`

#### Unified Snapshot
**Endpoint**: `GET /v3/snapshot?ticker.any_of={cryptoTickers}`

### 5. Technical Indicators

- `GET /v1/indicators/sma/{cryptoTicker}`
- `GET /v1/indicators/ema/{cryptoTicker}`
- `GET /v1/indicators/macd/{cryptoTicker}`
- `GET /v1/indicators/rsi/{cryptoTicker}`

### 6. Market Operations

#### Condition Codes
**Endpoint**: `GET /v3/reference/conditions?asset_class=crypto`

#### Exchanges
**Endpoint**: `GET /v3/reference/exchanges?asset_class=crypto`

#### Market Holidays
**Endpoint**: `GET /v1/marketstatus/upcoming`

#### Market Status
**Endpoint**: `GET /v1/marketstatus/now`

## Common Patterns

### Track Top Cryptocurrencies
```python
import os
import requests

api_key = os.environ['MASSIVE_API']
cryptos = ["X:BTCUSD", "X:ETHUSD", "X:ADAUSD", "X:SOLUSD"]

url = f"https://api.massive.com/v2/snapshot/locale/global/markets/crypto/tickers?tickers={','.join(cryptos)}&apiKey={api_key}"
response = requests.get(url)
data = response.json()

for ticker_data in data['tickers']:
    print(f"{ticker_data['ticker']}: ${ticker_data['day']['c']} ({ticker_data['todaysChangePerc']:.2f}%)")
    print(f"  24h Volume: ${ticker_data['day']['v']:,.0f}")
```
