#!/usr/bin/env python3
"""
Postiz Social Media Client for Sabrina
Self-hosted Postiz instance at postiz-production-14aa.up.railway.app
"""
import os
import json
import requests
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any


POSTIZ_URL = os.environ.get("POSTIZ_URL", "https://postiz-production-14aa.up.railway.app")
POSTIZ_EMAIL = os.environ.get("POSTIZ_EMAIL", "sanctusmm@gmail.com")
POSTIZ_PASSWORD = os.environ.get("POSTIZ_PASSWORD", "")


class PostizClient:
    def __init__(self, url: str = POSTIZ_URL, email: str = POSTIZ_EMAIL, password: str = POSTIZ_PASSWORD):
        self.url = url.rstrip("/")
        self.email = email
        self.password = password
        self.session = requests.Session()
        self.auth_token: Optional[str] = None
        self._logged_in = False

    def login(self) -> bool:
        """Login to Postiz and store the auth cookie."""
        resp = self.session.post(
            f"{self.url}/api/auth/login",
            json={"email": self.email, "password": self.password, "provider": "LOCAL"},
            timeout=15
        )
        if resp.status_code == 200 and resp.json().get("login"):
            # Extract auth cookie
            self.auth_token = self.session.cookies.get("auth")
            if self.auth_token:
                self.session.headers.update({"Cookie": f"auth={self.auth_token}"})
                self._logged_in = True
                return True
        return False

    def _ensure_logged_in(self):
        if not self._logged_in:
            if not self.login():
                raise RuntimeError("Failed to login to Postiz")

    def get_integrations(self) -> List[Dict]:
        """Get all connected social media accounts."""
        self._ensure_logged_in()
        resp = self.session.get(f"{self.url}/api/integrations", timeout=15)
        resp.raise_for_status()
        return resp.json() if isinstance(resp.json(), list) else resp.json().get("integrations", [])

    def get_posts(self, start_date: str, end_date: str) -> List[Dict]:
        """Get scheduled posts in a date range. Dates in ISO 8601 format."""
        self._ensure_logged_in()
        resp = self.session.get(
            f"{self.url}/api/posts",
            params={"startDate": start_date, "endDate": end_date},
            timeout=15
        )
        resp.raise_for_status()
        return resp.json()

    def schedule_post(
        self,
        content: str,
        schedule_date: str,
        integration_ids: List[str],
        media_ids: Optional[List[str]] = None,
        tags: Optional[List[str]] = None
    ) -> Dict:
        """
        Schedule a post to one or more social media accounts.
        
        Args:
            content: The post text
            schedule_date: ISO 8601 datetime string (e.g., "2026-04-10T14:00:00.000Z")
            integration_ids: List of social account IDs to post to
            media_ids: Optional list of uploaded media IDs
            tags: Optional list of tags
        
        Returns:
            Created post object
        """
        self._ensure_logged_in()
        payload = {
            "content": content,
            "date": schedule_date,
            "integrations": [{"id": iid} for iid in integration_ids],
            "image": [{"id": mid} for mid in (media_ids or [])],
        }
        if tags:
            payload["tags"] = tags

        resp = self.session.post(
            f"{self.url}/api/posts",
            json=payload,
            timeout=15
        )
        resp.raise_for_status()
        return resp.json()

    def upload_media(self, file_path: str) -> Dict:
        """Upload an image/video file and return the media object."""
        self._ensure_logged_in()
        with open(file_path, "rb") as f:
            resp = self.session.post(
                f"{self.url}/api/media",
                files={"file": (os.path.basename(file_path), f)},
                timeout=30
            )
        resp.raise_for_status()
        return resp.json()

    def delete_post(self, post_id: str) -> bool:
        """Delete a scheduled post."""
        self._ensure_logged_in()
        resp = self.session.delete(f"{self.url}/api/posts/{post_id}", timeout=15)
        return resp.status_code in (200, 204)

    def get_user_profile(self) -> Dict:
        """Get current user profile including API key."""
        self._ensure_logged_in()
        resp = self.session.get(f"{self.url}/api/user/self", timeout=15)
        resp.raise_for_status()
        return resp.json()


def main():
    """CLI usage example."""
    import sys
    
    client = PostizClient()
    
    if not client.login():
        print("ERROR: Failed to login to Postiz")
        sys.exit(1)
    
    print("Logged in to Postiz successfully!")
    
    # Get connected accounts
    integrations = client.get_integrations()
    print(f"\nConnected social accounts ({len(integrations)}):")
    for integration in integrations:
        print(f"  - {integration.get('name', 'Unknown')} ({integration.get('type', 'Unknown')}) ID: {integration.get('id')}")
    
    # Get upcoming posts
    now = datetime.now(timezone.utc).isoformat()
    end = "2026-12-31T23:59:59Z"
    posts = client.get_posts(now, end)
    print(f"\nUpcoming scheduled posts: {len(posts) if isinstance(posts, list) else 'N/A'}")


if __name__ == "__main__":
    main()
