import { invoke } from '@tauri-apps/api/core';
import { rpc, type Job } from './api';

export const quickTools = [ ['home','工具首页'],['ram','内存清理'],['relay','文件中转站'],['filesync','文件同步'],['memory','项目记忆'],['media','音视频工作台'],['live','实时助手'],['captions','实时字幕'],['practice','口语练习'],['expenses','记账'],['network','网络诊断'],['shizuku','Shizuku'],['fnconnect','FN 远程访问'],['gpu','独显守卫'],['codex','Codex 配置'],['scripts','快捷脚本'],['plugins','扩展工具'],['settings','设置'] ];
export const triggerNames: Record<string,string> = {always:'始终显示', text:'选中文本', foreign_text:'选中普通外语', files:'选中文件或文件夹', file_only:'仅选中文件', folders:'仅选中文件夹', selection:'选中任意内容'};
export interface QuickAction {id:string;name:string;description?:string;trigger:string;enabled:boolean;builtin?:boolean;path?:string}
export interface QuickSettingsValue {enabled:boolean;trigger:string;shortcut:string;hold_ms:number;auto_translate:boolean;target_language:string;tools:string[];actions:Record<string,{enabled:boolean;trigger:string}>;custom:QuickAction[];runtime_error?:string}
export interface QuickSnapshot {id:string;text:string;paths:string[];foreign:boolean;same_language?:boolean;protected:boolean;message?:string;translation?:{translation:string;phonetic:string;meanings:string[]};output:string;settings:QuickSettingsValue;actions:QuickAction[]}
export interface DissolvePlan {token:string;roots:string[];operations:{source:string;target:string}[]}
export const getQuickSettings = () => invoke<QuickSettingsValue>('quick_settings');
export const saveQuickSettings = (settings:QuickSettingsValue) => invoke<QuickSettingsValue>('quick_settings',{settings});
export const copyQuickText = (text:string) => invoke<void>('quick_copy',{text});
export const quickSnapshot = (id:string) => rpc<QuickSnapshot>('quick.snapshot',{id});
export const startQuickAction = (id:string,action:string,token?:string) => rpc<Job>('quick.run',{id,action,token});
