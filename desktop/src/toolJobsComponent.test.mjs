import { build } from 'esbuild';
import { mkdtemp, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { basename, dirname, join, resolve } from 'node:path';
import { createRequire } from 'node:module';

const directory = await mkdtemp(join(tmpdir(), 'wintoolbox-tool-jobs-'));
try {
  const result = await build({ stdin: { contents: `
    import React from 'react';
    import { renderToStaticMarkup } from 'react-dom/server';
    import { strict as assert } from 'node:assert';
    import ToolJobs from './src/ToolJobs';
    import { AppContext } from './src/context';
    const record = (id, tool, status, extra={}) => ({id,tool,status,params:{},created_at:id,progress:35,message:'processing',artifacts:[],...extra});
    const jobs=[record('2026-09-10','media','running'),record('2026-09-09','media','failed',{error:'Fixture failure'}),record('2026-09-08','media','completed',{artifacts:[{path:'D:/fixture/out.srt',label:'字幕',kind:'subtitles'}]}),record('2026-09-07','media','completed'),record('2026-09-11','plugin','running')];
    const html=renderToStaticMarkup(<AppContext.Provider value={{jobs,connected:true}}><ToolJobs scope="media" title="处理结果"/></AppContext.Provider>);
    assert(html.includes('取消处理'));
    assert(html.includes('重试'));
    assert(html.includes('Fixture failure'));
    assert(html.includes('编辑字幕'));
    assert(html.includes('打开 字幕'));
    assert(html.includes('更早记录'));
    assert(!html.includes('扩展工具'));
    assert(!html.includes('任务中心'));
    assert(html.includes('width:35%'));
    const empty=renderToStaticMarkup(<AppContext.Provider value={{jobs:[],connected:true}}><ToolJobs scope="media" title="处理结果"/></AppContext.Provider>);
    assert.equal(empty,'');
    console.log('Tool-local component: 10 rendering assertions passed.');
  `, resolveDir: resolve('.'), loader: 'tsx' }, bundle: true, platform: 'node', format: 'cjs', jsx: 'automatic', write: false, loader: { '.css': 'empty' } });
  const path = join(directory, 'check.cjs');
  await writeFile(path, result.outputFiles[0].text);
  createRequire(import.meta.url)(path);
} finally {
  if (dirname(resolve(directory)) !== resolve(tmpdir()) || !basename(directory).startsWith('wintoolbox-tool-jobs-')) throw new Error('Unexpected test temporary directory');
  await rm(directory, { recursive: true, force: true });
}
