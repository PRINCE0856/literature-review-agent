"""Modular scholarly search adapters.

One adapter per source, all returning :class:`PaperRecord` objects, so the rest
of the pipeline never sees a source-specific payload. Every adapter:

* respects a per-source rate limit and the shared retry/backoff policy;
* records the query and source on each record (provenance);
* degrades to an empty result with a logged reason rather than raising, so one
  dead source cannot abort a review;
* is skipped automatically when its optional API key is absent.

Legal boundaries: Google Scholar is never scraped, unauthorised mirrors are never
contacted (enforced in :mod:`http_client`), and publisher adapters return only
metadata plus whatever open-access URL the API itself supplies.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from .config import Settings, SourceSpec
from .http_client import AccessRestrictedError, BlockedHostError, HttpClient
from .logging_setup import get_logger
from .schemas import JobConfig, KeywordStrategy, PaperRecord
from .utils import coerce_year, normalize_doi, stable_id, utc_now_iso

LOG = get_logger("search")


@dataclass
class QueryLog:
    """One executed query, for ``search_log.jsonl`` and the Excel Search Log."""

    timestamp: str
    source: str
    query: str
    breadth: str
    results: int
    http_status: int | None = None
    outcome: str = "ok"
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialise for the JSONL log."""
        return {
            "timestamp": self.timestamp,
            "source": self.source,
            "query": self.query,
            "breadth": self.breadth,
            "results": self.results,
            "http_status": self.http_status,
            "outcome": self.outcome,
            "notes": self.notes,
        }


@dataclass
class SearchOutcome:
    """Everything one search run produced."""

    records: list[PaperRecord] = field(default_factory=list)
    logs: list[QueryLog] = field(default_factory=list)
    sources_used: list[str] = field(default_factory=list)
    sources_skipped: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Base adapter
# ---------------------------------------------------------------------------


class SearchAdapter(ABC):
    """Base class for every discovery source."""

    #: Registry key, matching the name in ``search_sources.yaml``.
    name: str = ""

    def __init__(self, spec: SourceSpec, settings: Settings, *, client: HttpClient | None = None) -> None:
        self.spec = spec
        self.settings = settings
        self._client = client
        self._owns_client = client is None

    @property
    def client(self) -> HttpClient:
        """A rate-limited HTTP client tuned to this source's limits."""
        if self._client is None:
            self._client = HttpClient(
                self.settings, requests_per_second=self.spec.requests_per_second
            )
        return self._client

    def close(self) -> None:
        """Release the HTTP client if this adapter created it."""
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    @abstractmethod
    def search(self, query: str, config: JobConfig, *, limit: int) -> list[PaperRecord]:
        """Run one query against the source and return normalised records."""

    def safe_search(
        self, query: str, config: JobConfig, *, limit: int, breadth: str = "balanced"
    ) -> tuple[list[PaperRecord], QueryLog]:
        """Run :meth:`search`, converting any failure into a logged outcome."""
        try:
            records = self.search(query, config, limit=limit)
            log = QueryLog(
                timestamp=utc_now_iso(),
                source=self.spec.label,
                query=query,
                breadth=breadth,
                results=len(records),
                http_status=200,
                outcome="ok",
            )
            return records, log
        except AccessRestrictedError as exc:
            LOG.warning(f"{self.spec.label}: access restricted ({exc}).")
            return [], QueryLog(
                timestamp=utc_now_iso(),
                source=self.spec.label,
                query=query,
                breadth=breadth,
                results=0,
                http_status=exc.status,
                outcome="access restricted",
                notes="The agent does not bypass authentication or paywalls.",
            )
        except BlockedHostError as exc:
            LOG.error(f"{self.spec.label}: {exc}")
            return [], QueryLog(
                timestamp=utc_now_iso(),
                source=self.spec.label,
                query=query,
                breadth=breadth,
                results=0,
                outcome="blocked host",
                notes=str(exc),
            )
        except Exception as exc:  # noqa: BLE001 - a dead source must not stop the run
            LOG.warning(f"{self.spec.label}: query failed ({type(exc).__name__}: {exc}).")
            return [], QueryLog(
                timestamp=utc_now_iso(),
                source=self.spec.label,
                query=query,
                breadth=breadth,
                results=0,
                outcome="error",
                notes=f"{type(exc).__name__}: {exc}",
            )

    # -- shared helpers -------------------------------------------------

    def _new_record(self, **fields: Any) -> PaperRecord:
        """Build a record stamped with this adapter's provenance."""
        fields.setdefault("discovery_source", self.spec.label)
        fields.setdefault("metadata_sources", [self.spec.label])
        record = PaperRecord.model_validate(fields)
        record.record_id = record.record_id or stable_id(
            record.doi or record.normalized_title, record.year
        )
        return record

    @staticmethod
    def _year_ok(year: int | None, config: JobConfig) -> bool:
        """True when *year* falls inside the job's range (or is unknown)."""
        if year is None:
            return True
        return config.year_from <= year <= config.year_to


