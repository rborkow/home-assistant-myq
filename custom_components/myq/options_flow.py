"""Battery-conscious recording defaults, adjustable without reauthorization."""

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult

from .const import CONF_CAMERA_LAN_ONLY


class MyQOptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(
        self, user_input: dict[str, object] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                title="", data={**self.config_entry.options, **user_input}
            )
        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_CAMERA_LAN_ONLY, default=options.get(CONF_CAMERA_LAN_ONLY, False)
                    ): bool,
                    vol.Required(
                        "recording_duration", default=options.get("recording_duration", 30)
                    ): vol.All(vol.Coerce(int), vol.Range(min=5, max=300)),
                    vol.Required(
                        "recording_cooldown", default=options.get("recording_cooldown", 60)
                    ): vol.All(vol.Coerce(int), vol.Range(min=0, max=3600)),
                    vol.Required(
                        "retention_days", default=options.get("retention_days", 30)
                    ): vol.All(vol.Coerce(int), vol.Range(min=0, max=365)),
                }
            ),
        )
