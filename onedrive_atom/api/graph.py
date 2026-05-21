"""Microsoft Graph API client with retry, rate-limiting, and chunked uploads."""

import logging
import os
import time
from pathlib import Path
from typing import Any, Generator, Iterator
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from onedrive_atom.auth.oauth import get_access_token
from onedrive_atom.config import get_config

log = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# Files smaller than this are uploaded in a single PUT.
SMALL_FILE_LIMIT = 4 * 1024 * 1024  # 4 MB


def _encode_path(path: str) -> str:
    return quote(path.strip("/"), safe="/")


def _session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=5,
        backoff_factor=1,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET", "PUT", "PATCH", "DELETE"],
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


class GraphError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"[{status}] {message}")
        self.status = status


class GraphClient:
    def __init__(self, account_id: str):
        self.account_id = account_id
        self._session = _session()

    def _headers(self) -> dict[str, str]:
        token = get_access_token(self.account_id)
        if not token:
            raise GraphError(401, "Could not acquire access token")
        return {"Authorization": f"Bearer {token}"}

    def _get(self, url: str, **kwargs) -> Any:
        return self._request("GET", url, **kwargs)

    def _post(self, url: str, **kwargs) -> Any:
        return self._request("POST", url, **kwargs)

    def _put(self, url: str, **kwargs) -> Any:
        return self._request("PUT", url, **kwargs)

    def _patch(self, url: str, **kwargs) -> Any:
        return self._request("PATCH", url, **kwargs)

    def _delete(self, url: str, **kwargs) -> Any:
        return self._request("DELETE", url, **kwargs)

    def _request(self, method: str, url: str, **kwargs) -> Any:
        if not url.startswith("http"):
            url = GRAPH_BASE + url

        headers = self._headers()
        headers.update(kwargs.pop("headers", {}))

        for attempt in range(5):
            resp = self._session.request(method, url, headers=headers, timeout=60, **kwargs)

            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", 10))
                log.warning("Rate limited. Waiting %d seconds.", wait)
                time.sleep(wait)
                continue

            if resp.status_code == 401 and attempt == 0:
                # Token may have just expired; retry once
                headers = self._headers()
                continue

            if resp.status_code == 204:
                return None

            if not resp.ok:
                try:
                    err = resp.json().get("error", {})
                    msg = err.get("message", resp.text)
                except Exception:
                    msg = resp.text
                raise GraphError(resp.status_code, msg)

            if resp.content:
                try:
                    return resp.json()
                except Exception:
                    return resp.content

            return None

        raise GraphError(429, "Too many retries due to rate limiting")

    # ── User ────────────────────────────────────────────────────────────────

    def get_me(self) -> dict:
        return self._get("/me")

    # ── Drives ───────────────────────────────────────────────────────────────

    def get_default_drive(self) -> dict:
        return self._get("/me/drive")

    def list_my_drives(self) -> list[dict]:
        return self._get("/me/drives").get("value", [])

    def list_sharepoint_sites(self) -> list[dict]:
        """Returns SharePoint sites the user follows or has access to."""
        try:
            return self._get("/sites?search=*").get("value", [])
        except GraphError as e:
            log.warning("Could not list SharePoint sites: %s", e)
            return []

    def get_site_drives(self, site_id: str) -> list[dict]:
        try:
            return self._get(f"/sites/{site_id}/drives").get("value", [])
        except GraphError as e:
            log.warning("Could not get drives for site %s: %s", site_id, e)
            return []

    # ── Delta (incremental sync) ─────────────────────────────────────────────

    def get_delta(self, drive_id: str, delta_link: str | None = None) -> tuple[list[dict], str]:
        """
        Yields all changed items since delta_link (or all items if None).
        Returns (items, new_delta_link).
        """
        url = delta_link or f"/drives/{drive_id}/root/delta"
        items = []

        while url:
            data = self._get(url)
            items.extend(data.get("value", []))
            url = data.get("@odata.nextLink")
            if not url:
                delta_link = data.get("@odata.deltaLink", "")

        return items, delta_link

    # ── Items ────────────────────────────────────────────────────────────────

    def list_children(self, drive_id: str, item_id: str = "root") -> list[dict]:
        endpoint = f"/drives/{drive_id}/items/{item_id}/children"
        items = []
        url = endpoint
        while url:
            data = self._get(url)
            items.extend(data.get("value", []))
            url = data.get("@odata.nextLink")
        return items

    def list_folders(self, drive_id: str, item_id: str = "root", remote_path: str = "") -> list[dict]:
        folders = []
        for item in self.list_children(drive_id, item_id):
            if "folder" not in item:
                continue
            name = item.get("name", "")
            path = f"{remote_path}/{name}".replace("//", "/")
            folders.append({
                "id": item.get("id", ""),
                "name": name,
                "path": path,
                "child_count": item.get("folder", {}).get("childCount", 0),
            })
        return folders

    def get_item_by_path(self, drive_id: str, remote_path: str) -> dict:
        encoded = _encode_path(remote_path)
        return self._get(f"/drives/{drive_id}/root:/{encoded}")

    def create_folder(self, drive_id: str, parent_id: str, name: str) -> dict:
        return self._post(
            f"/drives/{drive_id}/items/{parent_id}/children",
            json={"name": name, "folder": {}, "@microsoft.graph.conflictBehavior": "rename"},
        )

    def create_folder_by_path(self, drive_id: str, remote_path: str) -> dict:
        encoded = _encode_path(remote_path)
        parent_path = "/".join(encoded.split("/")[:-1])
        name = remote_path.strip("/").split("/")[-1]
        if parent_path:
            url = f"/drives/{drive_id}/root:/{parent_path}:/children"
        else:
            url = f"/drives/{drive_id}/root/children"
        return self._post(
            url,
            json={"name": name, "folder": {}, "@microsoft.graph.conflictBehavior": "rename"},
        )

    def ensure_folder_by_path(self, drive_id: str, remote_path: str) -> dict | None:
        remote_path = remote_path.strip("/")
        if not remote_path:
            return None
        try:
            return self.get_item_by_path(drive_id, remote_path)
        except GraphError as e:
            if e.status != 404:
                raise

        parent_path = remote_path.rsplit("/", 1)[0] if "/" in remote_path else ""
        if parent_path:
            self.ensure_folder_by_path(drive_id, parent_path)
        return self.create_folder_by_path(drive_id, remote_path)

    def download_item(self, drive_id: str, item_id: str, local_path: Path, progress_cb=None):
        """Download a file to local_path, streaming in chunks."""
        url = f"{GRAPH_BASE}/drives/{drive_id}/items/{item_id}/content"
        headers = self._headers()

        with self._session.get(url, headers=headers, stream=True, timeout=60) as resp:
            if not resp.ok:
                raise GraphError(resp.status_code, resp.text[:200])

            local_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = local_path.with_suffix(local_path.suffix + ".part")
            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0

            with open(tmp, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if progress_cb and total:
                            progress_cb(downloaded, total)

        tmp.replace(local_path)

    def upload_small(self, drive_id: str, remote_path: str, data: bytes, content_type: str = "application/octet-stream") -> dict:
        encoded = _encode_path(remote_path)
        return self._put(
            f"/drives/{drive_id}/root:/{encoded}:/content",
            data=data,
            headers={"Content-Type": content_type},
        )

    def create_upload_session(self, drive_id: str, remote_path: str, size: int) -> dict:
        encoded = _encode_path(remote_path)
        return self._post(
            f"/drives/{drive_id}/root:/{encoded}:/createUploadSession",
            json={
                "item": {
                    "@microsoft.graph.conflictBehavior": "replace",
                    "name": remote_path.strip("/").split("/")[-1],
                }
            },
        )

    def upload_large(self, drive_id: str, remote_path: str, local_path: Path, progress_cb=None) -> dict:
        """Chunked upload session for files larger than SMALL_FILE_LIMIT."""
        size = local_path.stat().st_size
        cfg = get_config()
        chunk_size = cfg.upload_chunk_size
        session = self.create_upload_session(drive_id, remote_path, size)
        upload_url = session["uploadUrl"]

        uploaded = 0
        last_response = {}

        with open(local_path, "rb") as f:
            while uploaded < size:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                end = uploaded + len(chunk) - 1
                headers = {
                    "Content-Range": f"bytes {uploaded}-{end}/{size}",
                    "Content-Length": str(len(chunk)),
                    "Content-Type": "application/octet-stream",
                }
                resp = self._session.put(upload_url, headers=headers, data=chunk, timeout=120)

                if resp.status_code == 429:
                    wait = int(resp.headers.get("Retry-After", 10))
                    time.sleep(wait)
                    continue

                if resp.status_code not in (200, 201, 202):
                    raise GraphError(resp.status_code, resp.text[:200])

                uploaded += len(chunk)
                if progress_cb:
                    progress_cb(uploaded, size)

                if resp.content:
                    try:
                        last_response = resp.json()
                    except Exception:
                        pass

        return last_response

    def upload_file(self, drive_id: str, remote_path: str, local_path: Path, progress_cb=None) -> dict:
        size = local_path.stat().st_size
        if size <= SMALL_FILE_LIMIT:
            data = local_path.read_bytes()
            return self.upload_small(drive_id, remote_path, data)
        return self.upload_large(drive_id, remote_path, local_path, progress_cb)

    def delete_item(self, drive_id: str, item_id: str):
        self._delete(f"/drives/{drive_id}/items/{item_id}")

    def move_item(self, drive_id: str, item_id: str, new_parent_id: str, new_name: str | None = None) -> dict:
        body: dict = {"parentReference": {"id": new_parent_id}}
        if new_name:
            body["name"] = new_name
        return self._patch(f"/drives/{drive_id}/items/{item_id}", json=body)

    def get_item_thumbnails(self, drive_id: str, item_id: str) -> list[dict]:
        try:
            return self._get(f"/drives/{drive_id}/items/{item_id}/thumbnails").get("value", [])
        except GraphError:
            return []
