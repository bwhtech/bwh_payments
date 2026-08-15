app_name = "bwh_payments"
app_title = "BWH Payments"
app_publisher = "developers@buildwithhussain.com"
app_description = "Payment gateway integrations for Frappe"
app_email = "developers@buildwithhussain.com"
app_license = "mit"

# Send non-GET requests for this app's endpoints as native `application/json`
# bodies instead of form-encoded, per-key JSON-stringified values.
use_json_request_body = True

# Apps
# ------------------

required_apps = ["frappe/payments"]

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "bwh_payments",
# 		"logo": "/assets/bwh_payments/logo.png",
# 		"title": "BWH Payments",
# 		"route": "/bwh_payments",
# 		"has_permission": "bwh_payments.api.permission.has_app_permission",
# 	}
# ]

# Companion apps that extend a host app (instead of taking their own apps-screen icon) can pin
# their workspaces into the host app's workspace dock (rail) with this hook. Declaring it keeps
# the app off the apps screen, so it takes precedence over any add_to_apps_screen above. Who can
# see a pinned workspace is controlled by that workspace's own Roles table.
# add_to_workspace_dock = [
# 	{
# 		"app": "erpnext",
# 		"workspace": "My Workspace",
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/bwh_payments/css/bwh_payments.css"
# app_include_js = "/assets/bwh_payments/js/bwh_payments.js"

# include js, css files in header of web template
# web_include_css = "/assets/bwh_payments/css/bwh_payments.css"
# web_include_js = "/assets/bwh_payments/js/bwh_payments.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "bwh_payments/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# doctype_js = {"doctype" : "public/js/doctype.js"}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "bwh_payments/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Setup Wizard
# ------------

# open a fresh site's setup in this app's own UI instead of the desk wizard.
# must be a non-desk route (not under /desk or /app); to customize setup within
# desk, use setup_wizard_stages / setup_wizard_complete instead.
# setup_wizard_url = "/bwh_payments/setup"

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# automatically load and sync documents of this doctype from downstream apps
# importable_doctypes = [doctype_1]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "bwh_payments.utils.jinja_methods",
# 	"filters": "bwh_payments.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "bwh_payments.install.before_install"
# after_install = "bwh_payments.install.after_install"

# Uninstallation
# ------------

# before_uninstall = "bwh_payments.uninstall.before_uninstall"
# after_uninstall = "bwh_payments.uninstall.after_uninstall"

# Disable / Enable
# ----------------
# Called when this app is logically disabled or re-enabled on a site,
# without uninstalling it. Use this to hide/restore fields this app adds
# to other apps' doctypes.

# before_disable = "bwh_payments.uninstall.before_disable"
# after_disable = "bwh_payments.uninstall.after_disable"
# before_enable = "bwh_payments.install.before_enable"
# after_enable = "bwh_payments.install.after_enable"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "bwh_payments.utils.before_app_install"
# after_app_install = "bwh_payments.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "bwh_payments.utils.before_app_uninstall"
# after_app_uninstall = "bwh_payments.utils.after_app_uninstall"

# Build
# ------------------
# To hook into the build process

# after_build = "bwh_payments.build.after_build"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "bwh_payments.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# Document Events
# ---------------
# Hook on document methods and events

doc_events = {
	"Payment Entry": {
		"on_submit": "bwh_payments.bwh_payments.doctype.gateway_payment_request.gateway_payment_request.refund_on_payment_entry",
	},
}

# doc_events = {
# 	"*": {
# 		"on_update": "method",
# 		"on_cancel": "method",
# 		"on_trash": "method"
# 	}
# }

# Scheduled Tasks
# ---------------

# scheduler_events = {
# 	"all": [
# 		"bwh_payments.tasks.all"
# 	],
# 	"daily": [
# 		"bwh_payments.tasks.daily"
# 	],
# 	"hourly": [
# 		"bwh_payments.tasks.hourly"
# 	],
# 	"weekly": [
# 		"bwh_payments.tasks.weekly"
# 	],
# 	"monthly": [
# 		"bwh_payments.tasks.monthly"
# 	],
# }

# Testing
# -------

# before_tests = "bwh_payments.install.before_tests"

# Extend DocType Class
# ------------------------------
#
# Specify custom mixins to extend the standard doctype controller.
# extend_doctype_class = {
# 	"Task": "bwh_payments.custom.task.CustomTaskMixin"
# }

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "bwh_payments.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "bwh_payments.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["bwh_payments.utils.before_request"]
# after_request = ["bwh_payments.utils.after_request"]

# Job Events
# ----------
# before_job = ["bwh_payments.utils.before_job"]
# after_job = ["bwh_payments.utils.after_job"]

# after_file_upload = ["bwh_payments.utils.after_file_upload"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"bwh_payments.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
export_python_type_annotations = True

# Require all whitelisted methods to have type annotations
require_type_annotated_api_methods = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

# Translation
# ------------
# List of apps whose translatable strings should be excluded from this app's translations.
# ignore_translatable_strings_from = []
