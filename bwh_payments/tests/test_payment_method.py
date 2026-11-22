from bwh_payments.bwh_payments.doctype.razorpay_gateway_settings.test_razorpay_gateway_settings import (
	RazorpayTestCase,
)
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
