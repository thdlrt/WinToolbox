import { Binary, Braces, CaseSensitive, Copy, FileCode2, FileText, Fingerprint, FolderOutput, Link2, TerminalSquare, Unlink, WrapText } from 'lucide-react';
export const quickActionIcons:Record<string,typeof Copy>={dissolve:FolderOutput,paths:Copy,names:FileText,sha256:Fingerprint,join:WrapText,count:CaseSensitive,json:Braces,url_encode:Link2,url_decode:Unlink,base64_encode:Binary,base64_decode:FileCode2};
export const quickActionIcon=(id:string)=>quickActionIcons[id]||TerminalSquare;
