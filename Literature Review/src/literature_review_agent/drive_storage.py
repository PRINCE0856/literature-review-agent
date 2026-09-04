"""Google Drive storage using the official Drive v3 API.

Design rules enforced here:

* **Nothing is reported as uploaded until Drive returns a file ID** and the
  post-upload verification (a separate ``files().get`` call) confirms size and,
  where Drive supplies it, the MD5 checksum.
* **Credentials never enter configuration, logs, or artefacts.** Only *paths* to
  credential files are configured, and those files are git-ignored.
* **A failed upload never aborts the pipeline.** The local staging copy is kept,
  the error is recorded in the job's ``drive_manifest.json``, and the entry stays
  retryable.
* Both **My Drive** and **Shared Drives** are supported through configuration.

The Google client libraries are imported lazily so the rest of the package (and
the mocked test suite) works even when they are not installed.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Settings, get_secret
from .logging_setup import get_logger
from .utils import ensure_dir

LOG = get_logger("drive")

#: MIME type Drive uses for folders.
FOLDER_MIME = "application/vnd.google-apps.folder"

#: Fields requested whenever a file is created or inspected.
FILE_FIELDS = (
    "id, name, mimeType, size, md5Checksum, webViewLink, webContentLink, "
    "parents, modifiedTime"
)

#: HTTP statuses from Drive that are worth retrying.
RETRYABLE_DRIVE_STATUS = frozenset({429, 500, 502, 503, 504})


class DriveError(RuntimeError):
    """Base class for Drive storage problems."""


class DriveNotConfiguredError(DriveError):
    """Raised when Drive is requested but credentials are not in place.

    Carries a step-by-step remedy so the CLI can print exactly what the user must
    configure. No secret material is ever included in the message.
    """

    def __init__(self, message: str, *, steps: list[str] | None = None) -> None:
        super().__init__(message)
        self.steps = steps or []


class DriveUploadError(DriveError):
    """Raised when an upload or its verification fails."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class DriveVerificationError(DriveUploadError):
    """Raised when Drive accepted the bytes but verification did not pass."""


# ---------------------------------------------------------------------------
# Configured paths
# ---------------------------------------------------------------------------


def _credentials_path(settings: Settings) -> Path | None:
    """Path to the credential file appropriate for the configured auth method."""
    auth_method = str(settings.drive.get("auth_method", "oauth")).lower()
    if auth_method == "service_account":
        env = get_secret("GOOGLE_SERVICE_ACCOUNT_FILE")
        configured = settings.drive.get("service_account_file")
    else:
        env = get_secret("GOOGLE_DRIVE_CREDENTIALS_FILE")
        configured = settings.drive.get("credentials_file")
    return settings.resolve_path(env or configured)


def _token_path(settings: Settings) -> Path | None:
    """Path to the cached OAuth token file."""
    env = get_secret("GOOGLE_DRIVE_TOKEN_FILE")
    return settings.resolve_path(env or settings.drive.get("token_file"))


def shared_drive_id(settings: Settings) -> str | None:
    """Configured Shared Drive ID, if any."""
    return get_secret("GOOGLE_SHARED_DRIVE_ID") or settings.drive.get("shared_drive_id") or None


def root_folder_id_override(settings: Settings) -> str | None:
    """Pinned root folder ID, if the user supplied one."""
    return (
        get_secret("GOOGLE_DRIVE_ROOT_FOLDER_ID")
        or settings.drive.get("root_folder_id")
        or None
    )


# ---------------------------------------------------------------------------
# Readiness reporting
# ---------------------------------------------------------------------------


@dataclass
class DriveReadiness:
    """Whether Drive can be used, and if not, precisely what is missing."""

    enabled: bool
    auth_method: str
    location: str
    libraries_installed: bool
    credentials_present: bool
    token_present: bool
    shared_drive_configured: bool
    problems: list[str] = field(default_factory=list)
    setup_steps: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        """True when an authenticated Drive call could plausibly be made."""
        return (
            self.enabled
            and self.libraries_installed
            and self.credentials_present
            and not self.problems
        )

    def summary(self) -> str:
        """One-line human-readable readiness summary."""
        if not self.enabled:
            return "Google Drive syncing is disabled in config/google_drive.yaml."
        if self.ready:
            target = "a Shared Drive" if self.location == "shared_drive" else "My Drive"
            return f"Google Drive is configured ({self.auth_method}, {target})."
        return "Google Drive is enabled but not yet usable: " + "; ".join(self.problems)


