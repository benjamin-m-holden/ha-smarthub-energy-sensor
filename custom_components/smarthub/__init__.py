"""
Custom integration to integrate SmartHub Coop energy sensors with Home Assistant.

For more details about this integration, please refer to
https://github.com/gagata/ha-smarthub-energy-sensor
"""
from __future__ import annotations

import logging
import re
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import entity_registry as er

from .api import SmartHubAPI
from .sensor import  SmartHubDataUpdateCoordinator
from .const import DOMAIN, DEFAULT_POLL_INTERVAL
from .utils import sanitize_host

from datetime import timedelta

# Remove explicit config flow import
# from . import config_flow  # noqa: F401

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]

# Matches unique_ids produced by the pre-dedup-guard sensor code, where
# config_entry.unique_id was always None: "None_{location_id}_energy".
_LEGACY_ELECTRIC_UNIQUE_ID_RE = re.compile(r"^None_(?P<location_id>.+)_energy$")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up SmartHub from a config entry."""
    config = entry.data

    # Validate required configuration
    required_fields = ["email", "password", "account_id", "host"]
    missing_fields = [field for field in required_fields if not config.get(field)]

    if missing_fields:
        _LOGGER.error("Missing required configuration fields: %s", missing_fields)
        raise ConfigEntryError(f"Missing configuration fields: {missing_fields}")

    # Initialize the API object
    api = SmartHubAPI(
        email=config["email"],
        password=config["password"],
        account_id=config["account_id"],
        timezone=config.get("timezone", "GMT"), # timezone was not previously required - default it to be GMT
        mfa_totp=config.get("mfa_totp", ""), # mfa_totp is optional
        host=config["host"],
    )

    # Test the connection
    try:
        await api.get_token()
        _LOGGER.info("Successfully connected to SmartHub API")
    except Exception as e:
        _LOGGER.error("Failed to connect to SmartHub API: %s", e)
        await api.close()
        raise ConfigEntryError(f"Cannot connect to SmartHub: {e}") from e

    # Create update coordinator, and store in the config entry
    coordinator = SmartHubDataUpdateCoordinator(
        hass=hass,
        api=api,
        update_interval=timedelta(minutes=config.get("poll_interval", DEFAULT_POLL_INTERVAL)),
        config_entry=entry,
    )
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    # Set up platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok and getattr(entry, "runtime_data", None) is not None:
        await entry.runtime_data.api.close()

    return unload_ok


def _migrate_legacy_electric_unique_id(
    entity_entry: er.RegistryEntry, new_config_unique_id: str
) -> dict[str, Any] | None:
    """Rewrite a pre-dedup-guard electric sensor's unique_id to the new format.

    Keeps the entity_id (and any user customizations) attached to the same
    entity instead of the old unique_id going orphaned and a new entity_id
    being created alongside it.
    """
    match = _LEGACY_ELECTRIC_UNIQUE_ID_RE.match(entity_entry.unique_id)
    if not match:
        return None

    return {
        "new_unique_id": f"{new_config_unique_id}_{match.group('location_id')}_electric"
    }


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate an old config entry to the current version."""
    if entry.version == 1 and entry.minor_version < 2:
        new_unique_id = f"{sanitize_host(entry.data['host'])}_{entry.data['account_id']}"

        existing = hass.config_entries.async_entry_for_domain_unique_id(
            DOMAIN, new_unique_id
        )
        if existing is not None and existing.entry_id != entry.entry_id:
            # Two entries already resolve to the same host+account_id (a
            # pre-existing duplicate from before the dedup guard existed).
            # Don't assign a colliding unique_id - just bump the version and
            # leave this entry unique_id-less; the user can remove the dup.
            _LOGGER.warning(
                "Not backfilling unique_id for SmartHub entry %s: %s is already "
                "used by entry %s. This entry appears to be a duplicate of an "
                "existing account+host and should be removed.",
                entry.entry_id, new_unique_id, existing.entry_id,
            )
        else:
            entity_registry = er.async_get(hass)
            existing_entities = list(
                er.async_entries_for_config_entry(entity_registry, entry.entry_id)
            )
            for entity_entry in existing_entities:
                updates = _migrate_legacy_electric_unique_id(entity_entry, new_unique_id)
                if updates is not None:
                    entity_registry.async_update_entity(entity_entry.entity_id, **updates)
            hass.config_entries.async_update_entry(entry, unique_id=new_unique_id)

        hass.config_entries.async_update_entry(entry, minor_version=2)

    _LOGGER.debug(
        "SmartHub config entry %s is at version %s.%s",
        entry.entry_id, entry.version, entry.minor_version,
    )
    return True
