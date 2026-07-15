# Partners API Reference

Third-party data including Benzinga news, ETF analytics, and corporate events.

## Benzinga

### Analyst Details
**Endpoint**: `GET /v1/reference/analysts`

Retrieve structured data on financial analysts.

**Query Parameters**:
- `analyst_id` (string): Filter by analyst ID
- `firm_id` (string): Filter by firm ID
- `order` (string, default: "asc"): Sort order
- `limit` (integer, default: 100, max: 1000): Results per page

**Response includes**: analyst_id, analyst_name, firm_id, firm_name

### Analyst Insights
**Endpoint**: `GET /v1/reference/analyst-insights`

Retrieve analyst insights including ratings and price targets.

**Query Parameters**:
- `ticker` (string): Filter by ticker
- `analyst_id` (string): Filter by analyst ID
- `published_utc.gte` (string): Published after
- `published_utc.lte` (string): Published before
- `order` (string, default: "desc"): Sort order
- `limit` (integer, default: 100, max: 1000): Results per page

**Response includes**: analyst_id, analyst_name, firm_name, insight_date, price_target, rating, rationale, ticker

### Analyst Ratings
**Endpoint**: `GET /v1/reference/analyst-ratings`

Retrieve historical analyst ratings.

**Query Parameters**:
- `ticker` (string): Filter by ticker
- `rating_date.gte` (string): Rating date after
- `rating_date.lte` (string): Rating date before
- `order` (string, default: "desc"): Sort order
- `limit` (integer, default: 100, max: 1000): Results per page

**Response includes**: action, analyst_name, firm_name, price_target, price_target_previous, rating, rating_previous, ticker, updated

**Example**:
```python
import os
import requests

api_key = os.environ['MASSIVE_API']
ticker = "AAPL"
url = f"https://api.massive.com/v1/reference/analyst-ratings?ticker={ticker}&order=desc&limit=20&apiKey={api_key}"
response = requests.get(url)
data = response.json()

for rating in data['results']:
    print(f"{rating['updated']}: {rating['firm_name']} - {rating['rating']} (PT: ${rating['price_target']})")
```

### Bulls Bears Say
**Endpoint**: `GET /v1/reference/bulls-bears-say`

Retrieve analyst bull and bear case summaries.

**Query Parameters**:
- `ticker` (string): Filter by ticker
- `order` (string, default: "desc"): Sort order
- `limit` (integer, default: 100, max: 1000): Results per page

**Response includes**: bear_case, bull_case, published_utc, ticker

### Consensus Ratings
**Endpoint**: `GET /v1/reference/consensus-ratings`

Retrieve consensus ratings from analysts.

**Query Parameters**:
- `ticker` (string): Filter by ticker
- `order` (string, default: "desc"): Sort order
- `limit` (integer, default: 100, max: 1000): Results per page

**Response includes**: buy_count, consensus_rating, hold_count, price_target_average, price_target_high, price_target_low, sell_count, ticker

**Example**:
```python
import os
import requests

api_key = os.environ['MASSIVE_API']
ticker = "AAPL"
url = f"https://api.massive.com/v1/reference/consensus-ratings?ticker={ticker}&limit=1&apiKey={api_key}"
response = requests.get(url)
consensus = response.json()['results'][0]

print(f"Consensus Rating: {consensus['consensus_rating']}")
print(f"Buy: {consensus['buy_count']}, Hold: {consensus['hold_count']}, Sell: {consensus['sell_count']}")
print(f"Price Target: ${consensus['price_target_average']} (${consensus['price_target_low']} - ${consensus['price_target_high']})")
```

### Corporate Guidance
**Endpoint**: `GET /v1/reference/corporate-guidance`

Retrieve earnings guidance data.

**Query Parameters**:
- `ticker` (string): Filter by ticker
- `guidance_date.gte` (string): Guidance date after
- `guidance_date.lte` (string): Guidance date before
- `order` (string, default: "desc"): Sort order
- `limit` (integer, default: 100, max: 1000): Results per page

**Response includes**: eps_guidance_high, eps_guidance_low, fiscal_period, fiscal_year, guidance_date, revenue_guidance_high, revenue_guidance_low, ticker

### Earnings
**Endpoint**: `GET /v1/reference/earnings`

Retrieve historical and upcoming earnings announcements.

**Query Parameters**:
- `ticker` (string): Filter by ticker
- `report_date.gte` (string): Report date after
- `report_date.lte` (string): Report date before
- `order` (string, default: "desc"): Sort order
- `limit` (integer, default: 100, max: 1000): Results per page

**Response includes**: actual_eps, consensus_eps, fiscal_period, fiscal_year, report_date, surprise_eps, surprise_percent, ticker

**Example**:
```python
import os
import requests

api_key = os.environ['MASSIVE_API']
ticker = "AAPL"
url = f"https://api.massive.com/v1/reference/earnings?ticker={ticker}&order=desc&limit=8&apiKey={api_key}"
response = requests.get(url)
data = response.json()

for earnings in data['results']:
    print(f"{earnings['report_date']} {earnings['fiscal_period']}: EPS ${earnings['actual_eps']} vs ${earnings['consensus_eps']} (Surprise: {earnings['surprise_percent']:.1f}%)")
```

### Firm Details
**Endpoint**: `GET /v1/reference/analyst-firms`

Retrieve data on analyst firms.

**Query Parameters**:
- `firm_id` (string): Filter by firm ID
- `order` (string, default: "asc"): Sort order
- `limit` (integer, default: 100, max: 1000): Results per page

**Response includes**: firm_id, firm_name

### Real-time Benzinga News
**Endpoint**: `GET /v2/reference/news`

(See stocks.md for details - same endpoint)

## ETF Global

