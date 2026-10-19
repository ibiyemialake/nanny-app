# Gunicorn reads this automatically when started from this folder (the Docker image does).
# SQLite handles a home's traffic easily with a couple of workers.
bind = "0.0.0.0:8000"
workers = 2
threads = 4

# Caddy is the only thing that can reach gunicorn, so trust the headers it sets.
forwarded_allow_ips = "*"

# Log the visitor's real address (set by Caddy) rather than Caddy's own.
accesslog = "-"
access_log_format = '%({x-forwarded-for}i)s "%(r)s" %(s)s %(b)s %(M)sms "%(a)s"'
