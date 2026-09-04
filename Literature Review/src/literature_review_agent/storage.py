"""Staging-to-Drive storage router.

Every artefact the pipeline produces goes through :class:`StorageManager`:

1. The stage writes the file into the local numbered folder (staging).
2. :meth:`StorageManager.publish` uploads it to the matching Google Drive folder.
3. The upload is verified against Drive's own metadata (file ID, size, checksum).
4. Only then is the artefact recorded as ``uploaded`` in ``drive_manifest.json``.

If an upload fails, the local copy is kept, the error is recorded, and the entry
stays retryable — ``publish`` never raises for an upload failure, so one bad
artefact cannot abort a long review run.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Settings
from .drive_storage import (
    DriveClient,
    DriveNotConfiguredError,
    DriveReadiness,
    UploadResult,
    describe_drive_readiness,
)
from .job_manager import JobPaths
from .logging_setup import get_logger
from .utils import read_json, utc_now_iso, write_json

LOG = get_logger("storage")

#: Logical destinations, mapped to the Drive folder key used by
#: :meth:`DriveClient.ensure_job_tree`.
DESTINATIONS: dict[str, str] = {
    "keywords": "01 Keywords",
    "papers": "02 Literature Papers",
    "downloaded_papers": "02 Literature Papers/Downloaded Papers",
    "extracted_text": "02 Literature Papers/Extracted Text",
    "unable_to_download": "02 Literature Papers/Unable to Download",
    "reports": "03 Reports",
    "verification": "04 Verification",
    "logs": "05 Logs and State",
}


class UnknownDestinationError(KeyError):
    """Raised when a stage asks to publish to an unrecognised destination."""


@dataclass
class PublishOutcome:
    """What happened to one artefact: staged locally, and uploaded or not."""

    destination: str
    local_path: Path
    upload: UploadResult | None
    drive_enabled: bool

    @property
    def verified(self) -> bool:
        """True only when Drive confirmed the upload."""
        return bool(self.upload and self.upload.verified)

    @property
    def file_id(self) -> str | None:
        """The Drive file ID, or ``None`` when nothing was verified."""
        return self.upload.file_id if self.upload and self.upload.verified else None

    @property
    def web_view_link(self) -> str | None:
        """The Drive web-view link, or ``None`` when nothing was verified."""
        return self.upload.web_view_link if self.upload and self.upload.verified else None

    @property
    def status(self) -> str:
        """Compact status string for logs and the completion summary."""
        if not self.drive_enabled:
            return "local only (Drive syncing disabled)"
        if self.verified:
            return "uploaded and verified"
        if self.upload is None:
            return "local only (Drive not configured)"
        return f"local only ({self.upload.status}: {self.upload.error or 'not verified'})"


class StorageManager:
    """Routes artefacts from local staging to verified Google Drive storage."""

    def __init__(
        self,
        settings: Settings,
        paths: JobPaths,
        *,
        job_date: str,
        topic_slug: str,
        drive_client: DriveClient | None = None,
        enable_drive: bool | None = None,
    ) -> None:
        self.settings = settings
        self.paths = paths
        self.job_date = job_date
        self.topic_slug = topic_slug
        self.readiness: DriveReadiness = describe_drive_readiness(settings)

        requested = settings.drive_enabled if enable_drive is None else enable_drive
        #: Drive is attempted only when it is both requested and actually usable.
        self.drive_enabled = bool(requested) and (
            drive_client is not None or self.readiness.ready
        )
        self._client = drive_client
        self._tree: dict[str, str] | None = None
        self._manifest: dict[str, Any] = self._load_manifest()

        if requested and not self.drive_enabled:
            LOG.warning(
                "Google Drive syncing was requested but is not usable: "
                f"{self.readiness.summary()} Artefacts will stay in local staging "
                "and remain queued for a later 'drive-sync' run."
            )

    # -- manifest -------------------------------------------------------

    @property
    def manifest_path(self) -> Path:
        """Location of the job's Drive manifest."""
        return self.paths.logs / "drive_manifest.json"

    def _load_manifest(self) -> dict[str, Any]:
        """Load (or initialise) the Drive manifest for this job."""
        existing = read_json(self.manifest_path)
        if isinstance(existing, dict) and "artefacts" in existing:
            return existing
        return {
            "job_date": self.job_date,
            "topic_slug": self.topic_slug,
            "root_folder_name": self.settings.drive.get("root_folder_name", "Literature Review"),
            "location": self.settings.drive.get("location", "my_drive"),
            "drive_folder_ids": {},
            "artefacts": {},
            "updated_at": utc_now_iso(),
        }

    def save_manifest(self) -> Path:
        """Persist the Drive manifest."""
        self._manifest["updated_at"] = utc_now_iso()
        return write_json(self.manifest_path, self._manifest)

    @property
    def manifest(self) -> dict[str, Any]:
        """The in-memory Drive manifest."""
        return self._manifest

    def artefact_entry(self, local_path: Path) -> dict[str, Any] | None:
        """Return the manifest entry for *local_path*, if it has one."""
        return self._manifest["artefacts"].get(self._key(local_path))

    def _key(self, local_path: Path) -> str:
        """Manifest key: the artefact path relative to the output root."""
        local_path = Path(local_path)
        try:
            return str(local_path.relative_to(self.paths.root))
        except ValueError:
            return str(local_path)

    # -- Drive plumbing -------------------------------------------------

    @property
    def client(self) -> DriveClient:
        """The Drive client, built on first use."""
        if self._client is None:
            self._client = DriveClient(self.settings)
        return self._client

    def drive_tree(self) -> dict[str, str]:
        """Ensure and cache the Drive folder tree for this job."""
        if self._tree is None:
            self._tree = self.client.ensure_job_tree(self.job_date, self.topic_slug)
            self._manifest["drive_folder_ids"] = dict(self._tree)
            self.save_manifest()
        return self._tree

    def ensure_remote_tree(self) -> dict[str, str] | None:
        """Create the Drive folder structure up front, or return ``None``.

        Called once at job start so the folders exist in Drive before any
        artefact is produced.
        """
        if not self.drive_enabled:
            return None
        try:
            return self.drive_tree()
        except DriveNotConfiguredError as exc:
            self.drive_enabled = False
            LOG.warning(f"Drive folder tree not created: {exc}")
            return None
        except Exception as exc:  # noqa: BLE001 - Drive problems must not abort the job
            self.drive_enabled = False
            LOG.warning(f"Drive folder tree could not be created ({exc}); staying local.")
            return None

    def local_dir(self, destination: str) -> Path:
        """Return the local staging directory for a logical *destination*."""
        mapping = {
            "keywords": self.paths.keywords,
            "papers": self.paths.papers,
            "downloaded_papers": self.paths.downloaded_papers,
            "extracted_text": self.paths.extracted_text,
            "unable_to_download": self.paths.unable_to_download,
            "reports": self.paths.reports,
            "verification": self.paths.verification,
            "logs": self.paths.logs,
        }
        if destination not in mapping:
            raise UnknownDestinationError(
                f"Unknown storage destination '{destination}'. "
                f"Valid destinations: {', '.join(sorted(mapping))}"
            )
        return mapping[destination]

    # -- publishing -----------------------------------------------------

    def publish(
        self,
        local_path: Path,
        destination: str,
        *,
        force: bool = False,
    ) -> PublishOutcome:
        """Upload one staged artefact and record the verified result.

        Returns a :class:`PublishOutcome` in every case — including when Drive is
        disabled, unconfigured, or the upload fails — so callers can report
        honestly without handling exceptions.
        """
        local_path = Path(local_path)
        if destination not in DESTINATIONS:
            raise UnknownDestinationError(
                f"Unknown storage destination '{destination}'. "
                f"Valid destinations: {', '.join(sorted(DESTINATIONS))}"
            )

        key = self._key(local_path)

        if not self.drive_enabled:
            self._record(key, destination, local_path, None)
            return PublishOutcome(destination, local_path, None, drive_enabled=False)

        # Skip work already verified in an earlier run (resumability).
        previous = self._manifest["artefacts"].get(key)
        if not force and previous and previous.get("verified"):
            if _unchanged_since_upload(local_path, previous):
                LOG.debug(f"Already uploaded and unchanged, skipping: {local_path.name}")
                return PublishOutcome(
                    destination,
                    local_path,
                    _result_from_entry(previous),
                    drive_enabled=True,
                )

        try:
            tree = self.drive_tree()
        except DriveNotConfiguredError as exc:
            self.drive_enabled = False
            LOG.warning(f"Drive upload skipped: {exc}")
            self._record(key, destination, local_path, None, error=str(exc))
            return PublishOutcome(destination, local_path, None, drive_enabled=False)

        folder_key = DESTINATIONS[destination]
        parent_id = tree.get(folder_key)
        if not parent_id:
            error = f"Drive folder '{folder_key}' was not created; cannot upload."
            self._record(key, destination, local_path, None, error=error)
            return PublishOutcome(destination, local_path, None, drive_enabled=True)

        drive_path = f"{folder_key}/{self.job_date}/{self.topic_slug}/{local_path.name}"
        result = self.client.upload_file(local_path, parent_id, drive_path=drive_path)
        if previous:
            result.attempts += int(previous.get("attempts", 0))
        self._record(key, destination, local_path, result)

        if result.verified:
            LOG.info(f"Uploaded and verified in Drive: {local_path.name} (id {result.file_id})")
        return PublishOutcome(destination, local_path, result, drive_enabled=True)

    def publish_many(
        self,
        local_paths: list[Path],
        destination: str,
        *,
        force: bool = False,
    ) -> list[PublishOutcome]:
        """Publish several artefacts to the same destination."""
        return [self.publish(path, destination, force=force) for path in local_paths]

    def _record(
        self,
        key: str,
        destination: str,
        local_path: Path,
        result: UploadResult | None,
        *,
        error: str = "",
    ) -> None:
        """Write one artefact's outcome into the manifest."""
        entry: dict[str, Any] = {
            "destination": destination,
            "local_path": str(local_path),
            "filename": local_path.name,
            "exists_locally": local_path.exists(),
            "size_local": local_path.stat().st_size if local_path.exists() else None,
            "recorded_at": utc_now_iso(),
        }
        if result is not None:
            entry.update(result.to_dict())
        else:
            entry.update(
                {
                    "file_id": None,
                    "web_view_link": None,
                    "verified": False,
                    "status": "local_only",
                    "error": error or "Google Drive syncing is not active.",
                    "attempts": int(
                        (self._manifest["artefacts"].get(key) or {}).get("attempts", 0)
                    ),
                    "local_retained": True,
                }
            )
        self._manifest["artefacts"][key] = entry
        self.save_manifest()

    # -- retry / reporting ---------------------------------------------

    def pending_uploads(self) -> list[dict[str, Any]]:
        """Artefacts that still need a successful, verified upload."""
        return [
            entry
            for entry in self._manifest["artefacts"].values()
            if not entry.get("verified")
        ]

    def retry_pending(self) -> list[PublishOutcome]:
        """Retry every unverified artefact whose local copy still exists.

        This is what makes Drive syncing resumable: after fixing credentials or
        a network outage, ``drive-sync`` re-publishes exactly what is missing.
        """
        outcomes: list[PublishOutcome] = []
        for entry in list(self.pending_uploads()):
            local_path = Path(entry.get("local_path", ""))
            if not local_path.exists():
                LOG.warning(
                    f"Cannot retry upload for {entry.get('filename')}: the local "
                    "staging copy is gone."
                )
                continue
            outcomes.append(self.publish(local_path, entry.get("destination", "logs")))
        return outcomes

    def summary(self) -> dict[str, Any]:
        """Counts and links for the completion message."""
        artefacts = list(self._manifest["artefacts"].values())
        verified = [a for a in artefacts if a.get("verified")]
        return {
            "drive_enabled": self.drive_enabled,
            "drive_status": self.readiness.summary(),
            "artefacts_tracked": len(artefacts),
            "artefacts_uploaded_and_verified": len(verified),
            "artefacts_pending_upload": len(artefacts) - len(verified),
            "root_folder_name": self._manifest.get("root_folder_name"),
            "manifest": str(self.manifest_path),
            "verified_links": [
                {"filename": a.get("filename"), "link": a.get("web_view_link")}
                for a in verified
                if a.get("web_view_link")
            ],
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _unchanged_since_upload(local_path: Path, entry: dict[str, Any]) -> bool:
    """True when the local file still matches what was uploaded.

    A changed size means the artefact was regenerated and must be re-uploaded.
    """
    if not local_path.exists():
        return True  # nothing local to re-upload; trust the recorded upload
    recorded = entry.get("size_local")
    if recorded is None:
        return False
    return int(recorded) == local_path.stat().st_size


def _result_from_entry(entry: dict[str, Any]) -> UploadResult:
    """Rebuild an :class:`UploadResult` from a manifest entry."""
    return UploadResult(
        local_path=str(entry.get("local_path", "")),
        drive_path=str(entry.get("drive_path", "")),
        file_id=entry.get("file_id"),
        web_view_link=entry.get("web_view_link"),
        size_local=entry.get("size_local"),
        size_remote=entry.get("size_remote"),
        md5_local=entry.get("md5_local"),
        md5_remote=entry.get("md5_remote"),
        verified=bool(entry.get("verified")),
        status=str(entry.get("status", "uploaded")),
        error=str(entry.get("error", "")),
        attempts=int(entry.get("attempts", 0)),
        uploaded_at=entry.get("uploaded_at"),
        local_retained=bool(entry.get("local_retained", True)),
    )
