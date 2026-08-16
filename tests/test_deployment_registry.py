from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import stat
import subprocess
import sys
import weakref
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

import agent_ops.deployment.registry as registry_module
from agent_ops.deployment.models import (
    DeploymentAudit,
    DeploymentManifest,
    DeploymentReceipt,
    SourceSpec,
    TargetSpec,
    TargetState,
    TargetStatus,
)
from agent_ops.deployment.registry import (
    ChannelSpec,
    DeploymentRegistry,
    RegistryConfig,
    RegistrySnapshot,
)
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


def _tree_snapshot(root: Path) -> dict[Path, tuple[int, bytes | None]]:
    return {
        path.relative_to(root): (
            path.lstat().st_mode,
            path.read_bytes() if path.is_file() else None,
        )
        for path in root.rglob("*")
    }


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
        registry = DeploymentRegistry(Path(path))
        registry.append_receipt(
            DeploymentReceipt("refresh", (commit,), ()),
            snapshot=registry.load_snapshot(),
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


def test_registry_reads_use_existing_lock_without_filesystem_mutation(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.save(_config(tmp_path))
    before = _tree_snapshot(tmp_path)

    snapshot = registry.load_snapshot()
    assert registry.receipt_records() == ()
    assert registry.receipts() == ()
    assert registry.status(
        "codex-stable",
        manifest=_manifest("codex-stable", "a" * 40),
        resolved_commit="a" * 40,
        audit=DeploymentAudit("codex-stable", True),
    ).state is TargetState.STABLE

    assert snapshot.config == _config(tmp_path)
    assert _tree_snapshot(tmp_path) == before


@pytest.mark.parametrize("operation", ("load", "records", "status"))
def test_registry_reads_fail_closed_when_initialization_lock_is_missing(
    tmp_path: Path, operation: str
) -> None:
    registry = _registry(tmp_path)
    registry.save(_config(tmp_path))
    registry.lock_path.unlink()

    with pytest.raises(RuntimeError, match="initializ|lock.*missing"):
        if operation == "load":
            registry.load_snapshot()
        elif operation == "records":
            registry.receipt_records()
        else:
            registry.status(
                "codex-stable",
                manifest=None,
                resolved_commit=None,
                audit=None,
            )

    assert not registry.lock_path.exists()


@pytest.mark.parametrize("replacement", ("wrong-mode", "symlink"))
def test_registry_reads_reject_invalid_existing_lock_without_replacing_it(
    tmp_path: Path, replacement: str
) -> None:
    registry = _registry(tmp_path)
    registry.save(_config(tmp_path))
    if replacement == "wrong-mode":
        registry.lock_path.chmod(0o640)
    else:
        destination = tmp_path / "lock-target"
        destination.write_text("not a lock")
        registry.lock_path.unlink()
        registry.lock_path.symlink_to(destination)
    before = _tree_snapshot(tmp_path)

    with pytest.raises((OSError, RuntimeError, PermissionError), match="lock|refus"):
        registry.load_snapshot()

    assert _tree_snapshot(tmp_path) == before


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
    registry.save(_config(tmp_path))
    registry.path.write_text(text)
    registry.path.chmod(0o600)

    with pytest.raises(ValueError, match=message):
        registry.load()


def test_registry_rejects_unknown_nested_keys_and_invalid_references(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.save(_config(tmp_path))
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

    monkeypatch.setattr(registry_module, "_replace_at", fail_replace, raising=False)
    with pytest.raises(OSError, match="replace failed"):
        registry.save(_config(tmp_path, channel="feature"))

    assert registry.path.read_bytes() == original
    assert not tuple(tmp_path.glob(".deployment-registry.yaml.*.tmp"))


def test_save_rejects_displaced_canonical_parent_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "root" / "live"
    parent.mkdir(parents=True)
    registry = DeploymentRegistry(parent / "deployment-registry.yaml")
    registry.save(_config(parent))
    original = registry.path.read_bytes()
    displaced = parent.with_name("displaced")
    moved = False
    verify = registry_module._verify_canonical_parent

    def displace_before_verification(
        path: Path, descriptor: int, identities: tuple[tuple[int, int], ...]
    ) -> None:
        nonlocal moved
        if not moved:
            parent.rename(displaced)
            parent.mkdir()
            moved = True
        verify(path, descriptor, identities)

    monkeypatch.setattr(
        registry_module,
        "_verify_canonical_parent",
        displace_before_verification,
        raising=False,
    )
    with pytest.raises(RuntimeError, match="canonical registry parent"):
        registry.save(_config(parent, channel="feature"))

    assert not registry.path.exists()
    assert (displaced / registry.path.name).read_bytes() == original
    assert not tuple(displaced.glob(".deployment-registry.yaml.*.tmp"))


def test_save_rolls_back_if_canonical_parent_changes_after_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "root" / "live"
    parent.mkdir(parents=True)
    registry = DeploymentRegistry(parent / "deployment-registry.yaml")
    registry.save(_config(parent))
    original = registry.path.read_bytes()
    displaced = parent.with_name("displaced")
    verify = registry_module._verify_canonical_parent
    calls = 0

    def displace_after_publication(
        path: Path, descriptor: int, identities: tuple[tuple[int, int], ...]
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 6:
            parent.rename(displaced)
            parent.mkdir()
        verify(path, descriptor, identities)

    monkeypatch.setattr(registry_module, "_verify_canonical_parent", displace_after_publication)
    with pytest.raises(RuntimeError, match="canonical registry parent"):
        registry.save(_config(parent, channel="feature"))

    assert not registry.path.exists()
    assert (displaced / registry.path.name).read_bytes() == original


def test_save_revalidates_parent_after_backup_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "root" / "live"
    parent.mkdir(parents=True)
    registry = DeploymentRegistry(parent / "deployment-registry.yaml")
    registry.save(_config(parent))
    displaced = parent.with_name("displaced")
    verify = registry_module._verify_canonical_parent
    calls = 0

    def displace_at_final_verification(
        path: Path, descriptor: int, identities: tuple[tuple[int, int], ...]
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 7:
            parent.rename(displaced)
            parent.mkdir()
        verify(path, descriptor, identities)

    monkeypatch.setattr(
        registry_module, "_verify_canonical_parent", displace_at_final_verification
    )
    with pytest.raises(RuntimeError, match="canonical registry parent"):
        registry.save(_config(parent, channel="feature"))

    assert not registry.path.exists()
    assert (displaced / registry.path.name).exists()


@pytest.mark.parametrize("exception", (KeyboardInterrupt, SystemExit))
def test_save_preserves_process_control_from_final_parent_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exception: type[BaseException],
) -> None:
    registry = _registry(tmp_path)
    registry.save(_config(tmp_path))
    verify = registry_module._verify_canonical_parent
    calls = 0

    def interrupt_final_check(
        path: Path, descriptor: int, identities: tuple[tuple[int, int], ...]
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 7:
            raise exception("process control")
        verify(path, descriptor, identities)

    monkeypatch.setattr(registry_module, "_verify_canonical_parent", interrupt_final_check)
    with pytest.raises(exception, match="process control"):
        registry.save(_config(tmp_path, channel="feature"))


def test_receipts_are_private_strict_append_only_and_stably_ordered(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.save(_config(tmp_path))
    snapshot = registry.load_snapshot()
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

    first_path = registry.append_receipt(first, snapshot=snapshot)
    second_path = registry.append_receipt(second, snapshot=snapshot)

    assert registry.receipts() == (first, second)
    assert first_path != second_path
    assert first_path.read_bytes().endswith(b"\n")
    stored = json.loads(first_path.read_bytes())
    assert stored == {
        "schema_version": 1,
        "registry_fingerprint": hashlib.sha256(registry.path.read_bytes()).hexdigest(),
        "receipt": {
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
        },
    }
    assert stat.S_IMODE(first_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(first_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(second_path.stat().st_mode) == 0o600
    assert (registry.state_path / "receipt-count").read_bytes() == b"2\n"
    assert stat.S_IMODE((registry.state_path / "receipt-count").stat().st_mode) == 0o600


def test_snapshot_fingerprint_binds_receipts_and_expected_revision(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.save(_config(tmp_path))
    snapshot = registry.load_snapshot()

    assert snapshot.config == _config(tmp_path)
    assert snapshot.fingerprint == hashlib.sha256(registry.path.read_bytes()).hexdigest()
    with pytest.raises(FrozenInstanceError):
        snapshot.fingerprint = "0" * 64  # type: ignore[misc]

    receipt = DeploymentReceipt("refresh", ("a" * 40,), ())
    registry.append_receipt(receipt, snapshot=snapshot)
    assert registry.receipts() == (receipt,)


def test_append_requires_exact_current_snapshot_even_for_same_registry_bytes(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    registry.save(_config(tmp_path))
    snapshot = registry.load_snapshot()
    receipt = DeploymentReceipt("refresh", ("a" * 40,), ())

    with pytest.raises(TypeError, match="snapshot"):
        registry.append_receipt(receipt)

    registry.save(_config(tmp_path))
    with pytest.raises(ValueError, match="snapshot|registry"):
        registry.append_receipt(receipt, snapshot=snapshot)

    assert not registry.state_path.exists()


@pytest.mark.parametrize("replacement_check", (3, 4))
def test_append_rejects_registry_replacement_after_receipt_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement_check: int,
) -> None:
    registry = _registry(tmp_path)
    registry.save(_config(tmp_path))
    snapshot = registry.load_snapshot()
    require_current = registry_module._require_current_snapshot
    calls = 0

    def replace_registry_at_check(
        parent: int, name: str, expected: RegistrySnapshot
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == replacement_check:
            replacement = tmp_path / "same-bytes-replacement"
            replacement.write_bytes(registry.path.read_bytes())
            replacement.chmod(0o600)
            os.replace(replacement, registry.path)
        require_current(parent, name, expected)

    monkeypatch.setattr(
        registry_module, "_require_current_snapshot", replace_registry_at_check
    )
    with pytest.raises(ValueError, match="snapshot"):
        registry.append_receipt(
            DeploymentReceipt("refresh", ("a" * 40,), ()), snapshot=snapshot
        )

    assert (registry.state_path / "receipt-count").read_bytes() == b"0\n"
    assert not tuple(registry.receipts_path.glob("*.json"))


@pytest.mark.parametrize("exception", (KeyboardInterrupt, SystemExit))
def test_append_preserves_process_control_from_snapshot_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exception: type[BaseException],
) -> None:
    registry = _registry(tmp_path)
    registry.save(_config(tmp_path))
    snapshot = registry.load_snapshot()
    require_current = registry_module._require_current_snapshot
    calls = 0

    def interrupt_third_check(
        parent: int, name: str, expected: RegistrySnapshot
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise exception("process control")
        require_current(parent, name, expected)

    monkeypatch.setattr(registry_module, "_require_current_snapshot", interrupt_third_check)
    with pytest.raises(exception, match="process control"):
        registry.append_receipt(
            DeploymentReceipt("refresh", ("a" * 40,), ()), snapshot=snapshot
        )

    assert (registry.state_path / "receipt-count").read_bytes() == b"0\n"
    assert not tuple(registry.receipts_path.glob("*.json"))


def test_save_recovers_strict_old_fingerprint_orphan_before_new_registry(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    registry.save(_config(tmp_path))
    old_snapshot = registry.load_snapshot()
    receipt = DeploymentReceipt("refresh", ("a" * 40,), ())
    first = registry.append_receipt(receipt, snapshot=old_snapshot)
    counter = registry.state_path / "receipt-count"
    counter.write_text("0\n")
    counter.chmod(0o600)

    registry.save(_config(tmp_path, channel="feature"))

    assert counter.read_bytes() == b"1\n"
    record = registry.receipt_records()[0]
    assert record.sequence == 0
    assert record.registry_fingerprint == old_snapshot.fingerprint
    assert record.receipt == receipt
    assert first.exists()


def test_save_refuses_corrupt_orphan_without_replacing_registry(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.save(_config(tmp_path))
    snapshot = registry.load_snapshot()
    receipt = DeploymentReceipt("refresh", ("a" * 40,), ())
    first = registry.append_receipt(receipt, snapshot=snapshot)
    original_registry = registry.path.read_bytes()
    counter = registry.state_path / "receipt-count"
    counter.write_text("0\n")
    counter.chmod(0o600)
    wrapper = json.loads(first.read_bytes())
    wrapper["registry_fingerprint"] = "0" * 64
    first.write_text(json.dumps(wrapper, separators=(",", ":"), sort_keys=True) + "\n")
    first.chmod(0o600)

    with pytest.raises(RuntimeError, match="fingerprint|orphan|history"):
        registry.save(_config(tmp_path, channel="feature"))

    assert registry.path.read_bytes() == original_registry
    assert counter.read_bytes() == b"0\n"


def test_save_streams_large_historical_receipt_validation_with_constant_retention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = _registry(tmp_path)
    registry.save(_config(tmp_path))
    snapshot = registry.load_snapshot()
    registry.state_path.mkdir(mode=0o700)
    registry.receipts_path.mkdir(mode=0o700)
    count = 96
    counter = registry.state_path / "receipt-count"
    counter.write_text(f"{count}\n")
    counter.chmod(0o600)
    payload = b"x" * (32 * 1024)
    for sequence in range(count):
        receipt_path = registry.receipts_path / f"{sequence:020d}.json"
        receipt_path.write_bytes(payload)
        receipt_path.chmod(0o600)

    alive: weakref.WeakSet[object] = weakref.WeakSet()
    peak_retained = 0

    class ParsedReceipt:
        pass

    def track_parse(directory: int, name: str) -> tuple[str, object]:
        del directory, name
        nonlocal peak_retained
        parsed = ParsedReceipt()
        alive.add(parsed)
        peak_retained = max(peak_retained, len(alive))
        return snapshot.fingerprint, parsed

    monkeypatch.setattr(registry_module, "_read_receipt_wrapper", track_parse)
    registry.save(_config(tmp_path, channel="feature"))

    assert peak_retained <= 2
    assert len(alive) <= 1


def test_receipt_records_preserve_mixed_registry_fingerprints(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.save(_config(tmp_path))
    stable = registry.load_snapshot()
    first = DeploymentReceipt("refresh", ("a" * 40,), ())
    registry.append_receipt(first, snapshot=stable)
    registry.save(_config(tmp_path, channel="feature"))
    feature = registry.load_snapshot()
    second = DeploymentReceipt("refresh", ("b" * 40,), ())
    registry.append_receipt(second, snapshot=feature)

    records = registry.receipt_records()

    assert tuple(type(record) for record in records) == (
        registry_module.ReceiptRecord,
        registry_module.ReceiptRecord,
    )
    assert tuple(record.sequence for record in records) == (0, 1)
    assert tuple(record.registry_fingerprint for record in records) == (
        stable.fingerprint,
        feature.fingerprint,
    )
    assert tuple(record.receipt for record in records) == (first, second)
    assert registry.receipts() == (first, second)
    with pytest.raises(FrozenInstanceError):
        records[0].sequence = 2  # type: ignore[misc]


def test_append_fully_parses_registry_before_receipt_mutation(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.save(_config(tmp_path))
    snapshot = registry.load_snapshot()
    registry.path.write_text("schema_version: 1\ninvalid: true\n")
    registry.path.chmod(0o600)

    with pytest.raises(ValueError, match="unknown|missing"):
        registry.append_receipt(
            DeploymentReceipt("refresh", ("a" * 40,), ()), snapshot=snapshot
        )

    assert not registry.state_path.exists()


def test_append_rejects_displaced_canonical_parent_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "root" / "live"
    parent.mkdir(parents=True)
    registry = DeploymentRegistry(parent / "deployment-registry.yaml")
    registry.save(_config(parent))
    snapshot = registry.load_snapshot()
    displaced = parent.with_name("displaced")
    moved = False
    verify = registry_module._verify_canonical_parent

    def displace_before_verification(
        path: Path, descriptor: int, identities: tuple[tuple[int, int], ...]
    ) -> None:
        nonlocal moved
        if not moved:
            parent.rename(displaced)
            parent.mkdir()
            moved = True
        verify(path, descriptor, identities)

    monkeypatch.setattr(
        registry_module,
        "_verify_canonical_parent",
        displace_before_verification,
        raising=False,
    )
    with pytest.raises(RuntimeError, match="canonical registry parent"):
        registry.append_receipt(
            DeploymentReceipt("refresh", ("a" * 40,), ()), snapshot=snapshot
        )

    assert not registry.path.exists()
    displaced_receipts = displaced / f"{registry.path.name}.state" / "receipts"
    assert not displaced_receipts.exists() or not tuple(displaced_receipts.glob("*.json"))


def test_append_rolls_back_if_canonical_parent_changes_after_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "root" / "live"
    parent.mkdir(parents=True)
    registry = DeploymentRegistry(parent / "deployment-registry.yaml")
    registry.save(_config(parent))
    snapshot = registry.load_snapshot()
    displaced = parent.with_name("displaced")
    verify = registry_module._verify_canonical_parent
    moved = False

    def displace_after_publication(
        path: Path, descriptor: int, identities: tuple[tuple[int, int], ...]
    ) -> None:
        nonlocal moved
        state = parent / f"{registry.path.name}.state"
        receipt_files = tuple((state / "receipts").glob("*.json"))
        counter = state / "receipt-count"
        if not moved and receipt_files and counter.read_bytes() == b"1\n":
            parent.rename(displaced)
            parent.mkdir()
            moved = True
        verify(path, descriptor, identities)

    monkeypatch.setattr(registry_module, "_verify_canonical_parent", displace_after_publication)
    with pytest.raises(RuntimeError, match="canonical registry parent"):
        registry.append_receipt(
            DeploymentReceipt("refresh", ("a" * 40,), ()), snapshot=snapshot
        )

    receipts = displaced / f"{registry.path.name}.state" / "receipts"
    assert not tuple(receipts.glob("*.json"))
    assert (displaced / f"{registry.path.name}.state" / "receipt-count").read_bytes() == b"0\n"


def test_append_rejects_replaced_registry_before_creating_receipt_state(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.save(_config(tmp_path))
    snapshot = registry.load_snapshot()
    replacement = tmp_path / "replacement.yaml"
    replacement.write_text("untrusted\n")
    registry.path.unlink()
    registry.path.symlink_to(replacement)

    with pytest.raises((OSError, RuntimeError), match="symlink|regular|refus"):
        registry.append_receipt(
            DeploymentReceipt("refresh", ("a" * 40,), ()), snapshot=snapshot
        )

    assert not registry.state_path.exists()


def test_corrupt_receipt_fails_closed(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.save(_config(tmp_path))
    snapshot = registry.load_snapshot()
    receipt = DeploymentReceipt("refresh", ("a" * 40,), ())
    path = registry.append_receipt(receipt, snapshot=snapshot)

    path.write_text(
        '{"schema_version":1,"registry_fingerprint":"'
        + "a" * 64
        + '","receipt":{"operation":"refresh","operation":"again","commits":[],"targets":[]}}\n'
    )
    path.chmod(0o600)
    with pytest.raises(ValueError, match="duplicate"):
        registry.receipts()

    path.write_text(
        '{"schema_version":1,"registry_fingerprint":"'
        + "a" * 64
        + '","receipt":{"operation":"refresh","commits":["short"],"targets":[]}}\n'
    )
    path.chmod(0o600)
    with pytest.raises(ValueError, match="commit"):
        registry.receipts()


def test_receipt_counter_detects_missing_final_and_corrupt_state(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.save(_config(tmp_path))
    snapshot = registry.load_snapshot()
    first = registry.append_receipt(
        DeploymentReceipt("refresh", ("a" * 40,), ()), snapshot=snapshot
    )
    final = registry.append_receipt(
        DeploymentReceipt("refresh", ("b" * 40,), ()), snapshot=snapshot
    )
    final.unlink()

    with pytest.raises(RuntimeError, match="history|missing"):
        registry.receipts()

    final.write_bytes(first.read_bytes())
    final.chmod(0o600)
    counter = registry.state_path / "receipt-count"
    counter.write_text("not-an-integer\n")
    counter.chmod(0o600)
    with pytest.raises(ValueError, match="counter"):
        registry.receipts()

    counter.unlink()
    with pytest.raises(RuntimeError, match="counter"):
        registry.receipts()


def test_receipt_counter_has_practical_bound_without_expected_name_allocation(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    registry.save(_config(tmp_path))
    snapshot = registry.load_snapshot()
    registry.state_path.mkdir(mode=0o700)
    registry.receipts_path.mkdir(mode=0o700)
    counter = registry.state_path / "receipt-count"
    counter.write_text("1000000\n")
    counter.chmod(0o600)

    assert registry_module._MAX_RECEIPTS == 1_000_000
    with pytest.raises(RuntimeError, match="history"):
        registry.receipt_records()
    with pytest.raises(ValueError, match="bound"):
        registry.append_receipt(
            DeploymentReceipt("refresh", ("a" * 40,), ()), snapshot=snapshot
        )

    counter.write_text("99999999999999999999\n")
    counter.chmod(0o600)
    with pytest.raises(ValueError, match="bound"):
        registry.receipt_records()


def test_append_recovers_only_matching_strict_orphan_receipt(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.save(_config(tmp_path))
    snapshot = registry.load_snapshot()
    receipt = DeploymentReceipt("refresh", ("a" * 40,), ())
    first = registry.append_receipt(receipt, snapshot=snapshot)
    counter = registry.state_path / "receipt-count"
    counter.write_text("0\n")
    counter.chmod(0o600)

    assert registry.append_receipt(receipt, snapshot=snapshot) == first
    assert registry.receipts() == (receipt,)
    assert counter.read_bytes() == b"1\n"

    counter.write_text("0\n")
    counter.chmod(0o600)
    with pytest.raises(RuntimeError, match="orphan"):
        registry.append_receipt(
            DeploymentReceipt("refresh", ("b" * 40,), ()), snapshot=snapshot
        )
    assert tuple(registry.receipts_path.glob("*.json")) == (first,)


def test_append_recovers_matching_linked_stage_left_before_counter_advance(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    registry.save(_config(tmp_path))
    snapshot = registry.load_snapshot()
    receipt = DeploymentReceipt("refresh", ("a" * 40,), ())
    first = registry.append_receipt(receipt, snapshot=snapshot)
    counter = registry.state_path / "receipt-count"
    counter.write_text("0\n")
    counter.chmod(0o600)
    stage = first.with_name(f".{first.name}.interrupted.tmp")
    os.link(first, stage)

    assert registry.append_receipt(receipt, snapshot=snapshot) == first
    assert registry.receipts() == (receipt,)
    assert counter.read_bytes() == b"1\n"
    assert not stage.exists()


def test_append_does_not_consume_stage_over_noncontiguous_history(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    registry.save(_config(tmp_path))
    snapshot = registry.load_snapshot()
    receipt = DeploymentReceipt("refresh", ("a" * 40,), ())
    first = registry.append_receipt(receipt, snapshot=snapshot)
    registry.append_receipt(
        DeploymentReceipt("refresh", ("b" * 40,), ()), snapshot=snapshot
    )
    counter = registry.state_path / "receipt-count"
    counter.write_text("0\n")
    counter.chmod(0o600)
    stage = first.with_name(f".{first.name}.interrupted.tmp")
    os.link(first, stage)

    with pytest.raises(RuntimeError, match="contiguous"):
        registry.append_receipt(receipt, snapshot=snapshot)

    assert stage.exists()
    assert len(tuple(registry.receipts_path.iterdir())) == 3


def test_append_rejects_counter_replacement_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = _registry(tmp_path)
    registry.save(_config(tmp_path))
    snapshot = registry.load_snapshot()
    registry.append_receipt(
        DeploymentReceipt("refresh", ("a" * 40,), ()), snapshot=snapshot
    )
    counter = registry.state_path / "receipt-count"
    attacker = tmp_path / "attacker-counter"
    attacker.write_text("unchanged\n")
    write_all = registry_module._write_all

    def replace_counter(descriptor: int, content: bytes) -> None:
        write_all(descriptor, content)
        if content == b"2\n":
            counter.unlink()
            counter.symlink_to(attacker)

    monkeypatch.setattr(registry_module, "_write_all", replace_counter)
    with pytest.raises(RuntimeError, match="changed|symlink|regular"):
        registry.append_receipt(
            DeploymentReceipt("refresh", ("b" * 40,), ()), snapshot=snapshot
        )

    assert counter.is_symlink()
    assert attacker.read_text() == "unchanged\n"
    assert len(tuple(registry.receipts_path.glob("*.json"))) == 1


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
    try:
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
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)
            if process.is_alive():
                process.kill()
                process.join(timeout=2)
            process.close()
        results.close()
        results.join_thread()


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


@pytest.mark.parametrize(
    "diagnostic",
    ("missing", "changed", "unexpected", "duplicates", "validation_errors"),
)
def test_status_treats_any_audit_diagnostic_as_modified(
    tmp_path: Path,
    diagnostic: str,
) -> None:
    registry = _registry(tmp_path)
    registry.save(_config(tmp_path))
    target = "codex-stable"
    revision = "a" * 40

    audit_values = {diagnostic: ("reported",)}
    assert registry.status(
        target,
        manifest=_manifest(target, revision),
        resolved_commit=revision,
        audit=DeploymentAudit(target, True, **audit_values),
    ).state is TargetState.MODIFIED


def test_status_audit_diagnostics_follow_failure_and_missing_ref_precedence(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    registry.save(_config(tmp_path))
    target = "codex-stable"
    revision = "a" * 40
    audit = DeploymentAudit(target, True, changed=("reported",))

    assert registry.status(
        target,
        manifest=_manifest(target, revision),
        resolved_commit=revision,
        audit=audit,
        failure="operation failed",
    ).state is TargetState.FAILED
    assert registry.status(
        target,
        manifest=_manifest(target, revision),
        resolved_commit=None,
        audit=audit,
    ).state is TargetState.MISSING_REF


def test_status_can_reuse_immutable_snapshot_without_reopening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = _registry(tmp_path)
    registry.save(_config(tmp_path))
    snapshot = registry.load_snapshot()

    def unexpected_reopen() -> object:
        raise AssertionError("status reopened registry")

    monkeypatch.setattr(registry, "load_snapshot", unexpected_reopen)
    status = registry.status(
        "codex-stable",
        manifest=_manifest("codex-stable", "a" * 40),
        resolved_commit="a" * 40,
        audit=DeploymentAudit("codex-stable", True),
        snapshot=snapshot,
    )

    assert status.state is TargetState.STABLE


def test_status_rejects_invalid_audit_matches_type(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.save(_config(tmp_path))
    target = "codex-stable"
    revision = "a" * 40
    assert registry.status(
        target,
        manifest=_manifest(target, revision),
        resolved_commit=revision,
        audit=DeploymentAudit(target, "yes"),  # type: ignore[arg-type]
    ).state is TargetState.MODIFIED


@pytest.mark.parametrize("missing_support", ("dir_fd", "fd"))
def test_platform_preflight_rejects_partial_descriptor_support_before_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing_support: str
) -> None:
    if missing_support == "dir_fd":
        supported = set(os.supports_dir_fd)
        supported.discard(os.link)
        monkeypatch.setattr(os, "supports_dir_fd", supported)
    else:
        supported = set(os.supports_fd)
        supported.discard(os.listdir)
        monkeypatch.setattr(os, "supports_fd", supported)
    registry = _registry(tmp_path)

    with pytest.raises(RuntimeError, match="platform|descriptor"):
        registry.save(_config(tmp_path))

    assert not registry.path.exists()
    assert not registry.state_path.exists()


def test_registry_lock_replacement_after_acquisition_fails_before_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = _registry(tmp_path)
    flock = registry_module.fcntl.flock
    replaced = False

    def replace_lock_after_acquisition(descriptor: int, operation: int) -> None:
        nonlocal replaced
        flock(descriptor, operation)
        if not replaced and operation == registry_module.fcntl.LOCK_EX:
            registry.lock_path.unlink()
            registry.lock_path.write_text("replacement")
            registry.lock_path.chmod(0o600)
            replaced = True

    monkeypatch.setattr(registry_module.fcntl, "flock", replace_lock_after_acquisition)
    with pytest.raises(RuntimeError, match="lock changed"):
        registry.save(_config(tmp_path))

    assert not registry.path.exists()
    assert not registry.state_path.exists()


def test_registry_module_imports_without_posix_lock_module(tmp_path: Path) -> None:
    script = """
import builtins
real_import = builtins.__import__
def blocked(name, *args, **kwargs):
    if name == 'fcntl':
        raise ImportError('simulated unavailable fcntl')
    return real_import(name, *args, **kwargs)
builtins.__import__ = blocked
import agent_ops.deployment.registry as registry
assert registry.fcntl is None
"""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.fspath(Path.cwd() / "src")
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


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
