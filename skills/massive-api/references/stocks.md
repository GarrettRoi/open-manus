# Stocks API Reference

Comprehensive U.S. stock market data from all 19 major exchanges, dark pools, FINRA facilities, and OTC markets.

## Market Hours

- **Pre-Market**: 4:00 AM - 9:30 AM ET
- **Regular Hours**: 9:30 AM - 4:00 PM ET
- **After-Hours**: 4:00 PM - 8:00 PM ET

All timestamps are Unix milliseconds in UTC. Convert to ET for display.

## Endpoint Categories

### 1. Aggregate Bars (OHLC)

#### Custom Bars
**Endpoint**: `GET /v2/aggs/ticker/{stocksTicker}/range/{multiplier}/{timespan}/{from}/{to}`

Retrieve OHLC and volume data over custom date range and time interval.

**Path Parameters**:
- `stocksTicker` (string): Ticker symbol (e.g., "AAPL")
- `multiplier` (integer): Timespan multiplier (e.g., 5 for 5-minute bars)
- `timespan` (string): Time window ("minute", "hour", "day", "week", "month", "quarter", "year")
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
url = f"https://api.massive.com/v2/aggs/ticker/AAPL/range/1/day/2024-01-01/2024-12-31?apiKey={api_key}"
response = requests.get(url)
data = response.json()
```

#### Daily Market Summary
**Endpoint**: `GET /v2/aggs/grouped/locale/us/market/stocks/{date}`

Retrieve daily OHLC for all U.S. stocks on a specific date.

#### Daily Ticker Summary
**Endpoint**: `GET /v2/aggs/ticker/{stocksTicker}/prev`

Retrieve previous trading day's OHLC for a ticker.

#### Previous Day Bar
**Endpoint**: `GET /v1/open-close/{stocksTicker}/{date}`

Retrieve opening and closing trades for a specific date.

### 2. Tickers

#### All Tickers
**Endpoint**: `GET /v3/reference/tickers`

Retrieve comprehensive list of all supported tickers.

**Query Parameters**:
- `ticker` (string): Filter by ticker symbol
- `type` (string): Filter by type ("CS", "ADRC", "ETF", etc.)
- `market` (string): Filter by market ("stocks", "crypto", "fx", "otc")
- `exchange` (string): Filter by exchange
- `active` (boolean): Filter by active status
- `limit` (integer, default: 100, max: 1000): Results per page

**Example**:
```python
import os
import requests

api_key = os.environ['MASSIVE_API']
url = f"https://api.massive.com/v3/reference/tickers?market=stocks&active=true&limit=1000&apiKey={api_key}"
response = requests.get(url)
data = response.json()

for ticker in data['results']:
    print(f"{ticker['ticker']}: {ticker['name']}")
```

#### Ticker Overview
**Endpoint**: `GET /v3/reference/tickers/{ticker}`

Retrieve detailed information for a single ticker.

**Response includes**: name, market, locale, primary_exchange, type, active, currency_name, cik, composite_figi, share_class_figi, market_cap, phone_number, address, description, sic_code, sic_description, ticker_root, homepage_url, total_employees, list_date, branding, share_class_shares_outstanding, weighted_shares_outstanding, round_lot

#### Related Tickers
**Endpoint**: `GET /v1/related-companies/{ticker}`

Retrieve tickers related to a specified ticker based on news coverage and returns data.

#### Ticker Types
**Endpoint**: `GET /v3/reference/tickers/types`

Retrieve list of all ticker types supported.

### 3. Snapshots

#### Full Market Snapshot
**Endpoint**: `GET /v2/snapshot/locale/us/markets/stocks/tickers`

Retrieve comprehensive snapshot of entire U.S. stock market.

**Query Parameters**:
- `tickers` (string): Comma-separated list of tickers
- `include_otc` (boolean): Include OTC securities

**Response includes**: ticker, day (open, high, low, close, volume, vw, previous_close), lastTrade, lastQuote, min (minute bar), prevDay

#### Single Ticker Snapshot
**Endpoint**: `GET /v2/snapshot/locale/us/markets/stocks/tickers/{stocksTicker}`

Retrieve most recent market data snapshot for a single ticker.

**Example**:
```python
import os
import requests

