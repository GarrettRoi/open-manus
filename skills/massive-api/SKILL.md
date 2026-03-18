---
name: massive-api
description: Comprehensive access to Massive.com API (evolution of polygon.io) for financial market data. Use for retrieving stock prices, options chains, futures contracts, forex rates, cryptocurrency data, economic indicators, company fundamentals, corporate actions, analyst ratings, and real-time market data across all major U.S. exchanges and global markets.
---

# Massive API

Access comprehensive financial market data from Massive.com (evolution of polygon.io) covering stocks, options, futures, indices, forex, crypto, and economic indicators.

## Overview

Massive provides real-time and historical market data from all major U.S. exchanges and global markets through REST API, WebSocket streams, and flat files. The platform offers unparalleled coverage including:

- **19 U.S. stock exchanges** plus dark pools, FINRA facilities, and OTC markets
- **17 U.S. options exchanges** with complete OPRA feed
- **Futures markets** with comprehensive contract data
- **1,750+ forex currency pairs** from major institutions
- **Cryptocurrency markets** with tick-level precision
- **Economic indicators** from Federal Reserve and government sources
- **Third-party data** including Benzinga news, ETF analytics, and corporate events

## Authentication

Massive uses API key authentication. The API key is available in the `MASSIVE_API` environment variable.

### Authentication Methods

**Query String Parameter** (recommended for simple requests):
```python
import os
import requests

api_key = os.environ['MASSIVE_API']
url = f"https://api.massive.com/v3/reference/tickers?apiKey={api_key}"
response = requests.get(url)
```

**Authorization Header** (recommended for production):
```python
import os
import requests

api_key = os.environ['MASSIVE_API']
headers = {'Authorization': f'Bearer {api_key}'}
url = "https://api.massive.com/v3/reference/tickers"
response = requests.get(url, headers=headers)
```

## Base URL

All REST API endpoints use the base URL:
```
https://api.massive.com
```

## Common Response Structure

All REST endpoints return JSON with a consistent structure:

```json
{
  "status": "OK",
  "request_id": "unique-request-id",
  "count": 100,
  "resultsCount": 100,
  "results": [
    // Array of data objects
  ],
  "next_url": "https://api.massive.com/v3/endpoint?cursor=..." // Optional pagination
}
```

**Common Fields**:
- `status` (string): Request status ("OK", "ERROR", etc.)
- `request_id` (string): Unique identifier for the request
- `count` or `resultsCount` (integer): Number of results returned
- `results` (array): Array of data objects
- `next_url` (string, optional): URL for next page of results

## Timestamps and Timezones

**Important**: All timestamps are Unix timestamps (milliseconds since epoch) in **UTC**.

- **Stocks, Options, Indices**: Market hours are in Eastern Time (ET), but timestamps are UTC
- **Forex**: Operates 24/5, timestamps in UTC
- **Crypto**: Operates 24/7, timestamps in UTC
- **Futures**: Market hours in Central Time (CT), but timestamps are UTC

Convert timestamps to appropriate timezone for display:
```python
from datetime import datetime, timezone
import pytz

# Convert Unix millisecond timestamp to ET
timestamp_ms = 1577941200000
dt_utc = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
et = pytz.timezone('America/New_York')
dt_et = dt_utc.astimezone(et)
print(dt_et)  # 2020-01-02 09:30:00-05:00
```

## Date Formats

Endpoints accept dates in two formats:
- **YYYY-MM-DD**: e.g., `2024-01-15`
- **Unix millisecond timestamp**: e.g., `1705276800000`

## Pagination

Endpoints that return large datasets support pagination via `next_url`:

```python
import os
import requests

api_key = os.environ['MASSIVE_API']
url = f"https://api.massive.com/v3/reference/tickers?apiKey={api_key}&limit=1000"

all_results = []
while url:
    response = requests.get(url)
    data = response.json()
    all_results.extend(data.get('results', []))
    url = data.get('next_url')  # None if no more pages
```

## Error Handling

Handle errors by checking the `status` field and HTTP status codes:

```python
import os
import requests

api_key = os.environ['MASSIVE_API']
url = f"https://api.massive.com/v3/reference/tickers/AAPL?apiKey={api_key}"

response = requests.get(url)
if response.status_code == 200:
    data = response.json()
    if data.get('status') == 'OK':
        # Process results
        results = data.get('results', [])
    else:
        print(f"API Error: {data.get('message', 'Unknown error')}")
elif response.status_code == 401:
    print("Authentication failed - check API key")
elif response.status_code == 429:
    print("Rate limit exceeded - implement backoff")
else:
    print(f"HTTP Error: {response.status_code}")
```

## Common Parameters

Many endpoints share these parameters:

- `adjusted` (boolean, default: true): Adjust for stock splits
- `sort` (string, default: "asc"): Sort order ("asc" or "desc")
- `limit` (integer, default: 5000, max: 50000): Number of results per page
- `order` (string): Order by field
- `timestamp` or `timestamp.gte`/`timestamp.lte`: Filter by timestamp range

## When to Read Reference Files

Read the appropriate reference file based on the asset class or data type needed:

