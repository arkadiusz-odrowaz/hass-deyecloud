"""DeyeCloud integration."""

from datetime import timedelta
from pathlib import Path
import gzip
import logging

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.start import async_at_started

from .const import CONF_ENABLE_CONTROL, DOMAIN
from .control import DeyeCloudControlError, DeyeCloudController
from .control_entity import control_inverters
from .sensor import DeyeCloudCoordinator
from .services import async_register_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BUTTON]
CONTROL_PLATFORMS: list[Platform] = [Platform.SWITCH, Platform.SELECT, Platform.NUMBER]
# Each read is a command delivered to the inverter, so keep it infrequent.
SETTINGS_REFRESH_INTERVAL = timedelta(minutes=30)

CARD_VERSION = "2.8.2"
CARD_STATIC_URL = "/deyecloud/frontend"
CARD_MODULE_URL = (
    f"{CARD_STATIC_URL}/deyecloud-energy-flow-card.js?v={CARD_VERSION}"
)
DATA_FRONTEND_MODULE_URL = "frontend_module_url"


def _remove_stale_precompressed(frontend_dir: Path) -> None:
    """Delete .gz copies that no longer match their source file.

    Home Assistant serves a .gz sibling instead of the file itself when the
    browser accepts gzip. A copy left over from an older install (e.g. made
    by HACS) would keep serving the old card after an update.
    """
    for compressed in frontend_dir.glob("*.gz"):
        source = compressed.with_suffix("")
        try:
            try:
                current = source.is_file() and gzip.decompress(compressed.read_bytes()) == source.read_bytes()
            except (EOFError, gzip.BadGzipFile):
                current = False
            if current:
                continue
            compressed.unlink()
            _LOGGER.info("Removed stale precompressed frontend file %s", compressed.name)
        except OSError as exc:
            _LOGGER.warning("Could not remove frontend file %s: %s", compressed.name, exc)


async def _async_update_card_resource(hass: HomeAssistant) -> None:
    """Point a dashboard resource for the card at the current version.

    The card is also loaded automatically, but a resource added by hand
    keeps its old ?v= and the browser keeps running that cached copy.
    Only an existing resource is updated; none is created.
    """
    lovelace = hass.data.get("lovelace")
    resources = getattr(lovelace, "resources", None)
    if resources is None and isinstance(lovelace, dict):
        resources = lovelace.get("resources")
    if resources is None or not hasattr(resources, "async_update_item"):
        return  # YAML-mode dashboards: resources cannot be edited.
    if not getattr(resources, "loaded", True):
        await resources.async_load()
        resources.loaded = True
    card_path = f"{CARD_STATIC_URL}/deyecloud-energy-flow-card.js"
    for item in resources.async_items():
        url = str(item.get("url", ""))
        if url.split("?", 1)[0] == card_path and url != CARD_MODULE_URL:
            await resources.async_update_item(
                item["id"], {"res_type": "module", "url": CARD_MODULE_URL}
            )
            _LOGGER.info("Updated dashboard resource %s to %s", url, CARD_MODULE_URL)


async def _async_register_frontend(hass: HomeAssistant) -> None:
    """Serve and automatically load the bundled Lovelace card."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    if domain_data.get(DATA_FRONTEND_MODULE_URL) == CARD_MODULE_URL:
        return

    frontend_dir = Path(__file__).parent / "frontend"
    card_file = frontend_dir / "deyecloud-energy-flow-card.js"
    if not card_file.is_file():
        _LOGGER.error("Bundled DeyeCloud frontend card is missing: %s", card_file)
        return

    await hass.async_add_executor_job(_remove_stale_precompressed, frontend_dir)

    await hass.http.async_register_static_paths(
        [StaticPathConfig(CARD_STATIC_URL, str(frontend_dir), False)]
    )
    add_extra_js_url(hass, CARD_MODULE_URL)
    domain_data[DATA_FRONTEND_MODULE_URL] = CARD_MODULE_URL

    async def _update_resource(_hass: HomeAssistant) -> None:
        try:
            await _async_update_card_resource(hass)
        except Exception as exc:  # noqa: BLE001 - never block setup on this
            _LOGGER.debug("Could not update the card dashboard resource: %s", exc)

    async_at_started(hass, _update_resource)
    _LOGGER.info(
        "Registered bundled DeyeCloud Energy Flow card resource: %s",
        CARD_MODULE_URL,
    )


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the DeyeCloud integration and bundled dashboard card."""
    await _async_register_frontend(hass)
    async_register_services(hass)
    return True


async def _async_refresh_settings(controller, inverters) -> None:
    """Read inverter settings so control entities show real values."""
    for sn in inverters:
        try:
            await controller.async_refresh_settings(sn)
        except DeyeCloudControlError as exc:
            _LOGGER.warning("Could not read settings of inverter %s: %s", sn, exc)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up DeyeCloud from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    await _async_register_frontend(hass)
    async_register_services(hass)

    coordinator = DeyeCloudCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    entry_data = {"coordinator": coordinator, "platforms": list(PLATFORMS)}
    if entry.data.get(CONF_ENABLE_CONTROL):
        controller = DeyeCloudController(coordinator)
        inverters = control_inverters(coordinator)
        entry_data.update(controller=controller, inverters=inverters)
        entry_data["platforms"] += CONTROL_PLATFORMS
    hass.data[DOMAIN][entry.entry_id] = entry_data

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, entry_data["platforms"])

    if "controller" in entry_data:
        # Reading takes a few seconds per inverter; do not block startup.
        @callback
        def _schedule_refresh(_now=None) -> None:
            entry.async_create_background_task(
                hass,
                _async_refresh_settings(entry_data["controller"], entry_data["inverters"]),
                f"{DOMAIN} settings refresh",
            )

        _schedule_refresh()
        entry.async_on_unload(
            async_track_time_interval(hass, _schedule_refresh, SETTINGS_REFRESH_INTERVAL)
        )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a DeyeCloud config entry."""
    platforms = hass.data.get(DOMAIN, {}).get(entry.entry_id, {}).get("platforms", PLATFORMS)
    unload_ok = await hass.config_entries.async_unload_platforms(entry, platforms)

    if unload_ok:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)

    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Reload the DeyeCloud config entry."""
    unload_ok = await async_unload_entry(hass, entry)

    if not unload_ok:
        return False

    return await async_setup_entry(hass, entry)


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    await hass.config_entries.async_reload(entry.entry_id)
