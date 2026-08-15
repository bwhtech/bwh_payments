# Copyright (c) 2026, Build With Hussain and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from payments.utils import create_payment_gateway

from bwh_payments.base_class import PaymentGatewayBase


class PaymentGatewayProfile(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		enabled: DF.Check
		gateway_settings: DF.Link
		payment_gateway: DF.Link | None
	# end: auto-generated types

	def validate(self):
		self.validate_gateway_settings_implement_contract()

	def validate_gateway_settings_implement_contract(self):
		controller = frappe.get_single(self.gateway_settings)
		if not isinstance(controller, PaymentGatewayBase):
			frappe.throw(
				_("{0} does not implement the payment gateway contract").format(
					frappe.bold(self.gateway_settings)
				)
			)

	def on_update(self):
		create_payment_gateway(self.name, settings=self.gateway_settings)
		if not self.payment_gateway:
			self.db_set("payment_gateway", self.name, update_modified=False)

	def get_controller(self) -> PaymentGatewayBase:
		return frappe.get_single(self.gateway_settings)
