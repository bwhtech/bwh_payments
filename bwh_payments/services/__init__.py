import frappe
from pydantic import BaseModel, ConfigDict


class GatewayUnavailableError(frappe.ValidationError):
	"""The gateway could not answer (a 5xx, a rate limit or a dropped connection), so a retry may work."""


class GatewayModel(BaseModel):
	"""Gateways sometimes send ids or amounts as bare numbers; read them as strings."""

	model_config = ConfigDict(coerce_numbers_to_str=True)
