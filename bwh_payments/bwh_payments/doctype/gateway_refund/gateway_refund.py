# Copyright (c) 2026, Build With Hussain and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from bwh_payments.base_class import RefundResult

REFUND_STATUS_MAP = {
	"succeeded": "Processed",
	"processed": "Processed",
	"pending": "Pending",
	"failed": "Failed",
	"canceled": "Failed",
	"cancelled": "Failed",
}


def normalise_refund_status(status: str | None) -> str:
	# A gateway that answers without a status answers only once the refund went through.
	if not status:
		return "Processed"
	return REFUND_STATUS_MAP.get(status.strip().casefold(), "Pending")


class GatewayRefund(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		amount: DF.Currency
		currency: DF.Link | None
		error: DF.SmallText | None
		last_webhook_event_id: DF.Data | None
		payment_entry: DF.Data | None
		payment_request: DF.Link
		reference: DF.Data | None
		refund_id: DF.Data | None
		status: DF.Literal["Pending", "Processed", "Failed"]
	# end: auto-generated types

	def on_update(self):
		if not self.has_value_changed("status"):
			return

		payment_request = frappe.get_doc("Gateway Payment Request", self.payment_request)
		if self.status == "Processed":
			payment_request.add_processed_refund(self)

		frappe.get_doc(payment_request.ref_doctype, payment_request.ref_docname).run_method(
			"on_refund_update", self
		)

	def lock_row(self):
		frappe.db.get_value("Gateway Refund", self.name, "status", for_update=True)
		self.reload()

	def apply_gateway_result(self, result: RefundResult):
		self.refund_id = result.refund_id or self.refund_id
		self.status = normalise_refund_status(result.status)
		self.save(ignore_permissions=True)

	def apply_gateway_status(self, status: str | None, event_id: str | None = None) -> bool:
		"""Move a Pending refund to the status the gateway reports. A settled refund never changes."""
		self.lock_row()

		status = normalise_refund_status(status)
		if event_id and self.last_webhook_event_id == event_id:
			return False
		if self.status != "Pending" or status == "Pending":
			return False

		self.status = status
		self.last_webhook_event_id = event_id
		self.save(ignore_permissions=True)
		return True

	def mark_failed(self, error: str):
		self.status = "Failed"
		self.error = error
		self.save(ignore_permissions=True)
