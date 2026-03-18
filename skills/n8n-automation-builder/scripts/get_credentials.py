import os
import requests
import json

def get_credentials():
    api_url = os.environ.get("N8N_INSTANCE_URL")
    api_key = os.environ.get("N8N_API_KEY")
    
    if not api_url.startswith("http"):
        api_url = f"https://{api_url}"
    
    headers = {
        "X-N8N-API-KEY": api_key,
        "Accept": "application/json"
    }
    
    try:
        response = requests.get(f"{api_url}/api/v1/credentials", headers=headers)
        if response.status_code == 200:
            creds = response.json().get("data", [])
            print(json.dumps(creds, indent=2))
        else:
            print(f"Error: {response.status_code}")
            print(response.text)
    except Exception as e:
        print(f"Error: {str(e)}")

if __name__ == "__main__":
    get_credentials()
