# jouletrace/infrastructure/config.py
from __future__ import annotations
import os
from typing import Optional, List, Dict, Any
from pathlib import Path
from enum import Enum

from pydantic import Field, field_validator, ConfigDict
from pydantic_settings import BaseSettings

class Environment(str, Enum):
    """Deployment environment."""
    DEVELOPMENT = "development"
    TESTING = "testing"
    STAGING = "staging"
    PRODUCTION = "production"

class LogLevel(str, Enum):
    """Logging levels."""
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"

class CelerySettings(BaseSettings):
    """Celery task queue configuration."""
    
    model_config = ConfigDict(env_prefix="CELERY_")
    
    broker_url: str = Field(
        default="redis://localhost:6379/0",
        description="Celery broker URL (Redis)"
    )
    result_backend: str = Field(
        default="redis://localhost:6379/0",
        description="Celery result backend URL"
    )
    task_serializer: str = Field(default="json", description="Task serialization format")
    result_serializer: str = Field(default="json", description="Result serialization format")
    accept_content: List[str] = Field(default=["json"], description="Accepted content types")
    timezone: str = Field(default="UTC", description="Timezone for scheduled tasks")
    
    # Worker configuration
    worker_concurrency: int = Field(
        default=1,
        ge=1,
        le=4,
        description="Number of concurrent workers (keep at 1 for energy isolation)"
    )
    worker_prefetch_multiplier: int = Field(
        default=1,
        description="How many tasks to prefetch"
    )
    worker_max_tasks_per_child: int = Field(
        default=50,
        description="Max tasks before worker restart"
    )
    
    # Task timeouts
    task_soft_time_limit: int = Field(default=600, description="Soft timeout in seconds")
    task_time_limit: int = Field(default=900, description="Hard timeout in seconds")
    
    # Result expiration
    result_expires: int = Field(default=86400, description="Result expiration in seconds (24h)")

class EnergyMeasurementSettings(BaseSettings):
    """Energy measurement configuration."""
    
    model_config = ConfigDict(env_prefix="ENERGY_")
    
    # Energy meter configuration
    meter_type: Optional[str] = Field(
        default=None,
        description="Force specific meter type (perf, turbostat, etc.)"
    )
    use_sudo: bool = Field(
        default=False,
        description="Use sudo for perf commands"
    )
    perf_timeout: int = Field(
        default=60,
        ge=10,
        le=300,
        description="Perf command timeout in seconds"
    )
    
    # CPU isolation settings
    measurement_core: int = Field(
        default=0,
        ge=0,
        description="CPU core for energy measurement"
    )
    isolate_processes: bool = Field(
        default=True,
        description="Isolate other processes from measurement core"
    )
    thermal_baseline_wait: float = Field(
        default=5.0,
        ge=0.0,
        le=60.0,
        description="Thermal baseline wait time in seconds"
    )
    disable_frequency_scaling: bool = Field(
        default=True,
        description="Disable CPU frequency scaling for consistent measurements"
    )
    
    # Default measurement parameters
    default_trials: int = Field(default=5, ge=1, le=20, description="Default number of trials")
    default_warmup: int = Field(default=2, ge=0, le=10, description="Default warmup runs")
    default_timeout: int = Field(default=30, ge=5, le=300, description="Default execution timeout")
    default_memory_limit: int = Field(
        default=512,
        ge=64,
        le=8192,
        description="Default memory limit in MB"
    )

class APISettings(BaseSettings):
    """FastAPI configuration."""
    
    model_config = ConfigDict(env_prefix="API_")
    
    host: str = Field(default="0.0.0.0", description="API host address")
    port: int = Field(default=8000, ge=1024, le=65535, description="API port")
    workers: int = Field(
        default=4,
        ge=1,
        le=16,
        description="Number of API workers (for parallel request handling)"
    )
    
    # CORS configuration
    cors_origins: List[str] = Field(
        default=["http://localhost:3000", "http://localhost:8080"],
        description="Allowed CORS origins"
    )
    cors_allow_credentials: bool = Field(default=True, description="Allow CORS credentials")
    
    # API limits
    max_concurrent_measurements: int = Field(
        default=4,
        ge=1,
        le=10,
        description="Maximum concurrent measurements"
    )
    max_request_size_mb: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Maximum request size in MB"
    )
    rate_limit_requests: int = Field(
        default=100,
        description="Rate limit: requests per minute"
    )

class RedisSettings(BaseSettings):
    """Redis configuration."""
    
    model_config = ConfigDict(env_prefix="REDIS_")
    
    host: str = Field(default="localhost", description="Redis host")
    port: int = Field(default=6379, ge=1024, le=65535, description="Redis port")
    db: int = Field(default=0, ge=0, le=15, description="Redis database number")
    password: Optional[str] = Field(default=None, description="Redis password")
    
    # Connection pool
    max_connections: int = Field(default=50, description="Maximum Redis connections")
    socket_timeout: int = Field(default=5, description="Socket timeout in seconds")
    socket_connect_timeout: int = Field(default=5, description="Connection timeout in seconds")
    
    @property
    def url(self) -> str:
        """Construct Redis URL."""
        if self.password:
            return f"redis://:{self.password}@{self.host}:{self.port}/{self.db}"
        return f"redis://{self.host}:{self.port}/{self.db}"

