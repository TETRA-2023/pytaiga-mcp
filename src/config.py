"""Configuration management with secure credential handling."""

from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings

# Get the project root directory (parent of src/)
PROJECT_ROOT = Path(__file__).parent.parent
ENV_FILE_PATH = PROJECT_ROOT / ".env"

# Load .env file from project root
load_dotenv(dotenv_path=ENV_FILE_PATH)


class TaigaSettings(BaseSettings):
    """Taiga MCP server settings with secure credential handling."""

    # Connection settings
    host: str = Field(
        default="http://localhost:9000",
        alias="TAIGA_API_URL",
        description=(
            "Taiga base URL: scheme, host and any path prefix, e.g. https://taiga.example.com "
            "(the client adds /api/v1)"
        ),
    )

    # Credentials (using SecretStr for security)
    username: Optional[SecretStr] = Field(
        default=None,
        alias="TAIGA_USERNAME",
        description="Taiga username for auto-authentication",
    )
    password: Optional[SecretStr] = Field(
        default=None,
        alias="TAIGA_PASSWORD",
        description="Taiga password for auto-authentication",
    )

    # Team rule: moving a story or task to a closed status needs a receipt comment
    # (resolve._check_closing_receipt). Off only for a Taiga that does not use it.
    closing_receipts: bool = Field(
        default=True,
        alias="TAIGA_CLOSING_RECEIPTS",
        description="Refuse to close a story/task without a closing receipt comment",
    )

    class Config:
        env_file = str(ENV_FILE_PATH)
        env_file_encoding = "utf-8"
        extra = "ignore"
        populate_by_name = True

    @property
    def has_credentials(self) -> bool:
        """Check if credentials are available for auto-auth."""
        return bool(self.username and self.password)

    def get_username_value(self) -> Optional[str]:
        """Safely get username value."""
        return self.username.get_secret_value() if self.username else None

    def get_password_value(self) -> Optional[str]:
        """Safely get password value."""
        return self.password.get_secret_value() if self.password else None


# Global settings instance
settings = TaigaSettings()
