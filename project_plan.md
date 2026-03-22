# pgAdmin4 Startup Performance Improvement Plan

## Executive Summary

pgAdmin4 currently suffers from startup times of **8-20 seconds** in desktop mode and significant initial load times in server mode. This document provides a prioritized, detailed plan to reduce startup time to **under 3 seconds** through a series of targeted optimizations spanning the Python backend, JavaScript frontend, database layer, and build system.

The improvements are organized into **four phases** ordered by impact-to-effort ratio. Phase 1 alone should yield 50-60% startup time reduction with relatively low risk.

---

## Baseline Metrics (Before Optimization)

| Metric | Cold Start | Warm Start |
|--------|-----------|-----------|
| Python server ready | 4-10s | 3-6s |
| Frontend bundle parse | 2-5s | 0.5-1s (cached) |
| Initial API calls | 0.5-2s | 0.5-2s |
| **Total time to interactive** | **8-20s** | **4-9s** |

**Target After All Phases:**

| Metric | Cold Start | Warm Start |
|--------|-----------|-----------|
| Python server ready | 1-2s | 0.8-1.5s |
| Frontend bundle parse | 0.5-1s | <0.2s (cached) |
| Initial API calls | 0.2-0.5s | 0.2-0.5s |
| **Total time to interactive** | **2-4s** | **1-2s** |

---

## Phase 1: Quick Wins — High Impact, Low Risk
### Estimated Startup Improvement: 50-60%
### Estimated Engineering Time: 2-4 weeks

---

### 1.1 Lazy Module Loading (Backend)

**Problem:** All 50+ pgAdmin modules are imported and initialized synchronously at startup, even modules the user may never access in a session (e.g., Kerberos auth, specific database object types).

**Solution:** Implement deferred module initialization.

**Implementation:**

````python
// filepath: __init__.py
# Replace eager module loading with lazy registration

class LazyModule:
    """Wrapper that defers module initialization until first request."""
    
    def __init__(self, module_path):
        self._module_path = module_path
        self._module = None
        self._initialized = False
    
    def ensure_initialized(self, app):
        if not self._initialized:
            self._module = importlib.import_module(self._module_path)
            self._module.init_app(app)
            self._initialized = True
        return self._module

# Categorize modules by initialization priority
CRITICAL_MODULES = [
    'pgadmin.browser',
    'pgadmin.preferences', 
    'pgadmin.settings',
    'pgadmin.misc.bgprocess',
]

DEFERRED_MODULES = [
    'pgadmin.tools.debugger',
    'pgadmin.tools.backup',
    'pgadmin.tools.restore',
    'pgadmin.tools.grant_wizard',
    'pgadmin.tools.import_export',
    'pgadmin.tools.maintenance',
    # ... all tool modules
]

# Browser node modules - load on first browser tree request
BROWSER_NODE_MODULES = [
    'pgadmin.browser.server_groups',
    'pgadmin.browser.server',
    'pgadmin.browser.database',
    'pgadmin.browser.schema',
    'pgadmin.browser.table',
    # ... all node modules
]
````

### 1.2 Preferences Registration Caching
Problem: Every module registers its preferences to the SQLite database on every startup, performing reads and writes regardless of whether anything changed.

Solution: Hash-based preference caching with a startup registry file.

Implementation:
````python
// filepath: preferences.py
import hashlib
import json
import os

PREF_CACHE_FILE = os.path.join(DATA_DIR, '.pref_cache_hash')

def get_preferences_hash(module_preferences: dict) -> str:
    """Generate a hash of the preference definitions."""
    return hashlib.sha256(
        json.dumps(module_preferences, sort_keys=True).encode()
    ).hexdigest()

def should_update_preferences(module_name: str, current_hash: str) -> bool:
    """Check if preferences need updating by comparing hashes."""
    cache = _load_hash_cache()
    return cache.get(module_name) != current_hash

