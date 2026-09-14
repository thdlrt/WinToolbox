"""Build a compact Chinese illustrated manual with vector annotations."""
from pathlib import Path
import json
from html import escape
from reportlab.pdfgen import canvas
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import Paragraph
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.utils import ImageReader
from PIL import Image

ROOT=Path(__file__).resolve().parents[2]
ASSETS=ROOT/'output/playwright/manual'
OUT=ROOT/'output/pdf'
OUT.mkdir(parents=True,exist_ok=True)
PDF=OUT/'WinToolbox_图文使用手册与设计说明.pdf'
DATA=json.loads((ROOT/'docs/manual/content.json').read_text('utf-8'))
pdfmetrics.registerFont(TTFont('CN','C:/Windows/Fonts/msyh.ttc'))
pdfmetrics.registerFont(TTFont('CNB','C:/Windows/Fonts/msyhbd.ttc'))
pdfmetrics.registerFontFamily('CN',normal='CN',bold='CNB')
W,H=A4; M=34; IW=W-M*2
BLUE='#2563EB'; INK='#172B4D'; MUTED='#52647F'; LIGHT='#EFF5FF'; BORDER='#DBE5F1'
C=canvas.Canvas(str(PDF),pagesize=A4,pageCompression=1)
C.setTitle('WinToolbox 图文使用手册与设计说明')
C.setAuthor('WinToolbox')
C.setSubject('Windows 工具箱快速上手、主要工作流程和可扩展架构')
LAYOUT=[]

def col(value):return colors.HexColor(value)

def para(value,x,top,width,size=10.4,leading=15.2,font='CN',color=INK,draw=True):
    p=Paragraph(escape(value).replace('\n','<br/>'),ParagraphStyle('t',fontName=font,fontSize=size,leading=leading,textColor=col(color),wordWrap='CJK',splitLongWords=True))
    _,height=p.wrap(width,1000)
    if draw:p.drawOn(C,x,top-height)
    return height

def header(page,title,subtitle):
    C.setFillColor(col('#F7FAFE'));C.rect(0,H-125,W,125,stroke=0,fill=1)
    C.setFillColor(col(BLUE));C.roundRect(M,H-42,18,18,5,stroke=0,fill=1)
    C.setFillColor(colors.white)
    for x,y in ((M+4,H-29),(M+10,H-29),(M+4,H-35),(M+10,H-35)):C.rect(x,y,4,4,stroke=0,fill=1)
    para('WinToolbox  /  图文使用手册',M+26,H-25,330,9.8,14,font='CNB')
    C.setFont('CN',8.8);C.setFillColor(col(MUTED));C.drawRightString(W-M,H-35,'0.1.20  ·  2026.09')
    C.setFillColor(col('#C8D9F5'));C.setFont('CNB',39);C.drawString(M,H-94,f'{page:02d}')
    para(title,M+66,H-65,IW-66,21,27,font='CNB')
    para(subtitle,M+66,H-99,IW-66,9.7,14,color=MUTED)
    C.bookmarkPage('p'+str(page));C.addOutlineEntry(f'{page:02d}  {title}','p'+str(page),0)

def footer(page):
    labels=['上手','模型','文件','实时','预览','迁移','设计']
    C.setStrokeColor(col(BORDER));C.line(M,43,W-M,43)
    for i,label in enumerate(labels):
        x=M+i*52
        C.setFillColor(col(BLUE if i+1==page else MUTED));C.setFont('CNB' if i+1==page else 'CN',8)
        C.drawString(x,27,f'{i+1} {label}')
        C.linkRect('',f'p{i+1}',(x,22,x+44,38),relative=0,thickness=0)
    C.setFont('CNB',9);C.setFillColor(col(INK));C.drawRightString(W-M,27,f'{page} / 7')
    C.showPage()