### ETF Analytics
**Endpoint**: `GET /v1/reference/etf/analytics`

Retrieve analytical metrics for ETFs.

**Query Parameters**:
- `ticker` (string): Filter by ticker
- `date` (string): Filter by date
- `order` (string, default: "desc"): Sort order
- `limit` (integer, default: 100, max: 1000): Results per page

**Response includes**: aum, expense_ratio, performance_1y, performance_ytd, ticker, volatility

**Example**:
```python
import os
import requests

api_key = os.environ['MASSIVE_API']
ticker = "SPY"
url = f"https://api.massive.com/v1/reference/etf/analytics?ticker={ticker}&limit=1&apiKey={api_key}"
response = requests.get(url)
analytics = response.json()['results'][0]

print(f"AUM: ${analytics['aum']:,.0f}")
print(f"Expense Ratio: {analytics['expense_ratio']:.2f}%")
print(f"1Y Performance: {analytics['performance_1y']:.2f}%")
```

### ETF Constituents
**Endpoint**: `GET /v1/reference/etf/constituents`

Access underlying holdings of ETFs.

**Query Parameters**:
- `ticker` (string): Filter by ETF ticker
- `date` (string): Filter by date
- `order` (string, default: "asc"): Sort order
- `limit` (integer, default: 100, max: 1000): Results per page

**Response includes**: constituent_ticker, etf_ticker, weight

**Example**:
```python
import os
import requests

api_key = os.environ['MASSIVE_API']
etf_ticker = "SPY"
url = f"https://api.massive.com/v1/reference/etf/constituents?ticker={etf_ticker}&limit=50&apiKey={api_key}"
response = requests.get(url)
data = response.json()

print(f"Top holdings of {etf_ticker}:")
for holding in data['results'][:10]:
    print(f"  {holding['constituent_ticker']}: {holding['weight']:.2f}%")
```

### ETF Fund Flows
**Endpoint**: `GET /v1/reference/etf/fundflows`

Track capital movements across ETFs.

**Query Parameters**:
- `ticker` (string): Filter by ticker
- `date.gte` (string): Date after
- `date.lte` (string): Date before
- `order` (string, default: "desc"): Sort order
- `limit` (integer, default: 100, max: 1000): Results per page

**Response includes**: date, flow, ticker

### ETF Profiles & Exposure
**Endpoint**: `GET /v1/reference/etf/profiles`

Retrieve industry classification and exposure data.

**Query Parameters**:
- `ticker` (string): Filter by ticker
- `order` (string, default: "asc"): Sort order
- `limit` (integer, default: 100, max: 1000): Results per page

**Response includes**: asset_class, category, focus, geographic_focus, inception_date, issuer, ticker

### ETF Taxonomies
**Endpoint**: `GET /v1/reference/etf/taxonomies`

Access standardized taxonomy systems.

**Query Parameters**:
- `taxonomy_type` (string): Filter by taxonomy type
- `order` (string, default: "asc"): Sort order
- `limit` (integer, default: 100, max: 1000): Results per page

**Response includes**: category, taxonomy_id, taxonomy_name, taxonomy_type

## TMX (Wall Street Horizon)

### Corporate Events
**Endpoint**: `GET /v1/reference/corporate-events`

Retrieve corporate event data.

**Query Parameters**:
- `ticker` (string): Filter by ticker
- `event_type` (string): Filter by event type ("earnings", "dividend", "split", "conference", etc.)
- `event_date.gte` (string): Event date after
- `event_date.lte` (string): Event date before
- `order` (string, default: "desc"): Sort order
- `limit` (integer, default: 100, max: 1000): Results per page

**Response includes**: event_date, event_type, ticker, description

**Example**:
```python
import os
import requests
from datetime import datetime, timedelta

api_key = os.environ['MASSIVE_API']
ticker = "AAPL"
today = datetime.now().strftime('%Y-%m-%d')
future = (datetime.now() + timedelta(days=90)).strftime('%Y-%m-%d')

url = f"https://api.massive.com/v1/reference/corporate-events?ticker={ticker}&event_date.gte={today}&event_date.lte={future}&order=asc&limit=50&apiKey={api_key}"
response = requests.get(url)
data = response.json()

print(f"Upcoming events for {ticker}:")
for event in data['results']:
    print(f"{event['event_date']}: {event['event_type']} - {event['description']}")
```

## Common Patterns

### Comprehensive Company Research
```python
import os
import requests

api_key = os.environ['MASSIVE_API']
ticker = "AAPL"

# Get analyst consensus
consensus_url = f"https://api.massive.com/v1/reference/consensus-ratings?ticker={ticker}&limit=1&apiKey={api_key}"
consensus = requests.get(consensus_url).json()['results'][0]

# Get recent earnings
earnings_url = f"https://api.massive.com/v1/reference/earnings?ticker={ticker}&order=desc&limit=4&apiKey={api_key}"
earnings = requests.get(earnings_url).json()['results']

# Get upcoming events
events_url = f"https://api.massive.com/v1/reference/corporate-events?ticker={ticker}&event_date.gte=2024-01-01&limit=10&apiKey={api_key}"
events = requests.get(events_url).json()['results']

# Get recent news
news_url = f"https://api.massive.com/v2/reference/news?ticker={ticker}&order=desc&limit=5&apiKey={api_key}"
news = requests.get(news_url).json()['results']

print(f"Research Report: {ticker}")
print(f"\nAnalyst Consensus: {consensus['consensus_rating']}")
print(f"Price Target: ${consensus['price_target_average']}")
print(f"\nRecent Earnings Surprises: {[e['surprise_percent'] for e in earnings]}")
print(f"\nUpcoming Events: {len(events)}")
print(f"\nRecent News: {[n['title'] for n in news]}")
```
