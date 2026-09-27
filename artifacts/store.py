"""
Minimal artifact storage: one JSON file per (capability_id, version) under
artifacts/store/. A real deployment would put this behind a service with
approval workflow (see REPORT.md Cuts / stretch goal "Confidence &
approval"); for this project a directory of versioned JSON files is
sufficient to demonstrate save -> load -> replay and keeps the "how it's
stored/serialized" decision honest and inspectable (Section 4).
"""
from __future__ import annotations

import glob
import os

from artifacts.schema import CapabilityArtifact

STORE_DIR = os.path.join(os.path.dirname(__file__), "store")


def save(artifact: CapabilityArtifact) -> str:
    os.makedirs(STORE_DIR, exist_ok=True)
    path = os.path.join(STORE_DIR, f"{artifact.capability_id}.v{artifact.version}.json")
    with open(path, "w") as f:
        f.write(artifact.to_json())
    latest = os.path.join(STORE_DIR, f"{artifact.capability_id}.latest.json")
    with open(latest, "w") as f:
        f.write(artifact.to_json())
    return path


def load(capability_id: str, version: "int | None" = None) -> CapabilityArtifact:
    if version is None:
        path = os.path.join(STORE_DIR, f"{capability_id}.latest.json")
    else:
        path = os.path.join(STORE_DIR, f"{capability_id}.v{version}.json")
    with open(path) as f:
        return CapabilityArtifact.model_validate_json(f.read())


def list_capabilities() -> list:
    if not os.path.isdir(STORE_DIR):
        return []
    return sorted({
        os.path.basename(p).split(".latest.json")[0]
        for p in glob.glob(os.path.join(STORE_DIR, "*.latest.json"))
    })
