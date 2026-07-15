# Futures API Reference

Futures contracts data including commodities, indices futures, and derivatives.

## Market Hours

Futures markets operate nearly 24 hours with specific session times per contract. All timestamps are Unix milliseconds in UTC, but market hours are typically in Central Time (CT).

## Endpoint Categories

### 1. Aggregates

#### Aggregate Bars (OHLC)
**Endpoint**: `GET /v2/aggs/ticker/{futuresTicker}/range/{multiplier}/{timespan}/{from}/{to}`

Retrieve OHLC and volume data for a futures contract.

**Path Parameters**:
- `futuresTicker` (string): Futures ticker (e.g., "ES", "GC", "CL")
- `multiplier` (integer): Timespan multiplier
- `timespan` (string): "minute", "hour", "day", "week", "month"
- `from` (string): Start date (YYYY-MM-DD or Unix ms timestamp)
- `to` (string): End date (YYYY-MM-DD or Unix ms timestamp)

**Query Parameters**:
- `adjusted` (boolean, default: true): Adjust for splits
- `sort` (string, default: "asc"): Sort order
- `limit` (integer, default: 5000, max: 50000): Results per page

**Response**: `results[]` with `o`, `h`, `l`, `c`, `v`, `vw`, `t`, `n`

**Example**:
```python
import os
import requests

api_key = os.environ['MASSIVE_API']
futures_ticker = "ES"  # E-mini S&P 500
url = f"https://api.massive.com/v2/aggs/ticker/{futures_ticker}/range/1/day/2024-01-01/2024-12-31?apiKey={api_key}"
response = requests.get(url)
data = response.json()

for bar in data['results']:
    print(f"Date: {bar['t']}, Close: ${bar['c']}, Volume: {bar['v']}")
```

### 2. Contracts

#### Contracts
**Endpoint**: `GET /v3/reference/futures/contracts`

Retrieve futures contract details.

**Query Parameters**:
- `ticker` (string): Filter by ticker
- `underlying_ticker` (string): Filter by underlying ticker
- `contract_type` (string): Filter by contract type
- `expiration_date` (string): Filter by expiration date
- `expired` (boolean): Include expired contracts
- `order` (string, default: "asc"): Sort order
- `limit` (integer, default: 1000, max: 1000): Results per page

**Response includes**: additional_underlyings, cfi, contract_type, expiration_date, first_notice_date, last_trading_date, primary_exchange, size, ticker, underlying_ticker

#### Products
**Endpoint**: `GET /v3/reference/futures/products`

Retrieve futures product information.

**Query Parameters**:
- `ticker` (string): Filter by ticker
- `exchange` (string): Filter by exchange
- `order` (string, default: "asc"): Sort order
- `limit` (integer, default: 1000, max: 1000): Results per page

**Response includes**: code, exchange, name, notional_multiplier_units, notional_multiplier_value, tick_size, ticker

#### Schedules
**Endpoint**: `GET /v3/reference/futures/schedules`

Retrieve futures trading schedules.

**Query Parameters**:
- `ticker` (string): Filter by ticker
- `date` (string): Filter by date
- `order` (string, default: "asc"): Sort order
- `limit` (integer, default: 1000, max: 1000): Results per page

**Response includes**: close_time, open_time, session_type, ticker, trading_date

### 3. Snapshots

#### Futures Contracts Snapshot
**Endpoint**: `GET /v3/snapshot/futures/{futuresTicker}`

Retrieve real-time snapshot for futures contracts.

**Path Parameters**:
- `futuresTicker` (string): Futures ticker

**Response includes**: break_even_price, day, details, greeks, last_quote, last_trade, open_interest, session, underlying_asset

**Example**:
```python
import os
import requests

api_key = os.environ['MASSIVE_API']
futures_ticker = "ES"
url = f"https://api.massive.com/v3/snapshot/futures/{futures_ticker}?apiKey={api_key}"
response = requests.get(url)
snapshot = response.json()['results']

print(f"Ticker: {snapshot['details']['ticker']}")
print(f"Last Trade: ${snapshot['last_trade']['price']}")
print(f"Day High: ${snapshot['day']['high']}, Low: ${snapshot['day']['low']}")
print(f"Volume: {snapshot['day']['volume']}")
```

### 4. Trades & Quotes

#### Trades
**Endpoint**: `GET /v3/trades/{futuresTicker}`

Retrieve tick-level trade data.

**Query Parameters**:
- `timestamp.gte` (string): Start of range
- `timestamp.lte` (string): End of range
- `order` (string, default: "asc"): Sort order
- `limit` (integer, default: 5000, max: 50000): Results per page

**Response includes**: conditions, exchange, participant_timestamp, price, sip_timestamp, size

#### Quotes
**Endpoint**: `GET /v3/quotes/{futuresTicker}`

Retrieve quote data.

**Query Parameters**: Same as Trades

**Response includes**: ask, ask_exchange, ask_size, bid, bid_exchange, bid_size, conditions, participant_timestamp, sip_timestamp

### 5. Market Operations

#### Exchanges
**Endpoint**: `GET /v3/reference/exchanges`

Retrieve list of supported futures exchanges.

**Query Parameters**:
- `asset_class` (string): "futures"

**Response includes**: acronym, asset_class, id, locale, mic, name, operating_mic, type, url

#### Market Status
**Endpoint**: `GET /v1/marketstatus/now`

Retrieve current trading status.

## Common Patterns

### Get Active Futures Contracts
```python
import os
import requests

api_key = os.environ['MASSIVE_API']
url = f"https://api.massive.com/v3/reference/futures/contracts?expired=false&order=asc&limit=1000&apiKey={api_key}"

all_contracts = []
while url:
    response = requests.get(url)
    data = response.json()
    all_contracts.extend(data.get('results', []))
    url = data.get('next_url')

print(f"Found {len(all_contracts)} active futures contracts")
for contract in all_contracts[:10]:
    print(f"{contract['ticker']}: Exp {contract['expiration_date']}")
```
