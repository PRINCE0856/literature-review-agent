"""Google Drive storage with a fully mocked Drive API.

No test here contacts Google. A fake ``files`` resource stands in for the Drive
v3 service, which lets the suite prove the behaviour that matters most: nothing
is ever reported as uploaded unless Drive returned a file ID and the
verification checks passed.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from literature_review_agent.config import Settings, load_settings
from literature_review_agent.drive_storage import (
    FOLDER_MIME,
    DriveClient,
    DriveNotConfiguredError,
    describe_drive_readiness,
    md5_of_file,
)
from literature_review_agent.job_manager import Job
from literature_review_agent.storage import DESTINATIONS, StorageManager


# ---------------------------------------------------------------------------
# A fake Drive service
# ---------------------------------------------------------------------------


class FakeRequest:
    """A Drive request object whose ``execute`` returns a canned result."""

    def __init__(self, result: Any, *, error: Exception | None = None) -> None:
        self._result = result
        self._error = error
        self.executed = 0

    def execute(self) -> Any:
        """Return the canned result, or raise the canned error."""
        self.executed += 1
        if self._error is not None:
            raise self._error
        return self._result


class FakeFiles:
    """An in-memory stand-in for the Drive ``files()`` resource."""

    def __init__(self, owner: FakeDriveService) -> None:
        self.owner = owner

    def list(self, **kwargs: Any) -> FakeRequest:
        """Answer a name/parent query from the in-memory store."""
        self.owner.calls.append(("list", kwargs))
        query = kwargs.get("q", "")
        matches = []
        for item in self.owner.items.values():
            name_ok = f"name = '{item['name']}'" in query
            parent_ok = any(
                f"'{parent}' in parents" in query for parent in item.get("parents", [])
            )
            folder_wanted = f"mimeType = '{FOLDER_MIME}'" in query
            is_folder = item["mimeType"] == FOLDER_MIME
            if name_ok and parent_ok and (is_folder == folder_wanted or not folder_wanted):
                matches.append(item)
        return FakeRequest({"files": matches})

    def create(self, **kwargs: Any) -> FakeRequest:
        """Create a folder or file, honouring the configured failure modes."""
        self.owner.calls.append(("create", kwargs))
        if self.owner.create_error is not None:
            raise self.owner.create_error

        body = kwargs.get("body", {})
        media = kwargs.get("media_body")
        self.owner.counter += 1
        file_id = f"id-{self.owner.counter}"

        item: dict[str, Any] = {
            "id": file_id,
            "name": body.get("name", ""),
            "mimeType": body.get("mimeType", "application/octet-stream"),
            "parents": body.get("parents", []),
            "webViewLink": f"https://drive.google.com/file/d/{file_id}/view",
        }
        if media is not None:
            path = Path(media._filename)
            item["mimeType"] = body.get("mimeType") or "application/pdf"
            item["size"] = str(
                self.owner.reported_size
                if self.owner.reported_size is not None
                else path.stat().st_size
            )
            item["md5Checksum"] = (
                self.owner.reported_md5
                if self.owner.reported_md5 is not None
                else hashlib.md5(path.read_bytes()).hexdigest()  # noqa: S324
            )
        if self.owner.omit_file_id:
            self.owner.items[file_id] = item
            return FakeRequest({k: v for k, v in item.items() if k != "id"})
        if self.owner.omit_web_view_link:
            item.pop("webViewLink", None)

        self.owner.items[file_id] = item
        return FakeRequest(dict(item))

    def update(self, **kwargs: Any) -> FakeRequest:
        """Replace the content of an existing file."""
        self.owner.calls.append(("update", kwargs))
        file_id = kwargs["fileId"]
        item = self.owner.items[file_id]
        media = kwargs.get("media_body")
        if media is not None:
            path = Path(media._filename)
            item["size"] = str(path.stat().st_size)
            item["md5Checksum"] = hashlib.md5(path.read_bytes()).hexdigest()  # noqa: S324
        self.owner.revisions[file_id] = self.owner.revisions.get(file_id, 1) + 1
        return FakeRequest(dict(item))

    def get(self, **kwargs: Any) -> FakeRequest:
        """Re-read a file's metadata, as the verification step does."""
        self.owner.calls.append(("get", kwargs))
        if self.owner.get_error is not None:
            raise self.owner.get_error
        return FakeRequest(dict(self.owner.items[kwargs["fileId"]]))


