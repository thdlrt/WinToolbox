import { build } from 'esbuild';
import { mkdtemp, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { basename, dirname, join, resolve } from 'node:path';
import { createRequire } from 'node:module';

const directory = await mkdtemp(join(tmpdir(), 'wintoolbox-relay-progress-'));
try {
  const result = await build({ stdin: { contents: `
    import React from 'react';
    import { renderToStaticMarkup } from 'react-dom/server';
    import { strict as assert } from 'node:assert';
    import Progress from './src/RelayDownloadProgress';
    const job = {id:'test',tool:'relay.download',status:'running',progress:25,params:{},artifacts:[],created_at:'test',transfer:{received_bytes:1024**2,total_bytes:4*1024**2,bytes_per_second:2*1024**2,file_name:'中文.bin',file_index:1,file_count:2,phase:'downloading'}};
    const render = value => renderToStaticMarkup(<Progress job={value} onCancel={()=>{}}/>);
    const html = render(job);
    assert(html.includes('下载中') && html.includes('25.0%'));
    assert(html.includes('1.0 MB / 4.0 MB') && html.includes('2.0 MB/s'));
    assert(html.includes('文件 1 / 2') && html.includes('中文.bin'));
    assert(html.includes('value="25"') && html.includes('取消'));
    const validating = render({...job,progress:99,transfer:{...job.transfer,phase:'verifying'}});
    assert(validating.includes('校验并保存中') && validating.includes('0 B/s'));
    const complete = render({...job,status:'completed',progress:100});
    assert(complete.includes('100.0%') && !complete.includes('MB/s') && !complete.includes('取消'));
    const failed = render({...job,status:'failed',message:'下载不完整'});
    assert(failed.includes('失败') && failed.includes('下载不完整') && !failed.includes('取消'));
    const stale = render({...job,updated_at:'2000-01-01T00:00:00Z'});
    assert(stale.includes('0 B/s'));
    const pending = render({...job,status:'queued',progress:0,transfer:undefined});
    assert(pending.includes('等待中') && pending.includes('value="0"'));
    console.log('Relay download progress: 9 rendering checks passed.');
  `, resolveDir: resolve('.'), loader: 'tsx' }, bundle: true, platform: 'node', format: 'cjs', jsx: 'automatic', write: false });
  const path = join(directory, 'check.cjs');
  await writeFile(path, result.outputFiles[0].text);
  createRequire(import.meta.url)(path);
} finally {
  if (dirname(resolve(directory)) !== resolve(tmpdir()) || !basename(directory).startsWith('wintoolbox-relay-progress-')) throw new Error('Unexpected test temporary directory');
  await rm(directory, { recursive: true, force: true });
}
