import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';

export const isDesktop = () => '__TAURI_INTERNALS__' in window;
export interface FileFilter { name: string; extensions: string[] }
export interface BackendEvent { type: string; [key: string]: unknown }

export function errorText(error: unknown): string {
  if (error instanceof Error) return error.message;
  if (typeof error === 'string') {
    try { const parsed = JSON.parse(error); return parsed.message || parsed.error?.message || error; } catch { return error; }
  }
  if (error && typeof error === 'object' && 'message' in error) return String(error.message);
  return JSON.stringify(error) || '操作未完成，请查看诊断信息。';
}

export async function rpc<T = Record<string, unknown>>(method: string, params: Record<string, unknown> = {}): Promise<T> {
  if (!isDesktop()) throw new Error('当前是浏览器预览。请打开 WinToolbox 桌面程序以使用本地文件、模型和任务。');
  return invoke<T>('rpc', { method, params });
}

async function desktopInvoke<T>(command: string, args?: Record<string, unknown>): Promise<T> {
  if (!isDesktop()) throw new Error('当前是浏览器预览。请打开 WinToolbox 桌面程序使用文件选择、播放与悬浮窗。');
  return invoke<T>(command, args);
}
export const native = {
  orb: (visible: boolean) => desktopInvoke<void>('set_orb', { visible }),
  orbResize: (expanded: boolean, focus = false) => desktopInvoke<void>('orb_resize', { expanded, focus }),
  orbSavePosition: () => desktopInvoke<void>('orb_save_position'),
  orbAction: (action: string) => desktopInvoke<void>('orb_action', { action }),
  dragFiles: (paths: string[]) => desktopInvoke<void>('drag_files', { paths }),
  installUpdate: (jobId: string) => desktopInvoke<void>('install_update', { jobId }),
  files: (multiple = true, filters?: FileFilter[]) => desktopInvoke<string[]>('pick_files', { multiple, filters }),
  directory: () => desktopInvoke<string | null>('pick_directory'),
  save: (defaultName: string, filters?: FileFilter[]) => desktopInvoke<string | null>('pick_save', { defaultName, filters }),
  open: (path: string) => desktopInvoke<void>('open_path', { path }),
  openExternal: (url: string) => desktopInvoke<void>('open_external', { url }),
  saveCourseFile: (defaultName: string, bytes: number[]) => desktopInvoke<string | null>('save_course_file', { defaultName, bytes }),
  overlay: (visible: boolean) => desktopInvoke<void>('set_overlay', { visible }),
  captionWindow: (visible: boolean) => desktopInvoke<void>('set_captions_window', { visible }),
  captionStyle: (enabled: boolean) => desktopInvoke<void>('set_captions_click_through', { enabled }),
  captionState: () => desktopInvoke<{ visible: boolean; click_through: boolean }>('get_captions_window'),
};

export async function subscribe(callback: (event: BackendEvent) => void) {
  if (!isDesktop()) return () => {};
  return listen<BackendEvent>('backend-event', event => callback(event.payload));
}

export interface Provider {
  id: string; name: string; kind: 'openai' | 'dashscope' | 'gemini'; base_url: string;
  has_key?: boolean; api_key?: string; model?: string; region?: string;
}
export interface RoleConfig { provider_id: string; model: string }
export interface Settings {
  providers: Provider[]; roles: Record<string, RoleConfig>;
  preferences: { library_path?: string; theme?: string; record_audio?: boolean; [key: string]: unknown };
  presets?: unknown[];
}
export interface Artifact { path: string; kind?: string; label?: string; name?: string }
export interface Job {
  id: string; tool: string; status: string; progress: number; message?: string; created_at: string;
  params: Record<string, unknown>; artifacts: Artifact[]; error?: string | Record<string, unknown>;
}
export interface Collection { id: string; name: string; prompt?: string; document_count?: number; index_ready?: boolean; index_status?: string }
export interface Citation { path?: string; page?: number; text?: string; title?: string; name?: string; document?: string; source?: string; [key: string]: unknown }
export interface Subtitle { id: string; start: number; end: number; text: string; translation?: string; speaker?: string }
export interface LocalModel { id: string; name: string; engine: string; installed: boolean; size_hint?: string; description?: string }
export interface PluginField { name: string; label?: string; type?: string; required?: boolean; default?: unknown; options?: (string | { value: string; label?: string })[]; description?: string }
export interface Plugin { id: string; name: string; version: string; description?: string; enabled?: boolean; permissions?: string[]; ui?: { fields?: PluginField[] }; manifest?: Partial<Plugin> }