api_key = os.environ['MASSIVE_API']
ticker = "AAPL"
url = f"https://api.massive.com/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}?apiKey={api_key}"
response = requests.get(url)
data = response.json()

snapshot = data['ticker']
print(f"Last Trade: ${snapshot['lastTrade']['p']} at {snapshot['lastTrade']['t']}")
print(f"Day High: ${snapshot['day']['h']}, Low: ${snapshot['day']['l']}")
print(f"Volume: {snapshot['day']['v']}")
```

#### Top Market Movers
**Endpoint**: `GET /v2/snapshot/locale/us/markets/stocks/{direction}`

Retrieve top 20 gainers or losers.

**Path Parameters**:
- `direction` (string): "gainers" or "losers"

#### Unified Snapshot
**Endpoint**: `GET /v3/snapshot`

Retrieve unified snapshots for multiple asset classes in a single request.

**Query Parameters**:
- `ticker.any_of` (string): Comma-separated tickers across asset classes

### 4. Trades & Quotes

#### Last Trade
**Endpoint**: `GET /v3/trades/{stocksTicker}/last`

Retrieve latest trade for a ticker.

**Response includes**: conditions, exchange, id, participant_timestamp, price, sequence_number, sip_timestamp, size, trf_id, trf_timestamp

#### Trades
**Endpoint**: `GET /v3/trades/{stocksTicker}`

Retrieve tick-level trade data over a time range.

**Query Parameters**:
- `timestamp` (string): Specific timestamp
- `timestamp.gte` (string): Greater than or equal to timestamp
- `timestamp.lte` (string): Less than or equal to timestamp
- `order` (string, default: "asc"): Sort order
- `limit` (integer, default: 5000, max: 50000): Results per page

**Example**:
```python
import os
import requests

api_key = os.environ['MASSIVE_API']
ticker = "AAPL"
url = f"https://api.massive.com/v3/trades/{ticker}?timestamp.gte=2024-01-01&timestamp.lte=2024-01-02&limit=10000&apiKey={api_key}"
response = requests.get(url)
data = response.json()

for trade in data['results']:
    print(f"Time: {trade['sip_timestamp']}, Price: ${trade['price']}, Size: {trade['size']}")
```

#### Last Quote
**Endpoint**: `GET /v3/quotes/{stocksTicker}/last`

Retrieve most recent NBBO quote.

**Response includes**: ask, ask_exchange, ask_size, bid, bid_exchange, bid_size, conditions, indicators, participant_timestamp, sequence_number, sip_timestamp, tape, trf_timestamp

#### Quotes
**Endpoint**: `GET /v3/quotes/{stocksTicker}`

Retrieve NBBO quotes over a time range.

**Query Parameters**: Same as Trades endpoint

### 5. Technical Indicators

All technical indicators follow the same pattern:

**Endpoints**:
- `GET /v1/indicators/sma/{stocksTicker}` - Simple Moving Average
- `GET /v1/indicators/ema/{stocksTicker}` - Exponential Moving Average
- `GET /v1/indicators/macd/{stocksTicker}` - MACD
- `GET /v1/indicators/rsi/{stocksTicker}` - Relative Strength Index

**Common Query Parameters**:
- `timestamp` (string): Specific timestamp
- `timestamp.gte` (string): Start of range
- `timestamp.lte` (string): End of range
- `timespan` (string): "minute", "hour", "day", "week", "month", "quarter", "year"
- `adjusted` (boolean, default: true): Adjust for splits
- `window` (integer): Indicator window size (e.g., 50 for 50-day SMA)
- `series_type` (string, default: "close"): Price type ("open", "high", "low", "close")
- `order` (string, default: "desc"): Sort order
- `limit` (integer, default: 5000, max: 50000): Results per page

**Example**:
```python
import os
import requests

