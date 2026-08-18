"""Native Windows registry backend using retained non-reparse path authority."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

from agent_ops.deployment.models import DeploymentReceipt
from agent_ops.deployment.windows_fs import WindowsPathPins, WindowsTargetLock


def _registry_module() -> Any:
    from agent_ops.deployment import registry

    return registry


def _lock(registry: Any, *, shared: bool, create: bool) -> WindowsTargetLock:
    return WindowsTargetLock(
        registry.path.parent,
        shared=shared,
        create=create,
        lock_name=registry.lock_path.name,
    )


def _validate_config(config: Any) -> None:
    module = _registry_module()
    module._validate_config_fields(config, require_nonempty=True)
    homes: set[str] = set()
    identities: set[tuple[int, int]] = set()
    for target in config.targets:
        home = Path(os.path.normcase(os.path.abspath(target.home.expanduser())))
        key = str(home)
        if key in homes:
            raise ValueError("two targets may not use the same home")
        homes.add(key)
        try:
            with WindowsPathPins(home, create=False) as pins:
                identity = pins.identities[-1]
        except FileNotFoundError:
            continue
        pair = (identity.volume, identity.index)
        if pair in identities:
            raise ValueError("two targets may not use the same home")
        identities.add(pair)


def _snapshot(registry: Any, lock: WindowsTargetLock) -> Any:
    module = _registry_module()
    content = lock.read_file(Path(registry.path.name), maximum=module._MAX_REGISTRY_BYTES)
    config = module._parse_registry(content)
    identity = lock.identity(Path(registry.path.name))
    return module.RegistrySnapshot(
        config,
        hashlib.sha256(content).hexdigest(),
        (identity.volume, identity.index),
    )


def load_snapshot(registry: Any) -> Any:
    with _lock(registry, shared=True, create=False) as lock:
        snapshot = _snapshot(registry, lock)
        lock.verify()
        return snapshot


class _Authority:
    def __init__(self, registry: Any, snapshot: Any, lock: WindowsTargetLock) -> None:
        self.registry = registry
        self.snapshot = snapshot
        self.lock = lock

    def verify(self) -> None:
        if _snapshot(self.registry, self.lock) != self.snapshot:
            raise RuntimeError("retained registry snapshot changed")
        self.lock.verify()


@contextmanager
def retain_snapshot(registry: Any, snapshot: Any) -> Iterator[_Authority]:
    module = _registry_module()
    if type(snapshot) is not module.RegistrySnapshot:
        raise TypeError("snapshot must be an exact RegistrySnapshot")
    with _lock(registry, shared=True, create=False) as lock:
        authority = _Authority(registry, snapshot, lock)
        authority.verify()
        yield authority
        authority.verify()


def save(
    registry: Any,
    config: Any,
    *,
    expected_snapshot: Any | None = None,
) -> Any:
    module = _registry_module()
    _validate_config(config)
    if expected_snapshot is not None and type(expected_snapshot) is not module.RegistrySnapshot:
        raise TypeError("expected snapshot must be an exact RegistrySnapshot")
    content = module._dump_registry(config)
    if module._parse_registry(content) != config:
        raise RuntimeError("rendered registry does not match requested configuration")
    with _lock(registry, shared=False, create=True) as lock:
        relative = Path(registry.path.name)
        prior = lock.read_optional(relative)
        if prior is None:
            if expected_snapshot is not None:
                raise ValueError("registry no longer matches the required snapshot")
            if lock.exists(Path(registry.state_path.name)):
                raise RuntimeError("receipt state exists without an initialized registry")
        else:
            observed = _snapshot(registry, lock)
            if expected_snapshot is not None and observed != expected_snapshot:
                raise ValueError("registry no longer matches the required snapshot")
        try:
            lock.write_atomic(relative, content)
            published = _snapshot(registry, lock)
            if (
                published.config != config
                or published.fingerprint != hashlib.sha256(content).hexdigest()
            ):
                raise RuntimeError("published registry does not match requested configuration")
            lock.verify()
            return published
        except BaseException:
            current = lock.read_optional(relative)
            if current == content:
                if prior is None:
                    lock.unlink(relative)
                else:
                    lock.write_atomic(relative, prior)
            raise


def _receipt_names(registry: Any, lock: WindowsTargetLock) -> list[str]:
    receipts = Path(registry.receipts_path.relative_to(registry.path.parent))
    if not lock.exists(receipts):
        return []
    entries = lock.scan(receipts)
    names = sorted(path.name for path, kind in entries.items() if kind == "regular")
    expected = [f"{index:020d}.json" for index in range(len(names))]
    if names != expected:
        raise RuntimeError("receipt history is not contiguous")
    return names


def append_receipt(registry: Any, receipt: DeploymentReceipt, *, snapshot: Any) -> Path:
    module = _registry_module()
    receipt_data = module._receipt_to_data(receipt)
    if type(snapshot) is not module.RegistrySnapshot:
        raise TypeError("snapshot must be an exact RegistrySnapshot")
    with _lock(registry, shared=False, create=False) as lock:
        if _snapshot(registry, lock) != snapshot:
            raise ValueError("registry no longer matches the required snapshot")
        state = Path(registry.state_path.name)
        receipts = state / "receipts"
        for directory in (state, receipts):
            marker = directory / ".agentops-create-marker"
            with lock.pin_parent(marker, create=True):
                pass
        names = _receipt_names(registry, lock)
        sequence = len(names)
        if sequence >= module._MAX_RECEIPTS:
            raise ValueError("receipt counter is at the supported bound")
        destination = receipts / f"{sequence:020d}.json"
        content = module._dump_receipt_wrapper(receipt_data, snapshot.fingerprint)
        lock.write_new(destination, content)
        try:
            if _snapshot(registry, lock) != snapshot:
                raise ValueError("registry changed during receipt append")
            fingerprint, observed = module._parse_receipt_wrapper(lock.read_file(destination))
            if fingerprint != snapshot.fingerprint or observed != receipt:
                raise RuntimeError("published receipt changed")
            lock.write_atomic(state / "receipt-count", f"{sequence + 1}\n".encode("ascii"))
            lock.verify()
            return registry.receipts_path / destination.name
        except BaseException:
            with suppress(BaseException):
                lock.unlink(destination)
            raise


def receipt_records(registry: Any) -> tuple[Any, ...]:
    module = _registry_module()
    with _lock(registry, shared=True, create=False) as lock:
        snapshot = _snapshot(registry, lock)
        names = _receipt_names(registry, lock)
        state = Path(registry.state_path.name)
        counter_content = lock.read_optional(state / "receipt-count")
        if names:
            if counter_content != f"{len(names)}\n".encode("ascii"):
                raise RuntimeError("receipt counter does not match history")
        elif counter_content not in {None, b"0\n"}:
            raise RuntimeError("receipt counter does not match empty history")
        records = []
        receipts = state / "receipts"
        for sequence, name in enumerate(names):
            fingerprint, receipt = module._parse_receipt_wrapper(
                lock.read_file(receipts / name, maximum=module._MAX_RECEIPT_BYTES)
            )
            if fingerprint != snapshot.fingerprint:
                raise RuntimeError("receipt history fingerprint differs from registry")
            records.append(module.ReceiptRecord(sequence, fingerprint, receipt))
        lock.verify()
        return tuple(records)
