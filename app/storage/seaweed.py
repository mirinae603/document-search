# storage/seaweed.py
# All SeaweedFS file operations: upload, sync fetch, async fetch, list directory.
import logging
from typing import List, Dict

import httpx
import requests

from config import SEAWEED_FILER

logger = logging.getLogger(__name__)


class SeaweedStore:
    """
    Wraps SeaweedFS filer HTTP API.
    Swap to S3/GCS: implement the same 4 methods in a new class, update main.py.
    """

    def __init__(self, filer_url: str = SEAWEED_FILER):
        self.filer = filer_url

    def upload(self, file_path: str, content: bytes, content_type: str) -> bool:
        """Upload bytes to filer. Returns True on success."""
        resp = requests.put(
            f"{self.filer}{file_path}",
            data    = content,
            headers = {"Content-Type": content_type},
            timeout = 30,
        )
        return resp.status_code in (200, 201, 204)

    def fetch(self, file_path: str) -> bytes:
        """Synchronous fetch. Raises on failure."""
        resp = requests.get(f"{self.filer}{file_path}", timeout=30)
        if resp.status_code != 200:
            raise RuntimeError(f"SeaweedFS fetch failed {resp.status_code}: {file_path}")
        return resp.content

    def preview(self, file_path: str):
        """Fetch with full response (headers needed for content-type)."""
        return requests.get(f"{self.filer}{file_path}", timeout=10)

    async def fetch_async(self, file_path: str) -> bytes:
        """Async fetch — used in webhook and reindex routes."""
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.get(f"{self.filer}{file_path}")
        if resp.status_code != 200:
            raise RuntimeError(f"SeaweedFS async fetch failed {resp.status_code}: {file_path}")
        return resp.content

    async def list_directory(self, directory: str = "/documents/", limit: int = 1000) -> List[Dict]:
        """List all entries in a filer directory. Returns raw Entries list."""
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{self.filer}{directory}",
                headers = {"Accept": "application/json"},
                params  = {"limit": limit},
            )
        if resp.status_code != 200:
            raise RuntimeError(f"Filer list failed {resp.status_code}")
        data = resp.json()
        return data.get("Entries") or data.get("Files") or []
    
    def upload_image(self, image_bytes: bytes, file_id: str, idx: int) -> str:
        file_path = f"/images/{file_id}/img_{idx}.png"
        ok = self.upload(file_path, image_bytes, "image/png")
        if not ok:
            raise RuntimeError("Image upload failed")
        return file_path


    def save_image_metadata(self, file_id: str, metadata: list):
        import json
        file_path = f"/images/{file_id}/metadata.json"
        self.upload(file_path, json.dumps(metadata).encode(), "application/json")


    def fetch_image_metadata(self, file_id: str):
        import json
        file_path = f"/images/{file_id}/metadata.json"
        try:
            data = self.fetch(file_path)
            return json.loads(data.decode())
        except Exception:
            return []