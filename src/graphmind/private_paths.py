"""Create private data directories without replacing a provisioned Windows DACL."""
from __future__ import annotations

import os
from pathlib import Path


def mkdir_private(path: Path, root: Path, *, exist_ok: bool = False) -> None:
    """Confine children to root; inherit its NTFS policy, retain POSIX mode 0700.

    The data root itself is created with 0700 on every platform. On Windows 3.13,
    0700 creates an owner-only DACL, so descendants must instead inherit the
    already protected root's explicitly provisioned service-account permissions.
    """
    root = Path(root).resolve()
    target = Path(path).resolve()
    if target != root and not target.is_relative_to(root):
        raise ValueError("Private directory must remain inside the data root")
    if target == root:
        root.mkdir(parents=True, mode=0o700, exist_ok=exist_ok)
        return
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    current = root
    mode = 0o777 if os.name == 'nt' else 0o700
    parts = target.relative_to(root).parts
    for position, part in enumerate(parts):
        current /= part
        current.mkdir(mode=mode, exist_ok=exist_ok if position == len(parts)-1 else True)
