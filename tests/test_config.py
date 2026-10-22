"""Tests for src/config.py (moved from the full-server suite)."""


class TestConfig:
    """Tests for the configuration module."""

    def test_mask_credential_normal(self):
        """Test masking a normal-length credential."""
        from src.config import mask_credential

        result = mask_credential("mysecretpassword")
        assert result.startswith("my")
        assert result.endswith("rd")
        assert "****" in result

    def test_mask_credential_short(self):
        """Test masking a short credential."""
        from src.config import mask_credential

        result = mask_credential("ab")
        assert result == "**"

    def test_mask_credential_empty(self):
        """Test masking an empty credential."""
        from src.config import mask_credential

        assert mask_credential("") == "<empty>"

    def test_taiga_settings_defaults(self):
        """Test TaigaSettings default values."""
        from src.config import TaigaSettings

        # Create with explicit values to avoid env pollution
        s = TaigaSettings(TAIGA_API_URL="http://test:9000")
        assert s.host == "http://test:9000"

    def test_taiga_settings_has_credentials_false(self):
        """Test has_credentials when no credentials set."""
        from src.config import TaigaSettings

        s = TaigaSettings(TAIGA_API_URL="http://test:9000")
        # If env vars are not set, credentials should be None
        if s.username is None and s.password is None:
            assert s.has_credentials is False
