use serde_json::{json, Value};
use std::{io::{Read, Write}, process::{Command, Stdio}, sync::{Mutex, OnceLock, atomic::{AtomicBool, AtomicU64, Ordering}}, time::{Duration, Instant}};
use tauri::{AppHandle, Emitter, Manager, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_global_shortcut::{GlobalShortcutExt, ShortcutState};

#[derive(Default)]
pub struct QuickState {
    config: Mutex<Value>, snapshot: Mutex<Value>, shortcut: Mutex<Option<String>>,
    pub toggle: Mutex<Option<tauri::menu::CheckMenuItem<tauri::Wry>>>,
    busy: AtomicBool, held: AtomicBool, fired: AtomicBool, generation: AtomicU64,
    press: Mutex<Option<Press>>, pub error: Mutex<Option<String>>,
}
#[derive(Clone, Copy)]
struct Press { button: u32, x: i32, y: i32, forwarded: bool }
static APP: OnceLock<AppHandle> = OnceLock::new();

#[cfg(windows)] mod win {
    #[repr(C)] #[derive(Clone,Copy,Default)] pub struct Point {pub x:i32,pub y:i32}
    #[repr(C)] pub struct MouseHook {pub pt:Point,pub data:u32,pub flags:u32,pub time:u32,pub extra:usize}
    #[repr(C)] #[derive(Default)] pub struct Message {pub hwnd:isize,pub message:u32,pub wparam:usize,pub lparam:isize,pub time:u32,pub pt:Point,pub private:u32}
    #[repr(C)] pub struct MouseInput {pub dx:i32,pub dy:i32,pub data:u32,pub flags:u32,pub time:u32,pub extra:usize}
    #[repr(C)] pub struct Input {pub kind:u32,pub mouse:MouseInput}
    #[link(name="user32")] extern "system" {
        pub fn SetWindowsHookExW(id:i32,callback:unsafe extern "system" fn(i32,usize,isize)->isize,module:isize,thread:u32)->isize;
        pub fn CallNextHookEx(hook:isize,code:i32,wparam:usize,lparam:isize)->isize;
        pub fn GetMessageW(message:*mut Message,window:isize,min:u32,max:u32)->i32;
        pub fn TranslateMessage(message:*const Message)->i32;
        pub fn DispatchMessageW(message:*const Message)->isize;
        pub fn GetForegroundWindow()->isize;
        pub fn GetCursorPos(point:*mut Point)->i32;
        pub fn SendInput(count:u32,inputs:*const Input,size:i32)->u32;
    }
    pub unsafe fn inject(button:u32,up:bool) {
        let input=Input{kind:0,mouse:MouseInput{dx:0,dy:0,data:if button==1 {0}else{button-1},flags:if button==1 {if up{0x40}else{0x20}}else if up{0x100}else{0x80},time:0,extra:0x57544251}};
        SendInput(1,&input,std::mem::size_of::<Input>() as i32);
    }
}

fn begin_hold(app: &AppHandle, source: isize, x:i32,y:i32) {
    let state=app.state::<QuickState>();
    state.held.store(true,Ordering::SeqCst); state.fired.store(false,Ordering::SeqCst);
    let generation=state.generation.fetch_add(1,Ordering::SeqCst)+1;
    let delay=state.config.lock().unwrap()["hold_ms"].as_u64().unwrap_or(450);
    let app=app.clone();
    std::thread::spawn(move|| {
        std::thread::sleep(Duration::from_millis(delay));
        let state=app.state::<QuickState>();
        if state.held.load(Ordering::SeqCst) && state.generation.load(Ordering::SeqCst)==generation {
            state.fired.store(true,Ordering::SeqCst);
            trigger(&app,source,x,y,generation);
        }
    });
}

