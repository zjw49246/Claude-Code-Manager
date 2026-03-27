from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "sqlite+aiosqlite:///./claude_manager.db"
    openai_api_key: str = ""
    auth_token: str = ""
    max_concurrent_instances: int = 5
    claude_binary: str = "claude"
    default_model: str = "opus"
    model_options: str = "default,opus[1m],opus,sonnet,haiku"  # comma-separated
    host: str = "0.0.0.0"
    port: int = 8000
    workspace_dir: str = "~/Projects"
    auto_start_dispatcher: bool = True
    merge_push_retries: int = 3
    auto_push_to_origin: bool = True
    task_timeout_seconds: int = 1800  # 30 minutes
    git_ssh_key_path: str = ""  # Instance-level SSH key, fallback when project has none

    # --- Backup service (auto-backup) ---
    backup_enabled: bool = False        # Set true to enable periodic DB backups
    backup_type: str = "local"          # local | s3 | oss
    backup_interval_seconds: int = 3600
    backup_max_copies: int = 10
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
