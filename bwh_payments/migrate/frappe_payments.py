"""Move gateway setup from frappe/payments, for apps switching to this one.

Each frappe/payments Payment Gateway becomes a Payment Gateway Profile with the same name, so Link
values that point at it stay valid. Credentials are copied only where this app has the gateway and
its settings are still empty. Anything else keeps a disabled profile, for history.
"""

import frappe
from frappe.utils.password import get_decrypted_password


def import_gateways() -> list[str]:
	"""Create the missing profiles and return their names. Safe to run more than once."""
	if not frappe.db.exists("DocType", "Payment Gateway"):
		return []

	imported = []
	for gateway in frappe.get_all(
		"Payment Gateway", fields=["name", "gateway_settings", "gateway_controller"], order_by="creation asc"
	):
		if frappe.db.exists("Payment Gateway Profile", gateway.name):
			continue

		settings = copy_settings(gateway.gateway_settings, gateway.gateway_controller)
		frappe.get_doc(
			{
				"doctype": "Payment Gateway Profile",
				"name": gateway.name,
				"gateway_settings": settings,
				"enabled": 1 if settings else 0,
			}
		).insert(ignore_permissions=True)
		imported.append(gateway.name)

	return imported


def copy_settings(source_doctype: str | None, source_name: str | None) -> str | None:
	"""Copy credentials into this app's settings and return their doctype, or None if not possible."""
	if source_doctype == "Razorpay Settings":
		return copy_razorpay_settings()
	if source_doctype == "Stripe Settings" and source_name:
		return copy_stripe_settings(source_name)
	return None


def copy_razorpay_settings() -> str | None:
	key_id = frappe.db.get_single_value("Razorpay Settings", "api_key")
	settings = frappe.get_single("Razorpay Gateway Settings")
	if not key_id or settings.key_id:
		return None

	settings.update(
		{
			"key_id": key_id,
			"key_secret": read_password("Razorpay Settings", "Razorpay Settings", "api_secret"),
			"webhook_secret": read_password("Razorpay Settings", "Razorpay Settings", "webhook_secret"),
			"test_mode": 1 if key_id.startswith("rzp_test_") else 0,
		}
	)
	settings.save(ignore_permissions=True)
	return settings.doctype


def copy_stripe_settings(account: str) -> str | None:
	# This app holds one Stripe account; a second frappe/payments account stays history only.
	settings = frappe.get_single("Stripe Gateway Settings")
	if settings.private_key:
		return None

	secret_key = read_password("Stripe Settings", account, "secret_key")
	if not secret_key:
		return None

	settings.update(
		{
			"public_key": frappe.db.get_value("Stripe Settings", account, "publishable_key"),
			"private_key": secret_key,
			"mode": "Test" if secret_key.startswith("sk_test_") else "Live",
		}
	)
	# frappe/payments never stored a Stripe webhook secret; it is set by hand once the webhook exists.
	settings.flags.ignore_mandatory = True
	settings.save(ignore_permissions=True)
	return settings.doctype


def read_password(doctype: str, name: str, fieldname: str) -> str | None:
	return get_decrypted_password(doctype, name, fieldname, raise_exception=False)