# ---------------------------------------------------------------------------
# Crossref
# ---------------------------------------------------------------------------


class CrossrefAdapter(SearchAdapter):
    """Crossref REST API: authoritative DOI metadata."""

    name = "crossref"

    def search(self, query: str, config: JobConfig, *, limit: int) -> list[PaperRecord]:
        """Query ``/works`` with a bibliographic query and a date filter."""
        params = {
            "query.bibliographic": query,
            "rows": min(limit, 100),
            "select": (
                "DOI,title,author,issued,container-title,volume,issue,page,ISSN,"
                "publisher,abstract,subject,type,language,is-referenced-by-count,URL,"
                "license,article-number,short-container-title"
            ),
            "filter": (
                f"from-pub-date:{config.year_from}-01-01,"
                f"until-pub-date:{config.year_to}-12-31"
            ),
            "mailto": self.settings.contact_email,
        }
        payload = self.client.get_json(f"{self.spec.base_url}/works", params=params)
        items = (payload.get("message") or {}).get("items") or []
        records = []
        for item in items:
            record = self._parse(item, query)
            if record is not None and self._year_ok(record.year, config):
                records.append(record)
        return records

    def _parse(self, item: dict[str, Any], query: str) -> PaperRecord | None:
        """Convert one Crossref work into a :class:`PaperRecord`."""
        titles = item.get("title") or []
        title = titles[0].strip() if titles else ""
        if not title:
            return None

        issued = ((item.get("issued") or {}).get("date-parts") or [[None]])[0]
        year = coerce_year(issued[0] if issued else None)
        containers = item.get("container-title") or []
        issns = item.get("ISSN") or []
        licences = item.get("license") or []

        return self._new_record(
            title=title,
            authors=[_crossref_author(a) for a in (item.get("author") or [])],
            year=year,
            journal=(containers[0] if containers else ""),
            volume=item.get("volume"),
            issue=item.get("issue"),
            pages=item.get("page"),
            article_number=item.get("article-number"),
            doi=item.get("DOI"),
            issn=(issns[0] if issns else None),
            eissn=(issns[1] if len(issns) > 1 else None),
            publisher=item.get("publisher"),
            abstract=_strip_jats(item.get("abstract", "")),
            keywords=list(item.get("subject") or []),
            document_type=item.get("type"),
            language=item.get("language"),
            citation_count=item.get("is-referenced-by-count"),
            citation_count_retrieved=utc_now_iso(),
            landing_page_url=item.get("URL"),
            licence=(licences[0].get("URL") if licences else None),
            discovery_query=query,
            external_ids={"doi": item.get("DOI", "")} if item.get("DOI") else {},
        )


def _crossref_author(author: dict[str, Any]) -> str:
    """Format a Crossref author object as ``Family, Given``."""
    family = (author.get("family") or "").strip()
    given = (author.get("given") or "").strip()
    if family and given:
        return f"{family}, {given}"
    return family or given or (author.get("name") or "").strip()


def _strip_jats(text: str) -> str:
    """Remove JATS/XML markup that Crossref embeds in abstracts."""
    if not text:
        return ""
    cleaned = re.sub(r"<[^>]+>", " ", text)
    cleaned = cleaned.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return " ".join(cleaned.split())


# ---------------------------------------------------------------------------
# OpenAlex
# ---------------------------------------------------------------------------