#[cfg(windows)] unsafe extern "system" fn mouse_hook(code:i32,wparam:usize,lparam:isize)->isize {
    if code<0 {return win::CallNextHookEx(0,code,wparam,lparam)}
    let mouse=&*(lparam as *const win::MouseHook);
    if mouse.extra == 0x57544251 {return win::CallNextHookEx(0,code,wparam,lparam)}
    let Some(app)=APP.get() else{return win::CallNextHookEx(0,code,wparam,lparam)};
    let state=app.state::<QuickState>();
    // Finish an already intercepted click even when settings changed mid-press.
    if wparam==0x208 || wparam==0x20c {
        let button=if wparam==0x208 {1}else{1+(mouse.data>>16)};
        let mut press=state.press.lock().unwrap();
        if press.as_ref().map(|p|p.button)==Some(button) {
            let p=press.take().unwrap(); state.held.store(false,Ordering::SeqCst);
            if p.forwarded {return win::CallNextHookEx(0,code,wparam,lparam)}
            if !state.fired.load(Ordering::SeqCst) {win::inject(button,false);win::inject(button,true);}
            return 1;
        }
    }
    if wparam==0x200 {
        let mut guard=state.press.lock().unwrap();
        if let Some(p)=guard.as_mut() {
            if !p.forwarded && !state.fired.load(Ordering::SeqCst) && ((mouse.pt.x-p.x).abs()>8 || (mouse.pt.y-p.y).abs()>8) {
                p.forwarded=true;state.held.store(false,Ordering::SeqCst);state.generation.fetch_add(1,Ordering::SeqCst);win::inject(p.button,false);
            }
        }
    }
    if wparam==0x207 || wparam==0x20b {
        let button=if wparam==0x207 {1}else{1+(mouse.data>>16)};
        let config=state.config.lock().unwrap();
        let expected=match config["trigger"].as_str(){Some("middle")=>1,Some("x1")=>2,Some("x2")=>3,_=>0};
        if config["enabled"].as_bool()==Some(true) && button==expected {
            drop(config);
            *state.press.lock().unwrap()=Some(Press{button,x:mouse.pt.x,y:mouse.pt.y,forwarded:false});
            begin_hold(app,win::GetForegroundWindow(),mouse.pt.x,mouse.pt.y);
            return 1;
        }
    }
    win::CallNextHookEx(0,code,wparam,lparam)
}

fn helper(app:&AppHandle,args:&[String],input:Option<String>)->Result<Value,String> {
    let (_,_,tools)=super::resource_paths(app)?;
    let exe=tools.join("quick-context/WinToolbox.QuickContext.exe");
    if !exe.is_file(){return Err("缺少选区读取组件，请更新完整便携版".into())}
    let mut command=Command::new(exe);
    command.args(args).stdin(if input.is_some(){Stdio::piped()}else{Stdio::null()}).stdout(Stdio::piped()).stderr(Stdio::null());
    let mut child=super::hidden(&mut command).spawn().map_err(|e|e.to_string())?;
    if let Some(text)=input {if let Some(mut pipe)=child.stdin.take(){pipe.write_all(text.as_bytes()).map_err(|e|e.to_string())?;}}
    let out=child.stdout.take().ok_or("选区输出不可用")?;
    let reader=std::thread::spawn(move||{let mut data=String::new();out.take(1024*1024).read_to_string(&mut data).map(|_|data)});
    let start=Instant::now();
    loop {
        if let Some(status)=child.try_wait().map_err(|e|e.to_string())? {
            let data=reader.join().map_err(|_|"选区读取失败")?.map_err(|e|e.to_string())?;
            if !status.success(){return Err("无法完成剪贴板操作，请重试".into())}
            return serde_json::from_str(data.trim_start_matches('\u{feff}').trim()).map_err(|_|"选区读取结果无效".into());
        }
        if start.elapsed()>Duration::from_secs(4){let _=child.kill();let _=child.wait();return Err("此应用响应较慢，未能读取选区；请重试".into())}
        std::thread::sleep(Duration::from_millis(20));
    }
}

