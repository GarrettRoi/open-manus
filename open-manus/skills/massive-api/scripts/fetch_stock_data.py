#!/usr/bin/env python3
"""
Helper script for fetching stock data from Massive API.
Usage: python fetch_stock_data.py TICKER START_DATE END_DATE
"""

import os
import sys
import requests
from datetime import datetime

def fetch_stock_bars(ticker, from_date, to_date, timespan='day', multiplier=1):
    """Fetch OHLC bars for a stock ticker."""
    api_key = os.environ.get('MASSIVE_API')
    if not api_key:
        raise ValueError("MASSIVE_API environment variable not set")
    
    url = f"https://api.massive.com/v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{from_date}/{to_date}"
    params = {'apiKey': api_key, 'adjusted': 'true', 'sort': 'asc', 'limit': 50000}
    
    all_bars = []
    while url:
        response = requests.get(url, params=params if params else None)
        response.raise_for_status()
        data = response.json()
        
        if data.get('status') != 'OK':
            raise Exception(f"API Error: {data.get('message', 'Unknown error')}")
        
        all_bars.extend(data.get('results', []))
        url = data.get('next_url')
        params = None  # next_url already includes params
    
    return all_bars

if __name__ == '__main__':
    if len(sys.argv) != 4:
        print("Usage: python fetch_stock_data.py TICKER START_DATE END_DATE")
        print("Example: python fetch_stock_data.py AAPL 2024-01-01 2024-12-31")
        sys.exit(1)
    
    ticker = sys.argv[1]
    from_date = sys.argv[2]
    to_date = sys.argv[3]
    
    print(f"Fetching {ticker} data from {from_date} to {to_date}...")
    bars = fetch_stock_bars(ticker, from_date, to_date)
    
    print(f"\nFetched {len(bars)} bars")
    print(f"\nFirst bar: {bars[0]}")
    print(f"Last bar: {bars[-1]}")
    
    # Convert to CSV
    import csv
    output_file = f"{ticker}_{from_date}_{to_date}.csv"
    with open(output_file, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=['t', 'o', 'h', 'l', 'c', 'v', 'vw', 'n'])
        writer.writeheader()
        writer.writerows(bars)
    
    print(f"\nSaved to {output_file}")
