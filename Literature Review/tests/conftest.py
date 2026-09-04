"""Shared fixtures. No test in this suite touches the live network."""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path
from typing import Any

import pytest

from literature_review_agent.config import Settings, load_settings
from literature_review_agent.job_manager import Job
from literature_review_agent.schemas import PaperRecord

FIXTURES = Path(__file__).parent / "fixtures"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session", autouse=True)
def _ensure_pdf_fixtures() -> None:
    """Generate the PDF fixtures if they are absent.

    ``.gitignore`` deliberately excludes ``*.pdf`` so no binary output is ever
    committed, which means a fresh clone has the generator script but not the
    PDFs. Building them here keeps the suite runnable straight after cloning.
    """
    expected = [
        FIXTURES / "rainfall_delhi.pdf",
        FIXTURES / "monsoon_mumbai.pdf",
        FIXTURES / "scanned_paper.pdf",
    ]
    if all(path.exists() for path in expected):
        return

    generator = FIXTURES / "make_fixtures.py"
    if not generator.exists():  # pragma: no cover - the script is version-controlled
        pytest.skip("tests/fixtures/make_fixtures.py is missing; cannot build PDF fixtures")

    spec = importlib.util.spec_from_file_location("make_fixtures", generator)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    module.main(str(FIXTURES))

    # The HTML-disguised-as-PDF fixture is plain text, so build it here.
    paywall = FIXTURES / "paywall_page.pdf"
    if not paywall.exists():
        paywall.write_text(
            "<!DOCTYPE html>\n<html><head><title>Log in to continue</title></head>\n"
            "<body><h1>Access denied</h1>\n"
            "<p>Please sign in with your institutional credentials to view this "
            "article.</p>\n<p>" + "x" * 1400 + "</p>\n</body></html>\n",
            encoding="utf-8",
        )


@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every credential from the environment.

    Guarantees the suite behaves identically on a machine with real API keys and
    on a clean CI runner, and that no test can accidentally reach a live service.
    """
    for name in (
        "OPENALEX_API_KEY", "SEMANTIC_SCHOLAR_API_KEY", "CORE_API_KEY",
        "ELSEVIER_API_KEY", "ELSEVIER_INSTTOKEN", "SPRINGER_API_KEY",
        "ANTHROPIC_API_KEY", "GOOGLE_DRIVE_CREDENTIALS_FILE",
        "GOOGLE_DRIVE_TOKEN_FILE", "GOOGLE_SERVICE_ACCOUNT_FILE",
        "GOOGLE_SHARED_DRIVE_ID", "GOOGLE_DRIVE_ROOT_FOLDER_ID",
        "LITERATURE_REVIEW_ROOT",
    ):
        monkeypatch.delenv(name, raising=False)
    # Drive is off by default in tests; the Drive tests enable it explicitly.
    monkeypatch.setenv("GOOGLE_DRIVE_ENABLED", "false")
    monkeypatch.setenv("LITERATURE_REVIEW_CONTACT_EMAIL", "test@example.org")


@pytest.fixture
def project_root() -> Path:
    """The real project root, used for its configuration files."""
    return PROJECT_ROOT


@pytest.fixture
def settings(project_root: Path) -> Settings:
    """Settings loaded from the project's own configuration."""
    return load_settings(project_root)


@pytest.fixture
def workspace(tmp_path: Path, project_root: Path) -> Path:
    """A throwaway project directory with real config and empty output folders."""
    shutil.copytree(project_root / "config", tmp_path / "config")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='test'\n", encoding="utf-8")
    for name in (
        "01 Keywords", "02 Literature Papers", "03 Reports", "04 Verification",
        "05 Logs and State",
    ):
        (tmp_path / name).mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture
def workspace_settings(workspace: Path) -> Settings:
    """Settings rooted at the throwaway workspace."""
    return load_settings(workspace)


@pytest.fixture
def job(workspace_settings: Settings) -> Job:
    """A job created inside the throwaway workspace."""
    return Job.create(
        "Effect of rainfall on urban travel behaviour",
        settings=workspace_settings,
        research_questions=[
            "How does rainfall intensity influence mode choice and daily travel?"
        ],
        maximum_papers=10,
    )


@pytest.fixture
def sample_records() -> list[PaperRecord]:
    """Two realistic, distinct paper records."""
    return [
        PaperRecord(
            record_id="rec-delhi",
            title="Rainfall Intensity and Mode Choice in Delhi",
            authors=["Sharma, Ravi", "Patel, Neha"],
            year=2021,
            journal="Transportation Research Part A",
            volume="150",
            issue="2",
            pages="45-61",
            doi="10.1016/j.tra.2021.01.001",
            issn="0965-8564",
            publisher="Elsevier",
            abstract=(
                "This study examines how rainfall intensity influences mode choice in "
                "Delhi using a mixed logit model."
            ),
            document_type="journal-article",
            open_access_status="gold",
            landing_page_url="https://doi.org/10.1016/j.tra.2021.01.001",
            discovery_source="Crossref",
            selected=True,
        ),
        PaperRecord(
            record_id="rec-mumbai",
            title="Monsoon Rainfall and Transit Ridership in Mumbai",
            authors=["Iyer, Anita"],
            year=2019,
            journal="Journal of Transport Geography",
            doi="10.1016/j.jtrangeo.2019.02.002",
            issn="0966-6923",
            abstract="This paper investigates monsoon rainfall and suburban rail ridership.",
            document_type="journal-article",
            open_access_status="green",
            discovery_source="OpenAlex",
            selected=True,
        ),
    ]


