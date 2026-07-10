"""
lib/gdrive.py
Google Drive upload module for Galgo.

Upload verified tick files to a shared Google Drive folder.
Uses a service account (JSON key) — no browser auth needed.

Setup:
  1. Create service account in Google Cloud Console
  2. Enable Drive API
  3. Download JSON key → set google_drive.credentials_path in config.yaml
  4. Share target folder with the service account email
  5. Set google_drive.history_folder_id in config.yaml
  6. Set google_drive.enabled: true

Usage:
  from lib.gdrive import GDriveClient
  gd = GDriveClient(cfg)
  file_id = gd.upload_file(Path("data/history/MES_trades_20260602.csv"))
  exists   = gd.file_exists("MES_trades_20260602.csv")
"""

import time
from pathlib import Path
from lib.logger import get_logger

log = get_logger("gdrive")

_RETRY_ATTEMPTS = 3
_RETRY_DELAY    = 5  # seconds


class GDriveClient:
    def __init__(self, cfg):
        self._enabled   = getattr(getattr(cfg, "google_drive", None), "enabled", False)
        self._creds     = getattr(getattr(cfg, "google_drive", None), "credentials_path", "")
        self._folder_id = getattr(getattr(cfg, "google_drive", None), "history_folder_id", "")
        self._service   = None

        if self._enabled:
            self._service = self._build_service()

    def _build_service(self):
        try:
            from googleapiclient.discovery import build
            from google.oauth2 import service_account

            creds = service_account.Credentials.from_service_account_file(
                self._creds,
                scopes=["https://www.googleapis.com/auth/drive"]
            )
            service = build("drive", "v3", credentials=creds, cache_discovery=False)
            log.info("Google Drive service account connected")
            return service
        except ImportError:
            log.warning("google-api-python-client not installed — Drive upload disabled. "
                        "Run: pip install google-api-python-client google-auth")
            self._enabled = False
            return None
        except Exception as e:
            log.error(f"Drive auth failed: {e}")
            self._enabled = False
            return None

    def upload_file(self, local_path: Path) -> str:
        """
        Upload local_path to the configured Drive folder.
        Returns Drive file ID on success, empty string on failure.
        Skips if file already exists on Drive (by name match).
        """
        if not self._enabled or self._service is None:
            return ""

        filename = local_path.name
        if self.file_exists(filename):
            log.info(f"Drive: {filename} already exists, skipping upload")
            return self._get_file_id(filename)

        from googleapiclient.http import MediaFileUpload

        for attempt in range(1, _RETRY_ATTEMPTS + 1):
            try:
                media = MediaFileUpload(str(local_path), mimetype="text/csv", resumable=True)
                meta  = {"name": filename, "parents": [self._folder_id]}
                result = self._service.files().create(
                    body=meta, media_body=media, fields="id"
                ).execute()
                file_id = result.get("id", "")
                log.info(f"Drive: uploaded {filename} → {file_id}")
                return file_id
            except Exception as e:
                log.warning(f"Drive upload attempt {attempt}/{_RETRY_ATTEMPTS} failed: {e}")
                if attempt < _RETRY_ATTEMPTS:
                    time.sleep(_RETRY_DELAY)

        log.error(f"Drive: all upload attempts failed for {filename}")
        return ""

    def file_exists(self, filename: str) -> bool:
        """Return True if a file with this exact name exists in the Drive folder."""
        if not self._enabled or self._service is None:
            return False
        try:
            q = (f"name='{filename}' and "
                 f"'{self._folder_id}' in parents and "
                 f"trashed=false")
            res = self._service.files().list(q=q, fields="files(id)").execute()
            return len(res.get("files", [])) > 0
        except Exception as e:
            log.warning(f"Drive file_exists check failed: {e}")
            return False

    def _get_file_id(self, filename: str) -> str:
        try:
            q = (f"name='{filename}' and "
                 f"'{self._folder_id}' in parents and "
                 f"trashed=false")
            res = self._service.files().list(q=q, fields="files(id)").execute()
            files = res.get("files", [])
            return files[0]["id"] if files else ""
        except Exception:
            return ""
