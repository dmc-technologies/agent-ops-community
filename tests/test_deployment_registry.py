from __future__ import annotations

import json
import multiprocessing
import os
import stat
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from agent_ops.deployment.models import (
    DeploymentAudit,
    DeploymentManifest,
    DeploymentReceipt,
    SourceSpec,
    TargetSpec,
    TargetState,
    TargetStatus,
)
from agent_ops.deployment.registry import ChannelSpec, DeploymentRegistry, RegistryConfig
from agent_ops.registries.models import Framework


class _SourceLike:
    id = "example"
    url = "https://example.invalid/source.git"
    stable_ref = "refs/heads/main"


class _ChannelLike:
    id = "stable"
    source = "example"
    ref = "refs/heads/main"


class _TargetLike:
    def __init__(self, home: Path) -> None:
        self.id = "codex-stable"
        self.framework = Framework.CODEX
        self.home = home
        self.channel = "stable"


class _SourceSubclass(SourceSpec):
    pass


class _ChannelSubclass(ChannelSpec):
    pass


class _TargetSubclass(TargetSpec):
    pass


def _config(tmp_path: Path, *, channel: str = "stable") -> RegistryConfig:
    home = tmp_path / "homes" / channel
    ref = "refs/heads/feature/example" if channel == "feature" else "refs/heads/main"
    return RegistryConfig(
        schema_version=1,
        sources=(
            SourceSpec(
                id="example",
                url="file:///srv/agentops/source.git",
                stable_ref="refs/heads/main",
            ),
        ),
        channels=(ChannelSpec(id=channel, source="example", ref=ref),),
        targets=(
            TargetSpec(
                id=f"codex-{channel}",
                framework=Framework.CODEX,
                home=home,
                channel=channel,
            ),
        ),
    )


def _registry(tmp_path: Path) -> DeploymentRegistry:
    return DeploymentRegistry(tmp_path / "deployment-registry.yaml")


def _manifest(target_id: str, revision: str) -> DeploymentManifest:
    return DeploymentManifest(
        schema_version=1,
        target_id=target_id,
        framework=Framework.CODEX,
        source_revision=revision,
        provider_ids=(),
        files=(),
        directories=(),
        transaction_id="transaction-1",
    )


def _append_after_signal(path: str, commit: str, start: object, results: object) -> None:
    start.wait()
    try:
        DeploymentRegistry(Path(path)).append_receipt(
            DeploymentReceipt("refresh", (commit,), ())
        )
    except BaseException as error:
        results.put(f"{type(error).__name__}: {error}")
    else:
        results.put(None)