class OpenAlexAdapter(SearchAdapter):
    """OpenAlex: broad coverage plus explicit open-access locations."""

    name = "openalex"

    def search(self, query: str, config: JobConfig, *, limit: int) -> list[PaperRecord]:
        """Query ``/works`` with a search term and publication-date filter."""
        params = {
            "search": query,
            "per-page": min(limit, 200),
            "filter": (
                f"from_publication_date:{config.year_from}-01-01,"
                f"to_publication_date:{config.year_to}-12-31"
            ),
            "mailto": self.settings.contact_email,
        }
        if key := self.spec.api_key:
            params["api_key"] = key
        payload = self.client.get_json(f"{self.spec.base_url}/works", params=params)
        records = []
        for item in payload.get("results") or []:
            record = self._parse(item, query)
            if record is not None and self._year_ok(record.year, config):
                records.append(record)
        return records

    def _parse(self, item: dict[str, Any], query: str) -> PaperRecord | None:
        """Convert one OpenAlex work into a :class:`PaperRecord`."""
        title = (item.get("title") or item.get("display_name") or "").strip()
        if not title:
            return None

        location = item.get("primary_location") or {}
        source = location.get("source") or {}
        biblio = item.get("biblio") or {}
        open_access = item.get("open_access") or {}
        issns = source.get("issn") or []

        pdf_urls: list[str] = []
        for loc in item.get("locations") or []:
            url = loc.get("pdf_url")
            if url and url not in pdf_urls:
                pdf_urls.append(url)
        if best := (item.get("best_oa_location") or {}).get("pdf_url"):
            if best not in pdf_urls:
                pdf_urls.insert(0, best)

        pages = None
        if biblio.get("first_page") and biblio.get("last_page"):
            pages = f"{biblio['first_page']}-{biblio['last_page']}"
        elif biblio.get("first_page"):
            pages = str(biblio["first_page"])

        return self._new_record(
            title=title,
            authors=[
                (a.get("author") or {}).get("display_name", "")
                for a in (item.get("authorships") or [])
            ],
            year=item.get("publication_year"),
            journal=source.get("display_name", ""),
            volume=biblio.get("volume"),
            issue=biblio.get("issue"),
            pages=pages,
            doi=item.get("doi"),
            issn=source.get("issn_l") or (issns[0] if issns else None),
            eissn=(issns[1] if len(issns) > 1 else None),
            publisher=source.get("host_organization_name"),
            abstract=_openalex_abstract(item),
            keywords=[
                (k.get("display_name") or "")
                for k in (item.get("keywords") or item.get("concepts") or [])
            ][:12],
            document_type=item.get("type"),
            language=item.get("language"),
            citation_count=item.get("cited_by_count"),
            citation_count_retrieved=utc_now_iso(),
            landing_page_url=location.get("landing_page_url") or item.get("id"),
            open_access_status=open_access.get("oa_status"),
            licence=location.get("license"),
            pdf_url=(pdf_urls[0] if pdf_urls else None),
            candidate_pdf_urls=pdf_urls,
            discovery_query=query,
            external_ids=_openalex_ids(item),
        )


def _openalex_abstract(item: dict[str, Any]) -> str:
    """Rebuild an abstract from OpenAlex's inverted index."""
    if direct := item.get("abstract"):
        return str(direct)
    inverted = item.get("abstract_inverted_index") or {}
    if not inverted:
        return ""
    positions: list[tuple[int, str]] = []
    for word, indexes in inverted.items():
        for index in indexes or []:
            positions.append((int(index), word))
    return " ".join(word for _, word in sorted(positions))


def _openalex_ids(item: dict[str, Any]) -> dict[str, str]:
    """Extract external identifiers from an OpenAlex work."""
    ids = item.get("ids") or {}
    out: dict[str, str] = {}
    for key in ("doi", "pmid", "pmcid", "mag", "openalex"):
        if value := ids.get(key):
            out[key] = str(value)
    return out


# ---------------------------------------------------------------------------
# Semantic Scholar
# ---------------------------------------------------------------------------