def google_libraries_installed() -> bool:
    """True when the official Google client libraries can be imported."""
    try:
        import google.auth  # noqa: F401
        from googleapiclient.discovery import build  # noqa: F401
    except ImportError:
        return False
    return True


def describe_drive_readiness(settings: Settings) -> DriveReadiness:
    """Inspect configuration and credential *files* without authenticating.

    Does no network I/O, so the CLI can report status instantly and the
    orchestrator can decide up front whether to attempt uploads.
    """
    drive = settings.drive
    auth_method = str(drive.get("auth_method", "oauth")).lower()
    location = str(drive.get("location", "my_drive")).lower()

    credentials_file = _credentials_path(settings)
    token_file = _token_path(settings)
    drive_id = shared_drive_id(settings)

    libraries = google_libraries_installed()
    credentials_present = bool(credentials_file and credentials_file.exists())
    token_present = bool(token_file and token_file.exists())

    if not settings.drive_enabled:
        return DriveReadiness(
            enabled=False,
            auth_method=auth_method,
            location=location,
            libraries_installed=libraries,
            credentials_present=credentials_present,
            token_present=token_present,
            shared_drive_configured=bool(drive_id),
        )

    problems: list[str] = []
    steps: list[str] = []

    if not libraries:
        problems.append("the Google API client libraries are not installed")
        steps.append(
            "Install the Google client libraries: pip install google-api-python-client "
            "google-auth google-auth-oauthlib google-auth-httplib2"
        )

    if not credentials_present:
        expected = credentials_file or Path("secrets/credentials.json")
        if auth_method == "service_account":
            problems.append("the service-account key file is missing")
            steps += [
                "In Google Cloud Console, create or open a project and enable the "
                "Google Drive API.",
                "Create a service account, then create a JSON key for it.",
                f"Save that JSON key at: {expected}",
                "Share the destination Drive folder (or the Shared Drive) with the "
                "service account's email address, granting Content manager / Editor.",
            ]
        else:
            problems.append("the OAuth client-secret file is missing")
            steps += [
                "In Google Cloud Console, create or open a project and enable the "
                "Google Drive API.",
                "Open APIs & Services > Credentials > Create credentials > OAuth "
                "client ID and choose application type 'Desktop app'.",
                f"Download the JSON and save it at: {expected}",
                "Run 'python -m literature_review_agent drive-login' once. A browser "
                "window will ask you to authorise access and the token is cached locally.",
            ]
        steps.append(
            "Do not commit these files. secrets/ and credential JSON patterns are "
            "already excluded by .gitignore."
        )

    if location == "shared_drive" and not drive_id:
        problems.append("location is 'shared_drive' but no Shared Drive ID is set")
        steps.append(
            "Open the Shared Drive in a browser: the ID is the last segment of "
            "drive.google.com/drive/folders/<ID>. Set GOOGLE_SHARED_DRIVE_ID in .env "
            "or drive.shared_drive_id in config/google_drive.yaml."
        )

    return DriveReadiness(
        enabled=True,
        auth_method=auth_method,
        location=location,
        libraries_installed=libraries,
        credentials_present=credentials_present,
        token_present=token_present,
        shared_drive_configured=bool(drive_id),
        problems=problems,
        setup_steps=steps,
    )


# ---------------------------------------------------------------------------
# Upload result
# ---------------------------------------------------------------------------


@dataclass
class UploadResult:
    """The outcome of one upload attempt.

    ``verified`` is only ever true when Drive returned a file ID *and* every
    configured verification check passed.
    """

    local_path: str
    drive_path: str
    file_id: str | None = None
    web_view_link: str | None = None
    size_local: int | None = None
    size_remote: int | None = None
    md5_local: str | None = None
    md5_remote: str | None = None
    verified: bool = False
    status: str = "pending"
    error: str = ""
    attempts: int = 0
    uploaded_at: str | None = None
    local_retained: bool = True

    def to_dict(self) -> dict[str, Any]:
        """Serialise for ``drive_manifest.json``."""
        return {
            "local_path": self.local_path,
            "drive_path": self.drive_path,
            "file_id": self.file_id,
            "web_view_link": self.web_view_link,
            "size_local": self.size_local,
            "size_remote": self.size_remote,
            "md5_local": self.md5_local,
            "md5_remote": self.md5_remote,
            "verified": self.verified,
            "status": self.status,
            "error": self.error,
            "attempts": self.attempts,
            "uploaded_at": self.uploaded_at,
            "local_retained": self.local_retained,
        }


