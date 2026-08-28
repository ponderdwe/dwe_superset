"""
DWE Superset — Pulumi entry point.
Reads cloud_provider from dwe-hydration.yaml and delegates to the provider module.
"""

import yaml
import pulumi
from pathlib import Path

_hydration = Path(__file__).parent / "dwe-hydration.yaml"
if _hydration.exists():
    cloud_provider = yaml.safe_load(_hydration.read_text()).get("cloud_provider", "azure")
else:
    cloud_provider = pulumi.Config().get("cloud_provider") or "azure"

if cloud_provider == "azure":
    import _azure  # noqa: F401
else:
    import _aws  # noqa: F401
