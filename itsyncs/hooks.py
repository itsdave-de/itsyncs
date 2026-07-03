app_name = "itsyncs"
app_title = "itsyncs"
app_publisher = "itsdave GmbH"
app_description = "Sync contacts between different Exchange Online sources"
app_email = "dev@itsdave.de"
app_license = "gpl-3.0"
app_logo_url = "/assets/itsyncs/images/itsyncs-logo.svg"
app_home = "/app/itsyncs"

add_to_apps_screen = [
	{
		"name": "itsyncs",
		"logo": "/assets/itsyncs/images/itsyncs-logo.svg",
		"title": "itsyncs",
		"route": "/app/itsyncs",
	}
]

# Svg Icons
# ------------------
app_include_icons = "/assets/itsyncs/icons/itsyncs.svg"

# Scheduled Tasks
# ---------------
scheduler_events = {
	"cron": {
		"*/5 * * * *": [
			"itsyncs.tasks.run_due_syncs",
			"itsyncs.tasks.radicale_watchdog"
		],
		"0 6 * * *": [
			"itsyncs.tasks.daily_sms_balance_check"
		],
		"30 5 * * *": [
			"itsyncs.tasks.run_consistency_audit"
		],
	},
	"daily": [
		"itsyncs.tasks.cleanup_old_logs",
	],
}

# Mobile enrollment landing page: /dav-enroll/<token> → www/dav_enroll
website_route_rules = [
	{"from_route": "/dav-enroll/<token>", "to_route": "dav_enroll"},
]
