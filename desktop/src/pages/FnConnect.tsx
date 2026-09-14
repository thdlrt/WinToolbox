import {useEffect,useState} from 'react';
import {rpc,errorText,type Job} from '../api';
import {useApp} from '../context';
import {Button,Field,Notice,Section,Progress,activeJob} from '../ui';
import '../fnconnect.css';
type State={phase:string;origin?:string;scope?:string;transport?:string;connections:number;tx:number;rx:number;error?:string};
type Info={state:State;tun:{phase:string;available:boolean;error?:string;service?:boolean;enabled?:boolean}};
type Probe={target:string;status_line:string;latency_ms:number;transport:string};
export default function FnConnectPage(){
 const {connected:backend,jobs,track}=useApp();
 const [info,setInfo]=useState<Info>(),[origin,setOrigin]=useState(''),[username,setUsername]=useState(''),[password,setPassword]=useState(''),[scope,setScope]=useState('lan'),[busy,setBusy]=useState(false),[failure,setFailure]=useState('');
 const [remember,setRemember]=useState(true),[saved,setSaved]=useState<{origin?:string;username?:string;remember?:boolean}>({});
 const hasSaved=!!saved.remember&&saved.origin===origin&&saved.username===username;
 useEffect(()=>{if(backend)void rpc<{origin?:string;username?:string;scope?:string;remember?:boolean}>('fnconnect.profile').then(p=>{setSaved(p);if(p.origin)setOrigin(p.origin);if(p.username)setUsername(p.username);if(p.scope)setScope(p.scope);}).catch(e=>setFailure(errorText(e)));},[backend]);
 const tasks=jobs.filter(j=>j.tool.startsWith('fnconnect.'));
 const running=tasks.find(j=>activeJob(j.status));
 const last=tasks[0];
 const probe=(tasks.find(j=>j.tool==='fnconnect.probe'&&j.status==='completed') as (Job&{result?:Probe})|undefined)?.result;
 const state=info?.state,online=state?.phase==='connected';
 useEffect(()=>{if(!backend)return;let live=true;const refresh=async()=>{try{const r=await rpc<Info>('fnconnect.status');if(live)setInfo(r);}catch(e){if(live)setFailure(errorText(e));}};void refresh();const timer=setInterval(refresh,2000);return()=>{live=false;clearInterval(timer);};},[backend]);
 async function action(method:string){if(busy||running)return;setBusy(true);setFailure('');const credentials=method==='connect'?{origin,scope,username,password,remember}:{};if(method==='connect'&&!password&&!hasSaved){setFailure('请填写飞牛密码后重新连接。');setBusy(false);return;}try{const job=await rpc<Job>('fnconnect.'+method,credentials);track(job,'操作已开始。');}catch(e){setFailure(errorText(e));}finally{setBusy(false);}}
 useEffect(()=>{if(online){setPassword('');void rpc<typeof saved>('fnconnect.profile').then(setSaved).catch(e=>setFailure(errorText(e)));}},[online]);
 const disabled=!backend||busy||!!running;
 async function reset(){setFailure('');try{await rpc('fnconnect.reset');}catch(e){setFailure(errorText(e));}}
 const phases:Record<string,string>={disconnected:'未连接',connecting:'正在连接',connected:'已连接',error:'连接失败'};
 return <div className="fnconnect-page">
 <div className="fnconnect-state"><strong>{phases[state?.phase||'disconnected']}</strong><span>{state?.transport||'Windows TCP 试验版'}</span><span>{info?.tun.phase==='running'?'TUN 接管中':'未启用 TUN'}</span></div>
 {failure&&<Notice tone="warning">{failure}</Notice>}{state?.error&&<Notice tone="warning">{state.error}</Notice>}
 <Section><form onSubmit={e=>{e.preventDefault();void action('connect');}}>
 <div className="form-grid"><Field label="NAS 地址" hint="外网使用 https://你的FNID.fnos.net；局域网 IP 仅用于测试。"><input aria-label="NAS 地址" value={origin} disabled={disabled||online} onChange={e=>setOrigin(e.target.value)} required/></Field>
 <Field label="代理范围"><select aria-label="代理范围" value={scope} disabled={disabled||online} onChange={e=>setScope(e.target.value)}><option value="lan">仅回家 · NAS 允许的局域网网段</option><option value="all">全局出口 · 使用家中网络</option></select></Field>
 <Field label="飞牛管理员"><input aria-label="飞牛管理员" value={username} autoComplete="username" disabled={disabled||online} onChange={e=>setUsername(e.target.value)} required/></Field>
 <Field label="密码"><input aria-label="飞牛密码" type="password" value={password} autoComplete="off" disabled={disabled||online} onChange={e=>setPassword(e.target.value)} placeholder={hasSaved?'已安全保存，留空即可连接':''} required={!hasSaved}/></Field></div>
 <div className="button-row"><label><input type="checkbox" checked={remember} onChange={e=>{setRemember(e.target.checked);if(!e.target.checked)void rpc('fnconnect.forget').then(()=>setSaved({})).catch(e=>setFailure(errorText(e)));}}/> 记住登录</label>{saved.remember&&<Button type="button" onClick={()=>void rpc('fnconnect.forget').then(()=>{setSaved({});setPassword('');}).catch(e=>setFailure(errorText(e)))}>清除已保存密码</Button>}</div>
 <p className="muted">记住登录后，密码由 Windows 当前用户加密保存，重启后可直接连接。当前试验版不支持双重验证。</p>
 <div className="button-row">{online?<Button type="button" variant="danger" disabled={!backend} onClick={()=>void reset()}>断开</Button>:<Button type="submit" variant="primary" disabled={disabled}>连接</Button>}<Button type="button" disabled={disabled||!online} onClick={()=>void action('probe')}>测试回家连接</Button><Button type="button" disabled={!backend} onClick={()=>void reset()}>重置连接</Button></div>
 </form></Section>
 <Section title="流量接管"><div className="button-row"><Button disabled={disabled||!online||!info?.tun.available||info?.tun.phase==='awaiting-admin'} onClick={()=>void action(info?.tun.phase==='running'?'tun-stop':'tun-start')}>{info?.tun.phase==='running'?'停止 TUN':info?.tun.phase==='awaiting-admin'?'等待首次服务安装授权':'启用 TUN'}</Button><span className="muted">SOCKS5：127.0.0.1:18792</span></div>
 <p className="muted">{info?.tun.service?'TUN 辅助服务已就绪，开关无需重复授权。':'首次启用需要管理员授权安装 TUN 辅助服务。'} {info?.tun.enabled?'已记住开启状态，下次连接自动恢复。':'手动启用后将记住开启状态。'} 断开或退出后自动撤销路由。全局模式代理 TCP，暂不支持 UDP 和 IPv6。</p>
 {!info?.tun.available&&<Notice tone="warning">TUN 核心尚未安装，请更新免安装版。</Notice>}{info?.tun.error&&<Notice tone="warning">{info.tun.error}</Notice>}
 <div className="fnconnect-counters"><span>活动连接 {state?.connections||0}</span><span>上行 {((state?.tx||0)/1024).toFixed(1)} KiB</span><span>下行 {((state?.rx||0)/1024).toFixed(1)} KiB</span></div>
 </Section>
 {running&&<Section><p>{running.message}</p><Progress value={running.progress||0}/></Section>}
 {!running&&last?.status==='failed'&&<Notice tone="warning">{errorText(last.error)}</Notice>}
 {probe&&<Notice tone="success">{probe.target} · {probe.status_line} · {probe.latency_ms} ms · {probe.transport}</Notice>}
 </div>;
}
