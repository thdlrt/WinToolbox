#!/usr/bin/env node
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';

const state = process.platform === 'win32'
  ? path.join(process.env.LOCALAPPDATA || path.join(os.homedir(), 'AppData', 'Local'), 'WinToolbox', 'agent')
  : path.join(process.env.XDG_STATE_HOME || path.join(os.homedir(), '.local', 'state'), 'wintoolbox-agent');
const configFile = process.env.WINTOOLBOX_MEMORY_CONFIG || path.join(state, 'client.json');
try {
  if (!fs.existsSync(configFile)) throw new Error('尚未配置工具箱项目记忆入口；请在工具箱中初始化，或通过 SSH 安装远端命令行。');
  const config = JSON.parse(fs.readFileSync(configFile, 'utf8').replace(/^\uFEFF/, ''));
  if (![config.python, config.core, config.store].every(v => typeof v === 'string' && v.length)) throw new Error('项目记忆客户端配置不完整');
  const windowsPython = /^[A-Za-z]:[\\/]/.test(config.python);
  const toWsl = v => '/mnt/' + v[0].toLowerCase() + '/' + v.slice(3).replaceAll('\\', '/');
  const toWindows = v => {
    const match = /^\/mnt\/([a-z])\/(.*)$/i.exec(v);
    if (match) return match[1].toUpperCase() + ':\\' + match[2].replaceAll('/', '\\');
    if (v.startsWith('/') && process.env.WSL_DISTRO_NAME) return '\\\\wsl.localhost\\' + process.env.WSL_DISTRO_NAME + v.replaceAll('/', '\\');
    return v;
  };
  const args = process.argv.slice(2).filter(v => v !== '--json');
  if (args[0] === 'resume') args[0] = 'read';
  // Compatibility for the old read/promote command spelling, with no old writer fallback.
  for (const command of ['read', 'promote', 'curate']) {
    if (args[0] === command && args.includes('--id')) args.splice(args.indexOf('--id'), 1);
  }
  if (windowsPython && process.platform !== 'win32') {
    for (let i = 0; i < args.length - 1; i++) {
      if (['--project', '--input', '--body-file', '--store', '--root'].includes(args[i]) && args[i + 1] !== '-') args[i + 1] = toWindows(args[i + 1]);
    }
  }
  const executable = windowsPython && process.platform !== 'win32' ? toWsl(config.python) : config.python;
  const code = 'import sys,runpy;sys.path.insert(0,sys.argv.pop(1));runpy.run_module("toolbox.project_memory",run_name="__main__")';
  const result = spawnSync(executable, ['-c', code, config.core, '--store', config.store, ...args], {
    stdio: 'inherit', windowsHide: true, env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' },
  });
  if (result.error) throw result.error;
  process.exit(result.status ?? 1);
} catch (error) {
  console.error(JSON.stringify({ error: String(error.message || error) }));
  process.exit(1);
}
