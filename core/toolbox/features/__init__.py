"""Built-in tools share the public registration interface used by extensions."""


def register_all(app):
    from . import backups, codex, files, knowledge, plugins, practice, webdav, expenses, phonetics, shizuku, fnconnect
    for module in (codex, files, plugins, knowledge, backups, practice, webdav, expenses, phonetics, shizuku, fnconnect):
        module.register(app)