fn trigger(app:&AppHandle,source:isize,x:i32,y:i32,generation:u64) {
    let state=app.state::<QuickState>();
    if state.busy.swap(true,Ordering::SeqCst){return}
    let result=(||->Result<(),String>{
        let mut capture=helper(app,&["--capture".into(),source.to_string()],None).unwrap_or_else(|e|json!({"text":"","paths":[],"message":e}));
        if state.generation.load(Ordering::SeqCst)!=generation || state.config.lock().unwrap()["enabled"]!=true {return Ok(())}
        // The selection reader never focuses a window. Capture finishes before popup creation.
        if !capture.is_object(){capture=json!({})}
        let snapshot=super::rpc_blocking(app,&app.state::<super::Bridge>(),"quick.capture".into(),capture)?;
        *state.snapshot.lock().unwrap()=snapshot.clone();
        let window=if let Some(w)=app.get_webview_window("quick"){w}else{
            let w=WebviewWindowBuilder::new(app,"quick",WebviewUrl::App("index.html?quick=1".into()))
                .title("WinToolbox · 快捷菜单").inner_size(360.0,500.0).resizable(false).decorations(false)
                .always_on_top(true).skip_taskbar(true).visible(false).build().map_err(|e|e.to_string())?;
            let handle=app.clone();let focused=std::sync::Arc::new(AtomicBool::new(false));
            w.on_window_event(move|event|{
                if matches!(event,tauri::WindowEvent::Focused(true)){focused.store(true,Ordering::SeqCst);}
                if matches!(event,tauri::WindowEvent::Focused(false)) && focused.swap(false,Ordering::SeqCst) {
                    let h=handle.clone();
                    std::thread::spawn(move||{std::thread::sleep(Duration::from_millis(200));if let Some(w)=h.get_webview_window("quick"){if !w.is_focused().unwrap_or(true){let _=w.hide();}}});
                }
            });
            w
        };
        let monitors=app.available_monitors().map_err(|e|e.to_string())?;
        if let Some(m)=monitors.iter().find(|m|{let a=m.work_area();x>=a.position.x&&x<a.position.x+a.size.width as i32&&y>=a.position.y&&y<a.position.y+a.size.height as i32}).or(monitors.first()) {
            let a=m.work_area();let scale=m.scale_factor();let width=(360.0*scale).round() as u32;let height=(500.0*scale).round() as u32;
            let w=width.min(a.size.width);let h=height.min(a.size.height);
            window.set_size(tauri::PhysicalSize::new(w,h)).map_err(|e|e.to_string())?;
            let (left,top)=position(x,y,w,h,a.position.x,a.position.y,a.size.width,a.size.height);
            window.set_position(tauri::PhysicalPosition::new(left,top)).map_err(|e|e.to_string())?;
        }
        super::webview_memory::set_background(&window,false);
        window.show().map_err(|e|e.to_string())?;let _=window.set_focus();
        let _=window.emit("quick-context",snapshot);
        Ok(())
    })();
    if let Err(error)=result {*state.error.lock().unwrap()=Some(error.clone());let _=app.emit("backend-event",json!({"type":"quick.error","message":error}));}
    state.busy.store(false,Ordering::SeqCst);
}

fn position(x:i32,y:i32,w:u32,h:u32,left:i32,top:i32,width:u32,height:u32)->(i32,i32){
    ((x+12).clamp(left,left+width.saturating_sub(w) as i32),(y+12).clamp(top,top+height.saturating_sub(h) as i32))
}

pub fn apply(app:&AppHandle,config:Value)->Result<(),String>{
    let state=app.state::<QuickState>();
    let desired=if config["enabled"]==true && config["trigger"]=="keyboard" {Some(config["shortcut"].as_str().unwrap_or("").to_string())}else{None};
    let mut old=state.shortcut.lock().unwrap();
    if *old!=desired {
        if let Some(key)=&desired {
            let lower=key.to_lowercase().replace(' ',"");
            if !lower.contains("ctrl") && !lower.contains("control") && !lower.contains("alt") && !lower.starts_with('f'){return Err("请使用 Ctrl/Alt 组合键或功能键".into())}
            if ["ctrl+alt+space","ctrl+alt+escape","ctrl+alt+arrowleft","ctrl+alt+l","ctrl+alt+c"].contains(&lower.as_str()){return Err("该快捷键已被实时助手或字幕占用".into())}
            app.global_shortcut().on_shortcut(key.as_str(),move|app,_,event| {
                let s=app.state::<QuickState>();
                if event.state==ShortcutState::Released {s.held.store(false,Ordering::SeqCst);return}
                if s.held.load(Ordering::SeqCst){return}
                #[cfg(windows)] unsafe {let mut p=win::Point::default();win::GetCursorPos(&mut p);begin_hold(app,win::GetForegroundWindow(),p.x,p.y);}
            }).map_err(|e|format!("快捷键无法注册，可能已被占用：{e}"))?;
        }
        if let Some(key)=old.take(){let _=app.global_shortcut().unregister(key.as_str());}
        *old=desired;
    }
    state.generation.fetch_add(1,Ordering::SeqCst);state.held.store(false,Ordering::SeqCst);
    *state.config.lock().unwrap()=config.clone();
    if let Some(item)=state.toggle.lock().unwrap().as_ref(){let _=item.set_checked(config["enabled"]==true);}
    Ok(())
}

