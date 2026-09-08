"""Read installed skill provenance and compare upstream refs without changing state."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from agent_ops.deployment.engine import _selection_from_shared_files
from agent_ops.deployment.registry import DeploymentRegistry
from agent_ops.deployment.source_store import (
    _normalize_source_url,
    _persisted_remote_url_and_transient_auth,
    _run_git,
    _urls_equivalent,
    _validate_ref,
)
from agent_ops.deployment.transaction import read_shared_target_evidence


def _safe_url(value: str) -> str:
    value, _ = _persisted_remote_url_and_transient_auth(value)
    parsed = urlsplit(value)
    if parsed.scheme and parsed.hostname:
        host = parsed.hostname
        if ":" in host:
            host = f"[{host}]"
        if parsed.port:
            host += f":{parsed.port}"
        return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    return value


def inspect_shared_source(
    registry: DeploymentRegistry, skill: str, checkout: Path | None = None
) -> dict[str, object]:
    snapshot = registry.load_snapshot()
    shared = snapshot.config.shared_selection
    if shared is None:
        raise ValueError("registry has no shared selection")
    manifest, audit, files = read_shared_target_evidence(shared.target)
    if manifest is None or audit is None or not audit.matches:
        raise ValueError("installed shared content is absent or modified")
    _selection_from_shared_files(files, manifest.source_revision)
    mapping = next(
        item
        for item in files
        if item.path == Path("snapshots") / manifest.source_revision / "source-map.json"
    )
    rows = [item for item in json.loads(mapping.content)["skills"] if item["name"] == skill]
    if len(rows) != 1:
        raise ValueError("skill is not present in installed source mapping")
    row = rows[0]
    sources = {source.id: source for source in snapshot.config.sources}
    if row["source_id"] not in sources:
        raise ValueError("installed skill source is no longer registered")
    source = sources[row["source_id"]]
    result = {**row, "url": _safe_url(source.url), "checkout": None, "dirty": None}
    if checkout is not None:
        root = checkout.expanduser().resolve(strict=True)
        if root == shared.home.resolve() or shared.home.resolve() in root.parents:
            raise ValueError("installed snapshots are generated content, not an authoring checkout")
        try:
            top = Path(
                _run_git(("rev-parse", "--show-toplevel"), cwd=root).stdout.strip()
            ).resolve()
            if top != root:
                raise ValueError("checkout must name a Git working tree root")
            expected = _normalize_source_url(source.url)
            local_match = _urls_equivalent(expected, str(root))
            if not local_match:
                remote = _run_git(("remote", "get-url", "origin"), cwd=root).stdout.strip()
                if not _urls_equivalent(_safe_url(expected), _safe_url(remote)):
                    raise ValueError("checkout repository does not match the registered source")
            selected_path = root / row["path"]
            if not selected_path.is_dir() or selected_path.is_symlink():
                raise ValueError("selected skill directory is unavailable in checkout")
            if root not in selected_path.resolve().parents:
                raise ValueError("selected skill path escapes checkout")
            dirty = bool(
                _run_git(("status", "--porcelain", "--untracked-files=all"), cwd=root).stdout
            )
            commit = _run_git(("rev-parse", "HEAD"), cwd=root).stdout.strip()
        except Exception:
            raise ValueError("checkout identity or contents could not be verified") from None
        result.update(checkout=str(root), dirty=dirty, checkout_commit=commit)
    return result


def inspect_shared_updates(registry: DeploymentRegistry) -> list[dict[str, object]]:
    config = registry.load()
    if config.shared_selection is None:
        raise ValueError("registry has no shared selection")
    sources = {source.id: source for source in config.sources}
    result = []
    for binding in config.shared_selection.selected.sources:
        source = sources[binding.source_id]
        row = {
            "source_id": source.id,
            "selected_commit": binding.commit,
            "upstream_commit": None,
            "ref": source.stable_ref,
            "state": "unknown",
        }
        try:
            _validate_ref(source.stable_ref)
            url = _normalize_source_url(source.url)
            remote, environment = _persisted_remote_url_and_transient_auth(url)
            output = _run_git(
                ("ls-remote", "--refs", remote, source.stable_ref), environment=environment
            ).stdout
            matches = [line.split() for line in output.splitlines()]
            if len(matches) == 1 and len(matches[0]) == 2 and matches[0][1] == source.stable_ref:
                commit = matches[0][0]
                if len(commit) == 40 and all(char in "0123456789abcdef" for char in commit):
                    row.update(
                        upstream_commit=commit,
                        state="current" if commit == binding.commit else "available",
                    )
        except Exception:
            # Error text may contain remote credentials; unknown is the complete public result.
            pass
        result.append(row)
    return result
