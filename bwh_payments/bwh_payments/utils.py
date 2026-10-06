import re
from urllib.parse import urlsplit, urlunsplit

import frappe
from frappe.integrations.utils import get_json
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


def create_request_log(data: dict, service_name: str, output=None, error=None, defer=False, **kwargs):
	"""`frappe.integrations.utils.create_request_log` without its `frappe.db.commit()`.

	That commit ends the caller's transaction: it releases the row lock a refund holds against a concurrent
	refund, and it makes the caller's half-done work permanent, such as a Payment Entry whose refund then fails.

	`defer` queues the record in Redis instead, for a failure the caller is about to roll back. It reaches the
	table when the scheduler next flushes deferred inserts.
	"""
	log = frappe.get_doc(
		{
			"doctype": "Integration Request",
			"integration_request_service": service_name,
			"data": get_json(data),
			"output": get_json(output),
			"error": get_json(error),
			**kwargs,
		}
	)
	if defer:
		log.deferred_insert()
		return log
	return log.insert(ignore_permissions=True)
