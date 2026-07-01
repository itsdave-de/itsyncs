"""Manage the Radicale CardDAV sidecar as a self-healing service.

Radicale runs as a detached subprocess (its own session, so it survives the
web/worker process that launched it). A scheduler watchdog reconciles the actual
state to the desired state (ITSync Settings.radicale_enabled) every few minutes,
so it auto-restarts after a crash or a host reboot — no systemd/supervisor
needed. Start/stop/status are also exposed on the Settings page.
"""

import os
import signal
import socket
import subprocess
import sys

import frappe

RADICALE_HOST = "127.0.0.1"
DEFAULT_PORT = 5232


def _port() -> int:
	return frappe.utils.cint(frappe.conf.get("carddav_radicale_port")) or DEFAULT_PORT


def _base() -> str:
	return os.path.abspath(frappe.get_site_path("carddav"))


def _config_path() -> str:
	return os.path.join(_base(), "config")


def _pid_path() -> str:
	return os.path.join(_base(), "radicale.pid")


def _log_path() -> str:
	return os.path.join(_base(), "radicale.log")


def ensure_config() -> None:
	"""Write the Radicale config + make sure storage/auth files exist."""
	base = _base()
	os.makedirs(os.path.join(base, "collections"), exist_ok=True)

	# htpasswd + rights are generated from the enrolled devices; ensure they exist.
	if not os.path.exists(os.path.join(base, "rights")):
		from itsyncs.carddav import radicale_config

		radicale_config.regenerate()

	cfg = (
		"[server]\n"
		f"hosts = {RADICALE_HOST}:{_port()}\n"
		"max_connections = 48\n\n"
		"[auth]\n"
		"type = htpasswd\n"
		f"htpasswd_filename = {base}/users\n"
		"htpasswd_encryption = bcrypt\n\n"
		"[rights]\n"
		"type = from_file\n"
		f"file = {base}/rights\n\n"
		"[storage]\n"
		"type = multifilesystem\n"
		f"filesystem_folder = {base}/collections\n\n"
		"[logging]\n"
		"level = info\n"
	)
	tmp = _config_path() + ".tmp"
	with open(tmp, "w", encoding="utf-8") as f:
		f.write(cfg)
	os.replace(tmp, _config_path())


def is_running() -> bool:
	"""True if something is serving on the Radicale port (the real liveness check)."""
	s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
	s.settimeout(1.5)
	try:
		s.connect((RADICALE_HOST, _port()))
		return True
	except OSError:
		return False
	finally:
		s.close()


def start() -> None:
	"""Launch Radicale detached (idempotent)."""
	if is_running():
		return
	ensure_config()
	log = open(_log_path(), "a")  # noqa: SIM115 — handed to the child
	proc = subprocess.Popen(
		[sys.executable, "-m", "radicale", "--config", _config_path()],
		stdout=log,
		stderr=log,
		stdin=subprocess.DEVNULL,
		start_new_session=True,  # detach: survives the launching process
	)
	with open(_pid_path(), "w") as f:
		f.write(str(proc.pid))


def stop() -> None:
	"""Terminate the Radicale process recorded in the pid file."""
	try:
		with open(_pid_path()) as f:
			pid = int(f.read().strip())
		os.kill(pid, signal.SIGTERM)
	except (FileNotFoundError, ProcessLookupError, ValueError):
		pass
	try:
		os.remove(_pid_path())
	except FileNotFoundError:
		pass


def refresh_status() -> dict:
	"""Check liveness and persist status + timestamp to ITSync Settings."""
	running = is_running()
	status = "Running" if running else "Stopped"
	settings = frappe.get_single("ITSync Settings")
	settings.db_set("radicale_status", status, update_modified=False)
	settings.db_set("radicale_status_checked_at", frappe.utils.now_datetime(), update_modified=False)
	frappe.db.commit()
	return {"status": status, "running": running, "port": _port()}
