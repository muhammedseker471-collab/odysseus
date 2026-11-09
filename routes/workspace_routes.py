"""Workspace directory browser plus separately mounted inventory routes."""
import os
from fastapi import APIRouter, Request, HTTPException, Query
from fastapi.responses import RedirectResponse

from src.auth_helpers import get_current_user
from src.tool_security import owner_is_admin_or_single_user
from routes.inventory_routes import router as inventory_router

_MAX_BROWSE_DIRS = 500


def setup_workspace_routes():
    # Keep inventory's /api/inventory paths independent of this workspace API prefix.
    router = APIRouter(tags=["workspace"])
    router.include_router(inventory_router)

    @router.get("/inventory")
    def inventory_page(request: Request):
        # Auth middleware protects this route; page data APIs are owner-scoped too.
        if not get_current_user(request):
            from src.auth_helpers import require_user
            require_user(request)
        return RedirectResponse(url="/static/inventory.html", status_code=302)

    @router.get("/api/workspace/browse")
    def browse(request: Request, path: str = Query(default="")):
        owner = get_current_user(request)
        if not owner_is_admin_or_single_user(owner):
            raise HTTPException(status_code=403, detail="Workspace browsing is admin-only")
        target = os.path.realpath(os.path.expanduser(path.strip() or "~"))
        if not os.path.isdir(target):
            target = os.path.realpath(os.path.expanduser("~"))
        dirs = []
        try:
            with os.scandir(target) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False) and not entry.name.startswith("."):
                            dirs.append({"name": entry.name, "path": os.path.join(target, entry.name)})
                    except OSError:
                        continue
        except (PermissionError, OSError):
            dirs = []
        dirs_sorted = sorted(dirs, key=lambda d: d["name"].lower())
        from src.tool_execution import vet_workspace
        parent = os.path.dirname(target)
        return {"path": target, "parent": parent if parent and parent != target else None,
                "dirs": dirs_sorted[:_MAX_BROWSE_DIRS], "truncated": len(dirs_sorted) > _MAX_BROWSE_DIRS,
                "selectable": vet_workspace(target) is not None}

    @router.get("/api/workspace/vet")
    def vet(request: Request, path: str = Query(default="")):
        owner = get_current_user(request)
        if not owner_is_admin_or_single_user(owner):
            raise HTTPException(status_code=403, detail="Workspace selection is admin-only")
        from src.tool_execution import vet_workspace
        resolved = vet_workspace(path)
        return {"ok": resolved is not None, "path": resolved}

    return router
