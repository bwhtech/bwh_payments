from contextlib import contextmanager
from unittest.mock import patch

import frappe
from frappe.database import get_db

from bwh_payments.bwh_payments.doctype.razorpay_gateway_settings.test_razorpay_gateway_settings import (
	RazorpayTestCase,
	make_razorpay_payment_request,
)
from bwh_payments.services.razorpay.live import log_request
from bwh_payments.services.razorpay.stub import StubRazorpayClient

# A GET /payments/{id} response as Razorpay sends it, trimmed of fields nobody reads.
CARD_PAYMENT = {
	"id": "pay_29QQoUBi66xm2f",
	"entity": "payment",
	"amount": 50000,
	"currency": "INR",
	"status": "captured",
	"order_id": "order_9A33XWu170gUtm",
	"method": "card",
	"captured": True,
	"card_id": "card_29QQoUBi66xm2f",
	"card": {
		"id": "card_29QQoUBi66xm2f",
		"entity": "card",
		"last4": "4366",
		"network": "Visa",
		"type": "credit",
		"issuer": "HDFC",
		"international": False,
		"emi": False,
	},
	"bank": None,
	"wallet": None,
	"vpa": None,
	"email": "gaurav.kumar@example.com",
	"contact": "+919876543210",
	"fee": 1180,
	"tax": 180,
	"created_at": 1543473327,
}


def recorded_payment(method: str, **fields) -> dict:
	payment = {key: value for key, value in CARD_PAYMENT.items() if key not in ("card", "card_id")}
	return {**payment, "method": method, **fields}


@contextmanager
def other_connection():
	"""A second database session, as a concurrent worker would hold."""
	conf = frappe.conf
	database = get_db(
		socket=conf.db_socket,
		host=conf.db_host,
		port=conf.db_port,
		user=conf.db_user,
		password=conf.db_password,
		cur_db_name=conf.db_name,
	)
	database.connect()
	try:
		yield database
	finally:
		database.rollback()
		database.close()


def is_locked_elsewhere(doctype: str, name: str) -> bool:
	with other_connection() as database:
		try:
			database.sql(f"select name from `tab{doctype}` where name = %s for update nowait", name)
		except frappe.QueryTimeoutError:
			return True
		return False


class TestRazorpayPaymentMethod(RazorpayTestCase):
	def get_payment_method(self, response: dict) -> str | None:
		link_id = StubRazorpayClient.add_link("paid")
		StubRazorpayClient.add_payment(link_id, "captured", response)
		return self.get_settings().get_payment_method(link_id)

	def test_a_credit_card_payment_reads_as_card_credit(self):
		self.assertEqual(self.get_payment_method(CARD_PAYMENT), "card_credit")

	def test_a_debit_card_payment_reads_as_card_debit(self):
		debit_card = {**CARD_PAYMENT["card"], "type": "debit"}

		self.assertEqual(self.get_payment_method({**CARD_PAYMENT, "card": debit_card}), "card_debit")

	def test_a_card_of_unknown_type_reads_as_card(self):
		prepaid_card = {**CARD_PAYMENT["card"], "type": "prepaid"}

		self.assertEqual(self.get_payment_method({**CARD_PAYMENT, "card": prepaid_card}), "card")

	def test_each_other_method_reads_as_its_razorpay_name(self):
		responses = {
			"upi": recorded_payment("upi", vpa="gaurav.kumar@exampleupi"),
			"netbanking": recorded_payment("netbanking", bank="HDFC"),
			"wallet": recorded_payment("wallet", wallet="freecharge"),
			"emi": recorded_payment("emi"),
			"paylater": recorded_payment("paylater", wallet="lazypay"),
		}
		for expected, response in responses.items():
			with self.subTest(method=expected):
				self.assertEqual(self.get_payment_method(response), expected)

	def test_a_cardless_emi_payment_reads_as_emi(self):
		self.assertEqual(self.get_payment_method(recorded_payment("cardless_emi")), "emi")

	def test_a_link_without_a_settled_payment_has_no_method(self):
		link_id = StubRazorpayClient.add_link("created")

		self.assertIsNone(self.get_settings().get_payment_method(link_id))

	def test_a_payment_imported_from_before_payment_links_is_read_by_its_payment_id(self):
		link_id = StubRazorpayClient.add_link("paid")
		payment_id = StubRazorpayClient.add_payment(link_id, "captured", recorded_payment("netbanking"))

		self.assertEqual(self.get_settings().get_payment_method(payment_id), "netbanking")

	def test_a_known_payment_is_read_without_asking_for_its_link_again(self):
		link_id = StubRazorpayClient.add_link("paid")
		payment_id = StubRazorpayClient.add_payment(link_id, "captured", recorded_payment("wallet"))
		del StubRazorpayClient.links[link_id]

		self.assertEqual(
			self.get_settings().get_payment_method(link_id, transaction_ref=payment_id), "wallet"
		)


class TestPaymentRequestPaymentMethod(RazorpayTestCase):
	def test_a_paid_request_stores_how_it_was_paid(self):
		payment_request = make_razorpay_payment_request(500)
		StubRazorpayClient.pay(payment_request.order_ref, CARD_PAYMENT)

		payment_request.sync_status()

		self.assertEqual(payment_request.payment_method, "card_credit")

	def test_the_settled_payment_is_looked_up_once(self):
		payment_request = make_razorpay_payment_request(500)
		StubRazorpayClient.pay(payment_request.order_ref, CARD_PAYMENT)

		with patch.object(
			StubRazorpayClient,
			"get_payment_link",
			autospec=True,
			side_effect=StubRazorpayClient.get_payment_link,
		) as get_payment_link:
			payment_request.sync_status()

		# Once for the status, once for the settled payment's id.
		self.assertEqual(get_payment_link.call_count, 2)
		self.assertEqual(payment_request.payment_method, "card_credit")

	def test_a_failed_method_lookup_does_not_block_the_payment(self):
		payment_request = make_razorpay_payment_request(500)
		payment_id = StubRazorpayClient.pay(payment_request.order_ref)
		del StubRazorpayClient.payments[payment_id]

		payment_request.sync_status()

		self.assertEqual(payment_request.status, "Paid")
		self.assertIsNone(payment_request.payment_method)

	def test_the_payment_stays_locked_while_its_method_is_read(self):
		payment_request = make_razorpay_payment_request(500)
		StubRazorpayClient.pay(payment_request.order_ref, CARD_PAYMENT)
		locked_during_lookup = []
		get_payment = StubRazorpayClient.get_payment

		def get_payment_as_the_live_client(client, payment_id):
			# The live client logs every call, and logging commits.
			log_request(f"payments/{payment_id}")
			locked_during_lookup.append(is_locked_elsewhere(payment_request.doctype, payment_request.name))
			return get_payment(client, payment_id)

		with patch.object(
			StubRazorpayClient, "get_payment", autospec=True, side_effect=get_payment_as_the_live_client
		):
			payment_request.apply_webhook_status("Paid", event_id="evt_locked_lookup")

		self.assertEqual(locked_during_lookup, [True])
		self.assertEqual(payment_request.payment_method, "card_credit")
