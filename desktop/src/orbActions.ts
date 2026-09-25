import { AudioLines, Boxes, Captions, Command, FolderSync, FolderUp, House, MemoryStick, Radio, ReceiptText, Settings2, Smartphone, Sparkles, Speech } from 'lucide-react';

export const orbActions = [
  { id: 'clean', label: '清理内存', description: '立即按已保存模式清理', icon: Sparkles },
  { id: 'relay', label: '文件中转', description: '打开文件中转站', icon: FolderUp },
  { id: 'subtitle-toggle', label: '开启字幕', description: '切换字幕识别，开启时显示字幕窗', icon: Captions },
  { id: 'home', label: '工具首页', description: '打开工具箱首页', icon: House },
  { id: 'ram', label: '内存设置', description: '打开内存清理工具', icon: MemoryStick },
  { id: 'captions', label: '字幕设置', description: '打开实时字幕页面', icon: Captions },
  { id: 'filesync', label: '文件同步', description: '打开文件同步', icon: FolderSync },
  { id: 'memory', label: '项目记忆', description: '打开项目记忆', icon: Boxes },
  { id: 'media', label: '音视频', description: '打开音视频工作台', icon: AudioLines },
  { id: 'live', label: '实时助手', description: '打开实时助手', icon: Radio },
  { id: 'practice', label: '口语练习', description: '打开口语练习', icon: Speech },
  { id: 'phonetics', label: '音标教学', description: '打开音标教学', icon: Speech },
  { id: 'expenses', label: '记账', description: '打开记账', icon: ReceiptText },
  { id: 'shizuku', label: 'Shizuku', description: '打开安卓工具', icon: Smartphone },
  { id: 'fnconnect', label: '远程访问', description: '打开 FN 远程访问', icon: Radio },
  { id: 'gpu', label: '独显守卫', description: '打开独显省电守卫', icon: Boxes },
  { id: 'codex', label: 'Codex 配置', description: '打开 Codex 配置', icon: Command },
  { id: 'files', label: '文件整理', description: '打开文件整理', icon: FolderSync },
  { id: 'plugins', label: '扩展工具', description: '打开扩展工具', icon: Boxes },
  { id: 'settings', label: '工具箱设置', description: '打开设置', icon: Settings2 },
] as const;
export type OrbActionId = typeof orbActions[number]['id'];
export interface OrbPreferences { actions: OrbActionId[] }
export const defaultOrbActions: OrbActionId[] = ['clean', 'relay', 'subtitle-toggle', 'home'];
