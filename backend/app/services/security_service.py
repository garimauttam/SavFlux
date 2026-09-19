"""
security_service.py — CVE audit for indexed dependencies ($0).

Pipeline:
  1. Collect dependency manifests from the Chroma index (requirements.txt,
     package.json, go.mod) and reconstruct their content from chunks.
  2. Parse pinned versions (pure functions — unit-tested, no network).
  3. Query the OSV database (osv.dev — free, no API key) in ONE batch call.
  4. Return per-package findings + unresolvable entries.

Every network/Chroma failure degrades gracefully: the response always
includes what was found locally plus a reason when OSV is unreachable.
"""

from __future__ import annotations

import json
import re
from typing import Any

OSV_BATCH_URL = "https://api.osv.dev/v1/querybatch"
_OSV_TIMEOUT = 15.0
_MAX_PACKAGES = 50

MANIFEST_FILES = {"requirements.txt", "package.json", "go.mod"}


# ── Pure parsers (no IO — unit test these) ────────────────────────────────────

def parse_requirements(content: str) -> list[dict[str, Any]]:
    """Parse requirements.txt → [{name, version|None, ecosystem}].

    Only `name==version` pins are OSV-queryable; everything else is kept
    with version=None so the UI can show 'unpinned'.
    """
    out = []
    for raw in (content or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        line = re.split(r"\s+#", line)[0].strip()
        line = line.split(";")[0].strip()  # environment markers
        m = re.match(r"^([A-Za-z0-9_.\-]+)\s*==\s*([A-Za-z0-9_.\-+]+)", line)
        if m:
            out.append({"name": m.group(1), "version": m.group(2), "ecosystem": "PyPI"})
            continue
        m = re.match(r"^([A-Za-z0-9_.\-]+)", line)
        if m:
            out.append({"name": m.group(1), "version": None, "ecosystem": "PyPI"})
    return out


def parse_package_json(content: str) -> list[dict[str, Any]]:
    """Parse package.json deps → [{name, version|None, ecosystem:'npm'}].

    Only exact semver (after stripping ^ ~ >= <=) is OSV-queryable.
    """
    try:
        data = json.loads(content or "{}")
    except Exception:
        return []
    out = []
    deps = {}
    for section in ("dependencies", "devDependencies", "peerDependencies"):
        if isinstance(data.get(section), dict):
            deps.update(data[section])
    for name, spec in deps.items():
        version = None
        if isinstance(spec, str):
            cleaned = re.sub(r"^[~^>=<\s]+", "", spec.strip())
            if re.fullmatch(r"\d+\.\d+\.\d+([-+][0-9A-Za-z.\-+]+)?", cleaned):
                version = cleaned
        out.append({"name": name, "version": version, "ecosystem": "npm"})
    return out


def parse_go_mod(content: str) -> list[dict[str, Any]]:
    """Parse go.mod requires → [{name, version, ecosystem:'Go'}]."""
    out = []
    for raw in (content or "").splitlines():
        line = raw.strip()
        m = re.match(r"^([A-Za-z0-9_.\-/]+)\s+v([0-9][A-Za-z0-9_.\-+]*)", line)
        if m and "=>" not in line:
            out.append({"name": m.group(1), "version": m.group(2), "ecosystem": "Go"})
    # de-dupe (multi-line require blocks list each module once anyway)
    seen, uniq = set(), []
    for p in out:
        key = (p["name"], p["version"])
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    return uniq


def parse_manifest(filename: str, content: str) -> list[dict[str, Any]]:
    base = (filename or "").rsplit("/", 1)[-1].lower()
    if base == "requirements.txt":
        return parse_requirements(content)
    if base == "package.json":
        return parse_package_json(content)
    if base == "go.mod":
        return parse_go_mod(content)
    return []


# ── OSV batch query ───────────────────────────────────────────────────────────

async def query_osv(packages: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Batch-query OSV for pinned packages. Returns {name@version: [vulns]}.

    Raises RuntimeError when OSV is unreachable — callers degrade gracefully.
    """
    import httpx
    pinned = [p for p in packages if p.get("version")][: _MAX_PACKAGES]
    if not pinned:
        return {}
    queries = [{"package": {"name": p["name"], "ecosystem": p["ecosystem"]},
                "version": p["version"]} for p in pinned]
    try:
        async with httpx.AsyncClient(timeout=_OSV_TIMEOUT) as client:
            resp = await client.post(OSV_BATCH_URL, json={"queries": queries})
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        raise RuntimeError(f"OSV unreachable: {str(e)[:120]}")
    findings: dict[str, list[dict[str, Any]]] = {}
    for pkg, result in zip(pinned, data.get("results", [])):
        vulns = []
        for v in result.get("vulns", [])[:10]:
            vulns.append({
                "id": v.get("id", ""),
                "summary": (v.get("summary") or "")[:300],
                "severity": _top_severity(v),
                "fixed": _fixed_versions(v),
                "url": f"https://osv.dev/vulnerability/{v.get('id', '')}",
            })
        if vulns:
            findings[f"{pkg['name']}@{pkg['version']}"] = vulns
    return findings


def _top_severity(vuln: dict) -> str:
    best, rank = "unknown", -1
    order = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1}
    for sev in vuln.get("severity", []) or []:
        score = str(sev.get("score", "")).upper()
        if score in order and order[score] > rank:
            best, rank = score, order[score]
    db_specific = vuln.get("database_specific") or {}
    if rank < 0 and isinstance(db_specific.get("severity"), str):
        return db_specific["severity"]
    return best


def _fixed_versions(vuln: dict) -> list[str]:
    fixed = []
    for affected in vuln.get("affected", []) or []:
        for r in affected.get("ranges", []) or []:
            for ev in r.get("events", []) or []:
                if "fixed" in ev:
                    fixed.append(str(ev["fixed"]))
    return sorted(set(fixed))[:5]


# ── Index collection + audit ──────────────────────────────────────────────────

def collect_manifests(repo_url: str | None = None) -> list[dict[str, Any]]:
    """Reconstruct manifest contents from Chroma chunks. Empty list on failure."""
    try:
        from app.services.retrieval_service import _get_vectorstore
        from app.services.chunk_reconstruction import reconstruct_chunks
        vs = _get_vectorstore()
        results = vs._collection.get(include=["documents", "metadatas"])
        docs = results.get("documents") or []
        metas = results.get("metadatas") or []
        by_source: dict[str, dict] = {}
        for doc, meta in zip(docs, metas):
            if repo_url and meta.get("repo_url") != repo_url:
                continue
            fname = meta.get("file_name", "")
            if fname not in MANIFEST_FILES:
                continue
            src = meta.get("source", "")
            by_source.setdefault(src, {"file_name": fname, "chunks": []})
            by_source[src]["chunks"].append((meta, doc))
        manifests = []
        for src, info in by_source.items():
            try:
                content = reconstruct_chunks(info["chunks"])
            except Exception:
                continue
            if content.strip():
                manifests.append({"file_name": info["file_name"],
                                  "source": src, "content": content[:50000]})
        return manifests
    except Exception:
        return []


async def audit_packages(packages: list[dict[str, Any]]) -> dict[str, Any]:
    """OSV-query parsed packages → findings envelope (never raises)."""
    try:
        findings = await query_osv(packages)
        return {"status": "success", "findings": findings,
                "vulnerable": len(findings),
                "scanned": len([p for p in packages if p.get("version")])}
    except RuntimeError as e:
        return {"status": "degraded", "reason": str(e),
                "findings": {}, "vulnerable": 0,
                "scanned": len([p for p in packages if p.get("version")])}


async def audit_repo(repo_url: str | None = None) -> dict[str, Any]:
    manifests = collect_manifests(repo_url)
    packages: list[dict[str, Any]] = []
    for m in manifests:
        for p in parse_manifest(m["file_name"], m["content"]):
            p["manifest"] = m["file_name"]
            packages.append(p)
    # de-dupe identical (name, version, ecosystem)
    seen, uniq = set(), []
    for p in packages:
        key = (p["name"], p.get("version"), p["ecosystem"])
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    result = await audit_packages(uniq)
    result.update({
        "repo_url": repo_url,
        "manifests": [m["file_name"] for m in manifests],
        "packages": uniq[: _MAX_PACKAGES * 2],
        "unpinned": [p for p in uniq if not p.get("version")][:50],
    })
    return result