def test_registry_save_load_is_typed_canonical_atomic_and_private(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    config = _config(tmp_path)

    registry.save(config)
    loaded = registry.load()

    assert loaded == config
    assert isinstance(loaded.sources[0], SourceSpec)
    assert isinstance(loaded.channels[0], ChannelSpec)
    assert isinstance(loaded.targets[0], TargetSpec)
    assert stat.S_IMODE(registry.path.stat().st_mode) == 0o600
    assert registry.path.read_text() == (
        "schema_version: 1\n"
        "sources:\n"
        "  example:\n"
        "    url: file:///srv/agentops/source.git\n"
        "    stable_ref: refs/heads/main\n"
        "channels:\n"
        "  stable:\n"
        "    source: example\n"
        "    ref: refs/heads/main\n"
        "targets:\n"
        "  codex-stable:\n"
        "    framework: codex\n"
        f"    home: {tmp_path / 'homes' / 'stable'}\n"
        "    channel: stable\n"
    )
    assert not tuple(tmp_path.glob(".deployment-registry.yaml.*.tmp"))

    with pytest.raises(FrozenInstanceError):
        loaded.schema_version = 2  # type: ignore[misc]


@pytest.mark.parametrize(
    "text, message",
    (
        ("", "mapping"),
        ("[]\n", "mapping"),
        ("schema_version: 2\nsources: {}\nchannels: {}\ntargets: {}\n", "schema version"),
        (
            "schema_version: 1\nschema_version: 1\nsources: {}\nchannels: {}\ntargets: {}\n",
            "duplicate",
        ),
        (
            "schema_version: 1\nsources: {}\nchannels: {}\ntargets: {}\nextra: true\n",
            "unknown",
        ),
        (
            "schema_version: '1'\nsources: {}\nchannels: {}\ntargets: {}\n",
            "integer",
        ),
        (
            "schema_version: 1\nsources: &shared {}\nchannels: *shared\ntargets: {}\n",
            "aliases",
        ),
        (
            "schema_version: 1\nsources: !unsafe {}\nchannels: {}\ntargets: {}\n",
            "tag",
        ),
    ),
)
def test_registry_load_rejects_malformed_yaml(
    tmp_path: Path, text: str, message: str
) -> None:
    registry = _registry(tmp_path)
    registry.path.write_text(text)
    registry.path.chmod(0o600)

    with pytest.raises(ValueError, match=message):
        registry.load()


def test_registry_rejects_unknown_nested_keys_and_invalid_references(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    invalid_documents = (
        """schema_version: 1
sources:
  example: {url: file:///source, stable_ref: refs/heads/main, other: no}
channels: {}
targets: {}
""",
        """schema_version: 1
sources:
  example: {url: file:///source, stable_ref: main}
channels: {}
targets: {}
""",
        """schema_version: 1
sources:
  example: {url: file:///source, stable_ref: refs/heads/main}
channels:
  preview: {source: absent, ref: refs/heads/preview}
targets: {}
""",
        f"""schema_version: 1
sources:
  example: {{url: file:///source, stable_ref: refs/heads/main}}
channels:
  stable: {{source: example, ref: refs/heads/main}}
targets:
  bad: {{framework: unknown, home: {tmp_path / 'home'}, channel: stable}}
""",
        f"""schema_version: 1
sources:
  example: {{url: file:///source, stable_ref: refs/heads/main}}
channels:
  stable: {{source: example, ref: refs/heads/main}}
targets:
  bad: {{framework: codex, home: {tmp_path / 'home'}, channel: absent}}
""",
    )
    for document in invalid_documents:
        registry.path.write_text(document)
        registry.path.chmod(0o600)
        with pytest.raises(ValueError):
            registry.load()


def test_registry_canonicalizes_mapping_order_and_rejects_duplicate_typed_ids(
    tmp_path: Path,
) -> None:
    home_a = tmp_path / "a"
    home_z = tmp_path / "z"
    config = RegistryConfig(
        schema_version=1,
        sources=(
            SourceSpec("z", "https://example.invalid/z.git"),
            SourceSpec("a", "https://example.invalid/a.git"),
        ),
        channels=(
            ChannelSpec("z", "z", "refs/heads/z"),
            ChannelSpec("a", "a", "refs/heads/a"),
        ),
        targets=(
            TargetSpec("z", Framework.CODEX, home_z, "z"),
            TargetSpec("a", Framework.CODEX, home_a, "a"),
        ),
    )
    registry = _registry(tmp_path)
    registry.save(config)

    assert tuple(item.id for item in registry.load().sources) == ("a", "z")
    assert registry.path.read_text().index("  a:") < registry.path.read_text().index("  z:")

    with pytest.raises(ValueError, match="duplicate channel"):
        RegistryConfig(
            1,
            sources=(SourceSpec("a", "https://example.invalid/a.git"),),
            channels=(
                ChannelSpec("same", "a", "refs/heads/a"),
                ChannelSpec("same", "a", "refs/heads/b"),
            ),
            targets=(),
        )


@pytest.mark.parametrize("wrong_kind", ("source", "channel", "target"))
def test_registry_config_rejects_duck_typed_nested_values(
    tmp_path: Path, wrong_kind: str
) -> None:
    sources: tuple[object, ...] = (
        _SourceLike() if wrong_kind == "source" else SourceSpec("example", "https://example.invalid"),
    )
    channels: tuple[object, ...] = (
        _ChannelLike()
        if wrong_kind == "channel"
        else ChannelSpec("stable", "example", "refs/heads/main"),
    )
    targets: tuple[object, ...] = (
        _TargetLike(tmp_path / "home")
        if wrong_kind == "target"
        else TargetSpec("codex-stable", Framework.CODEX, tmp_path / "home", "stable"),
    )

    with pytest.raises(ValueError, match=f"exact {wrong_kind}"):
        RegistryConfig(
            1,
            sources=sources,  # type: ignore[arg-type]
            channels=channels,  # type: ignore[arg-type]
            targets=targets,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("wrong_kind", ("source", "channel", "target"))
def test_registry_config_rejects_nested_subclasses(tmp_path: Path, wrong_kind: str) -> None:
    sources = (
        _SourceSubclass("example", "https://example.invalid")
        if wrong_kind == "source"
        else SourceSpec("example", "https://example.invalid"),
    )
    channels = (
        _ChannelSubclass("stable", "example", "refs/heads/main")
        if wrong_kind == "channel"
        else ChannelSpec("stable", "example", "refs/heads/main"),
    )
    targets = (
        _TargetSubclass("codex-stable", Framework.CODEX, tmp_path / "home", "stable")
        if wrong_kind == "target"
        else TargetSpec("codex-stable", Framework.CODEX, tmp_path / "home", "stable"),
    )

    with pytest.raises(ValueError, match=f"exact {wrong_kind}"):
        RegistryConfig(1, sources, channels, targets)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "section,field,value",
    (
        ("source", "id", "../unsafe"),
        ("source", "url", 7),
        ("source", "stable_ref", "main"),
        ("channel", "source", "absent"),
        ("channel", "ref", "feature"),
        ("target", "framework", "codex"),
        ("target", "home", "/tmp/not-a-path"),
        ("target", "channel", "absent"),
    ),
)
def test_save_revalidates_nested_fields_without_replacing_registry(
    tmp_path: Path, section: str, field: str, value: object
) -> None:
    registry = _registry(tmp_path)
    registry.save(_config(tmp_path))
    original = registry.path.read_bytes()
    invalid = _config(tmp_path)
    nested = {
        "source": invalid.sources[0],
        "channel": invalid.channels[0],
        "target": invalid.targets[0],
    }[section]
    object.__setattr__(nested, field, value)

    with pytest.raises(ValueError):
        registry.save(invalid)

    assert registry.path.read_bytes() == original


def test_target_homes_reject_relative_non_normal_symlink_and_alias_duplicates(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    source = (SourceSpec("example", "https://example.invalid/source.git"),)
    channels = (ChannelSpec("stable", "example", "refs/heads/main"),)

    for home in (Path("relative"), Path(f"{tmp_path}/homes/../other")):
        config = RegistryConfig(
            1,
            source,
            channels,
            (TargetSpec("target", Framework.CODEX, home, "stable"),),
        )
        with pytest.raises(ValueError, match="home"):
            registry.save(config)

    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(outside, target_is_directory=True)
    config = RegistryConfig(
        1,
        source,
        channels,
        (TargetSpec("target", Framework.CODEX, linked / "home", "stable"),),
    )
    with pytest.raises(ValueError, match="symlink"):
        registry.save(config)

    same = tmp_path / "same"
    config = RegistryConfig(
        1,
        source,
        channels,
        (
            TargetSpec("one", Framework.CODEX, same, "stable"),
            TargetSpec("two", Framework.CODEX, same, "stable"),
        ),
    )
    with pytest.raises(ValueError, match="same home"):
        registry.save(config)

    with pytest.raises(ValueError, match="home"):
        RegistryConfig(
            1,
            source,
            channels,
            (
                TargetSpec(  # type: ignore[arg-type]
                    "target", Framework.CODEX, "/tmp/not-a-path", "stable"
                ),
            ),
        )


def test_existing_home_identity_rejects_alias_but_preserves_posix_case(
    tmp_path: Path,
) -> None:
    existing = tmp_path / "existing"
    existing.mkdir()
    slash_alias = Path(f"//{os.fspath(existing).lstrip('/')}")
    source = (SourceSpec("example", "https://example.invalid/source.git"),)
    channels = (ChannelSpec("stable", "example", "refs/heads/main"),)
    with pytest.raises(ValueError, match="same home"):
        _registry(tmp_path).save(
            RegistryConfig(
                1,
                source,
                channels,
                (
                    TargetSpec("one", Framework.CODEX, existing, "stable"),
                    TargetSpec("two", Framework.CODEX, slash_alias, "stable"),
                ),
            )
        )

    upper = tmp_path / "A"
    lower = tmp_path / "a"
    upper.mkdir()
    lower.mkdir()
    if upper.stat().st_ino == lower.stat().st_ino:
        pytest.skip("host filesystem is case-insensitive")
    registry = _registry(tmp_path)
    registry.save(
        RegistryConfig(
            1,
            source,
            channels,
            (
                TargetSpec("upper", Framework.CODEX, upper, "stable"),
                TargetSpec("lower", Framework.CODEX, lower, "stable"),
            ),
        )
    )
    assert {target.home for target in registry.load().targets} == {upper, lower}


def test_missing_home_aliases_use_host_lexical_normalization(tmp_path: Path) -> None:
    missing = tmp_path / "not-created" / "home"
    slash_alias = Path(f"//{os.fspath(missing).lstrip('/')}")
    with pytest.raises(ValueError, match="same home"):
        _registry(tmp_path).save(
            RegistryConfig(
                1,
                (SourceSpec("example", "https://example.invalid/source.git"),),
                (ChannelSpec("stable", "example", "refs/heads/main"),),
                (
                    TargetSpec("one", Framework.CODEX, missing, "stable"),
                    TargetSpec("two", Framework.CODEX, slash_alias, "stable"),
                ),
            )
        )


def test_registry_load_rejects_permissions_size_and_symlinks(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.save(_config(tmp_path))
    registry.path.chmod(0o640)
    with pytest.raises(PermissionError, match="0600"):
        registry.load()

    registry.path.unlink()
    registry.path.write_bytes(b"x" * (1024 * 1024 + 1))
    registry.path.chmod(0o600)
    with pytest.raises(ValueError, match="large"):
        registry.load()

    registry.path.unlink()
    destination = tmp_path / "elsewhere"
    destination.write_text("not a registry")
    registry.path.symlink_to(destination)
    with pytest.raises((OSError, RuntimeError), match="symlink|regular|refus"):
        registry.load()


def test_atomic_save_failure_preserves_old_registry_and_cleans_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = _registry(tmp_path)
    registry.save(_config(tmp_path))
    original = registry.path.read_bytes()

    def fail_replace(*args: object, **kwargs: object) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        registry.save(_config(tmp_path, channel="feature"))

    assert registry.path.read_bytes() == original
    assert not tuple(tmp_path.glob(".deployment-registry.yaml.*.tmp"))


def test_receipts_are_private_strict_append_only_and_stably_ordered(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.save(_config(tmp_path))
    first = DeploymentReceipt(
        operation="refresh",
        commits=("a" * 40,),
        targets=(TargetStatus("codex-stable", TargetState.STABLE, "stable", "a" * 40),),
    )
    second = DeploymentReceipt(
        operation="refresh",
        commits=("b" * 40,),
        targets=(TargetStatus("codex-stable", TargetState.BRANCH, "stable", "b" * 40),),
    )

    first_path = registry.append_receipt(first)
    second_path = registry.append_receipt(second)

    assert registry.receipts() == (first, second)
    assert first_path != second_path
    assert first_path.read_bytes().endswith(b"\n")
    assert json.loads(first_path.read_bytes()) == {
        "operation": "refresh",
        "commits": ["a" * 40],
        "targets": [
            {
                "target_id": "codex-stable",
                "state": "stable",
                "channel": "stable",
                "commit": "a" * 40,
            }
        ],
    }
    assert stat.S_IMODE(first_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(first_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(second_path.stat().st_mode) == 0o600


def test_append_rejects_replaced_registry_before_creating_receipt_state(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.save(_config(tmp_path))
    replacement = tmp_path / "replacement.yaml"
    replacement.write_text("untrusted\n")
    registry.path.unlink()
    registry.path.symlink_to(replacement)

    with pytest.raises((OSError, RuntimeError), match="symlink|regular|refus"):
        registry.append_receipt(DeploymentReceipt("refresh", ("a" * 40,), ()))

    assert not registry.state_path.exists()


def test_corrupt_receipt_fails_closed(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.save(_config(tmp_path))
    receipt = DeploymentReceipt("refresh", ("a" * 40,), ())
    path = registry.append_receipt(receipt)

    path.write_text('{"operation":"refresh","operation":"again","commits":[],"targets":[]}\n')
    path.chmod(0o600)
    with pytest.raises(ValueError, match="duplicate"):
        registry.receipts()

    path.write_text('{"operation":"refresh","commits":["short"],"targets":[]}\n')
    path.chmod(0o600)
    with pytest.raises(ValueError, match="commit"):
        registry.receipts()


def test_cross_process_concurrent_receipts_are_each_appended_once(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.save(_config(tmp_path))
    context = multiprocessing.get_context("fork")
    start = context.Event()
    results = context.Queue()
    commits = tuple(f"{index:040x}" for index in range(12))
    processes = [
        context.Process(
            target=_append_after_signal,
            args=(os.fspath(registry.path), commit, start, results),
        )
        for commit in commits
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=10)

    assert all(not process.is_alive() and process.exitcode == 0 for process in processes)
    assert [results.get(timeout=1) for _ in processes] == [None] * len(processes)
    observed = registry.receipts()
    assert len(observed) == len(commits)
    assert {receipt.commits[0] for receipt in observed} == set(commits)


@pytest.mark.parametrize(
    "channel,manifest_revision,resolved,audit,failure,state,commit",
    (
        ("stable", None, None, None, "fetch failed", TargetState.FAILED, None),
        ("stable", None, None, None, None, TargetState.MISSING_REF, None),
        ("stable", "a" * 40, "a" * 40, False, None, TargetState.MODIFIED, "a" * 40),
        ("stable", None, "a" * 40, True, None, TargetState.STALE, "a" * 40),
        ("stable", "b" * 40, "a" * 40, True, None, TargetState.STALE, "b" * 40),
        ("preview", "a" * 40, "a" * 40, True, None, TargetState.PREVIEW, "a" * 40),
        ("stable", "a" * 40, "a" * 40, True, None, TargetState.STABLE, "a" * 40),
        ("feature", "a" * 40, "a" * 40, True, None, TargetState.BRANCH, "a" * 40),
    ),
)
def test_status_classification_table_is_read_only(
    tmp_path: Path,
    channel: str,
    manifest_revision: str | None,
    resolved: str | None,
    audit: bool | None,
    failure: str | None,
    state: TargetState,
    commit: str | None,
) -> None:
    registry = _registry(tmp_path)
    config = _config(tmp_path, channel=channel)
    registry.save(config)
    before = {
        path.relative_to(tmp_path): (
            path.lstat().st_mode,
            path.read_bytes() if path.is_file() else None,
        )
        for path in tmp_path.rglob("*")
    }
    target_id = f"codex-{channel}"

    result = registry.status(
        target_id,
        manifest=_manifest(target_id, manifest_revision) if manifest_revision else None,
        resolved_commit=resolved,
        audit=DeploymentAudit(target_id, audit) if audit is not None else None,
        failure=failure,
    )

    after = {
        path.relative_to(tmp_path): (
            path.lstat().st_mode,
            path.read_bytes() if path.is_file() else None,
        )
        for path in tmp_path.rglob("*")
    }
    assert result == TargetStatus(target_id, state, channel, commit)
    assert after == before


def test_status_uses_matches_as_authority_but_validation_errors_are_modified(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    registry.save(_config(tmp_path))
    target = "codex-stable"
    revision = "a" * 40

    assert registry.status(
        target,
        manifest=_manifest(target, revision),
        resolved_commit=revision,
        audit=DeploymentAudit(target, True, changed=("diagnostic-only",)),
    ).state is TargetState.STABLE
    assert registry.status(
        target,
        manifest=_manifest(target, revision),
        resolved_commit=revision,
        audit=DeploymentAudit(target, True, validation_errors=("invalid manifest",)),
    ).state is TargetState.MODIFIED
    assert registry.status(
        target,
        manifest=_manifest(target, revision),
        resolved_commit=revision,
        audit=DeploymentAudit(target, "yes"),  # type: ignore[arg-type]
    ).state is TargetState.MODIFIED


def test_status_rejects_facts_for_wrong_target_or_framework(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.save(_config(tmp_path))
    revision = "a" * 40
    wrong_target = _manifest("other", revision)
    wrong_framework = DeploymentManifest(
        **{**wrong_target.__dict__, "target_id": "codex-stable", "framework": Framework.CURSOR}
    )

    with pytest.raises(ValueError, match="target"):
        registry.status(
            "codex-stable", manifest=wrong_target, resolved_commit=revision, audit=None
        )
    with pytest.raises(ValueError, match="framework"):
        registry.status(
            "codex-stable", manifest=wrong_framework, resolved_commit=revision, audit=None
        )
    with pytest.raises(ValueError, match="target"):
        registry.status(
            "codex-stable",
            manifest=_manifest("codex-stable", revision),
            resolved_commit=revision,
            audit=DeploymentAudit("other", True),
        )
