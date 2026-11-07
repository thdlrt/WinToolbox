import { Gauge, HardDrive, ClipboardList, AudioLines, BookOpen, Captions, CircleDot, Command, FileCode2, Network, FolderInput, FolderSync, TerminalSquare, House, MemoryStick, MonitorUp, Music2, Puzzle, Radio, ReceiptText, Settings2, ShieldCheck, Speech } from 'lucide-react';
import type { Page } from './context';

// Shared by the home cards and navigation so each tool has one distinct icon.
export const toolIcons = {
  'ai-quota': Gauge,
  netmount: HardDrive,
  clipboard: ClipboardList,
  home: House,
  'orb-settings': CircleDot,
  ram: MemoryStick,
  relay: FolderInput,
  filesync: FolderSync,
  memory: BookOpen,
  media: AudioLines,
  netease: Music2,
  live: Radio,
  captions: Captions,
  practice: Speech,
  expenses: ReceiptText,
  network: Network,
  fnconnect: MonitorUp,
  gpu: ShieldCheck,
  codex: Command,
  'script-tools': FileCode2,
  scripts: TerminalSquare,
  plugins: Puzzle,
  settings: Settings2,
  stackchan: MonitorUp,
} satisfies Record<Exclude<Page, 'phonetics'>, typeof House>;
