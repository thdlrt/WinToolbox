// stdout is a private JSON-lines protocol with the resident Python backend.
import { createRequire } from 'node:module';
import readline from 'node:readline';
import path from 'node:path';
import fs from 'node:fs';
import { pathToFileURL } from 'node:url';
const require = createRequire(path.join(process.argv[2], 'package.json'));
const { QQBot } = await import(pathToFileURL(path.join(process.argv[2], 'node_modules/@tencent-connect/qqbot-nodejs/dist/index.js')).href);
const { startQrConnect } = await import(pathToFileURL(path.join(process.argv[2], 'node_modules/@tencent-connect/qqbot-connector/dist/esm/index.js')).href);
const qr = require('qrcode');
const emit = value => process.stdout.write(JSON.stringify(value) + '\n');
let bot, dispose, generation = 0;
const targets = new Map();
function stop() { generation++; dispose?.(); dispose = undefined; bot?.stop(); bot = undefined; targets.clear(); }
async function command(c) {
  if (c.op === 'stop') { stop(); return { ok: true }; }
  if (c.op === 'bind') {
    stop(); const current = generation;
    dispose = startQrConnect({
      onQrDisplayed: async url => {
        try { await qr.toFile(c.path, url, { width: 360, margin: 2 });
          if (current === generation) emit({ event: 'qr', path: c.path, url });
        } catch { emit({ event: 'error', message: '二维码文件生成失败' }); }
      },
      onQrExpired: () => current === generation && emit({ event: 'expired' }),
      onSuccess: credentials => current === generation && emit({ event: 'credentials', credentials }),
      onFailure: () => current === generation && emit({ event: 'error', message: 'QQ 扫码连接失败，请重新生成二维码' }),
    }, { displayQrCodeToConsole: false, source: 'wintoolbox' });
    return { ok: true };
  }
  if (c.op === 'start') {
    stop(); const current = generation;
    bot = new QQBot({ appId: c.appId, appSecret: c.appSecret, transport: 'websocket' });
    bot.on('ready', () => current === generation && emit({ event: 'ready' }));
    bot.on('resumed', () => current === generation && emit({ event: 'ready' }));
    bot.on('disconnected', () => current === generation && emit({ event: 'disconnected' }));
    bot.on('error', () => current === generation && emit({ event: 'error', message: 'QQ 网关连接错误，请检查机器人权限与网络' }));
    bot.on('message', (_ctx, m) => {
      if (current !== generation || !['c2c', 'group'].includes(m.kind) || m.senderIsBot) return;
      targets.set(m.messageId, m.replyTarget);
      if (targets.size > 2000) targets.delete(targets.keys().next().value);
      emit({ event: 'message', message_id: m.messageId, sender_id: m.senderId,
        kind: m.kind, group_id: m.groupOpenid || '', text: m.content || '',
        mentioned: m.rawEventType === 'GROUP_AT_MESSAGE_CREATE',
        attachments: (m.attachments || []).map(a => ({ url: a.url, type: a.content_type, name: a.filename || '' })) });
    });
    bot.start().catch(() => current === generation && emit({ event: 'error', message: 'QQ 网关启动失败，请检查绑定凭据与接收权限' }));
    return { ok: true };
  }
  if (c.op === 'send') {
    const target = targets.get(c.message_id);
    if (!bot || !target) throw new Error('回复来源已过期或工具箱已重启；请从 QQ 重新发一条消息');
    const ids = [];
    if (c.text) ids.push((await bot.sendText(target, c.text)).id);
    for (const file of c.files || []) {
      if (!path.isAbsolute(file) || !fs.statSync(file).isFile()) throw new Error('附件必须为存在的绝对文件路径');
      const sent = /\.(png|jpe?g|gif|webp)$/i.test(file)
        ? await bot.sendImage(target, { localPath: file })
        : await bot.sendFile(target, { localPath: file }, { fileName: path.basename(file) });
      if (!sent.message?.id) throw new Error('QQ 未确认图片或文件消息发送成功');
      ids.push(sent.message.id);
    }
    if (ids.some(id => !id)) throw new Error('QQ 未返回消息 ID');
    return { ok: true, message_ids: ids };
  }
  throw new Error('未知 QQ 操作');
}
const input = readline.createInterface({ input: process.stdin });
input.on('line', line => {
  let c; try { c = JSON.parse(line); } catch { return; }
  command(c).then(result => emit({ id: c.id, result }), err => {
    // Do not leak SDK HTTP bodies, signed URLs or credentials.
    emit({ id: c.id, error: c.op === 'send' ? (String(err.message).startsWith('回复来源') ? err.message : 'QQ 发送未确认，请检查权限、文件与来源消息有效期，勿盲目重发') : 'QQ 操作失败' });
  });
});
input.on('close', () => { stop(); process.exit(0); });
