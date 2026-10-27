import os


def connect():
    url = os.environ["APP_DB_URL"]
    pool = int(os.environ.get("APP_DB_POOL") or 5)
    return url, pool
