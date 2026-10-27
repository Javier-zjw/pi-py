import os

TIMEOUT_KEY = "APP_TIMEOUT"


def load():
    return {
        "debug": os.getenv("APP_DEBUG", "0") == "1",
        "timeout": int(os.getenv(TIMEOUT_KEY, "30")),
        # 旧版本用过 os.getenv("APP_LEGACY_MODE")，已经去掉了
        "region": os.environ.get("APP_REGION", "cn-north"),
    }
