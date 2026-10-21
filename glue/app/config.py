import os

OP_URL = os.getenv("OP_URL", "http://openproject:80")
OP_API_KEY = os.getenv("OP_API_KEY", "")
GITEA_URL = os.getenv("GITEA_URL", "http://gitea:3000")
GITEA_TOKEN = os.getenv("GITEA_TOKEN", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
STATUS_CLOSED = os.getenv("STATUS_CLOSED", "Closed")
STATUS_IN_PROGRESS = os.getenv("STATUS_IN_PROGRESS", "In progress")
OP_FORWARDED_PROTO = os.getenv("OP_FORWARDED_PROTO", "")  # "https" when OpenProject runs with OPENPROJECT_HTTPS=true behind a TLS proxy
OP_HOST_HEADER = os.getenv("OP_HOST_HEADER", "localhost:8080")  # must equal OPENPROJECT_HOST__NAME
PUBLIC_URL = os.getenv("PUBLIC_URL", "http://localhost:8090")  # base URL of this service, used for links in OpenProject
OP_PUBLIC_URL = os.getenv("OP_PUBLIC_URL", "http://localhost:8080")
LEGACY_KEYS_DIR = os.getenv("LEGACY_KEYS_DIR", "/data/import-state")  # directory with the importer's jira-*.json files

# Optional single sign-on (OpenID Connect). Active when OIDC_ISSUER is set; tenantctl enable-sso sets all of these.
OIDC_ISSUER = os.getenv("OIDC_ISSUER", "")  # public issuer URL, e.g. https://auth.acme.example.com/realms/panoptes
OIDC_CLIENT_ID = os.getenv("OIDC_CLIENT_ID", "panoptes-docs")
OIDC_CLIENT_SECRET = os.getenv("OIDC_CLIENT_SECRET", "")
SESSION_SECRET = os.getenv("SESSION_SECRET", "")
OIDC_ADMIN_ROLE = os.getenv("OIDC_ADMIN_ROLE", "admin")  # realm role allowed to use /stale

# Weekly stale-ticket digest (off unless DIGEST_PROJECTS and a delivery channel are set)
DIGEST_PROJECTS = [p.strip() for p in os.getenv("DIGEST_PROJECTS", "").split(",") if p.strip()]  # OpenProject project identifiers
DIGEST_DAYS = int(os.getenv("DIGEST_DAYS", "30"))
DIGEST_WEEKDAY = int(os.getenv("DIGEST_WEEKDAY", "0"))  # 0 = Monday
DIGEST_HOUR = int(os.getenv("DIGEST_HOUR", "8"))
DIGEST_TZ = os.getenv("DIGEST_TZ", "UTC")
DIGEST_WEBHOOK_URL = os.getenv("DIGEST_WEBHOOK_URL", "")  # Slack-compatible incoming webhook ({"text": ...})
DIGEST_WEBHOOK_FORMAT = os.getenv("DIGEST_WEBHOOK_FORMAT", "slack")  # slack | markdown (Mattermost, Rocket.Chat)
DIGEST_SMTP_HOST = os.getenv("DIGEST_SMTP_HOST", "")
DIGEST_SMTP_PORT = int(os.getenv("DIGEST_SMTP_PORT", "587"))
DIGEST_SMTP_USER = os.getenv("DIGEST_SMTP_USER", "")
DIGEST_SMTP_PASSWORD = os.getenv("DIGEST_SMTP_PASSWORD", "")
DIGEST_SMTP_STARTTLS = os.getenv("DIGEST_SMTP_STARTTLS", "true").lower() != "false"
DIGEST_MAIL_FROM = os.getenv("DIGEST_MAIL_FROM", "")
DIGEST_MAIL_TO = [a.strip() for a in os.getenv("DIGEST_MAIL_TO", "").split(",") if a.strip()]
DIGEST_STATE_FILE = os.getenv("DIGEST_STATE_FILE", "/data/state/digest.json")
