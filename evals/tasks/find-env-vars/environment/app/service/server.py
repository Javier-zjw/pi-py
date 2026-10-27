from os import environ

from service import config


def start():
    settings = config.load()
    port = int(environ.get("PORT", "8080"))
    return settings, port
