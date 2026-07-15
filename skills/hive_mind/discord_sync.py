#!/usr/bin/env python3
"""
Discord Sync — Bridges the Redis Task Board to a persistent Discord message.
Uses the DISCORD_BOT_API for live, real-time updates.
"""
import os
import json
import requests
import redis
from typing import List, Dict

# Configuration
DISCORD_BOT_API = os.getenv("DISCORD_BOT_API")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
TASK_BOARD_CHANNEL_ID = os.getenv("TASK_BOARD_CHANNEL_ID")
TASK_BOARD_KEY = "hive:task_board"
PERSISTENT_MSG_KEY = "hive:task_board_msg_id"

def get_tasks_from_redis() -> List[Dict]:
    r = redis.from_url(REDIS_URL, decode_responses=True)
    data = r.get(TASK_BOARD_KEY)
    return json.loads(data) if data else []

def format_task_board(tasks: List[Dict]) -> str:
    if not tasks:
        return "📋 **Task Board is currently empty.**"
    
    header = "📊 **HIVE MIND LIVE TASK BOARD**\n"
    header += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    
    body = ""
    for task in tasks:
        status_emoji = {
            "Pending": "⏳",
            "In Progress": "⚙️",
            "Completed": "✅",
            "Blocked": "❌"
        }.get(task.get("status", "Pending"), "❓")
        
        body += f"{status_emoji} **{task['id']}**: {task['name']}\n"
        body += f"   └ Assigned: `{task['assigned_to']}` | Status: *{task['status']}*\n"
        if task.get("notes"):
            body += f"   └ Notes: {task['notes']}\n"
        body += "\n"
    
    footer = f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    footer += f"🕒 Last Updated: {json.loads(tasks[0].get('updated_at_json', 'null')) if 'updated_at_json' in tasks[0] else 'Just now'}"
    
    return header + body + footer

def sync_to_discord():
    if not DISCORD_BOT_API or not TASK_BOARD_CHANNEL_ID:
        print("Error: DISCORD_BOT_API or TASK_BOARD_CHANNEL_ID not set.")
        return

    tasks = get_tasks_from_redis()
    content = format_task_board(tasks)
    
    r = redis.from_url(REDIS_URL, decode_responses=True)
    msg_id = r.get(PERSISTENT_MSG_KEY)
    
    headers = {
        "Authorization": f"Bot {DISCORD_BOT_API}",
        "Content-Type": "application/json"
    }
    
    # Base URL for Discord API
    base_url = f"https://discord.com/api/v10/channels/{TASK_BOARD_CHANNEL_ID}/messages"
    
    if msg_id:
        # Try to edit the existing message
        edit_url = f"{base_url}/{msg_id}"
        response = requests.patch(edit_url, headers=headers, json={"content": content})
        
        if response.status_code == 200:
            print(f"Successfully updated task board message {msg_id}.")
            return
        else:
            print(f"Failed to edit message {msg_id} (Status: {response.status_code}). Creating a new one.")

    # Create a new message if none exists or edit failed
    response = requests.post(base_url, headers=headers, json={"content": content})
    if response.status_code == 200:
        new_msg_id = response.json().get("id")
        r.set(PERSISTENT_MSG_KEY, new_msg_id)
        print(f"Created new task board message {new_msg_id}.")
    else:
        print(f"Failed to create message (Status: {response.status_code}): {response.text}")

if __name__ == "__main__":
    sync_to_discord()
