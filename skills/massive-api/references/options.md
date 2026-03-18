# Options API Reference

U.S. options market data from all 17 options exchanges with complete OPRA feed coverage.

## Market Hours

- **Regular Hours**: 9:30 AM - 4:00 PM ET (Monday-Friday)

All timestamps are Unix milliseconds in UTC.

## Endpoint Categories

### 1. Contracts

#### All Contracts
**Endpoint**: `GET /v3/reference/options/contracts`

Retrieve comprehensive index of options contracts (active and expired).

**Query Parameters**:
- `underlying_ticker` (string): Filter by underlying ticker
- `contract_type` (string): "call" or "put"
- `expiration_date` (string): Filter by expiration date
- `expiration_date.gte` (string): Expiration date greater than or equal
- `expiration_date.lte` (string): Expiration date less than or equal
- `strike_price` (number): Filter by strike price
- `strike_price.gte` (number): Strike price greater than or equal
- `strike_price.lte` (number): Strike price less than or equal
- `expired` (boolean): Include expired contracts
- `order` (string, default: "asc"): Sort order
- `limit` (integer, default: 1000, max: 1000): Results per page
- `sort` (string): Sort field

**Response includes**: cfi, contract_type, exercise_style, expiration_date, primary_exchange, shares_per_contract, strike_price, ticker, underlying_ticker

**Example**:
```python
import os
import requests

api_key = os.environ['MASSIVE_API']
underlying = "AAPL"
url = f"https://api.massive.com/v3/reference/options/contracts?underlying_ticker={underlying}&contract_type=call&expiration_date.gte=2024-01-01&limit=1000&apiKey={api_key}"
response = requests.get(url)
data = response.json()

for contract in data['results']:
    print(f"{contract['ticker']}: Strike ${contract['strike_price']}, Exp {contract['expiration_date']}")
```

#### Contract Overview
**Endpoint**: `GET /v3/reference/options/contracts/{options_ticker}`

Retrieve detailed information about a specific options contract.

**Path Parameters**:
- `options_ticker` (string): Options contract ticker (e.g., "O:AAPL240119C00150000")

**Response includes**: cfi, contract_type, exercise_style, expiration_date, primary_exchange, shares_per_contract, strike_price, ticker, underlying_ticker, additional_underlyings

**Example**:
```python
import os
import requests

api_key = os.environ['MASSIVE_API']
options_ticker = "O:AAPL240119C00150000"
url = f"https://api.massive.com/v3/reference/options/contracts/{options_ticker}?apiKey={api_key}"
response = requests.get(url)
contract = response.json()['results']

print(f"Contract: {contract['ticker']}")
print(f"Type: {contract['contract_type']}, Strike: ${contract['strike_price']}")
print(f"Expiration: {contract['expiration_date']}")
print(f"Underlying: {contract['underlying_ticker']}")
```

### 2. Aggregate Bars (OHLC)

#### Custom Bars
**Endpoint**: `GET /v2/aggs/ticker/{optionsTicker}/range/{multiplier}/{timespan}/{from}/{to}`

Retrieve OHLC and volume data for an options contract.

**Path Parameters**:
- `optionsTicker` (string): Options ticker (e.g., "O:AAPL240119C00150000")
- `multiplier` (integer): Timespan multiplier
- `timespan` (string): "minute", "hour", "day", "week", "month", "quarter", "year"
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
options_ticker = "O:AAPL240119C00150000"
url = f"https://api.massive.com/v2/aggs/ticker/{options_ticker}/range/1/day/2024-01-01/2024-01-19?apiKey={api_key}"
response = requests.get(url)
data = response.json()

for bar in data['results']:
    print(f"Date: {bar['t']}, Close: ${bar['c']}, Volume: {bar['v']}")
