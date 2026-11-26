"""Refresh the local Codex MCP registration without restarting its processes."""
import os
from pathlib import Path
import sys
import tempfile

import tomlkit

from afxx import common


def upgrade_mcp():
    from importlib.util import find_spec
    common.require_code(find_spec('mcp') is not None, 'MCP_DEPENDENCY_MISSING',
                        "Install afxx[mcp] in the current Python environment first")
    home = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))).expanduser()
    home.mkdir(parents=True, exist_ok=True)
    path = home / 'config.toml'
    with common.lock(home / '.afxx-mcp-upgrade.lock'):
        original = path.read_text() if path.exists() else ''
        document = tomlkit.parse(original)
        servers = document.setdefault('mcp_servers', tomlkit.table())
        server = servers.setdefault('afxx', tomlkit.table())
        common.require_code('url' not in server, 'MCP_CONFIG_CONFLICT',
                            'afxx is registered as an HTTP server; refusing to replace it with stdio')
        args = list(server.get('args', []))
        extra = args[args.index('mcp') + 1:] if 'mcp' in args else []
        common.require_code(not args or 'mcp' in args, 'MCP_CONFIG_CONFLICT',
                            'existing afxx arguments do not identify an MCP server')
        # Do not resolve the interpreter symlink: its venv selects the installed package.
        server['command'] = os.path.abspath(sys.executable)
        server['args'] = ['-m', 'afxx.cli', 'mcp', *extra]
        updated = tomlkit.dumps(document)
        backup = None
        if updated != original:
            if path.exists():
                with tempfile.NamedTemporaryFile(prefix='config.toml.afxx-', suffix='.bak', dir=home, delete=False) as saved:
                    backup = saved.name
                    saved.write(original.encode())
            with tempfile.NamedTemporaryFile(prefix='.afxx-mcp-', dir=home, delete=False) as staged:
                temporary = Path(staged.name)
                staged.write(updated.encode())
            try:
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
        return {'agent': 'codex', 'config': str(path), 'command': server['command'],
                'args': list(server['args']), 'changed': updated != original, 'backup': backup,
                'reconnect_required': True,
                'message': 'Reconnect the AFXX MCP server or start a new client session to load the current package.'}