api_key = os.environ['MASSIVE_API']
ticker = "AAPL"
url = f"https://api.massive.com/v1/indicators/sma/{ticker}?timespan=day&window=50&series_type=close&order=desc&limit=120&apiKey={api_key}"
response = requests.get(url)
data = response.json()

for point in data['results']['values']:
    print(f"Timestamp: {point['timestamp']}, SMA: {point['value']}")
```

### 6. Market Operations

#### Condition Codes
**Endpoint**: `GET /v3/reference/conditions`

Retrieve unified list of trade and quote conditions.

**Query Parameters**:
- `asset_class` (string): Filter by asset class
- `data_type` (string): "trade" or "quote"

#### Exchanges
**Endpoint**: `GET /v3/reference/exchanges`

Retrieve list of exchanges with identifiers and attributes.

**Query Parameters**:
- `asset_class` (string): Filter by asset class
- `locale` (string): Filter by locale

#### Market Holidays
**Endpoint**: `GET /v1/marketstatus/upcoming`

Retrieve upcoming market holidays and open/close times.

#### Market Status
**Endpoint**: `GET /v1/marketstatus/now`

Retrieve current trading status for exchanges and markets.

**Response includes**: market (stocks, options, forex, crypto), serverTime, exchanges (name, market, status), currencies (status)

**Example**:
```python
import os
import requests

api_key = os.environ['MASSIVE_API']
url = f"https://api.massive.com/v1/marketstatus/now?apiKey={api_key}"
response = requests.get(url)
data = response.json()

print(f"Market Status: {data['market']}")
print(f"Server Time: {data['serverTime']}")
for exchange in data['exchanges']:
    print(f"{exchange['name']}: {exchange['status']}")
```

### 7. Corporate Actions

#### Dividends
**Endpoint**: `GET /v3/reference/dividends`

Retrieve historical dividend distributions.

**Query Parameters**:
- `ticker` (string): Filter by ticker
- `ex_dividend_date` (string): Filter by ex-dividend date
- `declaration_date` (string): Filter by declaration date
- `record_date` (string): Filter by record date
- `pay_date` (string): Filter by pay date
- `frequency` (integer): Filter by frequency (1=annual, 2=semi-annual, 4=quarterly, 12=monthly)
- `cash_amount` (number): Filter by cash amount
- `dividend_type` (string): Filter by type
- `order` (string, default: "asc"): Sort order
- `limit` (integer, default: 1000, max: 1000): Results per page

**Response includes**: cash_amount, currency, declaration_date, dividend_type, ex_dividend_date, frequency, id, pay_date, record_date, ticker

**Example**:
```python
import os
import requests

api_key = os.environ['MASSIVE_API']
ticker = "AAPL"
url = f"https://api.massive.com/v3/reference/dividends?ticker={ticker}&order=desc&limit=10&apiKey={api_key}"
response = requests.get(url)
data = response.json()

for div in data['results']:
    print(f"Ex-Date: {div['ex_dividend_date']}, Amount: ${div['cash_amount']}, Pay Date: {div['pay_date']}")