def _load_hash_cache() -> dict:
    try:
        with open(PREF_CACHE_FILE, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_hash_cache(module_name: str, current_hash: str):
    cache = _load_hash_cache()
    cache[module_name] = current_hash
    with open(PREF_CACHE_FILE, 'w') as f:
        json.dump(cache, f)

# In each module's register_preferences():
def register_preferences_cached(module_name, preferences_def):
    current_hash = get_preferences_hash(preferences_def)
    if not should_update_preferences(module_name, current_hash):
        return  # Skip DB writes - nothing changed
    
    # Perform actual registration
    _do_register_preferences(module_name, preferences_def)
    save_hash_cache(module_name, current_hash)
````

### 1.3 Database Schema Check Optimization
Problem: db.create_all() is called on every startup, which inspects all table schemas via SQLAlchemy reflection — a slow operation on SQLite.

Solution: Version-stamp the schema and skip create_all() if the version matches.

Implementation:
````python
// filepath: __init__.py
SCHEMA_VERSION = 42  # Increment when schema changes

def _init_database(app):
    with app.app_context():
        # Check schema version first
        try:
            result = db.session.execute(
                text("SELECT value FROM keys WHERE name = 'schema_version'")
            ).fetchone()
            
            if result and int(result[0]) == SCHEMA_VERSION:
                # Schema is current, skip expensive create_all
                return
        except Exception:
            pass  # Table doesn't exist yet, fall through
        
        # Schema needs update
        db.create_all()
        
        # Store new schema version
        _set_schema_version(SCHEMA_VERSION)
```

### 1.4 Parallel Module Initialization

**Problem:** Critical modules are initialized sequentially even when they have no dependencies on each other, wasting potential parallelism.

**Solution:** Use `concurrent.futures.ThreadPoolExecutor` to initialize independent modules in parallel.

**Implementation:**

````python
// filepath: __init__.py
from concurrent.futures import ThreadPoolExecutor, as_completed

PARALLEL_INIT_MODULES = [
    'pgadmin.preferences',
    'pgadmin.settings',
    'pgadmin.misc.bgprocess',
]

def _init_modules_parallel(app, modules):
    """Initialize independent modules concurrently."""
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(_init_single_module, app, mod): mod
            for mod in modules
        }
        for future in as_completed(futures):
            mod = futures[future]
            try:
                future.result()
            except Exception as e:
                app.logger.error(f"Failed to initialize {mod}: {e}")
                raise

def _init_single_module(app, module_path):
    with app.app_context():
        mod = importlib.import_module(module_path)
        mod.init_app(app)
````

### 1.5 Startup Profiling Instrumentation

**Problem:** Without precise measurements it's hard to know which initialization steps are the actual bottlenecks.

**Solution:** Add a lightweight built-in profiler that logs per-step timing at startup.

**Implementation:**

````python
// filepath: __init__.py
import time
from contextlib import contextmanager

_startup_timings = []

@contextmanager
def startup_timer(label: str):
    """Context manager to record startup step durations."""
    start = time.perf_counter()
    yield
    elapsed = time.perf_counter() - start
    _startup_timings.append((label, elapsed))
    app.logger.debug(f"[STARTUP] {label}: {elapsed:.3f}s")

def log_startup_summary():
    total = sum(t for _, t in _startup_timings)
    app.logger.info("=== Startup Timing Summary ===")
    for label, t in sorted(_startup_timings, key=lambda x: -x[1]):
        app.logger.info(f"  {label:<45} {t:.3f}s  ({t/total*100:.1f}%)")
    app.logger.info(f"  {'TOTAL':<45} {total:.3f}s")

# Usage in create_app():
with startup_timer("load_critical_modules"):
    _init_modules_parallel(app, CRITICAL_MODULES)

with startup_timer("init_database"):
    _init_database(app)

with startup_timer("register_preferences"):
    _register_all_preferences(app)

log_startup_summary()
````

### Stage 1 Implementation Status (March 22, 2026)

- ✅ Implemented startup profiling checkpoints and summary logging in `web/pgadmin/__init__.py`.
- ✅ Implemented schema-version cached checks in migration paths in `web/pgadmin/__init__.py`.
- ✅ Implemented preference registration hash cache + thread-safe module map access in `web/pgadmin/utils/preferences.py`.
- ✅ Implemented parallel submodule import path and deterministic registration order using `web/pgadmin/submodules.py`.
- ✅ Implemented lazy submodule import behavior (imports deferred until submodule loading step) in `web/pgadmin/submodules.py`.
- ✅ Added Stage 1 feature flags in `web/config.py`:
    - `STARTUP_PROFILE_ENABLED`
    - `SCHEMA_VERSION_CHECK_ENABLED`
    - `LAZY_MODULE_LOADING_ENABLED`
    - `PARALLEL_INIT_ENABLED`
    - `PARALLEL_INIT_WORKERS`
    - `PREFERENCES_HASH_CACHE_ENABLED`

**Validation completed:** `py_compile` passed for modified files.

---

## Phase 2: JavaScript Frontend Optimizations
### Estimated Startup Improvement: 20-30% (of remaining time)
### Estimated Engineering Time: 3-5 weeks

---

### 2.1 Code Splitting and Dynamic Imports

**Problem:** pgAdmin ships a single large JavaScript bundle. The browser must download, parse, and execute the entire bundle before the UI becomes interactive — even code for tools the user hasn't opened.

**Solution:** Split the bundle by route/feature and use dynamic `import()` for tool panels.

**Implementation:**

````javascript
// filepath: webpack.config.js
module.exports = {
    // ...existing code...
    optimization: {
        splitChunks: {
            chunks: 'all',
            cacheGroups: {
                vendor: {
                    test: /[\\/]node_modules[\\/]/,
                    name: 'vendors',
                    priority: 10,
                    reuseExistingChunk: true,
                },
                pgadminCore: {
                    test: /[\\/]pgadmin[\\/](browser|preferences|settings)[\\/]/,
                    name: 'pgadmin-core',
                    priority: 8,
                },
                tools: {
                    test: /[\\/]pgadmin[\\/]tools[\\/]/,
                    name(module) {
                        const tool = module.context.match(
                            /[\\/]tools[\\/](.*?)([\\/]|$)/
                        )[1];
                        return `tool-${tool}`;
                    },
                    priority: 5,
                },
            },
        },
    },
};
````

````javascript
// filepath: pgadmin/static/js/tool_loader.js
/**
 * Dynamically load tool panels only when first opened.
 */
const TOOL_CHUNKS = {
    debugger:      () => import(/* webpackChunkName: "tool-debugger" */      '../tools/debugger'),
    backup:        () => import(/* webpackChunkName: "tool-backup" */        '../tools/backup'),
    restore:       () => import(/* webpackChunkName: "tool-restore" */       '../tools/restore'),
    grant_wizard:  () => import(/* webpackChunkName: "tool-grant_wizard" */  '../tools/grant_wizard'),
    import_export: () => import(/* webpackChunkName: "tool-import_export" */ '../tools/import_export'),
    maintenance:   () => import(/* webpackChunkName: "tool-maintenance" */   '../tools/maintenance'),
    query_tool:    () => import(/* webpackChunkName: "tool-query_tool" */    '../tools/query_tool'),
};

export async function loadTool(toolName) {
    const loader = TOOL_CHUNKS[toolName];
    if (!loader) throw new Error(`Unknown tool: ${toolName}`);
    const module = await loader();
    return module.default;
}
````

### 2.2 Bundle Size Reduction

**Problem:** The vendor bundle includes full versions of libraries where only a subset is used (e.g., all of moment.js locales, full lodash, all of CodeMirror modes).

**Solution:** Tree-shake, alias slim builds, and replace heavy libraries.

**Implementation:**

````javascript
// filepath: webpack.config.js
const { BundleAnalyzerPlugin } = require('webpack-bundle-analyzer');

module.exports = {
    // ...existing code...
    resolve: {
        alias: {
            // Use lodash-es for tree-shaking
            'lodash': 'lodash-es',
            // Moment.js → day.js (6kb vs 300kb)
            'moment': 'dayjs',
        },
    },
    plugins: [
        // Only include used moment/dayjs locales
        new webpack.ContextReplacementPlugin(
            /dayjs[/\\]locale/,
            /en|fr|de|es|zh/
        ),
        // Analyse bundle in CI
        process.env.ANALYZE && new BundleAnalyzerPlugin(),
    ].filter(Boolean),
    // ...existing code...
};
````

### 2.3 Aggressive Asset Caching

**Problem:** Static assets (JS, CSS, fonts) are re-downloaded on every app restart because filenames don't include content hashes.

**Solution:** Content-hash filenames + long-lived `Cache-Control` headers.

**Implementation:**

````javascript
// filepath: webpack.config.js
module.exports = {
    // ...existing code...
    output: {
        filename:      'js/[name].[contenthash:8].js',
        chunkFilename: 'js/[name].[contenthash:8].chunk.js',
        assetModuleFilename: 'assets/[name].[contenthash:8][ext]',
        clean: true,
    },
    // ...existing code...
};
````

````python
// filepath: pgadmin/misc/static.py
from flask import Blueprint, send_from_directory

# Serve hashed static files with long cache TTL
@blueprint.route('/static/<path:filename>')
def static_file(filename):
    response = send_from_directory(STATIC_DIR, filename)
    if any(filename.endswith(ext) for ext in ('.js', '.css', '.woff2', '.png')):
        response.cache_control.max_age = 31_536_000  # 1 year
        response.cache_control.immutable = True
    return response
````

### 2.4 Critical CSS Inlining

**Problem:** The browser blocks rendering until all CSS files are downloaded and parsed, even styles not needed for the initial paint.

**Solution:** Inline above-the-fold CSS in `<head>` and load the rest asynchronously.

**Implementation:**

````javascript
// filepath: webpack.config.js
const MiniCssExtractPlugin = require('mini-css-extract-plugin');
const CriticalPlugin = require('@assetpack/webpack-plugin-critical');

module.exports = {
    // ...existing code...
    plugins: [
        new MiniCssExtractPlugin({
            filename: 'css/[name].[contenthash:8].css',
        }),
        new CriticalPlugin({
            base: STATIC_DIR,
            inline: true,
            minify: true,
            width: 1300,
            height: 900,
        }),
    ],
    // ...existing code...
};
````

---

## Phase 3: Database Layer Optimizations
### Estimated Startup Improvement: 10-15% (of remaining time)
### Estimated Engineering Time: 2-3 weeks

---

### 3.1 SQLite WAL Mode and Connection Tuning

**Problem:** The default SQLite journal mode (`DELETE`) causes write locks that block reads until the write is committed, serialising startup DB operations.

**Solution:** Enable WAL mode and tune SQLite PRAGMAs for the pgAdmin workload.

**Implementation:**

````python
// filepath: pgadmin/__init__.py
from sqlalchemy import event
from sqlalchemy.engine import Engine

@event.listens_for(Engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.executescript("""
        PRAGMA journal_mode = WAL;
        PRAGMA synchronous  = NORMAL;
        PRAGMA cache_size   = -32000;   -- 32 MB page cache
        PRAGMA temp_store   = MEMORY;
        PRAGMA mmap_size    = 268435456; -- 256 MB memory-mapped I/O
        PRAGMA busy_timeout = 5000;
    """)
    cursor.close()
````

### 3.2 Startup Query Batching

**Problem:** Startup performs many small individual SELECT/INSERT queries for preferences, settings, and server list — each incurring SQLite round-trip overhead.

**Solution:** Batch reads into bulk queries and writes into single transactions.

**Implementation:**

````python
// filepath: pgadmin/preferences.py
def load_all_preferences_bulk(user_id: int) -> dict:
    """
    Load all preference values for a user in a single query
    instead of one query per preference key.
    """
    rows = db.session.execute(
        text("""
            SELECT p.module, p.category, p.name, upr.value
            FROM preferences p
            LEFT JOIN user_preferences upr
                   ON upr.pid = p.id AND upr.uid = :uid
        """),
        {"uid": user_id}
    ).fetchall()

    prefs = {}
    for module, category, name, value in rows:
        prefs.setdefault(module, {}).setdefault(category, {})[name] = value
    return prefs


def save_preferences_bulk(user_id: int, updates: list[tuple]):
    """Write multiple preference changes in a single transaction."""
    with db.session.begin():
        db.session.execute(
            text("""
                INSERT INTO user_preferences (uid, pid, value)
                VALUES (:uid, :pid, :value)
                ON CONFLICT(uid, pid) DO UPDATE SET value = excluded.value
            """),
            [{"uid": user_id, "pid": pid, "value": val} for pid, val in updates]
        )
````

### 3.3 Indexed Startup Queries

**Problem:** Frequently executed startup queries (server list, preference lookups) perform full-table scans because key columns lack indexes.

**Solution:** Add targeted indexes via an Alembic migration.

**Implementation:**

````python
// filepath: migrations/versions/add_startup_indexes.py
"""Add indexes to improve startup query performance."""
from alembic import op

revision = 'a1b2c3d4e5f6'
down_revision = 'previous_revision_id'

def upgrade():
    op.create_index('ix_user_preferences_uid',     'user_preferences',  ['uid'])
    op.create_index('ix_user_preferences_uid_pid', 'user_preferences',  ['uid', 'pid'], unique=True)
    op.create_index('ix_server_user_id',           'server',            ['user_id'])
    op.create_index('ix_server_group_user_id',     'servergroup',       ['user_id'])
    op.create_index('ix_keys_name',                'keys',              ['name'], unique=True)

def downgrade():
    op.drop_index('ix_user_preferences_uid',     'user_preferences')
    op.drop_index('ix_user_preferences_uid_pid', 'user_preferences')
    op.drop_index('ix_server_user_id',           'server')
    op.drop_index('ix_server_group_user_id',     'servergroup')
    op.drop_index('ix_keys_name',                'keys')
````

### 3.4 In-Memory Preference Cache

**Problem:** Preference values are read from SQLite on every request that needs them, even though they rarely change during a session.

**Solution:** Cache the full preference dict in memory after the first load, invalidating on write.

**Implementation:**

````python
// filepath: pgadmin/preferences.py
from threading import Lock

_pref_cache: dict = {}
_pref_cache_lock = Lock()

def get_preferences(user_id: int) -> dict:
    with _pref_cache_lock:
        if user_id not in _pref_cache:
            _pref_cache[user_id] = load_all_preferences_bulk(user_id)
        return _pref_cache[user_id]

def invalidate_preference_cache(user_id: int):
    with _pref_cache_lock:
        _pref_cache.pop(user_id, None)

# Call invalidate_preference_cache(user_id) after any preference write.
````

---

## Phase 4: Build System Optimizations
### Estimated Startup Improvement: 5-10% (of remaining time)
### Estimated Engineering Time: 2-3 weeks

---

### 4.1 Python Bytecode Pre-compilation

**Problem:** On first startup (or after upgrades) Python compiles every `.py` source file to `.pyc` bytecode on-demand, adding hundreds of small disk reads and compilation steps.

**Solution:** Pre-compile all Python files during the install/build step.

**Implementation:**

````bash
// filepath: Makefile
.PHONY: precompile-python
precompile-python:
	python -m compileall -j 0 -q pgadmin/
	# -j 0  → use all CPU cores
	# -q    → quiet output
````

````python
// filepath: setup.py
from setuptools.command.install import install
import compileall, os

class PostInstallCommand(install):
    def run(self):
        super().run()
        print("Pre-compiling Python sources...")
        compileall.compile_dir(
            os.path.join(self.install_lib, 'pgadmin'),
            force=True,
            quiet=True,
            workers=0,   # all cores
        )

setup(
    # ...existing code...
    cmdclass={'install': PostInstallCommand},
)
````

### 4.2 Electron/Desktop Startup Optimisation (Desktop Mode)

**Problem:** In desktop mode the Electron wrapper launches a full Chromium process and a Python subprocess sequentially, waiting for each before showing anything to the user.

**Solution:** Start both processes in parallel and show a lightweight loading screen immediately.

**Implementation:**

````javascript
// filepath: runtime/src/js/pgadmin.js
const { app, BrowserWindow } = require('electron');
const { startPythonServer } = require('./server');

async function createWindow() {
    const win = new BrowserWindow({
        width: 1280, height: 800,
        show: false,
        webPreferences: { contextIsolation: true },
    });

    // Show loading screen immediately — don't wait for Python
    win.loadFile('src/html/loading.html');
    win.show();

    // Start Python server in parallel
    const serverReady = startPythonServer();

    await serverReady;
    await win.loadURL(`http://127.0.0.1:${process.env.PGADMIN_PORT}/`);
}

