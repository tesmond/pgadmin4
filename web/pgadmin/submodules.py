import importlib
from concurrent.futures import ThreadPoolExecutor, as_completed

_SUBMODULE_IMPORTS = [
    ".about",
    ".authenticate",
    ".browser",
    ".dashboard",
    ".help",
    ".llm",
    ".misc",
    ".preferences",
    ".redirects",
    ".settings",
    ".tools",
]

_SUBMODULE_CACHE = {}


def _load_blueprint(module_name):
    if module_name in _SUBMODULE_CACHE:
        return _SUBMODULE_CACHE[module_name]

    module = importlib.import_module(module_name, package=__package__)
    blueprint = getattr(module, "blueprint")
    _SUBMODULE_CACHE[module_name] = blueprint
    return blueprint


def preload_all_submodules():
    for module_name in _SUBMODULE_IMPORTS:
        _load_blueprint(module_name)


def get_submodules(parallel=False, max_workers=4):
    if not parallel:
        return [_load_blueprint(module_name) for module_name in _SUBMODULE_IMPORTS]

    blueprints = [None] * len(_SUBMODULE_IMPORTS)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_load_blueprint, module_name): idx
            for idx, module_name in enumerate(_SUBMODULE_IMPORTS)
        }

        for future in as_completed(futures):
            idx = futures[future]
            blueprints[idx] = future.result()

    return blueprints
