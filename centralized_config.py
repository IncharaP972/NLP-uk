"""
Centralized environment configuration loader with schema validation and AWS Secrets Manager integration.
"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from hipaa_compliance import create_secure_client

DEFAULT_CONFIG_DIR = os.path.join("config", "runtime")
REQUIRED_TOP_LEVEL_KEYS = [
    "environment",
    "aws",
    "api_endpoints",
    "model_parameters",
    "feature_flags",
    "secrets",
]


@dataclass(frozen=True)
class RuntimeConfig:
    environment: str
    aws: Dict[str, Any]
    api_endpoints: Dict[str, str]
    model_parameters: Dict[str, Any]
    feature_flags: Dict[str, bool]
    secrets: Dict[str, Any]
    raw: Dict[str, Any]


def resolve_environment(explicit_environment: Optional[str] = None) -> str:
    return (explicit_environment or os.getenv("APP_ENV") or "dev").strip().lower()


def _load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"Configuration file must contain an object: {path}")
    return payload


def _validate_schema(config: Dict[str, Any]) -> None:
    missing = [key for key in REQUIRED_TOP_LEVEL_KEYS if key not in config]
    if missing:
        raise ValueError(f"Missing required configuration keys: {', '.join(missing)}")
    if not isinstance(config["aws"], dict):
        raise ValueError("aws must be an object")
    if not isinstance(config["api_endpoints"], dict):
        raise ValueError("api_endpoints must be an object")
    if not isinstance(config["model_parameters"], dict):
        raise ValueError("model_parameters must be an object")
    if not isinstance(config["feature_flags"], dict):
        raise ValueError("feature_flags must be an object")
    if not isinstance(config["secrets"], dict):
        raise ValueError("secrets must be an object")


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _load_secret_payload(secret_name: str, region_name: str, secrets_client=None) -> Dict[str, Any]:
    client = secrets_client or create_secure_client("secretsmanager", region_name=region_name)
    response = client.get_secret_value(SecretId=secret_name)
    if "SecretString" in response:
        value = response["SecretString"]
        parsed = json.loads(value) if value and value.strip().startswith("{") else {"value": value}
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    if "SecretBinary" in response:
        return {"binary_secret": "<redacted>"}
    return {}


def _load_all_secrets(config: Dict[str, Any], secrets_client=None) -> Dict[str, Any]:
    secret_names: List[str] = list(config.get("secrets", {}).get("secret_names", []))
    if not secret_names:
        return {}
    region_name = str(config.get("aws", {}).get("region", os.getenv("AWS_REGION", "us-east-1")))
    merged: Dict[str, Any] = {}
    for secret_name in secret_names:
        payload = _load_secret_payload(secret_name, region_name=region_name, secrets_client=secrets_client)
        merged = _deep_merge(merged, payload)
    return merged


def _apply_environment_overrides(config: Dict[str, Any]) -> Dict[str, Any]:
    overrides: Dict[str, Any] = {}
    if os.getenv("AWS_REGION"):
        overrides.setdefault("aws", {})["region"] = os.getenv("AWS_REGION")
    if os.getenv("FINAL_CONFIDENCE_THRESHOLD"):
        overrides.setdefault("model_parameters", {})["confidence_threshold"] = float(
            os.getenv("FINAL_CONFIDENCE_THRESHOLD")
        )
    return _deep_merge(config, overrides) if overrides else config


def load_runtime_config(
    environment: Optional[str] = None,
    config_dir: str = DEFAULT_CONFIG_DIR,
    load_secrets: Optional[bool] = None,
    secrets_client=None,
) -> RuntimeConfig:
    resolved_env = resolve_environment(environment)
    config_path = os.path.join(config_dir, f"{resolved_env}.json")
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Configuration file not found for environment '{resolved_env}': {config_path}")

    config = _load_json(config_path)
    _validate_schema(config)
    config = _apply_environment_overrides(config)

    should_load_secrets = (
        bool(load_secrets)
        if load_secrets is not None
        else os.getenv("CONFIG_LOAD_SECRETS", "0").strip() == "1"
    )

    secret_overrides = _load_all_secrets(config, secrets_client=secrets_client) if should_load_secrets else {}
    effective = _deep_merge(config, secret_overrides) if secret_overrides else config

    return RuntimeConfig(
        environment=effective["environment"],
        aws=effective["aws"],
        api_endpoints=effective["api_endpoints"],
        model_parameters=effective["model_parameters"],
        feature_flags=effective["feature_flags"],
        secrets=effective["secrets"],
        raw=effective,
    )


def get_api_endpoint(config: RuntimeConfig, key: str) -> str:
    if key not in config.api_endpoints:
        raise KeyError(f"Unknown API endpoint key: {key}")
    return config.api_endpoints[key]


def get_model_parameter(config: RuntimeConfig, key: str, default: Any = None) -> Any:
    return config.model_parameters.get(key, default)


def is_feature_enabled(config: RuntimeConfig, flag_name: str, default: bool = False) -> bool:
    return bool(config.feature_flags.get(flag_name, default))


def redact_effective_config(config: RuntimeConfig) -> Dict[str, Any]:
    payload = deepcopy(config.raw)
    secret_markers = ("secret", "password", "token", "key")

    def _redact(node: Any, parent_key: str = "") -> Any:
        if isinstance(node, dict):
            cleaned: Dict[str, Any] = {}
            for k, v in node.items():
                if any(marker in k.lower() for marker in secret_markers):
                    cleaned[k] = "<redacted>"
                else:
                    cleaned[k] = _redact(v, parent_key=k)
            return cleaned
        if isinstance(node, list):
            return [_redact(item, parent_key=parent_key) for item in node]
        return node

    return _redact(payload)
