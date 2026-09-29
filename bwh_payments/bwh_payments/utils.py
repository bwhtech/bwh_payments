import re
from urllib.parse import urlsplit, urlunsplit

import frappe
from frappe.utils.caching import site_cache

# A leading path segment that looks like "en" or "en-GB" is treated as the language prefix. Matching on
# shape avoids a Language lookup on every checkout redirect.
LANGUAGE_SEGMENT = re.compile(r"^[a-z]{2}(-[A-Za-z]{2})?$")


@site_cache(ttl=60 * 60)
def get_available_payment_modes() -> list[str]:
	# Runs on every checkout render and every Lifestyle Settings validate. site_cache lives in the worker
	# process, so Payment Gateway Profile.on_update only clears the worker that took the save; the TTL is
	# what bounds how long the other workers keep offering a just-disabled gateway.
	return frappe.get_all("Payment Gateway Profile", filters={"enabled": 1}, pluck="name")


@site_cache(ttl=60 * 60)
def get_gateway_currency_support() -> dict[str, tuple[str, ...] | None]:
	"""Map each enabled gateway to the currencies it can settle in, or None when it takes any of them.

	A gateway's allowlist is a code constant, so this is cached beside the enabled-profile list and cleared
	from the same two places. Building it costs one Single load per gateway, which is why it is not done
	inline on every checkout render.
	"""
	support = {}
	for gateway in get_available_payment_modes():
		support[gateway] = read_supported_currencies(gateway)
	return support


def read_supported_currencies(gateway: str) -> tuple[str, ...] | None:
	"""Normalise one gateway's declared currencies, treating an unreadable settings Single as unrestricted.

	Checkout is the one page that must never fail to render. A gateway whose controller raises on load
	would otherwise take the whole payment section down with it, so it is offered as before and refused
	later by `create_session` — the same place it would have been refused anyway.
	"""
	try:
		settings_doctype = frappe.get_cached_value("Payment Gateway Profile", gateway, "gateway_settings")
		currencies = frappe.get_single(settings_doctype).get_supported_currencies()
	except Exception:
		frappe.log_error(title=f"Could not read supported currencies for {gateway}")
		return None

	if not currencies:
		return None
	return tuple(currency.strip().upper() for currency in currencies)


def get_payment_modes_for_currency(currency: str) -> list[str]:
	"""The enabled gateways that can actually take this currency.

	Without this the storefront offers every enabled gateway on every cart, and a shopper who picks one
	that cannot settle their currency only finds out when `create_session` throws at them mid-checkout.
	An unknown currency falls through to the unfiltered list rather than hiding everything.
	"""
	requested = (currency or "").strip().upper()
	if not requested:
		return get_available_payment_modes()

	support = get_gateway_currency_support()
	return [
		gateway
		for gateway in get_available_payment_modes()
		# A gateway missing from the map was enabled after the map was cached; offer it rather than hide it.
		if support.get(gateway, None) is None or requested in support[gateway]
	]


def resolve_payment_mode(payment_mode: str) -> str | None:
	"""Return the enabled Payment Gateway Profile matching a client-supplied mode, case-insensitively."""
	requested = (payment_mode or "").strip().casefold()
	if not requested:
		return None
	for gateway in get_available_payment_modes():
		if gateway.casefold() == requested:
			return gateway
	return None


def get_localised_url(url: str) -> str:
	"""Retarget a configured redirect URL at the language the shopper is currently browsing in."""
	language = frappe.local.lang
	if not (url and language):
		return url

	parts = urlsplit(url)
	segments = parts.path.split("/")
	if len(segments) < 2 or not LANGUAGE_SEGMENT.match(segments[1]) or segments[1] == language:
		return url

	segments[1] = language
	return urlunsplit((parts.scheme, parts.netloc, "/".join(segments), parts.query, parts.fragment))