class SemanticScholarAdapter(SearchAdapter):
    """Semantic Scholar Graph API: citation context and OA PDF pointers."""

    name = "semantic_scholar"

    def search(self, query: str, config: JobConfig, *, limit: int) -> list[PaperRecord]:
        """Query ``/paper/search`` restricted to the job's year range."""
        params = {
            "query": query,
            "limit": min(limit, 100),
            "year": f"{config.year_from}-{config.year_to}",
            "fields": (
                "title,abstract,year,venue,publicationVenue,externalIds,authors,"
                "citationCount,openAccessPdf,publicationTypes,journal,url,fieldsOfStudy"
            ),
        }
        headers = {}
        if key := self.spec.api_key:
            headers["x-api-key"] = key
        payload = self.client.get_json(
            f"{self.spec.base_url}/paper/search", params=params, headers=headers or None
        )
        records = []
        for item in payload.get("data") or []:
            record = self._parse(item, query)
            if record is not None and self._year_ok(record.year, config):
                records.append(record)
        return records

    def _parse(self, item: dict[str, Any], query: str) -> PaperRecord | None:
        """Convert one Semantic Scholar paper into a :class:`PaperRecord`."""
        title = (item.get("title") or "").strip()
        if not title:
            return None

        external = item.get("externalIds") or {}
        venue_obj = item.get("publicationVenue") or {}
        journal = item.get("journal") or {}
        oa_pdf = item.get("openAccessPdf") or {}
        issns = venue_obj.get("issn")
        issn_list = [issns] if isinstance(issns, str) else list(issns or [])
        types = item.get("publicationTypes") or []

        return self._new_record(
            title=title,
            authors=[(a.get("name") or "") for a in (item.get("authors") or [])],
            year=item.get("year"),
            journal=item.get("venue") or venue_obj.get("name") or journal.get("name") or "",
            volume=journal.get("volume"),
            pages=journal.get("pages"),
            doi=external.get("DOI"),
            issn=(issn_list[0] if issn_list else None),
            publisher=venue_obj.get("publisher"),
            abstract=item.get("abstract") or "",
            keywords=list(item.get("fieldsOfStudy") or [])[:10],
            document_type=(types[0] if types else None),
            citation_count=item.get("citationCount"),
            citation_count_retrieved=utc_now_iso(),
            landing_page_url=item.get("url"),
            open_access_status=("gold" if oa_pdf.get("url") else None),
            licence=oa_pdf.get("license"),
            pdf_url=oa_pdf.get("url"),
            candidate_pdf_urls=[oa_pdf["url"]] if oa_pdf.get("url") else [],
            discovery_query=query,
            external_ids={k.lower(): str(v) for k, v in external.items() if v},
        )


# ---------------------------------------------------------------------------
# Europe PMC
# ---------------------------------------------------------------------------


class EuropePMCAdapter(SearchAdapter):
    """Europe PMC REST API: life-science and health literature."""

    name = "europe_pmc"

    def search(self, query: str, config: JobConfig, *, limit: int) -> list[PaperRecord]:
        """Query the ``search`` endpoint with a year-constrained expression."""
        expression = f'({query}) AND (FIRST_PDATE:[{config.year_from} TO {config.year_to}])'
        params = {
            "query": expression,
            "format": "json",
            "pageSize": min(limit, 100),
            "resultType": "core",
        }
        payload = self.client.get_json(f"{self.spec.base_url}/search", params=params)
        results = ((payload.get("resultList") or {}).get("result")) or []
        records = []
        for item in results:
            record = self._parse(item, query)
            if record is not None and self._year_ok(record.year, config):
                records.append(record)
        return records

    def _parse(self, item: dict[str, Any], query: str) -> PaperRecord | None:
        """Convert one Europe PMC result into a :class:`PaperRecord`."""
        title = (item.get("title") or "").strip().rstrip(".")
        if not title:
            return None

        journal_info = item.get("journalInfo") or {}
        journal = journal_info.get("journal") or {}
        author_string = item.get("authorString") or ""
        authors = [a.strip() for a in author_string.split(",") if a.strip()]

        pdf_urls: list[str] = []
        for link in ((item.get("fullTextUrlList") or {}).get("fullTextUrl")) or []:
            if str(link.get("documentStyle", "")).lower() == "pdf" and link.get("url"):
                pdf_urls.append(link["url"])

        is_open = str(item.get("isOpenAccess", "N")).upper() == "Y"
        return self._new_record(
            title=title,
            authors=authors,
            year=coerce_year(item.get("pubYear")),
            journal=journal.get("title", ""),
            volume=journal_info.get("volume"),
            issue=journal_info.get("issue"),
            pages=item.get("pageInfo"),
            doi=item.get("doi"),
            issn=journal.get("issn"),
            eissn=journal.get("essn"),
            abstract=item.get("abstractText") or "",
            keywords=list((item.get("keywordList") or {}).get("keyword") or [])[:12],
            document_type=item.get("pubType"),
            language=item.get("language"),
            citation_count=item.get("citedByCount"),
            citation_count_retrieved=utc_now_iso(),
            landing_page_url=(
                f"https://europepmc.org/article/{item.get('source', 'MED')}/{item.get('id', '')}"
                if item.get("id")
                else None
            ),
            open_access_status=("gold" if is_open else "closed"),
            licence=item.get("license"),
            pdf_url=(pdf_urls[0] if pdf_urls else None),
            candidate_pdf_urls=pdf_urls,
            discovery_query=query,
            external_ids={
                k: str(v)
                for k, v in {
                    "pmid": item.get("pmid"),
                    "pmcid": item.get("pmcid"),
                    "doi": item.get("doi"),
                }.items()
                if v
            },
        )


