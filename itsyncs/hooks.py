app_name = "itsyncs"
app_title = "itsyncs"
app_publisher = "itsdave GmbH"
app_description = "Sync contacts between different Exchange Online sources"
app_email = "dev@itsdave.de"
app_license = "gpl-3.0"
app_logo_url = "/assets/itsyncs/images/itsyncs-logo.svg"
app_home = "/app/itsync-pair"

add_to_apps_screen = [
	{
		"name": "itsyncs",
		"logo": "/assets/itsyncs/images/itsyncs-logo.svg",
		"title": "itsyncs",
		"route": "/app/itsync-pair",
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
			"itsyncs.tasks.run_due_syncs"
		],
	},
}

# default_log_clearing_doctypes = {
# 	"ITSync Log": 90
# }
