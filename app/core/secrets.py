"""Pluggable secrets loader for production deployments.

The default backend is ``env`` — secrets are read directly from environment
variables, which is exactly what ``.env`` files give us in local dev. In
staging / production, set ``SECRETS_BACKEND`` to one of:

    * ``aws``    — AWS Secrets Manager (requires ``boto3``)
    * ``gcp``    — Google Secret Manager (requires ``google-cloud-secret-manager``)
    * ``vault``  — HashiCorp Vault KV v2 (requires ``hvac``)

Each backend reads a logical secret *name* (lowercase, snake_case) and
optionally prepends ``SECRETS_PREFIX`` (e.g. ``reved/prod/``) when forming
the backend-specific identifier. When the backend cannot resolve a name,
``load_secret`` falls back to ``fallback_env`` (or ``name.upper()``) so the
service can continue to start during a partial migration.

Rotation contract: values are cached in-process for
``SECRETS_CACHE_TTL_SECONDS`` (default 300). Restarting the pod / process
always picks up the freshest value. See ``DEPLOY.md`` §2 for the operator
rotation playbook.
"""

from __future__ import annotations

import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

_DEFAULT_CACHE_TTL_SECONDS = 300

_cache: dict[str, tuple[str, float]] = {}
_cache_lock = threading.Lock()


def _get_backend() -> str:
    return (os.getenv("SECRETS_BACKEND", "env") or "env").strip().lower()


def _get_prefix() -> str:
    return (os.getenv("SECRETS_PREFIX", "") or "").strip()


def _get_cache_ttl() -> int:
    raw = os.getenv("SECRETS_CACHE_TTL_SECONDS")
    if not raw:
        return _DEFAULT_CACHE_TTL_SECONDS
    try:
        return max(0, int(raw))
    except ValueError:
        return _DEFAULT_CACHE_TTL_SECONDS


def _qualified_name(name: str) -> str:
    prefix = _get_prefix()
    if not prefix:
        return name
    if prefix.endswith("/"):
        return f"{prefix}{name}"
    return f"{prefix}/{name}"


def _cache_get(key: str) -> str | None:
    ttl = _get_cache_ttl()
    if ttl <= 0:
        return None
    with _cache_lock:
        entry = _cache.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if time.monotonic() >= expires_at:
            _cache.pop(key, None)
            return None
        return value


def _cache_put(key: str, value: str) -> None:
    ttl = _get_cache_ttl()
    if ttl <= 0:
        return
    with _cache_lock:
        _cache[key] = (value, time.monotonic() + ttl)


def clear_cache() -> None:
    """Drop all cached secret values. Intended for tests and forced reload."""

    with _cache_lock:
        _cache.clear()


def _read_aws(secret_id: str) -> str | None:
    try:
        import boto3  # type: ignore[import-not-found]
        from botocore.exceptions import BotoCoreError, ClientError  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - depends on optional install
        raise RuntimeError(
            "SECRETS_BACKEND=aws requires the boto3 package; install with `pip install boto3`."
        ) from exc

    region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
    client = boto3.client("secretsmanager", region_name=region) if region else boto3.client("secretsmanager")
    try:
        response = client.get_secret_value(SecretId=secret_id)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in {"ResourceNotFoundException", "AccessDeniedException"}:
            logger.warning("aws_secrets_manager_miss", extra={"secret_id": secret_id, "code": code})
            return None
        raise
    except BotoCoreError:
        raise

    if "SecretString" in response:
        return response["SecretString"]
    if "SecretBinary" in response:
        binary = response["SecretBinary"]
        return binary.decode("utf-8") if isinstance(binary, (bytes, bytearray)) else str(binary)
    return None