# ---------------------------------------------------------------------------
# arXiv
# ---------------------------------------------------------------------------


class ArxivAdapter(SearchAdapter):
    """arXiv Atom API. Results are always labelled as preprints."""

    name = "arxiv"

    _NS = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}

    def search(self, query: str, config: JobConfig, *, limit: int) -> list[PaperRecord]:
        """Query the Atom API and filter to the job's year range."""
        params = {
            "search_query": f"all:{query}",
            "start": 0,
            "max_results": min(limit, 100),
            "sortBy": "relevance",
            "sortOrder": "descending",
        }
        text = self.client.get_text(self.spec.base_url, params=params)
        try:
            root = ET.fromstring(text)
        except ET.ParseError as exc:
            LOG.warning(f"arXiv returned unparseable XML: {exc}")
            return []

        records = []
        for entry in root.findall("atom:entry", self._NS):
            record = self._parse(entry, query)
            if record is not None and self._year_ok(record.year, config):
                records.append(record)
        return records

    def _parse(self, entry: ET.Element, query: str) -> PaperRecord | None:
        """Convert one arXiv Atom entry into a :class:`PaperRecord`."""
        title = _xml_text(entry, "atom:title", self._NS)
        if not title:
            return None

        published = _xml_text(entry, "atom:published", self._NS)
        doi = _xml_text(entry, "arxiv:doi", self._NS) or None
        journal_ref = _xml_text(entry, "arxiv:journal_ref", self._NS)

        pdf_url = None
        landing = None
        for link in entry.findall("atom:link", self._NS):
            if link.get("title") == "pdf" or link.get("type") == "application/pdf":
                pdf_url = link.get("href")
            elif link.get("rel") == "alternate":
                landing = link.get("href")

        authors = [
            _xml_text(a, "atom:name", self._NS)
            for a in entry.findall("atom:author", self._NS)
        ]
        categories = [
            c.get("term", "") for c in entry.findall("atom:category", self._NS) if c.get("term")
        ]

        return self._new_record(
            title=" ".join(title.split()),
            authors=[a for a in authors if a],
            year=coerce_year(published),
            journal=journal_ref or "arXiv (preprint)",
            doi=doi,
            abstract=" ".join(_xml_text(entry, "atom:summary", self._NS).split()),
            keywords=categories[:10],
            document_type="preprint",
            landing_page_url=landing or _xml_text(entry, "atom:id", self._NS),
            open_access_status="green",
            licence="arXiv non-exclusive licence (see the paper's arXiv page)",
            pdf_url=pdf_url,
            candidate_pdf_urls=[pdf_url] if pdf_url else [],
            discovery_query=query,
            notes="arXiv preprint: not peer reviewed unless a journal reference is present.",
        )


def _xml_text(element: ET.Element, path: str, namespaces: dict[str, str]) -> str:
    """Return the stripped text of a child element, or ``""``."""
    found = element.find(path, namespaces)
    return (found.text or "").strip() if found is not None and found.text else ""


# ---------------------------------------------------------------------------
# CORE
# ---------------------------------------------------------------------------


