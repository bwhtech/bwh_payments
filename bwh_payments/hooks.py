app_name = "bwh_payments"
app_title = "BWH Payments"
app_publisher = "developers@buildwithhussain.com"
app_description = "Payment gateway integrations for Frappe"
app_email = "developers@buildwithhussain.com"
app_license = "mit"

use_json_request_body = True

required_apps = []

doc_events = {
	"Payment Entry": {
		"on_submit": "bwh_payments.bwh_payments.doctype.gateway_payment_request.gateway_payment_request.refund_on_payment_entry",
	},
}

export_python_type_annotations = True
require_type_annotated_api_methods = True
