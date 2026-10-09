import { existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { spawn } from 'node:child_process';

const source = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');
const root = process.env.WINTOOLBOX_HOME || (existsSync(path.join(source, 'dist/WinToolbox-portable/python/python.exe')) ? path.join(source, 'dist/WinToolbox-portable') : source);
const portable = existsSync(path.join(root, 'python/python.exe'));
const python = path.join(root, portable ? 'python/python.exe' : 'core/.venv/Scripts/python.exe');
if (!existsSync(python)) throw new Error('找不到 WinToolbox Python；请设置 WINTOOLBOX_HOME 为工具箱源码或便携版目录。');
const tools = portable ? path.join(root, 'tools') : path.join(root, 'desktop/src-tauri/resources/tools');
const env = { ...process.env, PYTHONPATH: path.join(root, 'core'), PYTHONIOENCODING: 'utf-8',
  WINTOOLBOX_FFMPEG: path.join(tools, 'ffmpeg.exe'), WINTOOLBOX_NCMDUMP: path.join(tools, 'ncmdump/ncmdump.exe') };
const args = ['-m', 'toolbox.netease_cli', '--data-dir', process.env.WINTOOLBOX_DATA_DIR || path.join(portable ? root : path.join(root, 'dist/WinToolbox-portable'), 'data'), ...process.argv.slice(2)];
const child = spawn(python, args, { env, windowsHide: true, stdio: 'inherit', shell: false });
child.on('error', error => { process.stderr.write(error.message + '\n'); process.exitCode = 1; });
child.on('exit', code => { process.exitCode = code ?? 1; });
