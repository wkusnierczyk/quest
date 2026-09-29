"""The `quest about` stanza, built from the installed package's metadata (single source: pyproject.toml)."""
from email.utils import parseaddr
from importlib import metadata

from . import DISTRIBUTION

FIELDS = ("version", "developer", "source", "licence")


def package_info(distribution=DISTRIBUTION):
    """Returns the values shown by `quest about`; 'unknown' where the metadata is unavailable."""
    try:
        meta = metadata.metadata(distribution)
    except metadata.PackageNotFoundError:
        return {"name": distribution.replace("-", " ").title(), "summary": "",
                **{field: "unknown" for field in FIELDS}}
    urls = project_urls(meta.get_all("Project-URL") or [])
    email = parseaddr(meta.get("Author-email") or "")[1]
    licence = meta.get("License-Expression")
    return {
        "name": (meta.get("Name") or distribution).replace("-", " ").title(),
        "summary": (meta.get("Summary") or "").rstrip("."),
        "version": meta.get("Version") or "unknown",
        "developer": f"mailto:{email}" if email else "unknown",
        "source": urls.get("Source", "unknown"),
        "licence": f"{licence} https://opensource.org/licenses/{licence}" if licence else "unknown",
    }


def project_urls(entries):
    """Parses "Label, URL" Project-URL entries; entries without a label are ignored."""
    urls = {}
    for entry in entries:
        label, sep, url = entry.partition(",")
        if sep and label.strip() and url.strip():
            urls[label.strip()] = url.strip()
    return urls


def render_about(info, unicode=True):
    """Title line, then one tree branch per field with the values aligned in one column."""
    branch, last = ("├─", "└─") if unicode else ("|-", "`-")
    lines = [f"{info['name']}: {info['summary']}" if info["summary"] else info["name"]]
    for field in FIELDS:
        lines.append(f"{last if field == FIELDS[-1] else branch} {field + ':':<12}{info[field]}")
    return "\n".join(lines)


def supports_unicode(stream):
    """False when the stream cannot encode the tree characters (e.g. a legacy Windows console)."""
    encoding = getattr(stream, "encoding", None)
    if not encoding:  # a text-only stream (e.g. io.StringIO) takes any character
        return True
    try:
        "├─└".encode(encoding)
        return True
    except (UnicodeEncodeError, LookupError):
        return False