```

#### Daily Ticker Summary
**Endpoint**: `GET /v1/open-close/{optionsTicker}/{date}`

Retrieve opening and closing prices for a specific date.

#### Previous Day Bar
**Endpoint**: `GET /v2/aggs/ticker/{optionsTicker}/prev`

Retrieve previous trading day's OHLC.

### 3. Snapshots

#### Option Chain Snapshot
**Endpoint**: `GET /v3/snapshot/options/{underlyingAsset}`

Retrieve comprehensive snapshot of all options contracts for an underlying ticker.

**Path Parameters**:
- `underlyingAsset` (string): Underlying ticker (e.g., "AAPL")

**Query Parameters**:
- `strike_price` (number): Filter by strike price
- `strike_price.gte` (number): Strike price greater than or equal
- `strike_price.lte` (number): Strike price less than or equal
- `expiration_date` (string): Filter by expiration date
- `expiration_date.gte` (string): Expiration date greater than or equal
- `expiration_date.lte` (string): Expiration date less than or equal
- `contract_type` (string): "call" or "put"
- `order` (string, default: "asc"): Sort order
- `limit` (integer, default: 250, max: 250): Results per page

**Response includes**: For each contract: break_even_price, day (open, high, low, close, volume, vw, previous_close, change, change_percent), details (contract_type, exercise_style, expiration_date, shares_per_contract, strike_price, ticker, underlying_ticker), greeks (delta, gamma, theta, vega), implied_volatility, last_quote, last_trade, open_interest, underlying_asset

**Example**:
```python
import os
import requests

api_key = os.environ['MASSIVE_API']
underlying = "AAPL"
url = f"https://api.massive.com/v3/snapshot/options/{underlying}?contract_type=call&expiration_date.gte=2024-01-01&expiration_date.lte=2024-03-31&apiKey={api_key}"
response = requests.get(url)
data = response.json()

for contract in data['results']:
    details = contract['details']
    greeks = contract.get('greeks', {})
    print(f"{details['ticker']}: Strike ${details['strike_price']}, IV {contract.get('implied_volatility', 'N/A')}")
    print(f"  Delta: {greeks.get('delta', 'N/A')}, Gamma: {greeks.get('gamma', 'N/A')}")
    print(f"  Last: ${contract['day']['close']}, Volume: {contract['day']['volume']}")
```

#### Option Contract Snapshot
**Endpoint**: `GET /v3/snapshot/options/{underlyingAsset}/{optionContract}`

Retrieve comprehensive snapshot of a specific options contract.

**Path Parameters**:
- `underlyingAsset` (string): Underlying ticker
- `optionContract` (string): Options contract ticker

**Response includes**: Same fields as Option Chain Snapshot, but for single contract

#### Unified Snapshot
**Endpoint**: `GET /v3/snapshot`

Retrieve unified snapshots for multiple asset classes.

**Query Parameters**:
- `ticker.any_of` (string): Comma-separated tickers (can include options tickers)

### 4. Trades & Quotes

#### Last Trade
**Endpoint**: `GET /v3/trades/{optionsTicker}/last`

Retrieve latest trade for an options contract.

**Response includes**: conditions, exchange, participant_timestamp, price, sip_timestamp, size

**Example**:
```python
import os
import requests

api_key = os.environ['MASSIVE_API']
options_ticker = "O:AAPL240119C00150000"
url = f"https://api.massive.com/v3/trades/{options_ticker}/last?apiKey={api_key}"
response = requests.get(url)
trade = response.json()['results']

print(f"Last Trade: ${trade['price']} x {trade['size']} at {trade['sip_timestamp']}")
```

#### Trades
**Endpoint**: `GET /v3/trades/{optionsTicker}`

Retrieve tick-level trade data over a time range.

**Query Parameters**:
- `timestamp` (string): Specific timestamp
- `timestamp.gte` (string): Greater than or equal to timestamp
- `timestamp.lte` (string): Less than or equal to timestamp
- `order` (string, default: "asc"): Sort order
- `limit` (integer, default: 5000, max: 50000): Results per page

#### Quotes
**Endpoint**: `GET /v3/quotes/{optionsTicker}`

Retrieve historical quotes over a time range.

**Query Parameters**: Same as Trades endpoint

**Response includes**: ask, ask_exchange, ask_size, bid, bid_exchange, bid_size, conditions, indicators, participant_timestamp, sip_timestamp

### 5. Technical Indicators

All technical indicators follow the same pattern:

**Endpoints**:
- `GET /v1/indicators/sma/{optionsTicker}` - Simple Moving Average
- `GET /v1/indicators/ema/{optionsTicker}` - Exponential Moving Average
- `GET /v1/indicators/macd/{optionsTicker}` - MACD
- `GET /v1/indicators/rsi/{optionsTicker}` - Relative Strength Index

**Common Query Parameters**:
- `timestamp.gte` (string): Start of range
- `timestamp.lte` (string): End of range
- `timespan` (string): "minute", "hour", "day", "week", "month"
- `adjusted` (boolean, default: true): Adjust for splits
- `window` (integer): Indicator window size
- `series_type` (string, default: "close"): Price type
- `order` (string, default: "desc"): Sort order
- `limit` (integer, default: 5000, max: 50000): Results per page

### 6. Market Operations

#### Condition Codes
**Endpoint**: `GET /v3/reference/conditions`

Retrieve unified list of trade and quote conditions.

**Query Parameters**:
- `asset_class` (string): "options"
- `data_type` (string): "trade" or "quote"

#### Exchanges
**Endpoint**: `GET /v3/reference/exchanges`

Retrieve list of options exchanges.

**Query Parameters**:
- `asset_class` (string): "options"

**Response includes**: acronym, asset_class, id, locale, mic, name, operating_mic, participant_id, type, url

#### Market Holidays
**Endpoint**: `GET /v1/marketstatus/upcoming`

Retrieve upcoming market holidays.

#### Market Status
**Endpoint**: `GET /v1/marketstatus/now`

Retrieve current trading status.

## Common Patterns

### Building an Options Chain
```python
import os
import requests
from datetime import datetime, timedelta

