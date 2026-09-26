"""Nonexpiring CRM records; callers commit related hashes in one transaction."""
import json
import os

PREFIX = "crm:v1:"


class CRMStore:
    def __init__(self, redis_client=None):
        if redis_client is None:
            import redis
            url = os.environ.get("REDIS_URL")
            if not url:
                raise RuntimeError("CRM requires REDIS_URL")
            redis_client = redis.from_url(url, decode_responses=True, socket_timeout=5)
        self.redis = redis_client

    def records(self, name):
        return [json.loads(v) for v in self.redis.hvals(PREFIX + name)]

    def lead(self, ident):
        value = self.redis.hget(PREFIX + "leads", ident)
        return json.loads(value) if value else None

    def events(self):
        return self.records("outbox")