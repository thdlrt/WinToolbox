import { useEffect, useMemo, useRef, useState } from 'react';
import { AudioLines, Boxes, Captions, Command, FolderSync, Radio, ReceiptText, Search, Smartphone, Speech, Star } from 'lucide-react';
import { useApp, type Page } from '../context';
import { isDesktop, rpc } from '../api';
import { validFavorites } from '../uiPreferences';
import { cx, Empty, Go } from '../ui';

const tools: { id: Page; name: string; icon: typeof AudioLines; tags: string[] }[] = [
  { id: 'media', name: '音视频工作台', icon: AudioLines, tags: ['转写', '字幕', '翻译', '配音', '增强'] },
  { id: 'live', name: '实时助手', icon: Radio, tags: ['会议', '问答', '录音', '实时'] },
  { id: 'captions', name: '实时字幕', icon: Captions, tags: ['直播', '视频', '翻译', '悬浮窗', '系统声音'] },
  { id: 'practice', name: '口语练习', icon: Speech, tags: ['英文', '朗读', '音标', '循环', '听力'] },
  { id: 'expenses', name: '记账', icon: ReceiptText, tags: ['AI 订阅', '报销', '发票', '附件', '费用'] },
  { id: 'shizuku', name: 'Shizuku', icon: Smartphone, tags: ['安卓', '无线调试', '启动', '手机', 'ADB'] },
  { id: 'fnconnect', name: '回家 VPN', icon: Radio, tags: ['FN Connect', '局域网', '代理', 'TUN'] },
  { id: 'codex', name: 'Codex 配置', icon: Command, tags: ['模型', '上下文', '开发'] },
  { id: 'files', name: '文件整理', icon: FolderSync, tags: ['移动', '重命名', '批量'] },
  { id: 'plugins', name: '扩展工具', icon: Boxes, tags: ['插件', '安装', '扩展'] },
];

export default function HomePage() {
  const { navigate, settings, error, refreshSettings } = useApp();
  const [query, setQuery] = useState('');
  const [favorites, setFavorites] = useState<string[]>(() => { try { return JSON.parse(localStorage.getItem('wintoolbox-favorites') || '["media","live"]'); } catch { return ['media', 'live']; } });
  const [filter, setFilter] = useState('all');
  const favoriteWrite = useRef(0);
  const favoriteQueue = useRef<Promise<unknown>>(Promise.resolve());
  const favoriteSaving = useRef(false);
  useEffect(() => {
    if (!favoriteSaving.current && settings?.preferences.ui_state_synced === true && validFavorites(settings.preferences.favorites)) {
      setFavorites(settings.preferences.favorites); localStorage.setItem('wintoolbox-favorites', JSON.stringify(settings.preferences.favorites));
    }
  }, [settings]);
  const matches = useMemo(() => tools.filter(tool => (filter !== 'favorites' || favorites.includes(tool.id)) && `${tool.name} ${tool.tags.join(' ')}`.toLowerCase().includes(query.toLowerCase())), [query, favorites, filter]);
  const toggleFavorite = (id: string) => {
    const next = favorites.includes(id) ? favorites.filter(value => value !== id) : [...favorites, id];
    setFavorites(next); localStorage.setItem('wintoolbox-favorites', JSON.stringify(next));
    if (!isDesktop()) return;
    const revision = ++favoriteWrite.current;
    favoriteSaving.current = true;
    const request = favoriteQueue.current.catch(() => {}).then(() => rpc('settings.update', { settings: { preferences: { favorites: next } } }));
    favoriteQueue.current = request;
    void request.then(async () => { if (revision === favoriteWrite.current) { favoriteSaving.current = false; await refreshSettings(); } }).catch(reason => { if (revision === favoriteWrite.current) favoriteSaving.current = false; error(reason); });
  };

  return <>
    <div className="home-toolbar"><div className="segmented small"><button className={cx(filter === 'all' && 'selected')} onClick={() => setFilter('all')}>全部工具</button><button className={cx(filter === 'favorites' && 'selected')} onClick={() => setFilter('favorites')}>收藏</button></div><div className="search-input"><Search size={16} /><input aria-label="搜索工具" placeholder="搜索工具" value={query} onChange={e => setQuery(e.target.value)} /></div></div>
    {settings && settings.preferences.model_mode !== 'local' && !settings.providers.some(provider => provider.has_key) && <div className="setup-nudge"><span>尚未配置模型服务</span><Go onClick={() => navigate('settings')}>设置模型</Go></div>}
      {matches.length ? <div className="tool-grid">{matches.map(tool => <article className="tool-card" key={tool.id}><button className="tool-main" aria-label={tool.name} onClick={() => navigate(tool.id)}><tool.icon size={22} strokeWidth={1.7} /><span><h2>{tool.name}</h2><span className="tool-tags">{tool.tags.slice(0, 3).join(' · ')}</span></span></button><button className={cx('favorite', favorites.includes(tool.id) && 'selected')} disabled={isDesktop() && settings?.preferences.ui_state_synced !== true} aria-label={`${favorites.includes(tool.id) ? '取消收藏' : '收藏'}${tool.name}`} title={`${favorites.includes(tool.id) ? '取消收藏' : '收藏'}${tool.name}`} onClick={() => toggleFavorite(tool.id)}><Star size={16} fill={favorites.includes(tool.id) ? 'currentColor' : 'none'} /></button></article>)}</div> : <Empty title={query ? '未找到工具' : '暂无收藏'} />}
  </>;
}
