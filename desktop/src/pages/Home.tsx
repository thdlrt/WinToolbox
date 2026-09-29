import { useMemo, useState } from 'react';
import { AudioLines, Search, Star } from 'lucide-react';
import { useApp, type Page } from '../context';
import { isDesktop } from '../api';
import { toolIcons } from '../toolIcons';
import { cx, Empty, Go } from '../ui';

const tools: { id: Page; name: string; icon: typeof AudioLines; tags: string[] }[] = [
  { id: 'ram', name: '内存清理', icon: toolIcons['ram'], tags: ['内存', '清理', 'RAM', '自动清理', '定时'] },
  { id: 'relay', name: '文件中转站', icon: toolIcons['relay'], tags: ['飞牛', 'WebDAV', '上传', '下载', '跨电脑', '缓存'] },
  { id: 'filesync', name: '文件同步', icon: toolIcons['filesync'], tags: ['FileSync', '双向同步', '备份', '文件夹', '笔记'] },
  { id: 'memory', name: '项目记忆', icon: toolIcons['memory'], tags: ['知识', '任务', '项目', 'SSH', 'WebDAV', '多设备'] },
  { id: 'media', name: '音视频工作台', icon: toolIcons['media'], tags: ['转写', '字幕', '翻译', '配音', '增强'] },
  { id: 'live', name: '实时助手', icon: toolIcons['live'], tags: ['会议', '问答', '录音', '实时'] },
  { id: 'captions', name: '实时字幕', icon: toolIcons['captions'], tags: ['直播', '视频', '翻译', '悬浮窗', '系统声音'] },
  { id: 'practice', name: '口语练习', icon: toolIcons['practice'], tags: ['英文', '朗读', '音标', '循环', '听力'] },
  { id: 'expenses', name: '记账', icon: toolIcons['expenses'], tags: ['项目', '报销', '发票', '附件', 'WebDAV', '表格导出'] },
  { id: 'network', name: '网络诊断', icon: toolIcons['network'], tags: ['DNS', 'TCP', 'TLS', 'HTTP', '端口', '连接', 'WebDAV'] },
  { id: 'shizuku', name: 'Shizuku', icon: toolIcons['shizuku'], tags: ['安卓', '无线调试', '启动', '手机', 'ADB'] },
  { id: 'fnconnect', name: 'FN 远程访问', icon: toolIcons['fnconnect'], tags: ['FN Connect', '端口映射', '局域网', '代理'] },
  { id: 'gpu', name: '独显省电守卫', icon: toolIcons['gpu'], tags: ['核显', '电池', '续航', '功耗', 'GPU'] },
  { id: 'codex', name: 'Codex 配置', icon: toolIcons['codex'], tags: ['模型', '上下文', '开发'] },
  { id: 'scripts', name: '快捷脚本', icon: toolIcons['scripts'], tags: ['选区', '翻译', '解散文件夹', '脚本', '触发条件'] },
  { id: 'plugins', name: '扩展工具', icon: toolIcons['plugins'], tags: ['插件', '安装', '扩展'] },
];

export default function HomePage() {
  const { navigate, settings, pinnedTools, togglePinnedTool } = useApp();
  const [query, setQuery] = useState('');
  const matches = useMemo(() => tools.filter(tool => `${tool.name} ${tool.tags.join(' ')}`.toLowerCase().includes(query.toLowerCase())), [query]);

  return <>
    <div className="home-toolbar"><span className="muted">星标固定到侧边栏</span><div className="search-input"><Search size={16} /><input aria-label="搜索工具" placeholder="搜索工具" value={query} onChange={e => setQuery(e.target.value)} /></div></div>
    {settings && settings.preferences.model_mode !== 'local' && !settings.providers.some(provider => provider.has_key) && <div className="setup-nudge"><span>尚未配置模型服务</span><Go onClick={() => navigate('settings')}>设置模型</Go></div>}
      {matches.length ? <div className="tool-grid">{matches.map(tool => <article className="tool-card" key={tool.id}><button className="tool-main" aria-label={tool.name} onClick={() => navigate(tool.id)}><tool.icon size={22} strokeWidth={1.7} /><span><h2>{tool.name}</h2><span className="tool-tags">{tool.tags.slice(0, 3).join(' · ')}</span></span></button><button className={cx('favorite', pinnedTools.includes(tool.id) && 'selected')} disabled={isDesktop() && settings?.preferences.ui_state_synced !== true} aria-label={`${pinnedTools.includes(tool.id) ? '取消固定' : '固定'}${tool.name}到侧边栏`} title={`${pinnedTools.includes(tool.id) ? '取消固定' : '固定'}${tool.name}到侧边栏`} onClick={() => togglePinnedTool(tool.id)}><Star size={16} fill={pinnedTools.includes(tool.id) ? 'currentColor' : 'none'} /></button></article>)}</div> : <Empty title="未找到工具" />}
  </>;
}
