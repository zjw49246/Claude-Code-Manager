from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "sqlite+aiosqlite:///./claude_manager.db"
    openai_api_key: str = ""
    auth_token: str = ""
    max_concurrent_instances: int = 5
    claude_binary: str = "claude"
    codex_binary: str = "codex"
    default_provider: str = "claude"
    provider_options: str = "claude,codex"
    default_model: str = "claude-opus-4-6"
    model_options: str = "default,claude-fable-5,claude-opus-4-6,claude-opus-4-6[1m],claude-opus-4-7,claude-opus-4-7[1m],claude-opus-4-8,claude-opus-4-8[1m],claude-sonnet-4-6,claude-sonnet-4-6[1m],claude-haiku-4-5"  # comma-separated
    default_codex_model: str = "gpt-5.5"
    codex_model_options: str = "default,gpt-5.5,gpt-5.4,gpt-5.4-mini,gpt-5.3-codex-spark"  # comma-separated
    codex_effort_options: str = "low,medium,high,xhigh"  # codex supports reasoning levels, no 'max'
    default_codex_goal_evaluator_model: str = "gpt-5.4-mini"
    default_effort: str = "medium"
    effort_options: str = "low,medium,high,xhigh,max"  # comma-separated
    host: str = "0.0.0.0"
    port: int = 8000
    workspace_dir: str = "~/Projects"
    auto_start_dispatcher: bool = True
    merge_push_retries: int = 3
    auto_push_to_origin: bool = True
    task_timeout_seconds: int = 1800  # 30 minutes
    default_goal_evaluator_model: str = "claude-haiku-4-5"
    goal_evaluation_timeout: int = 120
    git_ssh_key_path: str = ""  # Instance-level SSH key, fallback when project has none

    # --- Claude account pool (auto-rotation on rate limit) ---
    pool_enabled: bool = False
    pool_config_path: str = "~/.claude-pool/accounts.json"
    pool_cooldown_seconds: int = 300  # per-account cooldown after rate limit

    # --- Backup service (auto-backup) ---
    backup_enabled: bool = False        # Set true to enable periodic DB backups
    backup_type: str = "local"          # local | s3 | oss
    backup_interval_seconds: int = 3600
    backup_max_copies: int = 10
    backup_temp_dir: str = ""           # Custom temp dir for archive files (avoids filling /tmp)
    # local backend
    backup_destination_path: str = ""
    # AWS S3 backend
    backup_s3_bucket: str = ""
    backup_s3_region: str = ""
    backup_s3_access_key: str = ""
    backup_s3_secret_key: str = ""
    # Alibaba Cloud OSS backend
    backup_oss_endpoint: str = ""
    backup_oss_bucket: str = ""
    backup_oss_access_key: str = ""
    backup_oss_secret_key: str = ""

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
