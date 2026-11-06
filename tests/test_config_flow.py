import logging
from unittest.mock import MagicMock

import pytest
from aiohttp import ClientConnectionError
from homeassistant import config_entries, data_entry_flow
from homeassistant.core import HomeAssistant
from homeassistant.helpers.translation import async_get_translations
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.myq.const import (
    CONF_EMAIL,
    CONF_MFA_METHOD,
    CONF_TOKENS,
    DOMAIN,
    MFA_METHOD_EMAIL,
    MFA_METHOD_SMS,
)
from custom_components.myq.exceptions import (
    MyQCloudflareChallengeError,
    MyQInvalidCallbackError,
    MyQInvalidCredentialsError,
    MyQInvalidMfaError,
    MyQUnsupportedAuthPageError,
)
from custom_components.myq.models import GarageDoor, OAuthTokens

EMAIL = "driver@example.com"
PASSWORD = "secret"
TOKENS = OAuthTokens("access", "refresh", 1234567890.0)
DOOR = GarageDoor("account-1", "door-1", "Garage", "Wi-Fi GDO", "closed", True)


async def test_user_flow_creates_token_backed_entry_after_email_mfa(
    hass: HomeAssistant,
    mock_login_session: MagicMock,
    mock_myq_client: MagicMock,
) -> None:
    mock_login_session.async_start.return_value = None
    mock_login_session.async_submit_mfa.return_value = TOKENS
    mock_myq_client.async_get_garage_doors.return_value = (DOOR,)

    result = await _submit_credentials(hass, mfa_method=MFA_METHOD_EMAIL)

    assert result["type"] is data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "mfa"
    mock_login_session.async_start.assert_awaited_once_with(
        EMAIL,
        PASSWORD,
        MFA_METHOD_EMAIL,
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"code": "123456"},
    )

    assert result["type"] is data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["title"] == EMAIL
    assert result["data"] == {
        CONF_EMAIL: EMAIL,
        CONF_MFA_METHOD: MFA_METHOD_EMAIL,
        CONF_TOKENS: {
            "access_token": "access",
            "refresh_token": "refresh",
            "expires_at": 1234567890.0,
        },
    }
    assert "password" not in result["data"]
    assert result["result"].unique_id == EMAIL
    mock_login_session.http_session.detach.assert_called_once_with()


async def test_user_flow_offers_both_sign_in_methods(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )

    assert result["type"] is data_entry_flow.FlowResultType.MENU
    assert result["step_id"] == "user"
    assert result["menu_options"] == ("credentials", "browser_start")


async def test_user_flow_can_select_sms(
    hass: HomeAssistant,
    mock_login_session: MagicMock,
) -> None:
    mock_login_session.async_start.return_value = None

    await _submit_credentials(hass, mfa_method=MFA_METHOD_SMS)

    mock_login_session.async_start.assert_awaited_once_with(
        EMAIL,
        PASSWORD,
        MFA_METHOD_SMS,
    )


async def test_invalid_credentials_remain_on_user_form(
    hass: HomeAssistant,
    mock_login_session: MagicMock,
) -> None:
    mock_login_session.async_start.side_effect = MyQInvalidCredentialsError

    result = await _submit_credentials(hass)

    assert result["type"] is data_entry_flow.FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
    mock_login_session.http_session.detach.assert_called_once_with()


@pytest.mark.parametrize("error_type", [MyQCloudflareChallengeError, MyQUnsupportedAuthPageError])
async def test_unsupported_authentication_starts_browser_sign_in(
    hass: HomeAssistant,
    mock_login_session: MagicMock,
    error_type: type[MyQCloudflareChallengeError | MyQUnsupportedAuthPageError],
    caplog: pytest.LogCaptureFixture,
) -> None:
    detail = "HTTP 200 at /AccountMfa/VerifyOtp, forms=1"
    mock_login_session.async_start.side_effect = error_type(detail)

    result = await _submit_credentials(hass)

    assert result["type"] is data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "browser"
    assert result["description_placeholders"] == {
        "authorization_url": "https://partner-identity.myq-cloud.com/connect/authorize",
        "email": EMAIL,
    }
    assert (
        "custom_components.myq.config_flow",
        logging.WARNING,
        "Automatic MyQ authentication could not continue during login; "
        f"offering browser sign-in: {error_type.__name__}: {detail}",
    ) in caplog.record_tuples


async def test_browser_sign_in_can_be_selected_without_password(
    hass: HomeAssistant,
    mock_login_session: MagicMock,
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"next_step_id": "browser_start"},
    )

    assert result["type"] is data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "browser_start"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_EMAIL: " Driver@Example.com "},
    )

    assert result["type"] is data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "browser"
    assert result["description_placeholders"] == {
        "authorization_url": "https://partner-identity.myq-cloud.com/connect/authorize",
        "email": EMAIL,
    }
    mock_login_session.start_browser.assert_called_once_with()
    mock_login_session.async_start.assert_not_awaited()


