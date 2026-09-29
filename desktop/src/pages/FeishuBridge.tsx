import { useEffect, useState } from 'react';
import { Check, Plus, RefreshCw, Trash2 } from 'lucide-react';
import { rpc } from '../api';
import { useApp } from '../context';
import { Button, CheckField, Empty, Field, Notice, Section } from '../ui';
import '../feishuBridge.css';

type MessageType = 'text' | 'post' | 'image' | 'file' | 'audio' | 'video';
interface Rule {
  id: string; name: string; enabled: boolean; chat_type: 'p2p' | 'group'; chat_id: string;
  sender_id: string; require_bot_mention: boolean; target_thread_id: string;
  prompt: string; message_types: MessageType[]; ack_text: string; keyword: string;
}
interface Config { enabled: boolean; app_id: string; bot_open_id: string; ack_text: string; reply_wait_seconds: number; rules: Rule[] }
interface Job { message_id: string; rule_id: string; status: string; reply_id?: string; updated_at: number; detail?: string; target_thread_id?: string }
interface BridgeStatus { config: Config; connected: boolean; last_error?: string; last_event_at?: number; recent: Job[] }
interface Thread { id: string; title: string; cwd: string }
const messageTypes: { id: MessageType; label: string }[] = [
  { id: 'text', label: '文字' }, { id: 'post', label: '富文本' }, { id: 'image', label: '图片' },
  { id: 'file', label: '文件' }, { id: 'audio', label: '音频' }, { id: 'video', label: '视频' },
];
const initialPrompt = '请读取并核验飞书原消息，按消息内容完成请求。处理完毕后以机器人身份回复原消息。';
const newRule = (): Rule => ({ id: crypto.randomUUID().replaceAll('-', ''), name: '', enabled: true,
  chat_type: 'p2p', chat_id: '', sender_id: '', require_bot_mention: false,
  target_thread_id: '', prompt: initialPrompt, message_types: ['text', 'post'], ack_text: '', keyword: '' });