def figure(name,top,max_h,crop=None,caption='',pins=()):
    if name is None:
        labels=[('选择工具','文件处理 / 实时字幕 / 实时助手'),('提交任务','选择所需步骤并开始'),('查看结果','工具页面 / 字幕窗 / 回答区')]
        gap=10; width=(IW-gap*2)/3
        for i,(title,body) in enumerate(labels):
            x=M+i*(width+gap)
            C.setFillColor(col(LIGHT));C.roundRect(x,top-102,width,92,7,stroke=0,fill=1)
            para(f'{i+1:02d}  {title}',x+12,top-25,width-24,12,18,font='CNB',color=BLUE)
            para(body,x+12,top-52,width-24,9,14,color=MUTED)
        para(caption,M,top-112,IW,8.6,12.3,color=MUTED)
        return top-145
    image=ASSETS/name
    with Image.open(image) as im:ow,oh=im.size
    x0,y0,x1,y1=crop or (0,0,ow,oh)
    scale=min(IW/(x1-x0),max_h/(y1-y0));fw=(x1-x0)*scale;fh=(y1-y0)*scale
    x=M+(IW-fw)/2;y=top-fh
    C.saveState();clip=C.beginPath();clip.rect(x,y,fw,fh);C.clipPath(clip,stroke=0)
    C.drawImage(ImageReader(str(image)),x-x0*scale,y-(oh-y1)*scale,width=ow*scale,height=oh*scale,mask='auto')
    C.restoreState();C.setStrokeColor(col(BORDER));C.setLineWidth(.7);C.roundRect(x,y,fw,fh,6,stroke=1,fill=0)
    for num,px,py in pins:
        cx=x+(px-x0)*scale;cy=top-(py-y0)*scale
        if x<cx<x+fw and y<cy<top:
            C.setFillColor(col(BLUE));C.setStrokeColor(colors.white);C.setLineWidth(1.3);C.circle(cx,cy,9,stroke=1,fill=1)
            C.setFillColor(colors.white);C.setFont('CNB',9);C.drawCentredString(cx,cy-3,str(num))
    h=para(caption,M,y-9,IW,8.6,12.3,color=MUTED) if caption else 0
    return y-9-h-13

def step(i,title,text,top):
    C.setFillColor(col(LIGHT));C.circle(M+10,top-9,10,stroke=0,fill=1)
    C.setFillColor(col(BLUE));C.setFont('CNB',9.6);C.drawCentredString(M+10,top-12.3,str(i))
    h=para(title,M+28,top,IW-28,11,15.5,font='CNB')
    h2=para(text,M+28,top-h-3,IW-28,10.15,14.6,color=INK)
    return top-h-h2-12

def notes(values,top):
    if not values:return top
    text='\n'.join(values)
    h=para(text,M+13,top-11,IW-26,8.9,13.2,color=MUTED,draw=False)
    C.setFillColor(col('#F1F6FC'));C.roundRect(M,top-h-22,IW,h+22,6,stroke=0,fill=1)
    para(text,M+13,top-11,IW-26,8.9,13.2,color=MUTED)
    return top-h-22

FIGURES={
 'start': (None, None, 135, '工作流程示意：选择场景，处理内容，查看结果。'),
 'connect-models': ('../model-setup/bailian-api.png', (208,128,1068,405), 210, '模型设置局部：选择 API 或本地预设。'),
 'media-results': ('03-processing.png', None, 245, '处理步骤可组合；配音和增强需要相应服务或运行包。'),
 'live': ('../model-setup/live-local.png', (208,123,1256,730), 250, '声音来源与双语记录局部示意；资料库从页面顶部按钮准备。'),
 'safe-changes': ('06-codex.png', None, 245, 'Codex 模型与容量仅为演示，以本机扫描结果为准。'),
 'extend-migrate': ('07-backup.png', None, 230, '个人备份可包含媒体、模型与密钥；包含密钥时必须设置密码。'),
}
PAGES=[]
for item in DATA['pages']:
    image,crop,height,caption=FIGURES[item['id']]
    PAGES.append(dict(title=item['title'],subtitle=item['subtitle'],image=image,crop=crop,height=height,
      caption=caption,pins=[],steps=[(step['title'],step['text']) for step in item['steps']],notes=item['notes']))

