# Economy API Reference

Macroeconomic indicators from Federal Reserve and government sources.

## Endpoint Categories

### 1. Inflation

**Endpoint**: `GET /v1/indicators/economy/inflation`

Retrieve key indicators of realized inflation.

**Query Parameters**:
- `timeframe` (string): "monthly", "quarterly", "annual"
- `order` (string, default: "desc"): Sort order
- `limit` (integer, default: 100, max: 5000): Results per page

**Response includes**: cpi (Consumer Price Index), pce (Personal Consumption Expenditures), core_cpi, core_pce, date

**Example**:
```python
import os
import requests

api_key = os.environ['MASSIVE_API']
url = f"https://api.massive.com/v1/indicators/economy/inflation?timeframe=monthly&order=desc&limit=24&apiKey={api_key}"
response = requests.get(url)
data = response.json()

for point in data['results']:
    print(f"{point['date']}: CPI={point['cpi']:.2f}%, Core CPI={point['core_cpi']:.2f}%")
```

### 2. Inflation Expectations

**Endpoint**: `GET /v1/indicators/economy/inflation-expectations`

Retrieve how inflation is expected to evolve.

**Query Parameters**: Same as Inflation

**Response includes**: breakeven_5y, breakeven_10y, survey_1y, survey_5y, date

### 3. Labor Market

**Endpoint**: `GET /v1/indicators/economy/labor`

Retrieve key labor market indicators.

**Query Parameters**: Same as Inflation

**Response includes**: unemployment_rate, labor_force_participation_rate, average_hourly_earnings, job_openings, date

**Example**:
```python
import os
import requests

api_key = os.environ['MASSIVE_API']
url = f"https://api.massive.com/v1/indicators/economy/labor?timeframe=monthly&order=desc&limit=12&apiKey={api_key}"
response = requests.get(url)
data = response.json()

for point in data['results']:
    print(f"{point['date']}: Unemployment={point['unemployment_rate']:.1f}%, Job Openings={point['job_openings']:,.0f}")
```

### 4. Treasury Yields

**Endpoint**: `GET /v1/indicators/economy/treasury-yields`

Retrieve historical U.S. Treasury yields.

**Query Parameters**:
- `date` (string): Specific date
- `date.gte` (string): Date greater than or equal
- `date.lte` (string): Date less than or equal
- `order` (string, default: "desc"): Sort order
- `limit` (integer, default: 100, max: 5000): Results per page

**Response includes**: date, yield_1m, yield_3m, yield_6m, yield_1y, yield_2y, yield_3y, yield_5y, yield_7y, yield_10y, yield_20y, yield_30y

**Example**:
```python
import os
import requests

api_key = os.environ['MASSIVE_API']
url = f"https://api.massive.com/v1/indicators/economy/treasury-yields?order=desc&limit=10&apiKey={api_key}"
response = requests.get(url)
data = response.json()

for point in data['results']:
    print(f"{point['date']}: 2Y={point['yield_2y']:.2f}%, 10Y={point['yield_10y']:.2f}%, 30Y={point['yield_30y']:.2f}%")
```

## Common Patterns

### Build Economic Dashboard
```python
import os
import requests

api_key = os.environ['MASSIVE_API']

# Get latest inflation
inflation_url = f"https://api.massive.com/v1/indicators/economy/inflation?timeframe=monthly&order=desc&limit=1&apiKey={api_key}"
inflation = requests.get(inflation_url).json()['results'][0]

# Get latest labor data
labor_url = f"https://api.massive.com/v1/indicators/economy/labor?timeframe=monthly&order=desc&limit=1&apiKey={api_key}"
labor = requests.get(labor_url).json()['results'][0]

# Get latest treasury yields
yields_url = f"https://api.massive.com/v1/indicators/economy/treasury-yields?order=desc&limit=1&apiKey={api_key}"
yields = requests.get(yields_url).json()['results'][0]

print("Economic Dashboard")
print(f"CPI: {inflation['cpi']:.2f}%")
print(f"Unemployment: {labor['unemployment_rate']:.1f}%")
print(f"10Y Treasury: {yields['yield_10y']:.2f}%")
```
