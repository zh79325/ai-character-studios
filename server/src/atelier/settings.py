"""全局设置：路径解析与运行期可调参数。

仓库根 = server/ 的上一级。所有磁盘路径都以它为基准，保证从任意 cwd 启动一致。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ATELIER_",
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    repo_root: Path = REPO_ROOT

    # 上下文与路由默认值
    default_context_budget: int = 24000
    recent_turns: int = 8
    circuit_breaker_seconds: int = 300
    provider_retry_attempts: int = 2

    # 服务
    host: str = "127.0.0.1"
    port: int = Field(default=0, description="0 表示由系统分配空闲端口")

    @property
    def db_dir(self) -> Path:
        return self.repo_root / "db"

    @property
    def config_db_path(self) -> Path:
        return self.db_dir / "config.db"

    @property
    def runtime_db_path(self) -> Path:
        return self.db_dir / "runtime.db"

    @property
    def assets_dir(self) -> Path:
        return self.repo_root / "assets"

    @property
    def templates_dir(self) -> Path:
        return self.repo_root / "templates"

    @property
    def seeds_dir(self) -> Path:
        return self.repo_root / "seeds"

    @property
    def prompts_dir(self) -> Path:
        """工程级提示词目录，随代码包走，不受仓库根位置影响。"""
        return Path(__file__).resolve().parent / "prompts"

    @property
    def agent_prompts_dir(self) -> Path:
        return self.prompts_dir / "agents"

    def config_db_url(self) -> str:
        return f"sqlite:///{self.config_db_path}"

    def runtime_db_url(self) -> str:
        return f"sqlite:///{self.runtime_db_path}"


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.db_dir.mkdir(parents=True, exist_ok=True)
    return settings