for i,page in enumerate(PAGES,1):
    header(i,page['title'],page['subtitle'])
    y=figure(page['image'],H-139,page['height'],page['crop'],page['caption'],page['pins'])
    for j,(title,text) in enumerate(page['steps'],1):y=step(j,title,text,y)
    y=notes(page['notes'],y-1)
    if y<53:raise RuntimeError(f'Page {i} overflow: {y:.1f}')
    LAYOUT.append({'page':i,'bottom':round(y,1)})
    footer(i)

header(7,'设计：一套入口，持续扩展','工具可以增加，任务、配置和数据管理继续复用。')

def box(x,top,w,h,title,body,fill=LIGHT,title_color=INK):
    C.setFillColor(col(fill));C.setStrokeColor(col(BORDER));C.roundRect(x,top-h,w,h,8,stroke=1,fill=1)
    para(title,x+14,top-12,w-28,12,17,font='CNB',color=title_color)
    para(body,x+14,top-36,w-28,9.3,14,color=MUTED)

def arrow(x1,y1,x2,y2):
    C.setStrokeColor(col('#7991B3'));C.setFillColor(col('#7991B3'));C.setLineWidth(1.2);C.line(x1,y1,x2,y2)
    p=C.beginPath()
    if abs(x2-x1)>abs(y2-y1):p.moveTo(x2,y2);p.lineTo(x2-5,y2+3);p.lineTo(x2-5,y2-3)
    else:p.moveTo(x2,y2);p.lineTo(x2-3,y2+5);p.lineTo(x2+3,y2+5)
    p.close();C.drawPath(p,stroke=0,fill=1)

box(M,695,IW,70,'桌面入口  ·  React + Tauri','工具页面与设置；文件选择、快捷键、主窗口与悬浮窗。')
arrow(W/2,625,W/2,598)
box(M,598,IW,80,'共享服务  ·  Python','任务队列与进度、模型接口、字幕处理、资料检索、WASAPI 录音、备份恢复。','#E7F0FF',BLUE)
width=(IW-22)/3
for k,(title,body,fill) in enumerate([
    ('内置处理模块','音视频、实时字幕与助手、Codex 配置、文件整理。','#F1F6FC'),
    ('可安装工具包','清单 + 参数表单 + Python 入口；独立进程执行。','#F1F7F3'),
    ('模型能力','调用你配置的 API，或运行按需安装的本地模型。','#FFF6E8')]):
    x=M+k*(width+11);arrow(x+width/2,518,x+width/2,489);box(x,489,width,101,title,body,fill)
for k in range(3):
    x=M+k*(width+11)+width/2
    arrow(x,388,x,362)
box(M,362,IW,72,'本机数据  ·  SQLite + 文件目录','保存任务、索引、资料、录音和结果；密钥使用 Windows 用户加密保护。','#F2F5F9')
para('新增一个工具，主要完成这四步',M,265,IW,12,18,font='CNB')
cw=(IW-21)/4
for i,(title,body) in enumerate([('定义输入输出','明确参数与结果文件'),('编写工具包','manifest + Python'),('安装验证','导入 .toolpkg'),('查看结果','在工具页直接查看')]):
    x=M+i*(cw+7);C.setFillColor(col(LIGHT));C.roundRect(x,171,cw,66,6,stroke=0,fill=1)
    para(f'{i+1:02d}  {title}',x+9,227,cw-18,9.5,14,font='CNB',color=BLUE)
    para(body,x+9,206,cw-18,8.6,13,color=MUTED)
notes(['设计取舍：常用操作直接显示；重型运行包按需安装；任务有进度、有错误、有结果；程序和个人数据分开迁移。',
       '首版扩展采用参数表单，不含在线插件市场。更复杂的独立界面需要继续扩展宿主。',
       '按当前功能整理。局部截图沿用较早版本，已避开旧导航；入口与操作以正文和当前程序为准。历史验证见项目测试记录。'],151)
footer(7)
C.save()
(OUT/'manual-layout-check.json').write_text(json.dumps(LAYOUT,indent=2),encoding='utf-8')
print(PDF)
