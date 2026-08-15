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
