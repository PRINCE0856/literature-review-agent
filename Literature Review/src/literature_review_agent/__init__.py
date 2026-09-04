"""Literature Review Agent: a resumable, verification-first literature-review pipeline.

The public surface is deliberately small. Most work goes through
:class:`~literature_review_agent.job_manager.Job` and
:class:`~literature_review_agent.orchestrator.Orchestrator`, which is also what
the notebook uses, so the notebook holds no research logic of its own.

Legal boundary: PDFs are retrieved only from legitimate open-access locations or
from a direct PDF URL that a publisher's own API has identified as authorised.
Paywalls, institutional logins, CAPTCHAs, and anti-bot protections are never
bypassed, and unauthorised mirrors are never contacted.
"""

from __future__ import annotations

__version__ = "1.0.0"
__all__ = [
    "__version__",
    "Job",
    "JobConfig",
    "Orchestrator",
    "PaperRecord",
    "PaperAnalysis",
    "Settings",
    "StageName",
    "StorageManager",
    "find_project_root",
    "load_settings",
]

from .config import Settings, find_project_root, load_settings
from .job_manager import Job
from .orchestrator import Orchestrator
from .schemas import JobConfig, PaperAnalysis, PaperRecord, StageName
from .storage import StorageManager