async def test_browser_callback_creates_entry(
    hass: HomeAssistant,
    mock_login_session: MagicMock,
    mock_myq_client: MagicMock,
) -> None:
    mock_login_session.async_start.side_effect = MyQCloudflareChallengeError
    mock_login_session.async_complete_browser.return_value = TOKENS
    mock_myq_client.async_get_garage_doors.return_value = (DOOR,)
    result = await _submit_credentials(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "callback_url": (
                " com.myqops://android?code=browser-code&state=state"
                "&iss=https%3A%2F%2Fpartner-identity.myq-cloud.com "
            )
        },
    )

    assert result["type"] is data_entry_flow.FlowResultType.CREATE_ENTRY
    mock_login_session.async_complete_browser.assert_awaited_once_with(
        "com.myqops://android?code=browser-code&state=state"
        "&iss=https%3A%2F%2Fpartner-identity.myq-cloud.com"
    )


async def test_browser_callback_can_be_retried(
    hass: HomeAssistant,
    mock_login_session: MagicMock,
) -> None:
    mock_login_session.async_start.side_effect = MyQCloudflareChallengeError
    mock_login_session.async_complete_browser.side_effect = MyQInvalidCallbackError
    result = await _submit_credentials(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"callback_url": "https://example.com/callback"},
    )

    assert result["type"] is data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "browser"
    assert result["errors"] == {"base": "invalid_callback"}


async def test_invalid_mfa_can_be_retried(
    hass: HomeAssistant,
    mock_login_session: MagicMock,
) -> None:
    mock_login_session.async_start.return_value = None
    mock_login_session.async_submit_mfa.side_effect = MyQInvalidMfaError
    result = await _submit_credentials(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"code": "123456"},
    )

    assert result["type"] is data_entry_flow.FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_mfa"}
    mock_login_session.http_session.detach.assert_not_called()


@pytest.mark.parametrize("error_type", [MyQCloudflareChallengeError, MyQUnsupportedAuthPageError])
async def test_unsupported_screen_after_mfa_starts_browser_sign_in(
    hass: HomeAssistant,
    mock_login_session: MagicMock,
    error_type: type[MyQCloudflareChallengeError | MyQUnsupportedAuthPageError],
    caplog: pytest.LogCaptureFixture,
) -> None:
    mock_login_session.async_start.return_value = None
    mock_login_session.async_submit_mfa.side_effect = error_type
    result = await _submit_credentials(hass)

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "123456"})

    assert result["type"] is data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "browser"
    mock_login_session.start_browser.assert_called_once_with()
    assert (
        "custom_components.myq.config_flow",
        logging.WARNING,
        "Automatic MyQ authentication could not continue during MFA verification; "
        f"offering browser sign-in: {error_type.__name__}",
    ) in caplog.record_tuples


async def test_unsupported_reauthentication_preserves_existing_entry(
    hass: HomeAssistant,
    mock_login_session: MagicMock,
) -> None:
    original_data = _entry_data()
    entry = MockConfigEntry(domain=DOMAIN, unique_id=EMAIL, data=original_data)
    entry.add_to_hass(hass)
    mock_login_session.async_start.side_effect = MyQUnsupportedAuthPageError
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "reauth_credentials"}
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"password": PASSWORD, CONF_MFA_METHOD: MFA_METHOD_SMS}
    )

    assert result["step_id"] == "browser"
    assert entry.data == original_data


@pytest.mark.parametrize("step_id", ["credentials", "browser_start"])
@pytest.mark.parametrize("language", ["en", "ru"])
async def test_user_flow_aborts_duplicate_account(
    hass: HomeAssistant,
    mock_login_session: MagicMock,
    step_id: str,
    language: str,
) -> None:
    original_data = _entry_data()
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=EMAIL,
        data=original_data,
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": step_id}
    )
    user_input: dict[str, str] = {CONF_EMAIL: " Driver@Example.com "}
    if step_id == "credentials":
        user_input.update(password=PASSWORD, mfa_method=MFA_METHOD_EMAIL)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input)

    assert result["type"] is data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert hass.config_entries.async_entries(DOMAIN) == [entry]
    assert entry.data == original_data
    mock_login_session.async_start.assert_not_awaited()
    mock_login_session.start_browser.assert_not_called()

    translations = await async_get_translations(hass, language, "config", {DOMAIN})
    assert translations[f"component.{DOMAIN}.config.abort.{result['reason']}"].strip()


async def test_reauthentication_updates_tokens_and_mfa_method(
    hass: HomeAssistant,
    mock_login_session: MagicMock,
    mock_myq_client: MagicMock,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=EMAIL,
        data=_entry_data(),
    )
    entry.add_to_hass(hass)
    mock_login_session.async_start.return_value = TOKENS
    mock_myq_client.async_get_garage_doors.return_value = (DOOR,)

    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"next_step_id": "reauth_credentials"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"password": PASSWORD, CONF_MFA_METHOD: MFA_METHOD_SMS},
    )

    assert result["type"] is data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_MFA_METHOD] == MFA_METHOD_SMS
    assert entry.data[CONF_TOKENS]["refresh_token"] == "refresh"
    assert "password" not in entry.data


