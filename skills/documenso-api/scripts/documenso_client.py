#!/usr/bin/env python3
"""
Documenso API Client Helper

A Python client for interacting with the Documenso API v2.
Handles authentication, request formatting, and common operations.
"""

import os
import requests
from typing import Dict, List, Optional, Any


class DocumensoClient:
    """Client for Documenso API v2"""
    
    def __init__(self, api_key: Optional[str] = None, base_url: str = "https://app.documenso.com/api/v2"):
        """
        Initialize Documenso client
        
        Args:
            api_key: API key (defaults to DOCUMENSO_API env var)
            base_url: API base URL (production by default)
        """
        self.api_key = api_key or os.getenv("DOCUMENSO_API")
        if not self.api_key:
            raise ValueError("API key required. Set DOCUMENSO_API env var or pass api_key parameter")
        
        self.base_url = base_url.rstrip("/")
        self.headers = {
            "Authorization": self.api_key
        }
    
    def _request(self, method: str, endpoint: str, **kwargs) -> Dict[str, Any]:
        """Make API request with error handling"""
        url = f"{self.base_url}{endpoint}"
        
        # Add auth header
        if "headers" in kwargs:
            kwargs["headers"].update(self.headers)
        else:
            kwargs["headers"] = self.headers
        
        response = requests.request(method, url, **kwargs)
        
        # Handle errors
        if response.status_code >= 400:
            error_msg = f"API Error {response.status_code}: {response.text}"
            raise Exception(error_msg)
        
        return response.json() if response.text else {}
    
    # Envelope Operations
    
    def find_envelopes(self, query: Optional[str] = None, page: int = 1, 
                      per_page: int = 10, **filters) -> Dict:
        """Find envelopes based on search criteria"""
        params = {"page": page, "perPage": per_page}
        if query:
            params["query"] = query
        params.update(filters)
        return self._request("GET", "/envelope", params=params)
    
    def get_envelope(self, envelope_id: str) -> Dict:
        """Get specific envelope details"""
        return self._request("GET", f"/envelope/{envelope_id}")
    
    def create_envelope(self, title: str, envelope_type: str, recipients: List[Dict],
                       files: List[str], **options) -> Dict:
        """
        Create new envelope (document or template)
        
        Args:
            title: Envelope title
            envelope_type: "DOCUMENT" or "TEMPLATE"
            recipients: List of recipient dicts with email, name, role, fields
            files: List of PDF file paths
            **options: Additional envelope options
        """
        payload = {
            "type": envelope_type,
            "title": title,
            "recipients": recipients,
            **options
        }
        
        # Prepare multipart form data
        files_data = []
        for file_path in files:
            files_data.append(("files", open(file_path, "rb")))
        
        data = {"payload": str(payload).replace("'", '"')}
        
        return self._request("POST", "/envelope/create", data=data, files=files_data)
    
    def use_template(self, envelope_id: str, recipients: Optional[List[Dict]] = None,
                    custom_files: Optional[List[tuple]] = None, distribute: bool = False) -> Dict:
        """
        Generate document from template
        
        Args:
            envelope_id: Template envelope ID
            recipients: Optional list of recipient updates
            custom_files: Optional list of (file_path, envelope_item_id) tuples
            distribute: Whether to send immediately
        """
        payload = {"envelopeId": envelope_id}
        
        if recipients:
            payload["recipients"] = recipients
        
        if distribute:
            payload["distributeDocument"] = True
        
        if custom_files:
            payload["customDocumentData"] = [
                {"identifier": os.path.basename(f[0]), "envelopeItemId": f[1]}
                for f in custom_files
            ]
            files_data = [("files", open(f[0], "rb")) for f in custom_files]
            data = {"payload": str(payload).replace("'", '"')}
            return self._request("POST", "/envelope/use", data=data, files=files_data)
        
        return self._request("POST", "/envelope/use", json=payload)
    
    def distribute_envelope(self, envelope_id: str) -> Dict:
        """Send envelope to recipients"""
        return self._request("POST", "/envelope/distribute", json={"envelopeId": envelope_id})
    
    def delete_envelope(self, envelope_id: str) -> Dict:
        """Delete envelope"""
        return self._request("POST", "/envelope/delete", json={"envelopeId": envelope_id})
    
    # Recipient Operations
    
    def add_recipients(self, envelope_id: str, recipients: List[Dict]) -> Dict:
        """Add recipients to envelope"""
        return self._request("POST", "/envelope/recipient/create-many", 
                           json={"envelopeId": envelope_id, "data": recipients})
    
    def update_recipients(self, envelope_id: str, recipients: List[Dict]) -> Dict:
        """Update recipients in envelope"""
        return self._request("POST", "/envelope/recipient/update-many",
                           json={"envelopeId": envelope_id, "data": recipients})
    
    # Field Operations
    
    def add_fields(self, envelope_id: str, fields: List[Dict]) -> Dict:
        """Add fields to envelope"""
        return self._request("POST", "/envelope/field/create-many",
                           json={"envelopeId": envelope_id, "data": fields})
    
    def update_fields(self, envelope_id: str, fields: List[Dict]) -> Dict:
        """Update fields in envelope"""
        return self._request("POST", "/envelope/field/update-many",
                           json={"envelopeId": envelope_id, "data": fields})
    
    # Item Operations
    
    def download_item(self, envelope_item_id: str, output_path: str) -> None:
        """Download envelope item (PDF file)"""
        response = requests.get(
            f"{self.base_url}/envelope/item/{envelope_item_id}/download",
            headers=self.headers
        )
        with open(output_path, "wb") as f:
            f.write(response.content)


# Example usage
if __name__ == "__main__":
    client = DocumensoClient()
    
    # Find envelopes
    envelopes = client.find_envelopes(status="COMPLETED")
    print(f"Found {len(envelopes.get('data', []))} completed envelopes")