class CoreAdapter(SearchAdapter):
    """CORE v3 API: open-access repository aggregator. Requires ``CORE_API_KEY``."""

    name = "core"

    def search(self, query: str, config: JobConfig, *, limit: int) -> list[PaperRecord]:
        """POST a search to ``/search/works`` using the configured API key."""
        key = self.spec.api_key
        if not key:
            return []
        params = {
            "q": f"{query} AND yearPublished>={config.year_from} "
                 f"AND yearPublished<={config.year_to}",
            "limit": min(limit, 100),
        }
        payload = self.client.get_json(
            f"{self.spec.base_url}/search/works",
            params=params,
            headers={"Authorization": f"Bearer {key}"},
        )
        records = []
        for item in payload.get("results") or []:
            record = self._parse(item, query)
            if record is not None and self._year_ok(record.year, config):
                records.append(record)
        return records

    def _parse(self, item: dict[str, Any], query: str) -> PaperRecord | None:
        """Convert one CORE work into a :class:`PaperRecord`."""
        title = (item.get("title") or "").strip()
        if not title:
            return None
        journals = item.get("journals") or []
        journal_title = ""
        issn = None
        if journals:
            journal_title = journals[0].get("title", "")
            identifiers = journals[0].get("identifiers") or []
            issn = next((i for i in identifiers if i and "-" in str(i)), None)

        pdf_url = item.get("downloadUrl")
        return self._new_record(
            title=title,
            authors=[(a.get("name") or "") for a in (item.get("authors") or [])],
            year=coerce_year(item.get("yearPublished") or item.get("publishedDate")),
            journal=journal_title,
            doi=item.get("doi"),
            issn=issn,
            publisher=item.get("publisher"),
            abstract=item.get("abstract") or "",
            document_type=item.get("documentType"),
            language=((item.get("language") or {}) or {}).get("code"),
            landing_page_url=item.get("sourceFulltextUrls", [None])[0] or item.get("links", [{}])[0].get("url"),
            open_access_status="green",
            pdf_url=pdf_url,
            candidate_pdf_urls=[pdf_url] if pdf_url else [],
            discovery_query=query,
        )


# ---------------------------------------------------------------------------
# Elsevier
# ---------------------------------------------------------------------------


class ElsevierAdapter(SearchAdapter):
    """Elsevier Scopus Search API: metadata only.

    A PDF is never constructed for ScienceDirect. Only an explicitly
    open-access full-text URL returned by the API is passed on as a candidate.
    """

    name = "elsevier"

    def search(self, query: str, config: JobConfig, *, limit: int) -> list[PaperRecord]:
        """Query Scopus Search with a ``TITLE-ABS-KEY`` expression."""
        key = self.spec.api_key
        if not key:
            return []
        headers = {"X-ELS-APIKey": key, "Accept": "application/json"}
        if token := self.spec.insttoken:
            headers["X-ELS-Insttoken"] = token
        params = {
            "query": (
                f"TITLE-ABS-KEY({query}) AND PUBYEAR > {config.year_from - 1} "
                f"AND PUBYEAR < {config.year_to + 1}"
            ),
            "count": min(limit, 25),
            "view": "STANDARD",
        }
        payload = self.client.get_json(
            f"{self.spec.base_url}/search/scopus", params=params, headers=headers
        )
        entries = ((payload.get("search-results") or {}).get("entry")) or []
        records = []
        for item in entries:
            record = self._parse(item, query)
            if record is not None and self._year_ok(record.year, config):
                records.append(record)
        return records

    def _parse(self, item: dict[str, Any], query: str) -> PaperRecord | None:
        """Convert one Scopus entry into a :class:`PaperRecord`."""
        title = (item.get("dc:title") or "").strip()
        if not title:
            return None

        is_open = str(item.get("openaccess", "0")) in {"1", "true", "True"}
        links = item.get("link") or []
        landing = next(
            (link.get("@href") for link in links if link.get("@ref") == "scopus"), None
        )
        # Only an OA full-text link is ever offered as a PDF candidate.
        pdf_candidates: list[str] = []
        if is_open:
            for link in links:
                href = link.get("@href", "")
                if link.get("@ref") in {"full-text", "scidir"} and href:
                    pdf_candidates.append(href)

        return self._new_record(
            title=title,
            authors=[a for a in [item.get("dc:creator")] if a],
            year=coerce_year(item.get("prism:coverDate")),
            journal=item.get("prism:publicationName", ""),
            volume=item.get("prism:volume"),
            issue=item.get("prism:issueIdentifier"),
            pages=item.get("prism:pageRange"),
            article_number=item.get("article-number"),
            doi=item.get("prism:doi"),
            issn=item.get("prism:issn"),
            eissn=item.get("prism:eIssn"),
            publisher="Elsevier (indexed in Scopus)",
            abstract=item.get("dc:description") or "",
            document_type=item.get("subtypeDescription"),
            citation_count=_int_or_none(item.get("citedby-count")),
            citation_count_retrieved=utc_now_iso(),
            landing_page_url=landing,
            open_access_status=("gold" if is_open else "closed"),
            pdf_url=(pdf_candidates[0] if pdf_candidates else None),
            candidate_pdf_urls=pdf_candidates,
            discovery_query=query,
            notes=(
                "Scopus metadata. Full text is only retrieved when the record is "
                "open access; subscription content must be obtained through your "
                "own institutional access."
            ),
        )