@pytest.mark.parametrize("mfa_method", [MFA_METHOD_EMAIL, MFA_METHOD_SMS])
async def test_reauthentication_retries_mfa_without_replacing_entry(
    hass: HomeAssistant,
    mock_login_session: MagicMock,
    mock_myq_client: MagicMock,
    mfa_method: str,
) -> None:
    original_data = _entry_data()
    entry = MockConfigEntry(domain=DOMAIN, unique_id=EMAIL, data=original_data)
    entry.add_to_hass(hass)
    mock_login_session.async_start.return_value = None
    mock_login_session.async_submit_mfa.side_effect = [MyQInvalidMfaError, TOKENS]
    mock_myq_client.async_get_garage_doors.return_value = (DOOR,)

    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "reauth_credentials"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"password": PASSWORD, CONF_MFA_METHOD: mfa_method}
    )
    assert result["step_id"] == "reauth_mfa"
    mock_login_session.async_start.assert_awaited_once_with(EMAIL, PASSWORD, mfa_method)

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "123456"})
    assert result["type"] is data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "reauth_mfa"
    assert result["errors"] == {"base": "invalid_mfa"}
    assert entry.data == original_data
    mock_login_session.http_session.detach.assert_not_called()

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "654321"})
    assert result["type"] is data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert hass.config_entries.async_entries(DOMAIN) == [entry]
    assert entry.unique_id == EMAIL
    assert entry.data[CONF_MFA_METHOD] == mfa_method
    assert entry.data[CONF_TOKENS]["refresh_token"] == "refresh"
    assert mock_login_session.async_submit_mfa.await_count == 2
    mock_login_session.http_session.detach.assert_called_once_with()


@pytest.mark.parametrize(
    ("error_type", "error_key"),
    [(MyQInvalidCredentialsError, "invalid_auth"), (ClientConnectionError, "cannot_connect")],
)
async def test_reauthentication_credential_failure_preserves_entry(
    hass: HomeAssistant,
    mock_login_session: MagicMock,
    error_type: type[MyQInvalidCredentialsError | ClientConnectionError],
    error_key: str,
) -> None:
    original_data = _entry_data()
    entry = MockConfigEntry(domain=DOMAIN, unique_id=EMAIL, data=original_data)
    entry.add_to_hass(hass)
    mock_login_session.async_start.side_effect = error_type

    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "reauth_credentials"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"password": PASSWORD, CONF_MFA_METHOD: MFA_METHOD_SMS}
    )

    assert result["type"] is data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "reauth_credentials"
    assert result["errors"] == {"base": error_key}
    placeholders = result["description_placeholders"]
    assert placeholders is not None
    assert placeholders["email"] == EMAIL
    assert entry.data == original_data
    mock_login_session.http_session.detach.assert_called_once_with()


async def test_reauthentication_offers_browser_sign_in(
    hass: HomeAssistant,
    mock_login_session: MagicMock,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=EMAIL,
        data=_entry_data(),
    )
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)

    assert result["type"] is data_entry_flow.FlowResultType.MENU
    assert result["step_id"] == "reauth_confirm"
    assert result["menu_options"] == ("reauth_credentials", "browser_start")

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"next_step_id": "browser_start"},
    )

    assert result["type"] is data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "browser"
    assert result["description_placeholders"] == {
        "authorization_url": "https://partner-identity.myq-cloud.com/connect/authorize",
        "email": EMAIL,
        "name": "Mock Title",
    }
    mock_login_session.start_browser.assert_called_once_with()
    mock_login_session.async_start.assert_not_awaited()


async def _submit_credentials(
    hass: HomeAssistant,
    *,
    mfa_method: str = MFA_METHOD_EMAIL,
) -> config_entries.ConfigFlowResult:
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"next_step_id": "credentials"},
    )
    return await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "email": " Driver@Example.com ",
            "password": PASSWORD,
            CONF_MFA_METHOD: mfa_method,
        },
    )


def _entry_data() -> dict[str, object]:
    return {
        CONF_EMAIL: EMAIL,
        CONF_MFA_METHOD: MFA_METHOD_EMAIL,
        CONF_TOKENS: {
            "access_token": "old-access",
            "refresh_token": "old-refresh",
            "expires_at": 0.0,
        },
    }


async def test_lan_only_option_is_opt_in_and_preserves_other_options(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={"unrelated": 7})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is data_entry_flow.FlowResultType.FORM
    schema = result["data_schema"]
    assert schema is not None
    assert schema({}) == {"camera_lan_only": False}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"camera_lan_only": True}
    )
    assert result["type"] is data_entry_flow.FlowResultType.CREATE_ENTRY
    assert entry.options == {"unrelated": 7, "camera_lan_only": True}