api_key = os.environ['MASSIVE_API']
underlying = "AAPL"

# Get contracts expiring in next 60 days
today = datetime.now().strftime('%Y-%m-%d')
sixty_days = (datetime.now() + timedelta(days=60)).strftime('%Y-%m-%d')

url = f"https://api.massive.com/v3/reference/options/contracts?underlying_ticker={underlying}&expiration_date.gte={today}&expiration_date.lte={sixty_days}&order=asc&limit=1000&apiKey={api_key}"

contracts = []
while url:
    response = requests.get(url)
    data = response.json()
    contracts.extend(data.get('results', []))
    url = data.get('next_url')

# Group by expiration and strike
from collections import defaultdict
chain = defaultdict(lambda: defaultdict(dict))

for contract in contracts:
    exp = contract['expiration_date']
    strike = contract['strike_price']
    ctype = contract['contract_type']
    chain[exp][strike][ctype] = contract['ticker']

print(f"Found {len(contracts)} contracts across {len(chain)} expirations")
```

### Getting Greeks and IV for Options Chain
```python
import os
import requests

api_key = os.environ['MASSIVE_API']
underlying = "AAPL"

# Get snapshot with Greeks
url = f"https://api.massive.com/v3/snapshot/options/{underlying}?expiration_date.gte=2024-01-01&expiration_date.lte=2024-03-31&limit=250&apiKey={api_key}"

all_contracts = []
while url:
    response = requests.get(url)
    data = response.json()
    all_contracts.extend(data.get('results', []))
    url = data.get('next_url')

# Extract Greeks and IV
for contract in all_contracts:
    details = contract['details']
    greeks = contract.get('greeks', {})
    iv = contract.get('implied_volatility')
    
    print(f"{details['ticker']} (Strike: ${details['strike_price']})")
    print(f"  IV: {iv:.2%} if iv else 'N/A'}")
    print(f"  Delta: {greeks.get('delta', 'N/A')}")
    print(f"  Gamma: {greeks.get('gamma', 'N/A')}")
    print(f"  Theta: {greeks.get('theta', 'N/A')}")
    print(f"  Vega: {greeks.get('vega', 'N/A')}")
```

### Analyzing Option Volume and Open Interest
```python
import os
import requests

api_key = os.environ['MASSIVE_API']
underlying = "AAPL"

url = f"https://api.massive.com/v3/snapshot/options/{underlying}?limit=250&apiKey={api_key}"
response = requests.get(url)
data = response.json()

# Sort by volume
sorted_by_volume = sorted(data['results'], key=lambda x: x['day'].get('volume', 0), reverse=True)

print("Top 10 by Volume:")
for contract in sorted_by_volume[:10]:
    details = contract['details']
    day = contract['day']
    oi = contract.get('open_interest', 0)
    print(f"{details['ticker']}: Vol={day.get('volume', 0)}, OI={oi}, Price=${day.get('close', 0)}")
```

### Historical Options Pricing
```python
import os
import requests

api_key = os.environ['MASSIVE_API']
options_ticker = "O:AAPL240119C00150000"
from_date = "2023-12-01"
to_date = "2024-01-19"

url = f"https://api.massive.com/v2/aggs/ticker/{options_ticker}/range/1/day/{from_date}/{to_date}?apiKey={api_key}"
response = requests.get(url)
data = response.json()

for bar in data['results']:
    print(f"Date: {bar['t']}, Open: ${bar['o']}, High: ${bar['h']}, Low: ${bar['l']}, Close: ${bar['c']}, Volume: {bar['v']}")
```
