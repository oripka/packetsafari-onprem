"""Stable product vocabulary and strict, runtime-independent policy validation."""
from __future__ import annotations

CAPABILITIES = {
    "analysis.rca": "PacketSafari RCA",
    "analysis.security": "PacketSafari Security",
    "capture.remote": "PacketSafari Capture",
    "capture.decrypt": "Capture Decryption",
    "ndr.monitoring": "PacketSafari NDR",
}
EDITIONS = {"onprem", "onprem_airgapped"}


def capability_list(value):
    if not isinstance(value, list) or any(type(v) is not str or v not in CAPABILITIES for v in value):
        raise ValueError("Expected a list of known product capabilities")
    if len(value) != len(set(value)):
        raise ValueError("Duplicate product capability")
    return sorted(value)


def validate_policy(value):
    if not isinstance(value, dict) or set(value) - {"grants", "disabled", "teamDisabled"}:
        raise ValueError("Invalid product policy")
    grants = value.get("grants")
    if grants is not None:
        grants = capability_list(grants)
        if "capture.decrypt" in grants and "capture.remote" not in grants:
            raise ValueError("Decryption requires Capture")
    return {"grants": grants, "disabled": capability_list(value.get("disabled", [])),
            "teamDisabled": capability_list(value.get("teamDisabled", []))}


def policy_denial(capability, layers):
    """None grants inherit legacy rights; [] explicitly includes no products."""
    if capability not in CAPABILITIES:
        raise ValueError("Unknown product capability")
    required = {capability}
    if capability == "capture.decrypt":
        required.add("capture.remote")
    for source, raw in layers:
        try:
            policy = validate_policy(raw)
        except ValueError:
            return f"{source}:invalid_policy"
        if policy["grants"] is not None and not required.issubset(policy["grants"]):
            return f"{source}:not_included"
        if required.intersection(policy["disabled"]):
            return f"{source}:disabled_by_admin"
        if required.intersection(policy["teamDisabled"]):
            return f"{source}:disabled_by_team"
    return None


def license_products_error(payload):
    schema = payload.get("schema_version", payload.get("schemaVersion"))
    if schema != 4:
        return "product_claims_require_schema_4" if any(k in payload for k in ("capabilities", "edition")) else None
    try:
        grants = capability_list(payload.get("capabilities"))
        validate_policy({"grants": grants})
        if payload.get("edition") not in EDITIONS:
            raise ValueError("Invalid edition")
    except ValueError:
        return "invalid_product_claims"
    return None