def md5_of_file(path: Path, *, chunk_size: int = 1 << 20) -> str:
    """Return the hex MD5 digest of a file.

    MD5 is used solely because that is the checksum the Drive API reports; it is
    an integrity comparison, not a security measure.
    """
    digest = hashlib.md5()  # noqa: S324 - matches Drive's md5Checksum field
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Drive client
# ---------------------------------------------------------------------------


class DriveClient:
    """Wrapper over the Drive v3 ``files`` resource.

    The Google service object is injectable, which is how the test suite
    exercises folder creation, upload, verification, and failure handling without
    contacting Google.
    """

    def __init__(self, settings: Settings, *, service: Any | None = None) -> None:
        self.settings = settings
        self.drive_id = shared_drive_id(settings)
        self.is_shared_drive = (
            str(settings.drive.get("location", "my_drive")).lower() == "shared_drive"
        )
        self._service = service
        #: Cache of ``parent_id/name`` -> folder id, so a job with many artefacts
        #: does not re-query the same folders repeatedly.
        self._folder_cache: dict[str, str] = {}

    # -- authentication -------------------------------------------------

    @property
    def service(self) -> Any:
        """The authenticated Drive service, built on first use."""
        if self._service is None:
            self._service = self._build_service()
        return self._service

    def _build_service(self) -> Any:
        """Authenticate and build the Drive v3 service object."""
        readiness = describe_drive_readiness(self.settings)
        if not readiness.enabled:
            raise DriveNotConfiguredError(
                "Google Drive syncing is disabled in config/google_drive.yaml.",
                steps=["Set drive.enabled to true in config/google_drive.yaml."],
            )
        if not readiness.ready:
            raise DriveNotConfiguredError(readiness.summary(), steps=readiness.setup_steps)

        from googleapiclient.discovery import build

        return build(
            "drive", "v3", credentials=self._load_credentials(), cache_discovery=False
        )

    def _load_credentials(self) -> Any:
        """Load service-account or OAuth credentials from their configured file."""
        scopes = list(
            self.settings.drive.get("scopes") or ["https://www.googleapis.com/auth/drive.file"]
        )
        auth_method = str(self.settings.drive.get("auth_method", "oauth")).lower()
        credentials_file = _credentials_path(self.settings)
        if credentials_file is None or not credentials_file.exists():
            raise DriveNotConfiguredError(
                "The Google credential file named in config/google_drive.yaml does not exist.",
                steps=describe_drive_readiness(self.settings).setup_steps,
            )

        if auth_method == "service_account":
            from google.oauth2 import service_account

            return service_account.Credentials.from_service_account_file(
                str(credentials_file), scopes=scopes
            )
        return self._load_oauth_credentials(credentials_file, scopes)

    def _load_oauth_credentials(self, credentials_file: Path, scopes: list[str]) -> Any:
        """Load, refresh, or interactively obtain OAuth user credentials."""
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow

        token_file = _token_path(self.settings)
        credentials = None
        if token_file and token_file.exists():
            try:
                credentials = Credentials.from_authorized_user_file(str(token_file), scopes)
            except (ValueError, KeyError) as exc:
                LOG.warning(f"Cached Drive token unreadable ({exc}); re-authorisation needed.")

        if credentials and credentials.valid:
            return credentials

        if credentials and credentials.expired and credentials.refresh_token:
            try:
                credentials.refresh(Request())
                self._save_token(credentials, token_file)
                return credentials
            except Exception as exc:  # noqa: BLE001 - fall through to the full flow
                LOG.warning(f"Drive token refresh failed ({exc}); re-authorisation needed.")

        flow = InstalledAppFlow.from_client_secrets_file(str(credentials_file), scopes)
        credentials = flow.run_local_server(port=0)
        self._save_token(credentials, token_file)
        return credentials

    @staticmethod
    def _save_token(credentials: Any, token_file: Path | None) -> None:
        """Cache the OAuth token with owner-only permissions."""
        if token_file is None:
            return
        ensure_dir(token_file.parent)
        token_file.write_text(credentials.to_json(), encoding="utf-8")
        try:
            token_file.chmod(0o600)
        except OSError:  # pragma: no cover - platform dependent
            pass
        LOG.info(f"Drive token cached at {token_file} (git-ignored).")

    # -- shared-drive plumbing -----------------------------------------

    def _write_kwargs(self) -> dict[str, Any]:
        """Extra API arguments required when writing to a Shared Drive."""
        return {"supportsAllDrives": True} if (self.is_shared_drive and self.drive_id) else {}

    def _list_kwargs(self) -> dict[str, Any]:
        """Extra ``files().list`` arguments for Shared Drive traversal."""
        if self.is_shared_drive and self.drive_id:
            return {
                "supportsAllDrives": True,
                "includeItemsFromAllDrives": True,
                "corpora": "drive",
                "driveId": self.drive_id,
            }
        return {}

    # -- retry helper ---------------------------------------------------

    def _execute(self, request: Any, *, what: str) -> dict[str, Any]:
        """Execute a Drive request with bounded exponential-backoff retries."""
        policy = self.settings.upload
        max_retries = int(policy.get("max_retries", 5))
        delay = float(policy.get("backoff_initial_seconds", 1.0))
        max_delay = float(policy.get("backoff_max_seconds", 60.0))

        last_error: Exception | None = None
        for attempt in range(1, max_retries + 1):
            try:
                return request.execute()
            except Exception as exc:  # noqa: BLE001 - Drive raises HttpError subclasses
                last_error = exc
                status = _http_status_of(exc)
                if status is not None and status not in RETRYABLE_DRIVE_STATUS:
                    raise DriveUploadError(
                        f"{what} failed with HTTP {status}: {exc}", retryable=False
                    ) from exc
                if attempt == max_retries:
                    break
                LOG.warning(
                    f"{what} attempt {attempt}/{max_retries} failed ({exc}); "
                    f"retrying in {delay:.1f}s."
                )
                time.sleep(delay)
                delay = min(delay * 2, max_delay)
        raise DriveUploadError(f"{what} failed after {max_retries} attempts: {last_error}")

    # -- folders --------------------------------------------------------

    def find_folder(self, name: str, parent_id: str) -> str | None:
        """Return the ID of the folder called *name* under *parent_id*, if any."""
        escaped = name.replace("\\", "\\\\").replace("'", "\\'")
        query = (
            f"name = '{escaped}' and mimeType = '{FOLDER_MIME}' "
            f"and '{parent_id}' in parents and trashed = false"
        )
        response = self._execute(
            self.service.files().list(
                q=query,
                fields="files(id, name)",
                pageSize=10,
                **self._list_kwargs(),
            ),
            what=f"Looking up folder '{name}'",
        )
        files = response.get("files") or []
        return files[0]["id"] if files else None

    def create_folder(self, name: str, parent_id: str) -> str:
        """Create the folder *name* under *parent_id* and return its ID."""
        metadata = {"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]}
        created = self._execute(
            self.service.files().create(
                body=metadata, fields="id, name", **self._write_kwargs()
            ),
            what=f"Creating folder '{name}'",
        )
        folder_id = created.get("id")
        if not folder_id:
            raise DriveUploadError(f"Drive did not return an ID for new folder '{name}'.")
        LOG.info(f"Created Drive folder '{name}'.")
        return folder_id

    def ensure_folder(self, name: str, parent_id: str) -> str:
        """Return the ID of *name* under *parent_id*, creating it if absent."""
        cache_key = f"{parent_id}/{name}"
        if cache_key in self._folder_cache:
            return self._folder_cache[cache_key]
        folder_id = self.find_folder(name, parent_id) or self.create_folder(name, parent_id)
        self._folder_cache[cache_key] = folder_id
        return folder_id

    def root_parent_id(self) -> str:
        """Return the parent ID that the root folder should be created under."""
        if self.is_shared_drive and self.drive_id:
            return self.drive_id
        return "root"

    def ensure_root_folder(self) -> str:
        """Return the ID of the configured ``Literature Review`` root folder."""
        if pinned := root_folder_id_override(self.settings):
            return pinned
        name = str(self.settings.drive.get("root_folder_name", "Literature Review"))
        return self.ensure_folder(name, self.root_parent_id())

    def ensure_path(self, segments: list[str]) -> str:
        """Create (or reuse) a nested folder path below the root folder.

        ``ensure_path(["03 Reports", "2026-09-04", "my-topic"])`` returns the ID
        of the deepest folder, creating any missing level on the way.
        """
        parent_id = self.ensure_root_folder()
        for segment in segments:
            if not segment:
                continue
            parent_id = self.ensure_folder(str(segment), parent_id)
        return parent_id

    def ensure_job_tree(self, job_date: str, topic_slug: str) -> dict[str, str]:
        """Create the full ``<top level>/<date>/<slug>`` tree for one job.

        Returns a mapping of logical folder name to Drive folder ID, including
        the three nested folders inside ``02 Literature Papers``.
        """
        top_levels = list(
            self.settings.drive.get("top_level_folders")
            or [
                "01 Keywords",
                "02 Literature Papers",
                "03 Reports",
                "04 Verification",
                "05 Logs and State",
            ]
        )
        tree: dict[str, str] = {"root": self.ensure_root_folder()}
        for top in top_levels:
            job_folder_id = self.ensure_path([top, job_date, topic_slug])
            tree[top] = job_folder_id
            if top.startswith("02"):
                for sub in self.settings.drive.get("paper_subfolders") or [
                    "Downloaded Papers",
                    "Extracted Text",
                    "Unable to Download",
                ]:
                    tree[f"{top}/{sub}"] = self.ensure_folder(str(sub), job_folder_id)
        LOG.info(f"Drive job tree ready for {job_date}/{topic_slug} ({len(tree)} folders).")
        return tree

    # -- files ----------------------------------------------------------

    def find_file(self, name: str, parent_id: str) -> dict[str, Any] | None:
        """Return metadata for the file called *name* under *parent_id*, if any."""
        escaped = name.replace("\\", "\\\\").replace("'", "\\'")
        query = f"name = '{escaped}' and '{parent_id}' in parents and trashed = false"
        response = self._execute(
            self.service.files().list(
                q=query,
                fields=f"files({FILE_FIELDS})",
                pageSize=10,
                **self._list_kwargs(),
            ),
            what=f"Looking up file '{name}'",
        )
        files = response.get("files") or []
        return files[0] if files else None

    def get_file(self, file_id: str) -> dict[str, Any]:
        """Fetch fresh metadata for *file_id* — used for post-upload verification."""
        return self._execute(
            self.service.files().get(fileId=file_id, fields=FILE_FIELDS, **self._write_kwargs()),
            what=f"Verifying file {file_id}",
        )

    def guess_mime_type(self, path: Path) -> str:
        """Return the configured MIME type for a file extension."""
        configured = self.settings.drive_mime_types
        suffix = path.suffix.lower()
        if suffix in configured:
            return str(configured[suffix])
        import mimetypes

        guessed, _ = mimetypes.guess_type(path.name)
        return guessed or "application/octet-stream"

    def upload_file(
        self,
        local_path: Path,
        parent_id: str,
        *,
        drive_path: str = "",
        remote_name: str | None = None,
    ) -> UploadResult:
        """Upload one file and verify it, returning a :class:`UploadResult`.

        Never raises for an upload failure: the failure is captured in the
        result so the pipeline can continue and retry later.
        """
        local_path = Path(local_path)
        name = remote_name or local_path.name
        result = UploadResult(
            local_path=str(local_path),
            drive_path=drive_path or name,
            status="pending",
        )

        if not local_path.exists():
            result.status = "missing_local_file"
            result.error = f"Local staging file does not exist: {local_path}"
            return result
        if local_path.is_dir():
            result.status = "skipped_directory"
            result.error = "Directories are not uploaded individually."
            return result

        result.size_local = local_path.stat().st_size
        policy = self.settings.upload
        verify = policy.get("verify", {}) or {}
        if verify.get("compare_md5", True):
            result.md5_local = md5_of_file(local_path)

        try:
            existing = self.find_file(name, parent_id)
            existing_policy = str(policy.get("existing_file", "new_version")).lower()

            if existing and existing_policy == "skip":
                result.file_id = existing.get("id")
                result.web_view_link = existing.get("webViewLink")
                result.status = "skipped_existing"
                result.attempts = 1
                verified = self._verify(result, verify)
                result.verified = verified
                return result

            media = self._build_media(local_path)
            result.attempts = 1

            if existing and existing_policy == "new_version":
                response = self._execute(
                    self.service.files().update(
                        fileId=existing["id"],
                        media_body=media,
                        fields=FILE_FIELDS,
                        **self._write_kwargs(),
                    ),
                    what=f"Uploading new version of '{name}'",
                )
            else:
                response = self._execute(
                    self.service.files().create(
                        body={"name": name, "parents": [parent_id]},
                        media_body=media,
                        fields=FILE_FIELDS,
                        **self._write_kwargs(),
                    ),
                    what=f"Uploading '{name}'",
                )

            file_id = (response or {}).get("id")
            if not file_id:
                result.status = "failed"
                result.error = (
                    "Drive accepted the request but returned no file ID; "
                    "treating the upload as unverified."
                )
                return result

            result.file_id = file_id
            result.web_view_link = response.get("webViewLink")
            result.size_remote = _int_or_none(response.get("size"))
            result.md5_remote = response.get("md5Checksum")

            result.verified = self._verify(result, verify)
            if result.verified:
                result.status = "uploaded"
                result.uploaded_at = _now()
                self._apply_on_verified(local_path, result)
            else:
                result.status = "verification_failed"
        except DriveNotConfiguredError:
            raise
        except DriveUploadError as exc:
            result.status = "failed"
            result.error = str(exc)
        except Exception as exc:  # noqa: BLE001 - an upload must never crash the run
            result.status = "failed"
            result.error = f"{type(exc).__name__}: {exc}"

        if not result.verified:
            LOG.warning(f"Upload not verified for '{name}': {result.error or result.status}")
        return result

    def _build_media(self, local_path: Path) -> Any:
        """Build a simple or resumable ``MediaFileUpload`` for *local_path*."""
        from googleapiclient.http import MediaFileUpload

        policy = self.settings.upload
        threshold = int(policy.get("resumable_threshold_bytes", 5 * 1024 * 1024))
        chunk = int(policy.get("chunk_size_bytes", 5 * 1024 * 1024))
        size = local_path.stat().st_size
        resumable = size >= threshold
        kwargs: dict[str, Any] = {
            "mimetype": self.guess_mime_type(local_path),
            "resumable": resumable,
        }
        if resumable:
            kwargs["chunksize"] = chunk
        return MediaFileUpload(str(local_path), **kwargs)

    def _verify(self, result: UploadResult, verify: dict[str, Any]) -> bool:
        """Run the configured post-upload checks against Drive's own metadata."""
        if verify.get("require_file_id", True) and not result.file_id:
            result.error = "Drive returned no file ID."
            return False

        if verify.get("reread_metadata", True) and result.file_id:
            try:
                fresh = self.get_file(result.file_id)
            except DriveError as exc:
                result.error = f"Could not re-read Drive metadata after upload: {exc}"
                return False
            result.size_remote = _int_or_none(fresh.get("size")) or result.size_remote
            result.md5_remote = fresh.get("md5Checksum") or result.md5_remote
            result.web_view_link = fresh.get("webViewLink") or result.web_view_link

        if verify.get("require_web_view_link", True) and not result.web_view_link:
            result.error = "Drive returned no webViewLink for the uploaded file."
            return False

        if verify.get("compare_size", True):
            if result.size_remote is None:
                result.error = "Drive reported no file size; size could not be verified."
                return False
            if result.size_local is not None and result.size_remote != result.size_local:
                result.error = (
                    f"Size mismatch: local {result.size_local} bytes, "
                    f"Drive {result.size_remote} bytes."
                )
                return False

        if verify.get("compare_md5", True) and result.md5_local and result.md5_remote:
            if result.md5_local.lower() != result.md5_remote.lower():
                result.error = (
                    f"Checksum mismatch: local MD5 {result.md5_local}, "
                    f"Drive MD5 {result.md5_remote}."
                )
                return False

        result.error = ""
        return True

    def _apply_on_verified(self, local_path: Path, result: UploadResult) -> None:
        """Honour the ``upload.on_verified`` policy for the staging copy."""
        policy = str(self.settings.upload.get("on_verified", "keep")).lower()
        if policy != "delete":
            result.local_retained = True
            return
        try:
            local_path.unlink()
            result.local_retained = False
            LOG.info(f"Staging copy removed after verified upload: {local_path.name}")
        except OSError as exc:
            result.local_retained = True
            LOG.warning(f"Could not remove staging copy {local_path}: {exc}")

    def about(self) -> dict[str, Any]:
        """Return the authenticated account's Drive identity (a live check)."""
        return self._execute(
            self.service.about().get(fields="user(displayName, emailAddress), storageQuota"),
            what="Reading Drive account information",
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _http_status_of(exc: Exception) -> int | None:
    """Extract an HTTP status code from a Google API exception, if present."""
    response = getattr(exc, "resp", None)
    status = getattr(response, "status", None)
    if status is not None:
        try:
            return int(status)
        except (TypeError, ValueError):
            return None
    status_code = getattr(exc, "status_code", None)
    if status_code is not None:
        try:
            return int(status_code)
        except (TypeError, ValueError):
            return None
    return None


def _int_or_none(value: Any) -> int | None:
    """Coerce Drive's string-typed numeric fields to ``int``."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _now() -> str:
    """Current UTC timestamp (imported lazily to keep this module import-light)."""
    from .utils import utc_now_iso

    return utc_now_iso()