#[tauri::command] pub async fn quick_settings(app:AppHandle,settings:Option<Value>)->Result<Value,String>{
    tauri::async_runtime::spawn_blocking(move||{
        if let Some(settings)=settings {
            let previous=app.state::<QuickState>().config.lock().unwrap().clone();
            apply(&app,settings.clone())?;
            match super::rpc_blocking(&app,&app.state::<super::Bridge>(),"quick.save".into(),settings) {
                Ok(v)=>{apply(&app,v.clone())?;Ok(v)},
                Err(e)=>{let _=apply(&app,previous);Err(e)}
            }
        } else {let mut v=super::rpc_blocking(&app,&app.state::<super::Bridge>(),"quick.settings".into(),json!({}))?;v["runtime_error"]=json!(*app.state::<QuickState>().error.lock().unwrap());Ok(v)}
    }).await.map_err(|e|e.to_string())?
}
#[tauri::command] pub fn quick_snapshot(app:AppHandle)->Value{app.state::<QuickState>().snapshot.lock().unwrap().clone()}
#[tauri::command] pub fn quick_hide(app:AppHandle){if let Some(w)=app.get_webview_window("quick"){let _=w.hide();}}
#[tauri::command] pub async fn quick_copy(app:AppHandle,text:String)->Result<(),String>{
    if text.len()>1000000{return Err("复制内容过长".into())}
    tauri::async_runtime::spawn_blocking(move||helper(&app,&["--copy".into()],Some(text)).map(|_|())).await.map_err(|e|e.to_string())?
}
pub fn start(app:&AppHandle){
    let _=APP.set(app.clone());
    #[cfg(windows)] {let a=app.clone();std::thread::spawn(move||unsafe {
        if win::SetWindowsHookExW(14,mouse_hook,0,0)==0 {*a.state::<QuickState>().error.lock().unwrap()=Some("无法注册鼠标快捷菜单，请尝试键盘快捷键".into());return}
        let mut message=win::Message::default();while win::GetMessageW(&mut message,0,0,0)>0 {win::TranslateMessage(&message);win::DispatchMessageW(&message);}
    });}
    let a=app.clone();std::thread::spawn(move||{
        match super::rpc_blocking(&a,&a.state::<super::Bridge>(),"quick.settings".into(),json!({})).and_then(|v|apply(&a,v)){
            Ok(_)=>{},Err(e)=>*a.state::<QuickState>().error.lock().unwrap()=Some(e),
        }
        // A transient popup must not add a permanently resident renderer.
        let mut hidden_since=None;
        loop {std::thread::sleep(Duration::from_secs(5));if let Some(w)=a.get_webview_window("quick") {
            if w.is_visible().unwrap_or(true){hidden_since=None}else if hidden_since.get_or_insert_with(Instant::now).elapsed()>Duration::from_secs(30){let _=w.destroy();hidden_since=None;}
        }else{hidden_since=None;}}
    });
}

#[cfg(test)] mod tests{
    use super::position;
    #[test] fn stays_inside_negative_monitor(){assert_eq!(position(-3,1070,630,875,-1920,0,1920,1080),(-630,205));}
    #[test] fn small_monitor(){assert_eq!(position(20,20,400,500,0,0,400,500),(0,0));}
}
