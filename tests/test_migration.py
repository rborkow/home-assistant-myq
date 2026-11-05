"""Migration uses a fresh validated token chain and requires exclusive ownership."""

from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant import config_entries, data_entry_flow
from homeassistant.core import HomeAssistant

from custom_components.myq.const import DOMAIN
from custom_components.myq.models import OAuthTokens


async def test_migration_requires_stopped_owner(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )
    assert "migrate" in result["menu_options"]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "migrate"}
    )
    with patch(
        "custom_components.myq.config_flow.MyQAuth.async_refresh", new_callable=AsyncMock
    ) as refresh:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "email": "test@example.com",
                "refresh_token": "old-refresh",
                "previous_client_stopped": False,
            },
        )
    assert result["errors"] == {"base": "migration_owner_active"}
    refresh.assert_not_awaited()


async def test_migration_persists_rotated_tokens_for_keypad_only_account(
    hass: HomeAssistant,
    mock_myq_client: MagicMock,
) -> None:
    mock_myq_client.async_get_garage_doors.return_value = ()
    mock_myq_client.keypads = (MagicMock(),)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "migrate"}
    )
    with patch(
        "custom_components.myq.config_flow.MyQAuth.async_refresh",
        new_callable=AsyncMock,
        return_value=OAuthTokens("new-access", "new-refresh", 9999999999),
    ) as refresh:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "email": "test@example.com",
                "refresh_token": "old-refresh",
                "previous_client_stopped": True,
            },
        )
    assert result["type"] is data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["data"]["tokens"]["refresh_token"] == "new-refresh"
    assert "old-refresh" not in str(result["data"])
    refresh.assert_awaited_once()