class LoggingSettings(BaseSettings):
    """Logging configuration."""
    
    model_config = ConfigDict(env_prefix="LOG_")
    
    level: LogLevel = Field(default=LogLevel.INFO, description="Logging level")
    format: str = Field(
        default="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        description="Log format string"
    )
    
    # File logging
    log_to_file: bool = Field(default=False, description="Enable file logging")
    log_file_path: Path = Field(
        default=Path("/var/log/jouletrace/jouletrace.log"),
        description="Log file path"
    )
    log_file_max_bytes: int = Field(
        default=10485760,  # 10MB
        description="Maximum log file size"
    )
    log_file_backup_count: int = Field(default=5, description="Number of log file backups")
    
    # Structured logging
    json_logging: bool = Field(default=False, description="Use JSON structured logging")
    include_trace_id: bool = Field(default=True, description="Include trace IDs in logs")

class MonitoringSettings(BaseSettings):
    """Monitoring and metrics configuration."""
    
    model_config = ConfigDict(env_prefix="MONITORING_")
    
    enabled: bool = Field(default=True, description="Enable monitoring")
    
    # Prometheus metrics
    prometheus_enabled: bool = Field(default=False, description="Enable Prometheus metrics")
    prometheus_port: int = Field(default=9090, description="Prometheus metrics port")
    
    # Health checks
    health_check_interval: int = Field(default=60, description="Health check interval in seconds")
    
    # Performance tracking
    track_task_duration: bool = Field(default=True, description="Track task execution duration")
    track_energy_metrics: bool = Field(default=True, description="Track energy measurement metrics")

class SecuritySettings(BaseSettings):
    """Security configuration."""
    
    model_config = ConfigDict(env_prefix="SECURITY_")
    
    # API authentication (for future implementation)
    require_api_key: bool = Field(default=False, description="Require API key authentication")
    api_key_header: str = Field(default="X-API-Key", description="API key header name")
    
    # Rate limiting
    enable_rate_limiting: bool = Field(default=True, description="Enable rate limiting")
    
    # HTTPS (for production)
    force_https: bool = Field(default=False, description="Force HTTPS in production")

class JouleTraceConfig(BaseSettings):
    """Main JouleTrace configuration."""
    
    model_config = ConfigDict(
        env_prefix="JOULETRACE_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False
    )
    
    # Environment
    environment: Environment = Field(
        default=Environment.DEVELOPMENT,
        description="Deployment environment"
    )
    debug: bool = Field(default=False, description="Debug mode")
    
    # Service metadata
    service_name: str = Field(default="jouletrace", description="Service name")
    version: str = Field(default="1.0.0", description="Service version")
    
    # Component configurations
    celery: CelerySettings = Field(default_factory=CelerySettings)
    energy: EnergyMeasurementSettings = Field(default_factory=EnergyMeasurementSettings)
    api: APISettings = Field(default_factory=APISettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    monitoring: MonitoringSettings = Field(default_factory=MonitoringSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    
    @property
    def is_production(self) -> bool:
        """Check if running in production."""
        return self.environment == Environment.PRODUCTION
    
    @property
    def is_development(self) -> bool:
        """Check if running in development."""
        return self.environment == Environment.DEVELOPMENT
    
    def get_celery_config(self) -> Dict[str, Any]:
        """Get Celery configuration dictionary."""
        return {
            "broker_url": self.celery.broker_url,
            "result_backend": self.celery.result_backend,
            "task_serializer": self.celery.task_serializer,
            "result_serializer": self.celery.result_serializer,
            "accept_content": self.celery.accept_content,
            "timezone": self.celery.timezone,
            "worker_concurrency": self.celery.worker_concurrency,
            "worker_prefetch_multiplier": self.celery.worker_prefetch_multiplier,
            "worker_max_tasks_per_child": self.celery.worker_max_tasks_per_child,
            "task_soft_time_limit": self.celery.task_soft_time_limit,
            "task_time_limit": self.celery.task_time_limit,
            "result_expires": self.celery.result_expires,
            "task_track_started": True,
            "task_send_sent_event": True,
            "worker_send_task_events": True,
        }
    
    def validate_configuration(self) -> tuple[bool, List[str]]:
        """Validate complete configuration."""
        issues = []
        
        # Check Redis connectivity is possible
        if not self.redis.host:
            issues.append("Redis host not configured")
        
        # Validate production settings
        if self.is_production:
            if self.debug:
                issues.append("Debug mode should be disabled in production")
            if not self.security.enable_rate_limiting:
                issues.append("Rate limiting should be enabled in production")
            if self.logging.level == LogLevel.DEBUG:
                issues.append("Log level should not be DEBUG in production")
        
        # Validate energy measurement settings
        if self.energy.measurement_core < 0:
            issues.append("Invalid measurement core")
        
        # Validate API settings
        if self.api.workers < 2 and self.is_production:
            issues.append("Multiple API workers recommended for production throughput")
        
        return len(issues) == 0, issues

# Global configuration instance
_config: Optional[JouleTraceConfig] = None

def get_config() -> JouleTraceConfig:
    """Get global configuration instance."""
    global _config
    if _config is None:
        _config = JouleTraceConfig()
    return _config

def reload_config() -> JouleTraceConfig:
    """Reload configuration from environment."""
    global _config
    _config = JouleTraceConfig()
    return _config

def get_config_summary() -> Dict[str, Any]:
    """Get configuration summary for debugging."""
    config = get_config()
    
    return {
        "environment": config.environment.value,
        "debug": config.debug,
        "version": config.version,
        "api": {
            "host": config.api.host,
            "port": config.api.port,
            "workers": config.api.workers,
        },
        "celery": {
            "broker": config.celery.broker_url.split("@")[-1],  # Hide passwords
            "worker_concurrency": config.celery.worker_concurrency,
        },
        "energy": {
            "meter_type": config.energy.meter_type or "auto",
            "measurement_core": config.energy.measurement_core,
            "isolate_processes": config.energy.isolate_processes,
        },
        "logging": {
            "level": config.logging.level.value,
            "json_logging": config.logging.json_logging,
        }
    }