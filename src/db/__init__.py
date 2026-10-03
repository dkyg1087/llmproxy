"""
Modular Database Access Layer for LLMProxy.
Encapsulates SQLite connection lifecycles, schema management,
key encryption, model cataloging, usage tracking, and audit/traces.
"""

from src.db.connection import (
    INIT_SCHEMA_SQL,
    get_db_connection,
    init_db,
)

from src.db.settings import (
    get_setting,
    set_setting,
    get_all_settings,
    delete_setting,
)

from src.db.keys import (
    encrypt_key,
    decrypt_key,
    get_admin_keys,
    get_decrypted_key,
    save_key,
    toggle_key,
    set_key_health,
    delete_key,
)

from src.db.models import (
    get_candidate_models,
    get_admin_models,
    add_model,
    update_model,
    toggle_model,
    set_model_health,
    delete_model,
    update_model_limits,
)

from src.db.usage import (
    log_request_usage,
    add_cooldown,
    clear_cooldown,
    get_model_usage,
    get_model_ms_per_token,
)

from src.db.logs import (
    log_request_trace,
    get_recent_traces,
    log_admin_audit,
    get_audit_logs,
    get_admin_analytics,
)

__all__ = [
    # connection
    "INIT_SCHEMA_SQL",
    "get_db_connection",
    "init_db",
    # settings
    "get_setting",
    "set_setting",
    "get_all_settings",
    "delete_setting",
    # keys
    "encrypt_key",
    "decrypt_key",
    "get_admin_keys",
    "get_decrypted_key",
    "save_key",
    "toggle_key",
    "set_key_health",
    "delete_key",
    # models
    "get_candidate_models",
    "get_admin_models",
    "add_model",
    "update_model",
    "toggle_model",
    "set_model_health",
    "delete_model",
    "update_model_limits",
    # usage
    "log_request_usage",
    "add_cooldown",
    "clear_cooldown",
    "get_model_usage",
    "get_model_ms_per_token",
    # logs
    "log_request_trace",
    "get_recent_traces",
    "log_admin_audit",
    "get_audit_logs",
    "get_admin_analytics",
]