@pytest.fixture
def fixtures_dir() -> Path:
    """Directory holding the PDF fixtures."""
    return FIXTURES


@pytest.fixture
def real_pdf(fixtures_dir: Path) -> Path:
    """A valid, text-bearing five-page PDF."""
    return fixtures_dir / "rainfall_delhi.pdf"


@pytest.fixture
def second_pdf(fixtures_dir: Path) -> Path:
    """A second valid PDF."""
    return fixtures_dir / "monsoon_mumbai.pdf"


@pytest.fixture
def scanned_pdf(fixtures_dir: Path) -> Path:
    """A valid PDF with no text layer, which must be flagged for OCR."""
    return fixtures_dir / "scanned_paper.pdf"


@pytest.fixture
def html_disguised_as_pdf(fixtures_dir: Path) -> Path:
    """An HTML login page saved with a .pdf extension."""
    return fixtures_dir / "paywall_page.pdf"


@pytest.fixture
def crossref_payload() -> dict[str, Any]:
    """A minimal Crossref ``/works`` search response."""
    return {
        "status": "ok",
        "message": {
            "total-results": 1,
            "items": [
                {
                    "DOI": "10.1016/j.tra.2021.01.001",
                    "title": ["Rainfall Intensity and Mode Choice in Delhi"],
                    "author": [
                        {"family": "Sharma", "given": "Ravi"},
                        {"family": "Patel", "given": "Neha"},
                    ],
                    "issued": {"date-parts": [[2021, 3, 1]]},
                    "container-title": ["Transportation Research Part A"],
                    "volume": "150",
                    "issue": "2",
                    "page": "45-61",
                    "ISSN": ["0965-8564", "1879-2375"],
                    "publisher": "Elsevier",
                    "abstract": "<jats:p>Rainfall reduces cycling.</jats:p>",
                    "subject": ["Transportation"],
                    "type": "journal-article",
                    "language": "en",
                    "is-referenced-by-count": 42,
                    "URL": "https://doi.org/10.1016/j.tra.2021.01.001",
                    "license": [{"URL": "https://creativecommons.org/licenses/by/4.0/"}],
                }
            ],
        },
    }


@pytest.fixture
def openalex_payload() -> dict[str, Any]:
    """A minimal OpenAlex ``/works`` response with an open-access PDF."""
    return {
        "results": [
            {
                "id": "https://openalex.org/W123",
                "doi": "https://doi.org/10.1016/j.jtrangeo.2019.02.002",
                "title": "Monsoon Rainfall and Transit Ridership in Mumbai",
                "display_name": "Monsoon Rainfall and Transit Ridership in Mumbai",
                "publication_year": 2019,
                "type": "article",
                "language": "en",
                "cited_by_count": 17,
                "authorships": [{"author": {"display_name": "Anita Iyer"}}],
                "primary_location": {
                    "landing_page_url": "https://example.org/mumbai",
                    "license": "cc-by",
                    "source": {
                        "display_name": "Journal of Transport Geography",
                        "issn_l": "0966-6923",
                        "issn": ["0966-6923"],
                        "host_organization_name": "Elsevier",
                    },
                },
                "best_oa_location": {"pdf_url": "https://europepmc.org/mumbai.pdf"},
                "locations": [{"pdf_url": "https://europepmc.org/mumbai.pdf"}],
                "open_access": {"oa_status": "green", "is_oa": True},
                "biblio": {"volume": "74", "issue": "1", "first_page": "10", "last_page": "22"},
                "abstract_inverted_index": {
                    "Monsoon": [0], "rainfall": [1], "raises": [2], "ridership": [3]
                },
                "keywords": [{"display_name": "public transport"}],
                "ids": {"doi": "10.1016/j.jtrangeo.2019.02.002", "openalex": "W123"},
            }
        ]
    }


@pytest.fixture
def ranking_csv(tmp_path: Path) -> Path:
    """A small journal-ranking file in the Scimago column layout."""
    path = tmp_path / "ranking_2021.csv"
    path.write_text(
        "Title;Issn;SJR Best Quartile;Categories;Year;Publisher\n"
        "Transportation Research Part A;09658564, 18792375;Q1;Transportation;2021;Elsevier\n"
        "Journal of Transport Geography;09666923;Q2;Geography;2021;Elsevier\n"
        "Obscure Regional Bulletin;12345678;-;Miscellaneous;2021;Local Press\n"
        "Dual Category Review;22223333;Q1;Economics;2021;Test\n"
        "Dual Category Review;22223333;Q3;Sociology;2021;Test\n",
        encoding="utf-8",
    )
    return path