# ---------------------------------------------------------------------------
# Springer Nature
# ---------------------------------------------------------------------------


class SpringerAdapter(SearchAdapter):
    """Springer Nature Meta API. PDFs only from returned open-access URLs."""

    name = "springer"

    def search(self, query: str, config: JobConfig, *, limit: int) -> list[PaperRecord]:
        """Query the ``meta/v2/json`` endpoint with a year-constrained query."""
        key = self.spec.api_key
        if not key:
            return []
        params = {
            "q": f'{query} AND (datefrom:{config.year_from}-01-01 '
                 f'dateto:{config.year_to}-12-31)',
            "p": min(limit, 50),
            "api_key": key,
        }
        payload = self.client.get_json(f"{self.spec.base_url}/meta/v2/json", params=params)
        records = []
        for item in payload.get("records") or []:
            record = self._parse(item, query)
            if record is not None and self._year_ok(record.year, config):
                records.append(record)
        return records

    def _parse(self, item: dict[str, Any], query: str) -> PaperRecord | None:
        """Convert one Springer record into a :class:`PaperRecord`."""
        title = (item.get("title") or "").strip()
        if not title:
            return None

        is_open = str(item.get("openaccess", "false")).lower() == "true"
        pdf_candidates: list[str] = []
        landing = None
        for url in item.get("url") or []:
            href = url.get("value", "")
            if url.get("format") == "pdf" and is_open and href:
                pdf_candidates.append(href)
            elif url.get("format") in {"", "html"} and href:
                landing = landing or href

        return self._new_record(
            title=title,
            authors=[(c.get("creator") or "") for c in (item.get("creators") or [])],
            year=coerce_year(item.get("publicationDate")),
            journal=item.get("publicationName", ""),
            volume=item.get("volume"),
            issue=item.get("number"),
            pages=(
                f"{item.get('startingPage')}-{item.get('endingPage')}"
                if item.get("startingPage") and item.get("endingPage")
                else item.get("startingPage")
            ),
            doi=item.get("doi"),
            issn=item.get("issn"),
            eissn=item.get("eIssn"),
            publisher=item.get("publisher", "Springer Nature"),
            abstract=item.get("abstract") or "",
            keywords=list(item.get("keyword") or [])[:12],
            document_type=item.get("contentType"),
            language=item.get("language"),
            landing_page_url=landing,
            open_access_status=("gold" if is_open else "closed"),
            pdf_url=(pdf_candidates[0] if pdf_candidates else None),
            candidate_pdf_urls=pdf_candidates,
            discovery_query=query,
            notes=(
                "Springer Nature metadata. PDF retrieval is attempted only for "
                "open-access records."
            ),
        )