### Core Market Data

- **Stocks** (`/home/ubuntu/skills/massive-api/references/stocks.md`): U.S. stock market data including OHLC bars, tickers, snapshots, trades, quotes, technical indicators, market operations, corporate actions (dividends, splits, IPOs), fundamentals (balance sheets, income statements, cash flows, ratios, float, short interest), and SEC filings (10-K sections, risk factors). Use for any stock-related queries.

- **Options** (`/home/ubuntu/skills/massive-api/references/options.md`): U.S. options market data from all 17 exchanges including contracts, option chains, OHLC bars, snapshots, trades, quotes, technical indicators, and market operations. Use for options trading, Greeks calculations, or volatility analysis.

- **Futures** (`/home/ubuntu/skills/massive-api/references/futures.md`): Futures contracts data including aggregates, contract details, products, schedules, snapshots, trades, and quotes. Use for commodities, indices futures, or derivatives trading.

- **Indices** (`/home/ubuntu/skills/massive-api/references/indices.md`): Index values and data for S&P, Nasdaq, Dow Jones, and other indices including OHLC bars, snapshots, and technical indicators. Use for market benchmarking or index tracking.

- **Forex** (`/home/ubuntu/skills/massive-api/references/forex.md`): Foreign exchange data for 1,750+ currency pairs including OHLC bars, real-time quotes, currency conversion, snapshots, and technical indicators. Use for FX trading or currency analysis.

- **Crypto** (`/home/ubuntu/skills/massive-api/references/crypto.md`): Cryptocurrency market data including OHLC bars, trades, snapshots, technical indicators, and market operations. Use for crypto trading or blockchain analysis.

### Economic & Fundamental Data

- **Economy** (`/home/ubuntu/skills/massive-api/references/economy.md`): Macroeconomic indicators from Federal Reserve including inflation, inflation expectations, labor market data (unemployment, job openings, wages), and treasury yields. Use for economic analysis or macro trading strategies.

- **Partners** (`/home/ubuntu/skills/massive-api/references/partners.md`): Third-party data including Benzinga news and analyst ratings, ETF Global analytics and holdings, and Wall Street Horizon corporate events. Use for news sentiment, analyst consensus, ETF research, or event-driven strategies.

## Common Use Cases

### Get Historical Stock Prices
```python
import os
import requests

api_key = os.environ['MASSIVE_API']
ticker = "AAPL"
from_date = "2024-01-01"
to_date = "2024-12-31"

url = f"https://api.massive.com/v2/aggs/ticker/{ticker}/range/1/day/{from_date}/{to_date}"
params = {'apiKey': api_key, 'adjusted': 'true', 'sort': 'asc'}
response = requests.get(url, params=params)
data = response.json()

for bar in data['results']:
    print(f"{bar['t']}: O={bar['o']}, H={bar['h']}, L={bar['l']}, C={bar['c']}, V={bar['v']}")
```

### Get Real-Time Quote
```python
import os
import requests

api_key = os.environ['MASSIVE_API']
ticker = "AAPL"

url = f"https://api.massive.com/v3/quotes/{ticker}/last"
params = {'apiKey': api_key}
response = requests.get(url, params=params)
data = response.json()

quote = data['results']
print(f"Bid: ${quote['bid']} x {quote['bid_size']}")
print(f"Ask: ${quote['ask']} x {quote['ask_size']}")
```

### Get Company Fundamentals
```python
import os
import requests

api_key = os.environ['MASSIVE_API']
ticker = "AAPL"

url = f"https://api.massive.com/vX/reference/financials"
params = {'apiKey': api_key, 'ticker': ticker, 'limit': 10}
response = requests.get(url, params=params)
data = response.json()

# Process financial statements
for statement in data['results']:
    print(f"{statement['fiscal_period']}: Revenue={statement['financials']['revenue']}")
```

## Rate Limits

Rate limits vary by subscription plan. Implement exponential backoff for 429 errors:

```python
import time
import requests

def fetch_with_retry(url, max_retries=3):
    for attempt in range(max_retries):
        response = requests.get(url)
        if response.status_code == 429:
            wait_time = 2 ** attempt  # Exponential backoff
            time.sleep(wait_time)
            continue
        return response
    raise Exception("Max retries exceeded")
```

## Best Practices

1. **Use environment variable**: Always use `os.environ['MASSIVE_API']` for API key
2. **Handle pagination**: Use `next_url` for large datasets
3. **Cache responses**: Cache historical data to reduce API calls
4. **Batch requests**: Combine multiple tickers in snapshot endpoints
5. **Convert timestamps**: Always convert UTC timestamps to appropriate timezone
6. **Error handling**: Check both HTTP status and API `status` field
7. **Rate limiting**: Implement backoff for 429 errors

## Additional Resources

- **Official Documentation**: https://massive.com/docs
- **API Status**: Check for service disruptions
- **Client Libraries**: Python, Go, JavaScript, Kotlin available on GitHub
- **WebSocket API**: For real-time streaming (see references/websocket.md if needed)
- **Flat Files**: For bulk historical downloads (see references/flat-files.md if needed)
