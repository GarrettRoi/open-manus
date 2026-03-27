#!/usr/bin/env python3
"""
Webhook Communication — Send cross-channel messages between agents and Harmony.
Automatically logs all communication to the #harmony-communication channel.
"""
import os
import json
import requests
import argparse
from typing import List, Optional

# Configuration from environment
# These should be Discord Webhook URLs for each agent's channel
# Format: AGENT_WEBHOOK_URL_HARMONY, AGENT_WEBHOOK_URL_RAVEN, etc.
HARMONY_LOG_WEBHOOK_URL = os.getenv("HARMONY_LOG_WEBHOOK_URL")

def send_webhook(target_agent: str, message: str, sender: str):
    """Send a webhook message to an agent's channel and log it."""
    # 1. Resolve target webhook URL
    webhook_env_name = f"AGENT_WEBHOOK_URL_{target_agent.upper()}"
    target_webhook_url = os.getenv(webhook_env_name)
    
    if not target_webhook_url:
        print(f"Error: Webhook URL for {target_agent} not found in environment ({webhook_env_name}).")
        return

    # 2. Format the payload
    # Always @ the agent if Harmony is sending
    if sender.lower() == "harmony":
        payload_content = f"**[ORCHESTRATOR]** @{target_agent}: {message}"
    else:
        payload_content = f"**[{sender.upper()}]** {message}"

    payload = {
        "content": payload_content,
        "username": f"{sender} (via Hive Mind)",
    }

    # 3. Send to target channel
    try:
        response = requests.post(target_webhook_url, json=payload)
        response.raise_for_status()
        print(f"Message sent to {target_agent} channel.")
    except Exception as e:
        print(f"Error sending to {target_agent}: {e}")

    # 4. Log to #harmony-communication (BCC)
    if HARMONY_LOG_WEBHOOK_URL:
        log_payload = {
            "content": f"**[LOG]** {sender} -> {target_agent}: {message}",
            "username": "Hive Mind Log",
        }
        try:
            requests.post(HARMONY_LOG_WEBHOOK_URL, json=log_payload)
        except Exception as e:
            print(f"Error logging to Harmony-communication: {e}")

def main():
    parser = argparse.ArgumentParser(description="Send cross-channel agent webhooks.")
    parser.add_argument("--target", required=True, help="Target agent name (e.g., Raven, Harmony)")
    parser.add_argument("--message", required=True, help="Message content")
    parser.add_argument("--sender", required=True, help="Sender agent name")

    args = parser.parse_args()
    send_webhook(args.target, args.message, args.sender)

if __name__ == "__main__":
    main()