def _read_gcp(secret_id: str) -> str | None:
    try:
        from google.api_core import exceptions as gcp_exceptions  # type: ignore[import-not-found]
        from google.cloud import secretmanager  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - depends on optional install
        raise RuntimeError(
            "SECRETS_BACKEND=gcp requires google-cloud-secret-manager; "
            "install with `pip install google-cloud-secret-manager`."
        ) from exc

    project = os.getenv("GCP_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT")
    if not project:
        raise RuntimeError("SECRETS_BACKEND=gcp requires GCP_PROJECT or GOOGLE_CLOUD_PROJECT env var.")

    client = secretmanager.SecretManagerServiceClient()
    if "/" in secret_id and secret_id.startswith("projects/"):
        resource = secret_id
    else:
        resource = f"projects/{project}/secrets/{secret_id}/versions/latest"

    try:
        response = client.access_secret_version(name=resource)
    except gcp_exceptions.NotFound:
        logger.warning("gcp_secret_manager_miss", extra={"secret_id": secret_id})
        return None
    except gcp_exceptions.PermissionDenied:
        logger.warning("gcp_secret_manager_denied", extra={"secret_id": secret_id})
        return None
    return response.payload.data.decode("utf-8")


def _read_vault(secret_id: str) -> str | None:
    try:
        import hvac  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - depends on optional install
        raise RuntimeError(
            "SECRETS_BACKEND=vault requires hvac; install with `pip install hvac`."
        ) from exc

    addr = os.getenv("VAULT_ADDR")
    token = os.getenv("VAULT_TOKEN")
    if not addr or not token:
        raise RuntimeError("SECRETS_BACKEND=vault requires VAULT_ADDR and VAULT_TOKEN.")

    mount_point = os.getenv("VAULT_KV_MOUNT", "secret")
    client = hvac.Client(url=addr, token=token)
    try:
        response = client.secrets.kv.v2.read_secret_version(path=secret_id, mount_point=mount_point)
    except hvac.exceptions.InvalidPath:
        logger.warning("vault_secret_miss", extra={"path": secret_id})
        return None

    data = response.get("data", {}).get("data", {}) if isinstance(response, dict) else {}
    if "value" in data and isinstance(data["value"], str):
        return data["value"]
    if len(data) == 1:
        only = next(iter(data.values()))
        if isinstance(only, str):
            return only
    return None


_BACKEND_READER_NAMES = {
    "aws": "_read_aws",
    "gcp": "_read_gcp",
    "vault": "_read_vault",
}


def _read_from_backend(backend: str, name: str) -> str | None:
    attr_name = _BACKEND_READER_NAMES.get(backend)
    if attr_name is None:
        raise RuntimeError(
            f"Unknown SECRETS_BACKEND={backend!r}. Expected one of: env, aws, gcp, vault."
        )
    # Look up the reader lazily from the module's globals so test-time monkeypatching
    # of the module-level _read_aws / _read_gcp / _read_vault functions takes effect.
    reader = globals()[attr_name]
    secret_id = _qualified_name(name)
    try:
        return reader(secret_id)
    except RuntimeError:
        raise
    except Exception as exc:  # noqa: BLE001 - log and degrade to env fallback
        logger.error(
            "secrets_backend_error",
            extra={"backend": backend, "secret_id": secret_id, "error": str(exc)},
        )
        return None


def load_secret(name: str, fallback_env: str | None = None) -> str:
    """Resolve a secret value.

    Resolution order:
        1. In-process cache (TTL controlled by ``SECRETS_CACHE_TTL_SECONDS``).
        2. The configured backend (skipped when ``SECRETS_BACKEND`` is ``env``).
        3. The environment variable named ``fallback_env`` or ``name.upper()``.

    Returns an empty string if nothing resolves. Callers that require the
    value should validate non-emptiness themselves (the existing
    ``_require_env`` pattern in ``config.py`` already does this).
    """

    cache_key = f"{_get_backend()}::{name}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    backend = _get_backend()
    if backend != "env":
        value = _read_from_backend(backend, name)
        if value:
            stripped = value.strip()
            _cache_put(cache_key, stripped)
            return stripped

    env_name = fallback_env or name.upper()
    env_value = (os.getenv(env_name, "") or "").strip()
    if env_value:
        _cache_put(cache_key, env_value)
    return env_value
