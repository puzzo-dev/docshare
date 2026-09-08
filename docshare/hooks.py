app_name = "docshare"
app_title = "DocShare"
app_publisher = "I-Varse Technologies NG"
app_description = "Unauthenticated printable document share links for any Frappe document"
app_email = "dev@itechnologies.ng"
app_license = "mit"

# ------------------
# Required apps
# ------------------

# DocShare uses ERPNext's native linked-documents API and party fields
# (customer/lead/party_type) to resolve whom a shared document belongs to.
required_apps = ["erpnext"]

# ------------------
# Includes
# ------------------

# Inject the Share Document menu into every Desk form view.
app_include_js = "docshare.bundle.js"
app_include_css = "docshare.bundle.css"

# ------------------
# Website route rules
# ------------------

# /share/<token> -> www/share (server-rendered Jinja, no Desk chrome)
website_route_rules = [
	{"from_route": "/share/<token>", "to_route": "share"},
]

# ------------------
# Document events
# ------------------

# When any document is trashed, auto-disable any DocShare Links pointing to it.
doc_events = {
	"*": {
		"on_trash": "docshare.api.on_document_trash",
	}
}

# ------------------
# Permissions
# ------------------

# DocShare Link access is gated by the caller's permission on the
# referenced target document. See docshare.api for the implementation.
has_permission = {
	"DocShare Link": "docshare.api.has_docshare_link_permission",
	"DocShare View Log": "docshare.api.has_docshare_view_log_permission",
}

permission_query_conditions = {
	"DocShare Link": "docshare.api.get_docshare_link_permission_query_conditions",
	"DocShare View Log": "docshare.api.get_docshare_view_log_permission_query_conditions",
}

# Apps
# ------------------

# required_apps = []

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "docshare",
# 		"logo": "/assets/docshare/logo.png",
# 		"title": "DocShare",
# 		"route": "/docshare",
# 		"has_permission": "docshare.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "docshare.bundle.css"
# app_include_js = "/assets/docshare/js/docshare.js"

# include js, css files in header of web template
# web_include_css = "/assets/docshare/css/docshare.css"
# web_include_js = "/assets/docshare/js/docshare.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "docshare/public/scss/website"

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
# app_include_icons = "docshare/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "docshare.utils.jinja_methods",
# 	"filters": "docshare.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "docshare.install.before_install"
# after_install = "docshare.install.after_install"

# Uninstallation
# ------------

# before_uninstall = "docshare.uninstall.before_uninstall"
# after_uninstall = "docshare.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "docshare.utils.before_app_install"
# after_app_install = "docshare.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "docshare.utils.before_app_uninstall"
# after_app_uninstall = "docshare.utils.after_app_uninstall"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "docshare.notifications.get_notification_config"

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

# DocType Class
# ---------------
# Override standard doctype classes

# override_doctype_class = {
# 	"ToDo": "custom_app.overrides.CustomToDo"
# }

# Document Events
# ---------------
# Hook on document methods and events

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
# 		"docshare.tasks.all"
# 	],
# 	"daily": [
# 		"docshare.tasks.daily"
# 	],
# 	"hourly": [
# 		"docshare.tasks.hourly"
# 	],
# 	"weekly": [
# 		"docshare.tasks.weekly"
# 	],
# 	"monthly": [
# 		"docshare.tasks.monthly"
# 	],
# }

# Testing
# -------

# before_tests = "docshare.install.before_tests"

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "docshare.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "docshare.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["docshare.utils.before_request"]
# after_request = ["docshare.utils.after_request"]

# Job Events
# ----------
# before_job = ["docshare.utils.before_job"]
# after_job = ["docshare.utils.after_job"]

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
# 	"docshare.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

# Translation
# ------------
# List of apps whose translatable strings should be excluded from this app's translations.
# ignore_translatable_strings_from = []