```

#### Splits
**Endpoint**: `GET /v3/reference/splits`

Retrieve historical stock split events.

**Query Parameters**:
- `ticker` (string): Filter by ticker
- `execution_date` (string): Filter by execution date
- `reverse_split` (boolean): Filter by reverse split
- `order` (string, default: "asc"): Sort order
- `limit` (integer, default: 1000, max: 1000): Results per page

**Response includes**: execution_date, id, split_from, split_to, ticker

#### IPOs
**Endpoint**: `GET /v3/reference/ipos`

Retrieve IPO information (upcoming and historical from 2008).

**Query Parameters**:
- `ticker` (string): Filter by ticker
- `us_code` (string): Filter by US code
- `isin` (string): Filter by ISIN
- `listing_date` (string): Filter by listing date
- `type` (string): Filter by type
- `order` (string, default: "asc"): Sort order
- `limit` (integer, default: 1000, max: 1000): Results per page

#### Ticker Events
**Endpoint**: `GET /vX/reference/tickers/{id}/events`

Retrieve timeline of key events for a ticker, CUSIP, or Composite FIGI.

**Path Parameters**:
- `id` (string): Ticker symbol, CUSIP, or Composite FIGI

**Query Parameters**:
- `types` (string): Comma-separated event types

### 8. Fundamentals

#### Balance Sheets
**Endpoint**: `GET /vX/reference/financials`

Retrieve balance sheet data.

**Query Parameters**:
- `ticker` (string): Ticker symbol
- `cik` (string): CIK number
- `company_name` (string): Company name
- `sic` (string): SIC code
- `filing_date` (string): Filing date
- `filing_date.gte` (string): Filing date greater than or equal
- `filing_date.lte` (string): Filing date less than or equal
- `period_of_report_date` (string): Period of report date
- `period_of_report_date.gte` (string): Period greater than or equal
- `period_of_report_date.lte` (string): Period less than or equal
- `timeframe` (string): "annual", "quarterly", or "ttm"
- `include_sources` (boolean): Include source URLs
- `order` (string, default: "asc"): Sort order
- `limit` (integer, default: 10, max: 100): Results per page

**Response includes**: cik, company_name, end_date, filing_date, fiscal_period, fiscal_year, source_filing_url, source_filing_file_url, start_date, timeframe, financials (assets, liabilities, equity, etc.)

**Example**:
```python
import os
import requests

api_key = os.environ['MASSIVE_API']
ticker = "AAPL"
url = f"https://api.massive.com/vX/reference/financials?ticker={ticker}&timeframe=annual&limit=5&apiKey={api_key}"
response = requests.get(url)
data = response.json()

for statement in data['results']:
    print(f"Fiscal Year: {statement['fiscal_year']}, Period: {statement['fiscal_period']}")
    print(f"Total Assets: ${statement['financials']['balance_sheet']['assets']['value']}")
    print(f"Total Liabilities: ${statement['financials']['balance_sheet']['liabilities']['value']}")
```

#### Income Statements
**Endpoint**: `GET /vX/reference/financials`

Same endpoint as Balance Sheets, but access `financials.income_statement` in response.

**Key fields**: revenues, cost_of_revenue, gross_profit, operating_expenses, operating_income, net_income, basic_earnings_per_share, diluted_earnings_per_share

#### Cash Flow Statements
**Endpoint**: `GET /vX/reference/financials`

Same endpoint, access `financials.cash_flow_statement` in response.

**Key fields**: net_cash_flow_from_operating_activities, net_cash_flow_from_investing_activities, net_cash_flow_from_financing_activities, net_cash_flow

#### Ratios
**Endpoint**: `GET /vX/reference/financials`

Same endpoint, access `financials.comprehensive_income` and calculated ratios in response.

#### Float
**Endpoint**: `GET /v3/reference/tickers/{ticker}`

Retrieve free float from ticker details endpoint.

**Response field**: `share_class_shares_outstanding`

#### Short Interest
**Endpoint**: `GET /v3/reference/short-interest`

Retrieve bi-monthly aggregated short interest reported to FINRA.

**Query Parameters**:
- `ticker` (string): Ticker symbol
- `settlement_date` (string): Settlement date
- `settlement_date.gte` (string): Settlement date greater than or equal
- `settlement_date.lte` (string): Settlement date less than or equal
- `order` (string, default: "asc"): Sort order
- `limit` (integer, default: 10, max: 1000): Results per page

**Response includes**: market, settlement_date, short_interest, ticker

#### Short Volume
**Endpoint**: `GET /v3/reference/short-volume`

Retrieve daily aggregated short sale volume from off-exchange venues.

**Query Parameters**:
- `ticker` (string): Ticker symbol
- `date` (string): Trade date
- `date.gte` (string): Date greater than or equal
- `date.lte` (string): Date less than or equal
- `order` (string, default: "asc"): Sort order
- `limit` (integer, default: 10, max: 1000): Results per page

**Response includes**: date, market, short_exempt_volume, short_volume, ticker, total_volume

### 9. SEC Filings & Disclosures

#### 10-K Sections
**Endpoint**: `GET /v1/reference/sec/filings/{filing_id}/sections`

Retrieve plain-text content of specific sections from SEC filings.

**Path Parameters**:
- `filing_id` (string): Filing ID

**Query Parameters**:
- `section` (string): Section name (e.g., "1", "1A", "7", "7A")

#### Risk Factors
**Endpoint**: `GET /v1/reference/sec/filings/{filing_id}/risk-factors`

Retrieve standardized, machine-readable risk factor disclosures.

**Path Parameters**:
- `filing_id` (string): Filing ID

#### Risk Categories
**Endpoint**: `GET /v1/reference/sec/risk-categories`

Retrieve full taxonomy used to classify risk factors.

### 10. News

#### Benzinga Real-time News
**Endpoint**: `GET /v2/reference/news`

Retrieve real-time news articles from Benzinga.

**Query Parameters**:
- `ticker` (string): Filter by ticker
- `published_utc` (string): Filter by publish date
- `published_utc.gte` (string): Published after
- `published_utc.lte` (string): Published before
- `order` (string, default: "desc"): Sort order
- `limit` (integer, default: 10, max: 1000): Results per page

**Response includes**: id, publisher, title, author, published_utc, article_url, tickers, amp_url, image_url, description, keywords

**Example**:
```python
import os
import requests

