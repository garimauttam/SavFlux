"""
security.py — CVE audit API ($0, OSV database — no key needed).

Endpoints:
  GET  /security/cve-audit?repo_url=  — audit indexed manifests for a repo
  POST /security/cve-scan             — audit pasted manifests, no index needed
                                       {manifests: [{filename, content}]}
"""

import asyncio

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import require_api_key

router = APIRouter(prefix="/security", tags=["security"])


@router.get("/cve-audit")
async def cve_audit(repo_url: str | None = None, _: None = Depends(require_api_key)):
    try:
        from app.services.security_service import audit_repo
        return await audit_repo(repo_url)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/cve-scan")
async def cve_scan(body: dict, _: None = Depends(require_api_key)):
    manifests = body.get("manifests")
    if not isinstance(manifests, list) or not manifests:
        raise HTTPException(status_code=400, detail="manifests must be a non-empty array")
    if len(manifests) > 10:
        raise HTTPException(status_code=400, detail="Too many manifests (max 10)")
    try:
        from app.services.security_service import parse_manifest, audit_packages
        packages = []
        for m in manifests:
            if not isinstance(m, dict):
                continue
            for p in parse_manifest(m.get("filename", ""), m.get("content", "")[:50000]):
                p["manifest"] = m.get("filename", "")
                packages.append(p)
        result = await audit_packages(packages)
        result["packages"] = packages[:100]
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