class FakeAbout:
    """Stand-in for the Drive ``about()`` resource."""

    def get(self, **kwargs: Any) -> FakeRequest:
        """Return a fixed account identity."""
        return FakeRequest({"user": {"emailAddress": "researcher@example.org"}})


class FakeDriveService:
    """A configurable in-memory Drive service."""

    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}
        self.revisions: dict[str, int] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.counter = 0
        #: Failure switches used to prove the verification really verifies.
        self.create_error: Exception | None = None
        self.get_error: Exception | None = None
        self.omit_file_id = False
        self.omit_web_view_link = False
        self.reported_size: int | None = None
        self.reported_md5: str | None = None

    def files(self) -> FakeFiles:
        """The ``files()`` resource."""
        return FakeFiles(self)

    def about(self) -> FakeAbout:
        """The ``about()`` resource."""
        return FakeAbout()

    # -- inspection helpers --------------------------------------------

    def folder_names(self) -> set[str]:
        """Every folder name created."""
        return {i["name"] for i in self.items.values() if i["mimeType"] == FOLDER_MIME}

    def file_names(self) -> set[str]:
        """Every non-folder name created."""
        return {i["name"] for i in self.items.values() if i["mimeType"] != FOLDER_MIME}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def drive_settings(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Settings with Drive switched on and a dummy credential file in place."""
    monkeypatch.setenv("GOOGLE_DRIVE_ENABLED", "true")
    secrets = workspace / "secrets"
    secrets.mkdir(exist_ok=True)
    (secrets / "credentials.json").write_text('{"installed": {}}', encoding="utf-8")
    return load_settings(workspace)


@pytest.fixture
def service() -> FakeDriveService:
    """A fresh fake Drive service."""
    return FakeDriveService()


@pytest.fixture
def client(drive_settings: Settings, service: FakeDriveService) -> DriveClient:
    """A Drive client wired to the fake service."""
    return DriveClient(drive_settings, service=service)


@pytest.fixture
def drive_job(drive_settings: Settings) -> Job:
    """A job in the Drive-enabled workspace."""
    return Job.create("Rainfall and urban travel", settings=drive_settings)


@pytest.fixture
def manager(
    drive_settings: Settings, drive_job: Job, client: DriveClient
) -> StorageManager:
    """A storage manager using the fake Drive client."""
    return StorageManager(
        drive_settings,
        drive_job.paths,
        job_date=drive_job.config.job_date,
        topic_slug=drive_job.config.topic_slug,
        drive_client=client,
        enable_drive=True,
    )


@pytest.fixture
def artefact(tmp_path: Path) -> Path:
    """A small local artefact to upload."""
    path = tmp_path / "Introduction.docx"
    path.write_bytes(b"a synthesis document" * 200)
    return path


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------


class TestReadiness:
    """Readiness is reported without authenticating or touching the network."""

    def test_disabled_drive_is_reported_as_disabled(self, workspace_settings: Settings) -> None:
        readiness = describe_drive_readiness(workspace_settings)
        assert readiness.enabled is False
        assert readiness.ready is False
        assert "disabled" in readiness.summary()

    def test_missing_credentials_gives_actionable_steps(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GOOGLE_DRIVE_ENABLED", "true")
        readiness = describe_drive_readiness(load_settings(workspace))
        assert readiness.enabled is True
        assert readiness.ready is False
        assert any("client-secret file is missing" in p for p in readiness.problems)
        assert readiness.setup_steps
        assert any("Google Cloud Console" in s for s in readiness.setup_steps)
        assert any("drive-login" in s for s in readiness.setup_steps)

    def test_credentials_present_becomes_ready(self, drive_settings: Settings) -> None:
        readiness = describe_drive_readiness(drive_settings)
        assert readiness.credentials_present is True
        assert readiness.ready is True

    def test_shared_drive_without_an_id_is_not_ready(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GOOGLE_DRIVE_ENABLED", "true")
        config_path = workspace / "config" / "google_drive.yaml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                "location: my_drive", "location: shared_drive"
            ),
            encoding="utf-8",
        )
        (workspace / "secrets").mkdir(exist_ok=True)
        (workspace / "secrets" / "credentials.json").write_text("{}", encoding="utf-8")
        readiness = describe_drive_readiness(load_settings(workspace))
        assert readiness.ready is False
        assert any("Shared Drive ID" in p for p in readiness.problems)

    def test_no_secret_is_ever_included_in_the_report(self, drive_settings: Settings) -> None:
        # The report may name paths, never contents.
        readiness = describe_drive_readiness(drive_settings)
        blob = readiness.summary() + " ".join(readiness.setup_steps)
        assert "installed" not in blob
        assert "client_secret" not in blob.replace("client-secret", "")

    def test_authenticating_without_credentials_raises_with_steps(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GOOGLE_DRIVE_ENABLED", "true")
        client = DriveClient(load_settings(workspace))
        with pytest.raises(DriveNotConfiguredError) as info:
            _ = client.service
        assert info.value.steps


# ---------------------------------------------------------------------------
# Folder tree
# ---------------------------------------------------------------------------


class TestFolderTree:
    """The Drive folder structure mirrors the local layout."""

    def test_creates_the_root_folder(self, client: DriveClient, service: FakeDriveService) -> None:
        root_id = client.ensure_root_folder()
        assert root_id
        assert "Literature Review" in service.folder_names()

    def test_root_folder_is_reused_not_duplicated(
        self, client: DriveClient, service: FakeDriveService
    ) -> None:
        first = client.ensure_root_folder()
        client._folder_cache.clear()
        assert client.ensure_root_folder() == first
        assert len([n for n in service.folder_names() if n == "Literature Review"]) == 1

    def test_creates_the_complete_job_tree(
        self, client: DriveClient, service: FakeDriveService
    ) -> None:
        tree = client.ensure_job_tree("2026-09-04", "rainfall-travel")
        names = service.folder_names()
        for folder in (
            "Literature Review", "01 Keywords", "02 Literature Papers", "03 Reports",
            "04 Verification", "05 Logs and State", "2026-09-04", "rainfall-travel",
            "Downloaded Papers", "Extracted Text", "Unable to Download",
        ):
            assert folder in names, f"missing Drive folder: {folder}"

        for key in (
            "01 Keywords", "02 Literature Papers", "03 Reports", "04 Verification",
            "05 Logs and State", "02 Literature Papers/Downloaded Papers",
            "02 Literature Papers/Extracted Text",
            "02 Literature Papers/Unable to Download",
        ):
            assert key in tree, f"missing tree key: {key}"

    def test_job_folders_are_nested_by_date_then_topic(
        self, client: DriveClient, service: FakeDriveService
    ) -> None:
        tree = client.ensure_job_tree("2026-09-04", "rainfall-travel")
        reports_id = tree["03 Reports"]
        slug_folder = service.items[reports_id]
        assert slug_folder["name"] == "rainfall-travel"
        date_folder = service.items[slug_folder["parents"][0]]
        assert date_folder["name"] == "2026-09-04"
        top_folder = service.items[date_folder["parents"][0]]
        assert top_folder["name"] == "03 Reports"

    def test_ensure_path_is_idempotent(
        self, client: DriveClient, service: FakeDriveService
    ) -> None:
        first = client.ensure_path(["03 Reports", "2026-09-04", "slug"])
        before = len(service.items)
        client._folder_cache.clear()
        assert client.ensure_path(["03 Reports", "2026-09-04", "slug"]) == first
        assert len(service.items) == before

    def test_shared_drive_adds_the_required_arguments(
        self, drive_settings: Settings, service: FakeDriveService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GOOGLE_SHARED_DRIVE_ID", "drive-xyz")
        config_path = drive_settings.root / "config" / "google_drive.yaml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                "location: my_drive", "location: shared_drive"
            ),
            encoding="utf-8",
        )
        client = DriveClient(load_settings(drive_settings.root), service=service)
        assert client.is_shared_drive is True
        assert client.root_parent_id() == "drive-xyz"
        client.ensure_root_folder()
        create_calls = [kwargs for name, kwargs in service.calls if name == "create"]
        assert create_calls and create_calls[0].get("supportsAllDrives") is True


# ---------------------------------------------------------------------------
# Upload and verification
# ---------------------------------------------------------------------------


class TestUploadVerification:
    """An upload counts only when Drive confirms it."""

    def test_successful_upload_is_verified_with_an_id_and_link(
        self, client: DriveClient, artefact: Path
    ) -> None:
        parent = client.ensure_root_folder()
        result = client.upload_file(artefact, parent)
        assert result.verified is True
        assert result.status == "uploaded"
        assert result.file_id
        assert result.web_view_link and result.web_view_link.startswith("https://")
        assert result.size_remote == result.size_local
        assert result.md5_remote == result.md5_local
        assert result.uploaded_at

    def test_verification_re_reads_metadata_from_drive(
        self, client: DriveClient, service: FakeDriveService, artefact: Path
    ) -> None:
        parent = client.ensure_root_folder()
        client.upload_file(artefact, parent)
        assert any(name == "get" for name, _ in service.calls), (
            "verification must independently re-read the file from Drive"
        )

    def test_missing_file_id_is_not_treated_as_success(
        self, client: DriveClient, service: FakeDriveService, artefact: Path
    ) -> None:
        parent = client.ensure_root_folder()
        # Set the switch only after the folders exist, so it affects the upload
        # rather than the folder creation that precedes it.
        service.omit_file_id = True
        result = client.upload_file(artefact, parent)
        assert result.verified is False
        assert result.file_id is None
        assert "no file ID" in result.error

    def test_missing_web_view_link_fails_verification(
        self, client: DriveClient, service: FakeDriveService, artefact: Path
    ) -> None:
        parent = client.ensure_root_folder()
        service.omit_web_view_link = True
        result = client.upload_file(artefact, parent)
        assert result.verified is False
        assert result.status == "verification_failed"

    def test_size_mismatch_fails_verification(
        self, client: DriveClient, service: FakeDriveService, artefact: Path
    ) -> None:
        parent = client.ensure_root_folder()
        service.reported_size = 7
        result = client.upload_file(artefact, parent)
        assert result.verified is False
        assert "Size mismatch" in result.error

    def test_checksum_mismatch_fails_verification(
        self, client: DriveClient, service: FakeDriveService, artefact: Path
    ) -> None:
        parent = client.ensure_root_folder()
        service.reported_md5 = "0" * 32
        result = client.upload_file(artefact, parent)
        assert result.verified is False
        assert "Checksum mismatch" in result.error

    def test_upload_error_is_captured_not_raised(
        self, client: DriveClient, service: FakeDriveService, artefact: Path
    ) -> None:
        parent = client.ensure_root_folder()
        service.create_error = RuntimeError("network went away")
        result = client.upload_file(artefact, parent)
        assert result.verified is False
        assert result.status == "failed"
        assert "network went away" in result.error

    def test_local_copy_is_retained_on_failure(
        self, client: DriveClient, service: FakeDriveService, artefact: Path
    ) -> None:
        parent = client.ensure_root_folder()
        service.create_error = RuntimeError("boom")
        client.upload_file(artefact, parent)
        assert artefact.exists(), "a failed upload must never delete the local copy"

    def test_missing_local_file_is_reported(self, client: DriveClient, tmp_path: Path) -> None:
        parent = client.ensure_root_folder()
        result = client.upload_file(tmp_path / "nope.docx", parent)
        assert result.verified is False
        assert result.status == "missing_local_file"

    def test_local_copy_is_kept_by_default(self, client: DriveClient, artefact: Path) -> None:
        parent = client.ensure_root_folder()
        result = client.upload_file(artefact, parent)
        assert result.local_retained is True
        assert artefact.exists()

    def test_existing_file_is_uploaded_as_a_new_version(
        self, client: DriveClient, service: FakeDriveService, artefact: Path
    ) -> None:
        parent = client.ensure_root_folder()
        first = client.upload_file(artefact, parent)
        artefact.write_bytes(b"revised content" * 300)
        second = client.upload_file(artefact, parent)
        assert second.verified is True
        assert second.file_id == first.file_id, "a redeploy must reuse the same Drive file"
        assert service.revisions.get(first.file_id, 0) >= 2

    def test_mime_type_is_set_from_configuration(
        self, client: DriveClient, tmp_path: Path
    ) -> None:
        assert client.guess_mime_type(tmp_path / "x.pdf") == "application/pdf"
        assert "wordprocessingml" in client.guess_mime_type(tmp_path / "x.docx")
        assert "spreadsheetml" in client.guess_mime_type(tmp_path / "x.xlsx")
        assert client.guess_mime_type(tmp_path / "x.csv") == "text/csv"

    def test_md5_helper_matches_hashlib(self, artefact: Path) -> None:
        assert md5_of_file(artefact) == hashlib.md5(artefact.read_bytes()).hexdigest()  # noqa: S324


# ---------------------------------------------------------------------------
# Storage manager
# ---------------------------------------------------------------------------


class TestStorageManager:
    """The staging-to-Drive router and its manifest."""

    def test_publishes_to_the_matching_drive_folder(
        self, manager: StorageManager, service: FakeDriveService, artefact: Path
    ) -> None:
        outcome = manager.publish(artefact, "reports")
        assert outcome.verified is True
        assert outcome.file_id
        assert outcome.web_view_link
        assert "Introduction.docx" in service.file_names()

    def test_manifest_records_the_id_and_link(
        self, manager: StorageManager, artefact: Path
    ) -> None:
        manager.publish(artefact, "reports")
        entry = manager.artefact_entry(artefact)
        assert entry is not None
        assert entry["verified"] is True
        assert entry["file_id"]
        assert entry["web_view_link"]
        assert entry["destination"] == "reports"

    def test_manifest_is_written_to_disk(
        self, manager: StorageManager, artefact: Path
    ) -> None:
        manager.publish(artefact, "reports")
        assert manager.manifest_path.exists()
        assert "drive_manifest.json" == manager.manifest_path.name

    def test_status_is_never_overstated_when_drive_is_off(
        self, workspace_settings: Settings, drive_job: Job, artefact: Path
    ) -> None:
        manager = StorageManager(
            workspace_settings,
            drive_job.paths,
            job_date=drive_job.config.job_date,
            topic_slug=drive_job.config.topic_slug,
            enable_drive=False,
        )
        outcome = manager.publish(artefact, "reports")
        assert outcome.verified is False
        assert outcome.file_id is None
        assert "local only" in outcome.status
        assert manager.summary()["artefacts_uploaded_and_verified"] == 0

    def test_failed_upload_stays_pending_and_retryable(
        self, manager: StorageManager, service: FakeDriveService, artefact: Path
    ) -> None:
        manager.ensure_remote_tree()
        service.create_error = RuntimeError("temporary outage")
        outcome = manager.publish(artefact, "reports")
        assert outcome.verified is False
        assert artefact.exists()
        assert len(manager.pending_uploads()) == 1

        # The outage clears; the retry must now succeed.
        service.create_error = None
        retried = manager.retry_pending()
        assert len(retried) == 1
        assert retried[0].verified is True
        assert manager.pending_uploads() == []

    def test_retry_counts_accumulate_across_attempts(
        self, manager: StorageManager, service: FakeDriveService, artefact: Path
    ) -> None:
        manager.ensure_remote_tree()
        service.create_error = RuntimeError("outage")
        manager.publish(artefact, "reports")
        service.create_error = None
        manager.retry_pending()
        assert manager.artefact_entry(artefact)["attempts"] >= 2

    def test_unchanged_artefact_is_not_re_uploaded(
        self, manager: StorageManager, service: FakeDriveService, artefact: Path
    ) -> None:
        manager.publish(artefact, "reports")
        create_calls = sum(1 for name, _ in service.calls if name == "create")
        manager.publish(artefact, "reports")
        assert sum(1 for name, _ in service.calls if name == "create") == create_calls

    def test_changed_artefact_is_re_uploaded(
        self, manager: StorageManager, service: FakeDriveService, artefact: Path
    ) -> None:
        manager.publish(artefact, "reports")
        artefact.write_bytes(b"a regenerated document" * 400)
        outcome = manager.publish(artefact, "reports")
        assert outcome.verified is True
        assert any(name == "update" for name, _ in service.calls)

    def test_force_republishes(
        self, manager: StorageManager, service: FakeDriveService, artefact: Path
    ) -> None:
        manager.publish(artefact, "reports")
        before = len(service.calls)
        manager.publish(artefact, "reports", force=True)
        assert len(service.calls) > before

    def test_every_destination_maps_to_a_drive_folder(
        self, manager: StorageManager
    ) -> None:
        tree = manager.ensure_remote_tree()
        assert tree is not None
        for destination, folder_key in DESTINATIONS.items():
            assert folder_key in tree, f"{destination} has no Drive folder"

    def test_unknown_destination_is_rejected(
        self, manager: StorageManager, artefact: Path
    ) -> None:
        with pytest.raises(KeyError):
            manager.publish(artefact, "not-a-destination")

    def test_summary_lists_verified_links_only(
        self, manager: StorageManager, service: FakeDriveService, artefact: Path,
        tmp_path: Path,
    ) -> None:
        manager.publish(artefact, "reports")
        failing = tmp_path / "Research_Gaps.docx"
        failing.write_bytes(b"gaps" * 300)
        service.create_error = RuntimeError("outage")
        manager.publish(failing, "reports")

        summary = manager.summary()
        assert summary["artefacts_tracked"] == 2
        assert summary["artefacts_uploaded_and_verified"] == 1
        assert summary["artefacts_pending_upload"] == 1
        assert len(summary["verified_links"]) == 1
        assert summary["verified_links"][0]["filename"] == "Introduction.docx"

    def test_manifest_survives_a_reload(
        self, drive_settings: Settings, drive_job: Job, client: DriveClient, artefact: Path
    ) -> None:
        first = StorageManager(
            drive_settings, drive_job.paths, job_date=drive_job.config.job_date,
            topic_slug=drive_job.config.topic_slug, drive_client=client, enable_drive=True,
        )
        first.publish(artefact, "reports")

        second = StorageManager(
            drive_settings, drive_job.paths, job_date=drive_job.config.job_date,
            topic_slug=drive_job.config.topic_slug, drive_client=client, enable_drive=True,
        )
        assert second.artefact_entry(artefact) is not None
        assert second.summary()["artefacts_uploaded_and_verified"] == 1

    def test_publish_many(
        self, manager: StorageManager, tmp_path: Path
    ) -> None:
        paths = []
        for name in ("Introduction.docx", "Research_Gaps.docx", "Paper_Summaries.docx"):
            path = tmp_path / name
            path.write_bytes(b"content" * 300)
            paths.append(path)
        outcomes = manager.publish_many(paths, "reports")
        assert len(outcomes) == 3
        assert all(o.verified for o in outcomes)

    def test_unusable_drive_falls_back_to_local_without_crashing(
        self, workspace: Path, drive_job: Job, monkeypatch: pytest.MonkeyPatch,
        artefact: Path,
    ) -> None:
        # Drive is requested but has no credentials: the run must continue locally.
        monkeypatch.setenv("GOOGLE_DRIVE_ENABLED", "true")
        (workspace / "secrets" / "credentials.json").unlink(missing_ok=True)
        settings = load_settings(workspace)
        manager = StorageManager(
            settings, drive_job.paths, job_date=drive_job.config.job_date,
            topic_slug=drive_job.config.topic_slug,
        )
        assert manager.drive_enabled is False
        outcome = manager.publish(artefact, "reports")
        assert outcome.verified is False
        assert artefact.exists()
