# Copyright (c) 2026, Build With Hussain and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from bwh_payments.base_class import LinkedAccountState
from bwh_payments.bwh_payments.utils import get_gateway_settings

UNSETTLED_STATUSES = ("Pending", "Needs Clarification")


class GatewayLinkedAccount(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		account_id: DF.Data | None
		gateway: DF.Link
		gateway_reference: DF.SmallText | None
		gateway_status: DF.Data | None
		ref_docname: DF.DynamicLink
		ref_doctype: DF.Link
		requirements: DF.JSON | None
		status: DF.Literal["Pending", "Active", "Needs Clarification", "Suspended"]
	# end: auto-generated types

	def validate(self):
		self.validate_gateway_can_split()
		self.validate_one_active_account()

	def validate_gateway_can_split(self):
		if not get_gateway_settings(self.gateway).split_currencies():
			frappe.throw(
				_("{0} cannot send payments on to linked accounts").format(frappe.bold(self.gateway))
			)

	def validate_one_active_account(self):
		if self.status != "Active":
			return
		other = frappe.db.exists(
			"Gateway Linked Account",
			{
				"gateway": self.gateway,
				"ref_doctype": self.ref_doctype,
				"ref_docname": self.ref_docname,
				"status": "Active",
				"name": ("!=", self.name),
			},
		)
		if other:
			frappe.throw(
				_("{0} already has an active linked account on {1}").format(self.ref_docname, self.gateway)
			)

	def update_from_gateway(self):
		state = LinkedAccountState.model_validate(
			get_gateway_settings(self.gateway).get_linked_account_state(self)
		)
		self.status = state.status
		self.gateway_status = state.gateway_status
		self.requirements = frappe.as_json(state.requirements)
		self.save(ignore_permissions=True)


def sync_unsettled_accounts():
	"""Hourly: ask the gateway about accounts onboarded through it that are not active yet."""
	for name in frappe.get_all(
		"Gateway Linked Account",
		filters={"status": ("in", UNSETTLED_STATUSES), "gateway_reference": ("is", "set")},
		pluck="name",
	):
		try:
			frappe.get_doc("Gateway Linked Account", name).update_from_gateway()
		except Exception:
			frappe.log_error(title=f"Could not sync linked account {name}")
