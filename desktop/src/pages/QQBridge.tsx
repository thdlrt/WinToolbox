import { useEffect, useRef, useState } from 'react';
import { native, rpc } from '../api';
import { useApp } from '../context';
import { Button, CheckField, Field, Notice, Section } from '../ui';
interface Config { enabled: boolean; thread_id: string; groups: string[]; launch_codex: boolean }
interface Status { config: Config; bound: boolean; app_id: string; owner_id: string; connected: boolean;
  binding: boolean; last_error: string; qr_path: string; pair_code: string;
  recent: { id: string; status: string; detail: string }[] }
export default function QQBridgePanel() {
  const { connected, run } = useApp();
  const [status, setStatus] = useState<Status>();
  const [config, setConfig] = useState<Config>({ enabled: false, thread_id: '', groups: [], launch_codex: true });
  const [busy, setBusy] = useState(false);
  const serverConfig = useRef('');
  useEffect(() => {
    if (!connected) return;
    let active = true;
    const refresh = async () => {
      try { const value = await rpc<Status>('qq.bridge.status'); if (active) { setStatus(value); const serialized = JSON.stringify(value.config); if (serialized !== serverConfig.current) { serverConfig.current = serialized; setConfig(value.config); } } } catch { /* regular app disconnect banner */ }
    };
    void refresh(); const timer = setInterval(() => void refresh(), 3000);
    return () => { active = false; clearInterval(timer); };
  }, [connected]);
  const action = async (method: string, params = {}) => {
    setBusy(true); const result = await run(() => rpc<Status>(method, params));
    if (result) { setStatus(result); setConfig(result.config); } setBusy(false);
  };
  return <div className="form-stack">
    <Section title="QQ 消息中转" description="主人私聊投递到指定 Codex 对话；工具箱退出后停止监听。">
      <Notice tone={status?.connected ? 'success' : 'warning'}>{status?.connected ? 'QQ 已连接' : 'QQ 未连接'}{status?.app_id ? ` · AppID ${status.app_id}` : ''}{status?.owner_id ? ' · 主人已配对' : ' · 主人未配对'}</Notice>
      {status?.last_error && <Notice tone="warning">{status.last_error}</Notice>}
      <Field label="Codex 对话 ID"><input value={config.thread_id} onChange={e => setConfig({ ...config, thread_id: e.target.value.trim() })} /></Field>
      <CheckField checked={config.enabled} onChange={enabled => setConfig({ ...config, enabled })} label="开启 QQ 监听" />
      <CheckField checked={config.launch_codex} onChange={launch_codex => setConfig({ ...config, launch_codex })} label="消息到达时拉起 Codex" />
      <Field label="允许的群 openid（每行一个）" hint="仅处理主人的 @ 消息；主人也可在群里 @机器人发送 /接入本群。"><textarea value={config.groups.join('\n')} onChange={e => setConfig({ ...config, groups: e.target.value.split(/\s+/).filter(Boolean) })} /></Field>
      <div className="section-footer"><Button disabled={!connected} busy={busy} onClick={() => void action('qq.bridge.save', { config })}>保存</Button><Button disabled={!connected || !status?.config.thread_id} busy={busy} onClick={() => void action('qq.bridge.bind')}>扫码绑定独立机器人</Button>{status?.binding && <Button onClick={() => void action('qq.bridge.cancel')}>取消扫码</Button>}</div>
      {status?.qr_path && <Button onClick={() => void run(() => native.open(status.qr_path))}>打开绑定二维码</Button>}
      {status?.pair_code && <Notice tone="warning">用你的 QQ 私聊新机器人发送：绑定 {status.pair_code}（15 分钟有效）</Notice>}
      {status?.bound && !status.owner_id && !status.pair_code && <Button onClick={() => void action('qq.bridge.pair')}>生成主人配对码</Button>}
    </Section>
    {!!status?.recent.length && <Section title="最近消息">{status.recent.map(item => <p key={item.id}>{item.status} · {item.id}{item.detail ? ` · ${item.detail}` : ''}</p>)}</Section>}
  </div>;
}
