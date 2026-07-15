# Forex API Reference

Foreign exchange data for 1,750+ currency pairs from major institutions.

## Market Hours

Forex operates 24/5 (Sunday 5 PM ET - Friday 5 PM ET). All timestamps are Unix milliseconds in UTC.

## Endpoint Categories

### 1. Aggregate Bars (OHLC)

#### Custom Bars
**Endpoint**: `GET /v2/aggs/ticker/{forexTicker}/range/{multiplier}/{timespan}/{from}/{to}`

**Path Parameters**:
- `forexTicker` (string): Currency pair (e.g., "C:EURUSD", "C:GBPUSD")
- `multiplier`, `timespan`, `from`, `to`: Same as stocks

**Example**:
```python
import os
import requests

api_key = os.environ['MASSIVE_API']
pair = "C:EURUSD"
url = f"https://api.massive.com/v2/aggs/ticker/{pair}/range/1/day/2024-01-01/2024-12-31?apiKey={api_key}"
response = requests.get(url)
data = response.json()

for bar in data['results']:
    print(f"Date: {bar['t']}, Close: {bar['c']}")
```

#### Daily Market Summary
**Endpoint**: `GET /v2/aggs/grouped/locale/global/market/fx/{date}`

#### Previous Day Bar
**Endpoint**: `GET /v2/aggs/ticker/{forexTicker}/prev`

### 2. Currency Conversion

#### Currency Conversion
**Endpoint**: `GET /v1/conversion/{from}/{to}`

Retrieve real-time currency conversion rates.

**Path Parameters**:
- `from` (string): Source currency code (e.g., "USD")
- `to` (string): Target currency code (e.g., "EUR")

**Query Parameters**:
- `amount` (number, default: 1): Amount to convert
- `precision` (integer, default: 2): Decimal precision

**Example**:
```python
import os
import requests

api_key = os.environ['MASSIVE_API']
from_currency = "USD"
to_currency = "EUR"
amount = 1000

url = f"https://api.massive.com/v1/conversion/{from_currency}/{to_currency}?amount={amount}&precision=4&apiKey={api_key}"
response = requests.get(url)
data = response.json()

print(f"{amount} {from_currency} = {data['converted']} {to_currency}")
print(f"Exchange Rate: {data['last']['exchange_rate']}")
```

### 3. Tickers

#### All Tickers
**Endpoint**: `GET /v3/reference/tickers?market=fx`

#### Ticker Overview
**Endpoint**: `GET /v3/reference/tickers/{forexTicker}`

### 4. Quotes

#### Last Quote
**Endpoint**: `GET /v3/quotes/{forexTicker}/last`

**Example**:
```python
import os
import requests

api_key = os.environ['MASSIVE_API']
pair = "C:EURUSD"
url = f"https://api.massive.com/v3/quotes/{pair}/last?apiKey={api_key}"
response = requests.get(url)
quote = response.json()['results']

print(f"Bid: {quote['bid']}, Ask: {quote['ask']}")
print(f"Spread: {quote['ask'] - quote['bid']:.5f}")
```

#### Quotes
**Endpoint**: `GET /v3/quotes/{forexTicker}`

### 5. Snapshots

#### Full Market Snapshot
**Endpoint**: `GET /v2/snapshot/locale/global/markets/forex/tickers`

#### Single Ticker Snapshot
**Endpoint**: `GET /v2/snapshot/locale/global/markets/forex/tickers/{forexTicker}`

#### Top Market Movers
**Endpoint**: `GET /v2/snapshot/locale/global/markets/forex/{direction}`

**Path Parameters**: `direction` = "gainers" or "losers"

#### Unified Snapshot
**Endpoint**: `GET /v3/snapshot?ticker.any_of={forexTickers}`

### 6. Technical Indicators

- `GET /v1/indicators/sma/{forexTicker}`
- `GET /v1/indicators/ema/{forexTicker}`
- `GET /v1/indicators/macd/{forexTicker}`
- `GET /v1/indicators/rsi/{forexTicker}`

### 7. Market Operations

#### Exchanges
**Endpoint**: `GET /v3/reference/exchanges?asset_class=fx`

#### Market Holidays
**Endpoint**: `GET /v1/marketstatus/upcoming`

#### Market Status
**Endpoint**: `GET /v1/marketstatus/now`

## Common Patterns

### Monitor Multiple Currency Pairs
```python
import os
import requests

api_key = os.environ['MASSIVE_API']
pairs = ["C:EURUSD", "C:GBPUSD", "C:USDJPY", "C:AUDUSD"]

url = f"https://api.massive.com/v2/snapshot/locale/global/markets/forex/tickers?tickers={','.join(pairs)}&apiKey={api_key}"
response = requests.get(url)
data = response.json()

for ticker_data in data['tickers']:
    print(f"{ticker_data['ticker']}: {ticker_data['day']['c']} ({ticker_data['todaysChangePerc']:.2f}%)")
```
