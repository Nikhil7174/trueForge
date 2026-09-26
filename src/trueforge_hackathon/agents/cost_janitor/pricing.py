"""Live AWS Price List lookups. No hardcoded prices or SKUs.

Each kind maps to Price List filters plus a usage-type suffix. Exactly one product
with exactly one on-demand price dimension must match, otherwise the lookup is
rejected as ambiguous instead of guessing.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

import boto3

# The Price List API is served from us-east-1 regardless of the priced region.
PRICING_ENDPOINT_REGION = "us-east-1"

VOLUME_TYPES = {"gp2", "gp3", "io1", "io2", "st1", "sc1", "standard"}


@lru_cache(maxsize=1)
def _client():
    return boto3.client("pricing", region_name=PRICING_ENDPOINT_REGION)


def _query(kind: str, region: str, attributes: dict[str, Any]) -> tuple[str, dict[str, str], str]:
    """Return (service_code, term filters, required usagetype suffix)."""
    if kind == "ebs_volume":
        vtype = str(attributes.get("volume_type", "")).lower()
        if vtype not in VOLUME_TYPES:
            raise ValueError(f"attributes.volume_type must be one of {sorted(VOLUME_TYPES)}")
        filters = {"regionCode": region, "productFamily": "Storage", "volumeApiName": vtype}
        return "AmazonEC2", filters, f"EBS:VolumeUsage.{vtype}" if vtype != "standard" else "EBS:VolumeUsage"
    if kind == "ebs_snapshot":
        tier = str(attributes.get("tier", "standard")).lower()
        suffix = {"standard": "EBS:SnapshotUsage", "archive": "EBS:SnapshotArchiveStorage"}.get(tier)
        if suffix is None:
            raise ValueError("attributes.tier must be 'standard' or 'archive'")
        return "AmazonEC2", {"regionCode": region, "productFamily": "Storage Snapshot"}, suffix
    if kind == "public_ipv4":
        state = str(attributes.get("state", "idle")).lower()
        suffix = {"idle": "PublicIPv4:IdleAddress", "in_use": "PublicIPv4:InUseAddress"}.get(state)
        if suffix is None:
            raise ValueError("attributes.state must be 'idle' or 'in_use'")
        return "AmazonVPC", {"regionCode": region, "group": "VPCPublicIPv4Address"}, suffix
    raise ValueError("kind must be one of: ebs_volume, ebs_snapshot, public_ipv4")


def get_price(kind: str, region: str, attributes: dict[str, Any] | None = None) -> dict[str, Any]:
    attributes = attributes or {}
    service, filters, suffix = _query(kind, region, attributes)
    kwargs: dict[str, Any] = {
        "ServiceCode": service,
        "Filters": [{"Type": "TERM_MATCH", "Field": k, "Value": v} for k, v in filters.items()],
        "MaxResults": 100,
    }
    matches: list[dict[str, Any]] = []
    while True:
        page = _client().get_products(**kwargs)
        for raw in page["PriceList"]:
            product = json.loads(raw)
            usagetype = product["product"]["attributes"].get("usagetype", "")
            # usagetype is "<REGION-PREFIX>-<suffix>"; compare the part after the first dash.
            if usagetype.split("-", 1)[-1] == suffix:
                matches.append(product)
        token = page.get("NextToken")
        if not token:
            break
        kwargs["NextToken"] = token

    if len(matches) != 1:
        return {
            "ok": False,
            "error": f"ambiguous or missing price: {len(matches)} products matched",
            "kind": kind,
            "region": region,
            "candidates": [m["product"]["attributes"].get("usagetype") for m in matches][:10],
        }
    product = matches[0]
    dims = [
        (term, dim)
        for term in product.get("terms", {}).get("OnDemand", {}).values()
        for dim in term["priceDimensions"].values()
    ]
    if len(dims) != 1:
        return {"ok": False, "error": f"expected one on-demand price dimension, found {len(dims)}", "kind": kind}
    term, dim = dims[0]
    return {
        "ok": True,
        "kind": kind,
        "region": region,
        "unit": dim["unit"],
        "usd_per_unit": float(dim["pricePerUnit"]["USD"]),
        "description": dim["description"],
        "sku": product["product"]["sku"],
        "usagetype": product["product"]["attributes"].get("usagetype"),
        "effective_date": term.get("effectiveDate"),
        "source": f"AWS Price List API ({service})",
    }
