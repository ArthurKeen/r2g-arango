"""r2g as a tool of a context-and-governance control plane.

When ``CONTROL_PLANE_URL`` is set, the control plane decides who a request is for: it
signs a short-lived context token for one user in one project, and the browser sends it
as ``Authorization: Bearer`` (or as ``?token=`` on the load-progress stream, which an
``EventSource`` cannot give a header). In this mode:

* the context token is the only credential: r2g's own ``R2G_API_TOKEN`` is not used;
* r2g opens on the transformation project the control-plane project names for it, and
  shows no other;
* the project's ontology is read from AOE, with the same token, so the user sees the
  classes the project works on;
* storage is per tenant (``R2G_CATALOG_DIR``), and a context for another tenant is
  refused.

The control plane's SDK (``control_plane_sdk``) does the verification, offline, against
the control plane's published keys. It is imported only in this mode, so r2g installs and
runs without it otherwise. Everything else talks to the small :class:`ControlPlane`
interface below, which keeps the server testable without the SDK.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

TOOL_ID = "r2g"


class ContextRejected(Exception):  # noqa: N818 - mirrors the SDK's TokenRejected
    """The token is not a valid context: refuse the request (401)."""


class ContextUnavailable(Exception):  # noqa: N818
    """The token could not be checked (e.g. the control plane's keys could not be
    fetched): a temporary failure (503), never a reason to let the request in."""


@dataclass(frozen=True)
class Identity:
    """What r2g needs from a verified context."""

    principal: str
    roles: tuple[str, ...]
    tenant: str
    project: str
    #: ``(ontology_id, version)`` of the project ontology the context names, if any.
    ontology: tuple[str, str] | None
    token: str = field(repr=False, default="")
    verified: Any = field(repr=False, default=None)


@dataclass(frozen=True)
class Project:
    """The control-plane project, as a tool may see it (its FR-2.5 and FR-2.6)."""

    id: str
    name: str
    #: The transformation project the control-plane project names for r2g, if any.
    opens: str | None


class ControlPlane(Protocol):
    def verify(self, token: str) -> Identity: ...

    def project(self, identity: Identity) -> Project: ...


def mode() -> bool:
    """Whether r2g runs under a control plane."""
    return bool(os.environ.get("CONTROL_PLANE_URL", "").strip())


#: Why the MCP server does not run under a control plane: it checks no context.
MCP_REFUSED = (
    "CONTROL_PLANE_URL is set: the MCP server checks no context token, so it would "
    "reach this deployment's catalog around the control plane. It does not run here."
)


class _Sdk:
    """The real implementation: the control plane's SDK, created once per process."""

    def __init__(self, url: str) -> None:
        from control_plane_sdk import ControlPlane as Client  # only in this mode

        self.client = Client(url, tool=TOOL_ID)
        self._projects: dict[str, tuple[float, Project]] = {}
        self._lock = threading.Lock()

    def verify(self, token: str) -> Identity:
        from control_plane_sdk import ContextError, TokenRejected, Unavailable

        try:
            verified = self.client.verify(token)
        except (TokenRejected, ContextError) as exc:
            raise ContextRejected(str(exc)) from exc
        except Unavailable as exc:
            raise ContextUnavailable(str(exc)) from exc
        ctx = verified.context
        return Identity(
            principal=ctx.principal.id,
            roles=tuple(ctx.principal.roles),
            tenant=ctx.tenant,
            project=ctx.project,
            ontology=None if ctx.ontology is None
            else (ctx.ontology.ontology_id, ctx.ontology.version),
            token=token,
            verified=verified,
        )

    def project(self, identity: Identity) -> Project:
        from control_plane_sdk import ControlPlaneError

        key = identity.verified.token_id
        now = time.monotonic()
        with self._lock:
            hit = self._projects.get(key)
            if hit is not None and now - hit[0] < 60:
                return hit[1]
        try:
            details = self.client.project(identity.verified)
        except ControlPlaneError as exc:
            raise ContextUnavailable(f"cannot read the project: {exc}") from exc
        project = Project(details.id, details.name, details.opened_by(TOOL_ID))
        with self._lock:
            self._projects[key] = (now, project)
        return project


_instance: ControlPlane | None = None
_instance_lock = threading.Lock()


def get() -> ControlPlane:
    global _instance
    with _instance_lock:
        if _instance is None:
            _instance = _Sdk(os.environ["CONTROL_PLANE_URL"])
        return _instance


def set_instance(instance: ControlPlane | None) -> None:
    """Replace the control plane (tests), or reset it to be created on next use."""
    global _instance
    with _instance_lock:
        _instance = instance


def ontology_classes(identity: Identity, *, http: Any = None) -> dict[str, Any]:
    """The project ontology's classes, read from AOE (``AOE_URL``) with the user's own
    context, so AOE applies the same project scope it applies to the user. Never raises:
    a failure is reported with what went wrong, and r2g carries on without the list."""
    if identity.ontology is None:
        return {"ontology_id": None, "classes": [], "problem": "the project names no ontology"}
    ontology_id, version = identity.ontology
    base = os.environ.get("AOE_URL", "").rstrip("/")
    if not base:
        return {"ontology_id": ontology_id, "version": version, "classes": [],
                "problem": "AOE_URL is not set, so the ontology cannot be read"}
    import httpx

    client = http or httpx.Client(timeout=10.0)
    try:
        r = client.get(f"{base}/api/v1/ontology/{ontology_id}/classes",
                       params={"limit": 500},
                       headers={"Authorization": f"Bearer {identity.token}"})
        if r.status_code != 200:
            return {"ontology_id": ontology_id, "version": version, "classes": [],
                    "problem": f"AOE answered {r.status_code}"}
        rows = r.json().get("data", [])
    except Exception as exc:  # the panel is informative; r2g works without it
        return {"ontology_id": ontology_id, "version": version, "classes": [],
                "problem": f"cannot reach AOE: {exc}"}
    classes = sorted(({"uri": c.get("uri"), "label": c.get("label") or c.get("uri")}
                      for c in rows if c.get("uri")), key=lambda c: str(c["label"]).lower())
    return {"ontology_id": ontology_id, "version": version, "classes": classes, "problem": None}
