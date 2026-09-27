import { build } from 'esbuild';
import { mkdtemp, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { basename, dirname, join, resolve } from 'node:path';
import { createRequire } from 'node:module';

const directory = await mkdtemp(join(tmpdir(), 'wintoolbox-expense-export-'));
try {
  const result = await build({ stdin: { contents: `
    import React from 'react';
    import { renderToStaticMarkup } from 'react-dom/server';
    import { strict as assert } from 'node:assert';
    import ExpensesPage from './src/pages/Expenses';
    import { AppContext } from './src/context';
    const job=(id,tool,status,extra={})=>({id,tool,status,params:{},created_at:id,progress:35,artifacts:[],...extra});
    const old=[job('2026-09-01','expenses.export','failed',{error:'OLD_EXPORT_FAILURE'}),job('2026-09-30','expenses.sync','failed',{error:'SYNC_HISTORY_FAILURE'})];
    const render=(current)=>renderToStaticMarkup(<AppContext.Provider value={{jobs:[...old,...(current?[current]:[])],connected:true}}><ExpensesPage/></AppContext.Provider>);
    const success=render(job('2026-09-27','expenses.export','completed',{artifacts:[{path:'fixture.xlsx'}],result:{rows:3}}));
    assert(success.includes('打开表格'));
    assert(success.includes('3 条'));
    assert(!success.includes('OLD_EXPORT_FAILURE'));
    assert(!success.includes('SYNC_HISTORY_FAILURE'));
    assert(!success.includes('同步与导出任务'));
    const running=render(job('2026-09-27','expenses.export','running',{message:'CURRENT_EXPORT_PROGRESS'}));
    assert(running.includes('CURRENT_EXPORT_PROGRESS'));
    assert(running.includes('width:35%'));
    assert(!running.includes('打开表格'));
    const failed=render(job('2026-09-27','expenses.export','failed',{error:'CURRENT_EXPORT_FAILURE'}));
    assert(failed.includes('CURRENT_EXPORT_FAILURE'));
    assert(!failed.includes('打开表格'));
    console.log('Expense export UI: latest export only; progress, failure, result and no sync history passed.');
  `, resolveDir: resolve('.'), loader: 'tsx' }, bundle: true, platform: 'node', format: 'cjs', jsx: 'automatic', write: false, loader: { '.css': 'empty' } });
  const path = join(directory, 'check.cjs');
  await writeFile(path, result.outputFiles[0].text);
  createRequire(import.meta.url)(path);
} finally {
  if (dirname(resolve(directory)) !== resolve(tmpdir()) || !basename(directory).startsWith('wintoolbox-expense-export-')) throw new Error('Unexpected test temporary directory');
  await rm(directory, { recursive: true, force: true });
}
