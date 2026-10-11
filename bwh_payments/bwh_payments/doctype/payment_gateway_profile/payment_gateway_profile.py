# Copyright (c) 2026, Build With Hussain and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from bwh_payments.base_class import PaymentGatewayBase
from bwh_payments.bwh_payments.utils import get_available_payment_modes


class PaymentGatewayProfile(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		enabled: DF.Check
		gateway_settings: DF.Link | None
	# end: auto-generated types

	def validate(self):
		self.validate_gateway_settings_implement_contract()

	def validate_gateway_settings_implement_contract(self):
		# A disabled profile without settings is kept so old payments still name their gateway.
		if not self.gateway_settings:
			return
		controller = frappe.get_single(self.gateway_settings)
		if not isinstance(controller, PaymentGatewayBase):
			frappe.throw(
				_("{0} does not implement the payment gateway contract").format(
					frappe.bold(self.gateway_settings)
				)
			)

	def on_update(self):
		clear_available_payment_modes()

	def on_trash(self):
		clear_available_payment_modes()

	def after_rename(self, old_name: str, new_name: str, merge: bool = False):
		clear_available_payment_modes()

	def get_controller(self) -> PaymentGatewayBase:
		return frappe.get_single(self.gateway_settings)


def clear_available_payment_modes():
	# Cleared again once the transaction ends: a read in between caches rows that may not last.
	get_available_payment_modes.clear_cache()
	frappe.db.after_commit.add(get_available_payment_modes.clear_cache)
	frappe.db.after_rollback.add(get_available_payment_modes.clear_cache)