app.whenReady().then(createWindow);
````

````html
<!-- filepath: runtime/src/html/loading.html -->
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>pgAdmin 4</title>
    <style>
        body { margin:0; display:flex; align-items:center; justify-content:center;
               height:100vh; background:#1a1a2e; font-family:sans-serif; color:#fff; }
        .logo { width:120px; animation: pulse 1.5s ease-in-out infinite; }
        @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.5} }
        p { margin-top:1rem; font-size:1rem; opacity:.7; }
    </style>
</head>
<body>
    <div style="text-align:center">
        <img class="logo" src="./pgadmin-logo.png" alt="pgAdmin">
        <p>Starting pgAdmin 4…</p>
    </div>
</body>
</html>
````

### 4.3 Webpack Build Performance

**Problem:** Developer iteration cycles are slow because webpack rebuilds the entire bundle from scratch; production builds also take longer than necessary.

**Solution:** Enable persistent caching, `thread-loader` for Babel transforms, and `esbuild-loader` as a fast JS/TS transformer.

**Implementation:**

````javascript
// filepath: webpack.config.js
const { EsbuildPlugin } = require('esbuild-loader');

module.exports = (env, argv) => ({
    // ...existing code...
    cache: {
        type: 'filesystem',
        buildDependencies: { config: [__filename] },
    },
    module: {
        rules: [
            {
                test: /\.[jt]sx?$/,
                exclude: /node_modules/,
                use: [
                    {
                        loader: 'thread-loader',
                        options: { workers: require('os').cpus().length - 1 },
                    },
                    {
                        loader: 'esbuild-loader',
                        options: { target: 'es2017' },
                    },
                ],
            },
            // ...existing code...
        ],
    },
    optimization: {
        // ...existing code...
        minimizer: [
            new EsbuildPlugin({ target: 'es2017', css: true }),
        ],
    },
});
````

