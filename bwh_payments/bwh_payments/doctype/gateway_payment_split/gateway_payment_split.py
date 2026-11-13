# Copyright (c) 2026, Build With Hussain and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class GatewayPaymentSplit(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		account_id: DF.Data | None
		amount: DF.Currency
		hold_until: DF.Datetime | None
		linked_account: DF.Link
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		settlement_status: DF.Data | None
		transfer_id: DF.Data | None
		transfer_status: DF.Data | None
	# end: auto-generated types

	pass