def _int_or_none(value: Any) -> int | None:
    """Coerce a string-typed count to ``int`` where possible."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Registry and orchestration
# ---------------------------------------------------------------------------

#: Adapters that perform discovery. Unpaywall is an OA locator, used by the
#: downloader rather than for discovery, so it is not registered here.
ADAPTER_REGISTRY: dict[str, type[SearchAdapter]] = {
    CrossrefAdapter.name: CrossrefAdapter,
    OpenAlexAdapter.name: OpenAlexAdapter,
    SemanticScholarAdapter.name: SemanticScholarAdapter,
    EuropePMCAdapter.name: EuropePMCAdapter,
    ArxivAdapter.name: ArxivAdapter,
    CoreAdapter.name: CoreAdapter,
    ElsevierAdapter.name: ElsevierAdapter,
    SpringerAdapter.name: SpringerAdapter,
}


def build_adapters(
    settings: Settings,
    *,
    only: list[str] | None = None,
    client: HttpClient | None = None,
) -> tuple[list[SearchAdapter], dict[str, str]]:
    """Instantiate every usable adapter, plus the reasons others were skipped."""
    adapters: list[SearchAdapter] = []
    skipped: dict[str, str] = {}
    specs = settings.source_specs()

    for name, adapter_class in ADAPTER_REGISTRY.items():
        spec = specs.get(name)
        if spec is None:
            skipped[name] = "not present in config/search_sources.yaml"
            continue
        if only and name not in only:
            skipped[name] = "not selected for this run"
            continue
        if not spec.available:
            skipped[name] = spec.unavailable_reason
            continue
        adapters.append(adapter_class(spec, settings, client=client))

    for name, reason in settings.unavailable_sources().items():
        skipped.setdefault(name, reason)

    return adapters, skipped


def queries_for_source(strategy: KeywordStrategy, source_name: str, max_queries: int) -> list[tuple[str, str]]:
    """Choose the query strings to run against one source.

    Returns ``(query, breadth)`` pairs. API endpoints receive plain term
    combinations rather than Boolean strings, because most REST search fields do
    not honour Boolean syntax.
    """
    concepts = strategy.main_concepts
    user_terms = strategy.terms_in("user keyword")
    synonyms = strategy.terms_in("synonym")
    methods = strategy.terms_in("related method")

    pairs: list[tuple[str, str]] = []
    primary = " ".join(dict.fromkeys([*user_terms[:2], *concepts[:3]])).strip()
    if primary:
        pairs.append((primary, "balanced"))
    if len(concepts) >= 2:
        pairs.append((" ".join(concepts[:2]), "broad"))
    if synonyms and concepts:
        pairs.append((f"{concepts[0]} {synonyms[0]}", "balanced"))
    if methods and concepts:
        pairs.append((f"{concepts[0]} {methods[0]}", "narrow"))
    if len(concepts) >= 3:
        pairs.append((" ".join(concepts[:3]), "narrow"))

    # Deduplicate while preserving order, then cap.
    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for query, breadth in pairs:
        key = query.lower()
        if key and key not in seen:
            seen.add(key)
            unique.append((query, breadth))
    return unique[:max_queries]


def run_search(
    strategy: KeywordStrategy,
    config: JobConfig,
    settings: Settings,
    *,
    adapters: list[SearchAdapter] | None = None,
    only: list[str] | None = None,
) -> SearchOutcome:
    """Execute the full discovery stage across every available source."""
    skipped: dict[str, str] = {}
    if adapters is None:
        adapters, skipped = build_adapters(settings, only=only)

    per_source = int(settings.search.get("results_per_source", 60))
    max_queries = int(settings.search.get("max_queries_per_source", 4))
    outcome = SearchOutcome(sources_skipped=skipped)

    if not adapters:
        LOG.warning("No search sources are available; nothing was queried.")
        return outcome

    for adapter in adapters:
        source_records: list[PaperRecord] = []
        for query, breadth in queries_for_source(strategy, adapter.name, max_queries):
            records, log = adapter.safe_search(
                query, config, limit=per_source, breadth=breadth
            )
            outcome.logs.append(log)
            source_records.extend(records)
            LOG.info(f"{adapter.spec.label}: '{query}' returned {len(records)} records.")
        if source_records:
            outcome.sources_used.append(adapter.spec.label)
        outcome.records.extend(source_records)
        adapter.close()

    LOG.info(
        f"Discovery complete: {len(outcome.records)} raw records from "
        f"{len(outcome.sources_used)} source(s)."
    )
    return outcome


def filter_records(
    records: list[PaperRecord], config: JobConfig, settings: Settings
) -> tuple[list[PaperRecord], dict[str, int]]:
    """Apply the mechanical inclusion rules and report what was dropped."""
    search_cfg = settings.search
    min_title = int(search_cfg.get("min_title_length", 8))
    require_year = bool(search_cfg.get("require_year_in_range", True))
    drop_untitled = bool(search_cfg.get("drop_records_without_title", True))
    exclusions = [t.lower() for t in config.exclusion_terms]

    kept: list[PaperRecord] = []
    counters = {
        "dropped_no_title": 0,
        "dropped_short_title": 0,
        "dropped_year_out_of_range": 0,
        "dropped_exclusion_term": 0,
        "dropped_non_research_type": 0,
    }
    non_research = {"editorial", "erratum", "correction", "retraction", "book-review",
                    "peer-review", "component", "grant", "dataset"}

    for record in records:
        if not record.title:
            counters["dropped_no_title"] += 1
            if drop_untitled:
                continue
        if record.title and len(record.title) < min_title:
            counters["dropped_short_title"] += 1
            continue
        if require_year and record.year is not None:
            if not (config.year_from <= record.year <= config.year_to):
                counters["dropped_year_out_of_range"] += 1
                continue
        doc_type = (record.document_type or "").lower()
        if doc_type in non_research:
            counters["dropped_non_research_type"] += 1
            continue
        haystack = f"{record.title} {record.abstract}".lower()
        if any(term in haystack for term in exclusions if term):
            counters["dropped_exclusion_term"] += 1
            continue
        kept.append(record)

    return kept, counters