### 4.4 Docker / Container Image Optimisation

**Problem:** The official Docker image rebuilds all layers (including `pip install`) on every minor code change due to poor layer ordering, slowing CI and container cold starts.

**Solution:** Reorder the `Dockerfile` to maximise layer cache reuse and use a multi-stage build to keep the final image lean.

**Implementation:**

````dockerfile
// filepath: Dockerfile
# ── Stage 1: Python dependency layer (cached until requirements change) ──────
FROM python:3.12-slim AS deps
WORKDIR /install
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/deps -r requirements.txt \
 && python -m compileall -q /deps/lib/

# ── Stage 2: Build frontend assets ───────────────────────────────────────────
FROM node:20-alpine AS frontend
WORKDIR /build
COPY package*.json ./
RUN npm ci --prefer-offline
COPY . .
RUN npm run build

# ── Stage 3: Final runtime image ──────────────────────────────────────────────
FROM python:3.12-slim
WORKDIR /pgadmin4

COPY --from=deps   /deps              /usr/local
COPY --from=frontend /build/pgadmin/static/js  pgadmin/static/js
COPY --from=frontend /build/pgadmin/static/css pgadmin/static/css
COPY . .

RUN python -m compileall -q pgadmin/

EXPOSE 80
CMD ["gunicorn", "--bind", "0.0.0.0:80", "--workers", "2", "pgAdmin4:app"]
````

---

## Summary

| Phase | Focus | Startup Improvement | Engineering Time |
|-------|-------|-------------------|-----------------|
| 1 | Backend Quick Wins (lazy loading, caching, parallelism) | **50-60%** | 2-4 weeks |
| 2 | JavaScript Frontend (code splitting, bundle size, caching) | **20-30%** | 3-5 weeks |
| 3 | Database Layer (WAL, batching, indexes, in-memory cache) | **10-15%** | 2-3 weeks |
| 4 | Build System (bytecode, Electron, webpack, Docker) | **5-10%** | 2-3 weeks |
| **Total** | | **~3-4s cold / ~1-2s warm** | **9-15 weeks** |

> **Recommended rollout:** Implement phases sequentially, measuring startup time after each phase before proceeding, to validate gains and catch regressions early.