export default function FeishuBridgePanel() {
  const { connected, error, success } = useApp();
  const [saved, setSaved] = useState<BridgeStatus>();
  const [draft, setDraft] = useState<Config>();
  const [editing, setEditing] = useState<string>();
  const [threads, setThreads] = useState<Thread[]>([]);
  const [busy, setBusy] = useState('');
  const [dirty, setDirty] = useState(false);
  const load = async (force = false) => {
    try {
      const result = await rpc<BridgeStatus>('feishu.bridge.status');
      setSaved(result);
      if (!dirty || force) { setDraft(result.config); setDirty(false); }
    } catch (reason) { error(reason); }
  };
  useEffect(() => { if (!connected) return; void load(); const timer = setInterval(() => { void load(); }, 10000); return () => clearInterval(timer); }, [connected, dirty]);
  const change = (patch: Partial<Config>) => { setDraft(old => old && { ...old, ...patch }); setDirty(true); };
  const changeRule = (id: string, patch: Partial<Rule>) => change({ rules: (draft?.rules || []).map(rule => rule.id === id ? { ...rule, ...patch } : rule) });
  const add = () => { const rule = newRule(); change({ rules: [...(draft?.rules || []), rule] }); setEditing(rule.id); };
  const remove = (id: string) => { change({ rules: (draft?.rules || []).filter(rule => rule.id !== id) }); if (editing === id) setEditing(undefined); };
  const move = (id: string, direction: -1 | 1) => { const rules = [...(draft?.rules || [])]; const index = rules.findIndex(rule => rule.id === id); const next = index + direction; if (index < 0 || next < 0 || next >= rules.length) return; [rules[index], rules[next]] = [rules[next], rules[index]]; change({ rules }); };
  const save = async () => {
    if (!draft) return;
    setBusy('save');
    try { const result = await rpc<BridgeStatus>('feishu.bridge.save', { config: draft }); setSaved(result); setDraft(result.config); setDirty(false); success('飞书监听配置已保存并生效。'); }
    catch (reason) { error(reason); }
    finally { setBusy(''); }
  };
  const check = async () => { setBusy('check'); try { await rpc('feishu.bridge.check'); success('飞书与 Codex 桌面接口均已就绪。'); } catch (reason) { error(reason); } finally { setBusy(''); } };
  const listThreads = async () => { setBusy('threads'); try { const result = await rpc<{ threads: Thread[] }>('feishu.bridge.threads'); setThreads(result.threads); } catch (reason) { error(reason); } finally { setBusy(''); } };
  if (!draft) return <Section title="飞书监听"><Empty title={connected ? '正在读取监听配置' : '工具箱服务未连接'} /></Section>;
  const selected = draft.rules.find(rule => rule.id === editing);
  return <div className="feishu-bridge">
    <Section title="飞书监听" description="工具箱运行时接收飞书事件，把匹配的消息送入指定 Codex 对话。" action={<div className="button-row"><Button busy={busy === 'check'} onClick={check}>检查连接</Button><Button busy={busy === 'save'} disabled={!dirty} variant="primary" onClick={save}><Check size={15} />保存配置</Button></div>}>
      <div className="feishu-status"><span className={saved?.connected ? 'online' : ''} />{saved?.connected ? '监听中' : draft.enabled ? '正在连接' : '已关闭'}{saved?.last_event_at ? ` · 最近事件 ${new Date(saved.last_event_at * 1000).toLocaleString('zh-CN')}` : ''}</div>
      {saved?.last_error && <Notice tone="warning">{saved.last_error}</Notice>}
      <CheckField checked={draft.enabled} onChange={enabled => change({ enabled })} label="启用飞书监听" hint="关闭工具箱会停止监听；可在通用设置中开启工具箱开机启动。" />
      <div className="form-grid top-gap"><Field label="飞书 App ID"><input value={draft.app_id} onChange={e => change({ app_id: e.target.value })} placeholder="cli_…" spellCheck={false} /></Field><Field label="机器人 open_id"><input value={draft.bot_open_id} onChange={e => change({ bot_open_id: e.target.value })} placeholder="ou_…" spellCheck={false} /></Field></div>
      <div className="form-grid"><Field label="默认思考中回复"><input value={draft.ack_text} onChange={e => change({ ack_text: e.target.value })} placeholder="留空不发送" /></Field><Field label="最终回复等待秒数"><input type="number" min="30" max="7200" value={draft.reply_wait_seconds} onChange={e => change({ reply_wait_seconds: Number(e.target.value) })} /></Field></div>
    </Section>
    <Section title="匹配规则" description="按会话、发送者和消息类型匹配。第一条匹配的规则生效。" action={<Button onClick={add}><Plus size={15} />添加规则</Button>}>
      {draft.rules.length ? <div className="feishu-rule-list">{draft.rules.map((rule, index) => <div className="feishu-rule" key={rule.id}><div><strong>{rule.name || '未命名规则'}</strong><small>{rule.chat_type === 'p2p' ? '私聊' : '群聊'} · {rule.chat_id || '未填会话 ID'} · {rule.enabled ? '已启用' : '已停用'}</small></div><div className="button-row"><Button disabled={index === 0} title="上移规则" onClick={() => move(rule.id, -1)}>↑</Button><Button disabled={index === draft.rules.length - 1} title="下移规则" onClick={() => move(rule.id, 1)}>↓</Button><Button onClick={() => setEditing(editing === rule.id ? undefined : rule.id)}>{editing === rule.id ? '收起' : '编辑'}</Button><Button variant="danger" onClick={() => remove(rule.id)}><Trash2 size={14} />删除</Button></div></div>)}</div> : <Empty title="尚无规则" />}
      {selected && <div className="feishu-editor"><div className="form-grid"><Field label="规则名称"><input value={selected.name} onChange={e => changeRule(selected.id, { name: e.target.value })} placeholder="例如：主人私聊" /></Field><Field label="会话类型"><select value={selected.chat_type} onChange={e => changeRule(selected.id, { chat_type: e.target.value as Rule['chat_type'] })}><option value="p2p">私聊</option><option value="group">群聊</option></select></Field></div>
        <div className="form-grid"><Field label="飞书 chat_id"><input value={selected.chat_id} onChange={e => changeRule(selected.id, { chat_id: e.target.value })} placeholder="oc_…" spellCheck={false} /></Field><Field label={selected.chat_type === 'p2p' ? '发送者 open_id（必填）' : '发送者 open_id（可选）'}><input value={selected.sender_id} onChange={e => changeRule(selected.id, { sender_id: e.target.value })} placeholder="ou_…" spellCheck={false} /></Field></div>
        <Field label="消息关键词（可选）" hint="仅处理正文包含此文本的消息；留空则不限制。"><input value={selected.keyword || ''} onChange={e => changeRule(selected.id, { keyword: e.target.value })} placeholder="例如：构建" /></Field>
        <Field label={selected.chat_type === 'p2p' ? '默认 Codex 对话 ID' : 'Codex 对话 ID'} hint={selected.chat_type === 'p2p' ? '直接发消息使用默认对话；回复消息继续来源 context。来源无法确认时提示失败。' : '消息会送到这个本机对话，沿用该对话的权限和上下文。'}><input value={selected.target_thread_id} onChange={e => changeRule(selected.id, { target_thread_id: e.target.value })} placeholder="对话 UUID" spellCheck={false} /></Field>
        <div className="feishu-thread-actions"><Button busy={busy === 'threads'} onClick={listThreads}><RefreshCw size={14} />读取可用对话</Button>{threads.length > 0 && <select aria-label="选择 Codex 对话" value="" onChange={e => changeRule(selected.id, { target_thread_id: e.target.value })}><option value="">选择本机对话…</option>{threads.map(thread => <option key={thread.id} value={thread.id}>{thread.title} · {thread.id.slice(0, 8)}</option>)}</select>}</div>
        <div className="inline-checks"><CheckField checked={selected.enabled} onChange={enabled => changeRule(selected.id, { enabled })} label="启用这条规则" />{selected.chat_type === 'group' && <CheckField checked={selected.require_bot_mention} onChange={require_bot_mention => changeRule(selected.id, { require_bot_mention })} label="必须 @机器人" />}</div>
        <Field label="接收的消息类型"><div className="feishu-types">{messageTypes.map(item => <label key={item.id}><input type="checkbox" checked={selected.message_types.includes(item.id)} onChange={e => changeRule(selected.id, { message_types: e.target.checked ? [...selected.message_types, item.id] : selected.message_types.filter(value => value !== item.id) })} />{item.label}</label>)}</div></Field>
        <Field label="单独的思考中回复（可选）"><input value={selected.ack_text} onChange={e => changeRule(selected.id, { ack_text: e.target.value })} placeholder="留空使用默认回复" /></Field>
        <Field label="任务提示词" hint="可用 {rule_name}、{chat_id}、{message_id}、{sender_id}、{acknowledgement}。监听器会自动附加身份复核和回飞书要求。"><textarea rows={10} value={selected.prompt} onChange={e => changeRule(selected.id, { prompt: e.target.value })} /></Field>
      </div>}
    </Section>
    <Section title="最近处理" action={<Button onClick={() => void load(true)}><RefreshCw size={14} />刷新</Button>}>
      {saved?.recent.length ? <div className="feishu-recent">{saved.recent.map(item => <div key={item.message_id}><strong>{draft.rules.find(rule => rule.id === item.rule_id)?.name || item.rule_id}</strong><span>{item.status} · {new Date(item.updated_at * 1000).toLocaleString('zh-CN')}</span><code>{item.message_id}</code>{item.target_thread_id && <small>Context：{item.target_thread_id}</small>}{item.detail && <small>{item.detail}</small>}</div>)}</div> : <Empty title="还没有处理记录" />}
    </Section>
  </div>;
}
