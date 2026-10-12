import re
from contextlib import contextmanager
from urllib.parse import urlsplit, urlunsplit

import frappe
from frappe.model.base_document import get_controller
from frappe.utils.caching import redis_cache

from bwh_payments.base_class import PaymentGatewayBase

LANGUAGE_SEGMENT = re.compile(r"^[a-z]{2}(-[A-Za-z]{2})?$")


@redis_cache(ttl=60 * 60)
def get_available_payment_modes() -> list[str]:
	return frappe.get_all("Payment Gateway Profile", filters={"enabled": 1}, pluck="name")


def resolve_payment_mode(payment_mode: str) -> str | None:
	requested = (payment_mode or "").strip().casefold()
	if not requested:
		return None
	for gateway in get_available_payment_modes():
		if gateway.casefold() == requested:
			return gateway
	return None


def get_localised_url(url: str) -> str:
	language = frappe.local.lang
	if not (url and language):
		return url

	parts = urlsplit(url)
	segments = parts.path.split("/")
	if len(segments) < 2 or not LANGUAGE_SEGMENT.match(segments[1]) or segments[1] == language:
		return url

	segments[1] = language
	return urlunsplit((parts.scheme, parts.netloc, "/".join(segments), parts.query, parts.fragment))


def get_gateway_settings(gateway: str):
	"""The `<Gateway> Gateway Settings` Single behind a Payment Gateway Profile."""
	return frappe.get_cached_doc(
		frappe.get_cached_value("Payment Gateway Profile", gateway, "gateway_settings")
	)


def get_gateway_settings_doctypes() -> list[str]:
	"""Every `<Gateway> Gateway Settings` Single on the site, including another app's gateways."""
	doctypes = frappe.get_all(
		"DocType", filters={"issingle": 1, "name": ("like", "% Gateway Settings")}, pluck="name"
	)
	return [doctype for doctype in doctypes if issubclass(get_controller(doctype), PaymentGatewayBase)]


def supports_currency(settings, currency: str) -> bool:
	supported = settings.supported_currencies()
	return supported is None or currency.strip().upper() in supported


@contextmanager
def run_as_administrator():
	"""Settling an order downstream needs a real user. Restored after, so a worker does not stay Administrator.

	`set_user` overwrites the session's sid and data, and a web request saves that session back when it
	ends, so the whole session is restored in place. Otherwise a signed-in shopper is logged out.
	"""
	session = frappe._dict(frappe.local.session)
	try:
		frappe.set_user("Administrator")
		yield
	finally:
		frappe.set_user(session.user)
		frappe.local.session.update(session)


def is_local_path(url: str | None) -> bool:
	"""A path on this site. Browsers read `//host` and `/\\host` as another site."""
	if not url or not url.startswith("/") or url.startswith(("//", "/\\")):
		return False
	parts = urlsplit(url)
	return not (parts.scheme or parts.netloc)
