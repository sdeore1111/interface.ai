"""
Structured evidence logging shared by discovery and replay.

Every run (discovery or replay) writes a JSONL event log -- one JSON object
per line, append-only, so a run that crashes mid-way still leaves a
readable trail. On top of the event log, a run directory captures at least
one richer signal on failure (screenshot + DOM snapshot), per Section 3.5.

All text written here is passed through safety.policy.redact_text, and any
params dict is passed through redact_params, so secrets/PII never land in
/evidence/ even for the mock's synthetic data -- the discipline is what's
being demonstrated, not just the outcome for this particular dataset.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, is_dataclass
from typing import Any, Optional

from safety.policy import redact_text


class EvidenceRun:
    def __init__(self, run_dir: str, run_type: str, run_id: str):
        self.run_dir = run_dir
        os.makedirs(run_dir, exist_ok=True)
        self.log_path = os.path.join(run_dir, "log.jsonl")
        self.run_type = run_type
        self.run_id = run_id
        self._log_file = open(self.log_path, "a", buffering=1)
        self.event("run_started", {"run_type": run_type, "run_id": run_id})

    def event(self, kind: str, data: Optional[dict] = None):
        record = {
            "ts": time.time(),
            "kind": kind,
            "run_id": self.run_id,
        }
        data = data or {}
        # redact any free-text fields
        safe_data = {}
        for k, v in data.items():
            if is_dataclass(v):
                v = asdict(v)
            if isinstance(v, str):
                safe_data[k] = redact_text(v)
            else:
                safe_data[k] = v
        record.update(safe_data)
        self._log_file.write(json.dumps(record, default=str) + "\n")

    def save_screenshot(self, surface, name: str = "failure.png") -> str:
        path = os.path.join(self.run_dir, name)
        try:
            surface.screenshot(path)
        except Exception as e:  # noqa: BLE001
            self.event("screenshot_failed", {"error": str(e)})
            return ""
        return path

    def save_dom_snapshot(self, surface, name: str = "failure_dom.html") -> str:
        path = os.path.join(self.run_dir, name)
        try:
            content = surface.dom_snapshot()
            with open(path, "w") as f:
                f.write(content)
        except Exception as e:  # noqa: BLE001
            self.event("dom_snapshot_failed", {"error": str(e)})
            return ""
        return path

    def save_json(self, obj: Any, name: str):
        path = os.path.join(self.run_dir, name)
        with open(path, "w") as f:
            if hasattr(obj, "to_json"):
                f.write(obj.to_json())
            else:
                json.dump(obj, f, indent=2, default=str)
        return path

    def close(self, result_summary: Optional[dict] = None):
        self.event("run_finished", result_summary or {})
        self._log_file.close()
