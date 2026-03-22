#!/usr/bin/env python3
"""
Skill Sync — Push and pull actual skill files to/from Redis.
This allows Valentina to distribute new tools to all agents instantly.
"""
import argparse
import base64
import hashlib
import json
import os
import sys
import tarfile
import io
from datetime import datetime

try:
    import redis
except ImportError:
    os.system("pip install redis -q")
    import redis


def get_redis():
    url = os.getenv("REDIS_URL", "redis://localhost:6379")
    return redis.from_url(url, decode_responses=True)


def push_skill(r, skill_path, skill_name=None):
    """Package a skill directory and push it to Redis."""
    if not os.path.isdir(skill_path):
        print(f"ERROR: {skill_path} is not a directory")
        return False

    if not skill_name:
        skill_name = os.path.basename(os.path.normpath(skill_path))

    print(f"Packaging skill: {skill_name} from {skill_path}...")

    # Create a tarball in memory
    tar_stream = io.BytesIO()
    with tarfile.open(fileobj=tar_stream, mode='w:gz') as tar:
        tar.add(skill_path, arcname=skill_name)
    
    tar_data = tar_stream.getvalue()
    tar_b64 = base64.b64encode(tar_data).decode('utf-8')
    
    # Calculate hash for change detection
    skill_hash = hashlib.sha256(tar_data).hexdigest()
    
    # Store in Redis
    skill_key = f"hive:skill_data:{skill_name}"
    now = datetime.utcnow().isoformat()
    
    r.hset(skill_key, mapping={
        "name": skill_name,
        "data": tar_b64,
        "hash": skill_hash,
        "updated_at": now,
        "version": r.hincrby(skill_key, "version", 1) if r.exists(skill_key) else 1
    })
    
    # Register in the registry if not already there
    registry_key = f"hive:skill_meta:{skill_name}"
    if not r.exists(registry_key):
        r.hset(registry_key, mapping={
            "name": skill_name,
            "description": f"Auto-synced skill: {skill_name}",
            "created_at": now,
            "status": "active"
        })

    print(f"Successfully pushed skill '{skill_name}' to Redis (Hash: {skill_hash[:10]})")
    return True


def pull_all_skills(r, target_dir="/root/.hermes/skills"):
    """Pull all skill data from Redis and extract to the target directory."""
    print(f"Syncing skills from Redis to {target_dir}...")
    os.makedirs(target_dir, exist_ok=True)
    
    count = 0
    for key in r.scan_iter("hive:skill_data:*"):
        skill_data = r.hgetall(key)
        skill_name = skill_data.get("name")
        tar_b64 = skill_data.get("data")
        
        if not skill_name or not tar_b64:
            continue
            
        print(f"  - Extracting {skill_name}...")
        try:
            tar_data = base64.b64decode(tar_b64)
            tar_stream = io.BytesIO(tar_data)
            with tarfile.open(fileobj=tar_stream, mode='r:gz') as tar:
                tar.extractall(path=target_dir)
            count += 1
        except Exception as e:
            print(f"    ERROR extracting {skill_name}: {e}")
            
    print(f"Finished. Synced {count} skills.")


def main():
    parser = argparse.ArgumentParser(description="Hive Mind Skill Sync")
    parser.add_argument("--action", required=True, choices=["push", "pull"], help="Action to perform")
    parser.add_argument("--path", help="Path to the skill directory (for push)")
    parser.add_argument("--name", help="Override skill name (for push)")
    parser.add_argument("--target", default="/root/.hermes/skills", help="Target directory (for pull)")
    args = parser.parse_args()

    r = get_redis()

    if args.action == "push":
        if not args.path:
            print("ERROR: --path required for push")
            sys.exit(1)
        push_skill(r, args.path, args.name)
    elif args.action == "pull":
        pull_all_skills(r, args.target)


if __name__ == "__main__":
    main()
