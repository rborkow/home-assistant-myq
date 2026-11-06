from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry


async def test_options_save_without_reauthorization(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain="myq", data={})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == "form"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"recording_duration": 20, "recording_cooldown": 120, "retention_days": 30},
    )
    assert result["type"] == "create_entry"
    assert entry.options["retention_days"] == 30
    assert entry.options["recording_duration"] == 20
    assert entry.data == {}
