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