api_key = os.environ['MASSIVE_API']
ticker = "AAPL"
url = f"https://api.massive.com/v2/reference/news?ticker={ticker}&order=desc&limit=10&apiKey={api_key}"
response = requests.get(url)
data = response.json()

for article in data['results']:
    print(f"{article['published_utc']}: {article['title']}")
    print(f"URL: {article['article_url']}")
    print(f"Tickers: {', '.join(article['tickers'])}")
```

## Common Patterns

### Fetching Historical Prices with Pagination
```python
import os
import requests

api_key = os.environ['MASSIVE_API']
ticker = "AAPL"
from_date = "2020-01-01"
to_date = "2024-12-31"

url = f"https://api.massive.com/v2/aggs/ticker/{ticker}/range/1/day/{from_date}/{to_date}?adjusted=true&sort=asc&limit=50000&apiKey={api_key}"

all_bars = []
while url:
    response = requests.get(url)
    data = response.json()
    all_bars.extend(data.get('results', []))
    url = data.get('next_url')

print(f"Fetched {len(all_bars)} bars")
```

### Batch Snapshot Requests
```python
import os
import requests

api_key = os.environ['MASSIVE_API']
tickers = ["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA"]

url = f"https://api.massive.com/v2/snapshot/locale/us/markets/stocks/tickers?tickers={','.join(tickers)}&apiKey={api_key}"
response = requests.get(url)
data = response.json()

for ticker_data in data['tickers']:
    print(f"{ticker_data['ticker']}: ${ticker_data['day']['c']} ({ticker_data['todaysChangePerc']:.2f}%)")
```

### Combining Multiple Data Sources
```python
import os
import requests

api_key = os.environ['MASSIVE_API']
ticker = "AAPL"

# Get ticker details
ticker_url = f"https://api.massive.com/v3/reference/tickers/{ticker}?apiKey={api_key}"
ticker_data = requests.get(ticker_url).json()['results']

# Get latest quote
quote_url = f"https://api.massive.com/v3/quotes/{ticker}/last?apiKey={api_key}"
quote_data = requests.get(quote_url).json()['results']

# Get dividends
div_url = f"https://api.massive.com/v3/reference/dividends?ticker={ticker}&order=desc&limit=4&apiKey={api_key}"
div_data = requests.get(div_url).json()['results']

print(f"Company: {ticker_data['name']}")
print(f"Market Cap: ${ticker_data['market_cap']:,.0f}")
print(f"Current Bid/Ask: ${quote_data['bid']} / ${quote_data['ask']}")
print(f"Recent Dividends: {[d['cash_amount'] for d in div_data]}")
```
