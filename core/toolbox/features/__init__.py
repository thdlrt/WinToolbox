"""Built-in tools share the public registration interface used by extensions."""


def register_all(app):
    from . import orb_settings
    orb_settings.register(app)
    from . import system_memory
    system_memory.register(app)
    from . import relay
    relay.register(app)
    from . import filesync
    filesync.register(app)
    from . import backups, codex, files, knowledge, plugins, practice, webdav, expenses, phonetics, shizuku, fnconnect, gpu_guard, project_memory, general
    for module in (codex, files, plugins, knowledge, backups, practice, webdav, expenses, phonetics, shizuku, fnconnect, gpu_guard, project_memory, general):
        module.register(app)

    from .. import project_memory_curation, project_memory_export, project_memory_principles
    project_memory_curation.register(app, app.project_memory)
    project_memory_export.register(app, app.project_memory)
    project_memory_principles.register(app, app.project_memory)
