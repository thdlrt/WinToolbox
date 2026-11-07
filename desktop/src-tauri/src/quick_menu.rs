use serde_json::{json, Value};
use std::{io::{Read, Write}, process::{Command, Stdio}, sync::{mpsc, Arc, Mutex, OnceLock, atomic::{AtomicBool, AtomicU64, Ordering}}, time::{Duration, Instant}};
use tauri::{AppHandle, Emitter, Manager, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_global_shortcut::{GlobalShortcutExt, ShortcutState};

#[derive(Default)]
pub struct QuickState {
    config: Mutex<Value>, snapshot: Mutex<Value>, shortcut: Mutex<Option<String>>,
    pub toggle: Mutex<Option<tauri::menu::CheckMenuItem<tauri::Wry>>>,
    held: AtomicBool, fired: AtomicBool, generation: AtomicU64,
    selection_generation: AtomicU64,
    press: Mutex<Option<Press>>, selection_press: Mutex<Option<SelectionPress>>,
    last_left_up: Mutex<Option<Click>>, pending_since: Mutex<Option<Instant>>,
    pending_selection: Mutex<Option<Value>>,
    capture_cancel: Mutex<Option<Arc<AtomicBool>>>, window_lock: Mutex<()>,
    pub error: Mutex<Option<String>>,
}
#[derive(Clone, Copy)]
struct Press { button: u32, x: i32, y: i32, forwarded: bool }
#[derive(Clone, Copy)]
struct SelectionPress { source: isize, start_x: i32, start_y: i32, x: i32, y: i32, moved: bool, double_click: bool }
#[derive(Clone, Copy)]
struct Click { time: u32, x: i32, y: i32 }
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
        pub fn GetWindowThreadProcessId(window:isize,pid:*mut u32)->u32;
        pub fn GetCursorPos(point:*mut Point)->i32;
        pub fn GetDoubleClickTime()->u32;
        pub fn GetSystemMetrics(index:i32)->i32;
        pub fn SendInput(count:u32,inputs:*const Input,size:i32)->u32;
    }
    pub unsafe fn inject(button:u32,up:bool) {
        let input=Input{kind:0,mouse:MouseInput{dx:0,dy:0,data:if button==1 {0}else{button-1},flags:if button==1 {if up{0x40}else{0x20}}else if up{0x100}else{0x80},time:0,extra:0x57544251}};
        SendInput(1,&input,std::mem::size_of::<Input>() as i32);
    }
}

fn begin_hold(app: &AppHandle, source: isize, x:i32,y:i32) {
    let state=app.state::<QuickState>();
    cancel_capture(&state);
    state.held.store(true,Ordering::SeqCst); state.fired.store(false,Ordering::SeqCst);
    let generation=state.generation.fetch_add(1,Ordering::SeqCst)+1;
    let delay=Duration::from_millis(state.config.lock().unwrap()["hold_ms"].as_u64().unwrap_or(450));
    let app=app.clone();
    std::thread::spawn(move|| {
        let state=app.state::<QuickState>();
        let started=Instant::now();
        // Press-down already starts the reader and pre-builds the hidden popup, so the
        // .NET/WebView startup overlaps the hold instead of delaying the menu after it.
        let (cancel,receiver)=start_capture(&app,source);
        *state.capture_cancel.lock().unwrap()=Some(cancel.clone());
        if !state.held.load(Ordering::SeqCst) || state.generation.load(Ordering::SeqCst)!=generation {cancel.store(true,Ordering::SeqCst);return}
        if let Err(error)=prepare_window(&app){eprintln!("快捷菜单预建失败: {error}")}
        if let Some(remain)=delay.checked_sub(started.elapsed()){std::thread::sleep(remain);}
        if state.held.load(Ordering::SeqCst) && state.generation.load(Ordering::SeqCst)==generation {
            state.fired.store(true,Ordering::SeqCst);
            trigger(&app,source,x,y,generation,receiver);
        }
    });
}

fn selection_watch_enabled(config:&Value)->bool {
    config["enabled"]==true && config["auto_translate"]==true && config["selection_translate"]==true
}

#[cfg(windows)] unsafe fn own_window(source:isize)->bool {
    let mut pid=0;win::GetWindowThreadProcessId(source,&mut pid);pid==std::process::id()
}

const SELECTION_PENDING_TTL: Duration = Duration::from_secs(60);

fn pending_elapsed_ok(elapsed: Duration) -> bool { elapsed <= SELECTION_PENDING_TTL }

/// Consume the remembered mouse selection, if it is still fresh and translatable.
fn take_pending(state:&QuickState)->Option<Value> {
    let fresh={
        let mut guard=state.pending_since.lock().unwrap();
        let fresh=guard.as_ref().map(|t|pending_elapsed_ok(t.elapsed())).unwrap_or(false);
        *guard=None;  // A pending selection is single-use.
        fresh
    };
    let snapshot=state.pending_selection.lock().unwrap().take()?;
    if !fresh {return None}
    if snapshot["foreign"]==true && snapshot["protected"]!=true {Some(snapshot)}else{None}
}

fn begin_selection(app:&AppHandle,source:isize,generation:u64) {
    let app=app.clone();
    std::thread::spawn(move||{
        // Wait for the target application to publish its final UIA selection.
        std::thread::sleep(Duration::from_millis(180));
        let state=app.state::<QuickState>();
        if state.selection_generation.load(Ordering::SeqCst)==generation && selection_watch_enabled(&state.config.lock().unwrap()) {
            remember_selection(&app,source,generation);
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
            if !state.fired.load(Ordering::SeqCst) {cancel_capture(&state);}
            if p.forwarded {return win::CallNextHookEx(0,code,wparam,lparam)}
            if !state.fired.load(Ordering::SeqCst) {win::inject(button,false);win::inject(button,true);}
            return 1;
        }
    }
    if wparam==0x200 {
        let mut guard=state.press.lock().unwrap();
        if let Some(p)=guard.as_mut() {
            if !p.forwarded && !state.fired.load(Ordering::SeqCst) && ((mouse.pt.x-p.x).abs()>8 || (mouse.pt.y-p.y).abs()>8) {
                p.forwarded=true;state.held.store(false,Ordering::SeqCst);state.generation.fetch_add(1,Ordering::SeqCst);cancel_capture(&state);win::inject(p.button,false);
            }
        }
        drop(guard);
        if let Some(p)=state.selection_press.lock().unwrap().as_mut() {
            p.x=mouse.pt.x;p.y=mouse.pt.y;
            if (p.x-p.start_x).abs()>4 || (p.y-p.start_y).abs()>4 {p.moved=true;}
        }
    }
    if wparam==0x201 {
        let source=win::GetForegroundWindow();
        state.selection_generation.fetch_add(1,Ordering::SeqCst);
        let enabled=selection_watch_enabled(&state.config.lock().unwrap()) && source!=0 && !own_window(source);
        if enabled {
            let previous=*state.last_left_up.lock().unwrap();
            let double_click=previous.map(|p|mouse.time.wrapping_sub(p.time)<=win::GetDoubleClickTime()
                && (mouse.pt.x-p.x).abs()<=win::GetSystemMetrics(36).max(4)
                && (mouse.pt.y-p.y).abs()<=win::GetSystemMetrics(37).max(4)).unwrap_or(false);
            *state.selection_press.lock().unwrap()=Some(SelectionPress{source,start_x:mouse.pt.x,start_y:mouse.pt.y,x:mouse.pt.x,y:mouse.pt.y,moved:false,double_click});
        } else {*state.selection_press.lock().unwrap()=None;}
    }
    if wparam==0x202 {
        *state.last_left_up.lock().unwrap()=Some(Click{time:mouse.time,x:mouse.pt.x,y:mouse.pt.y});
        if let Some(p)=state.selection_press.lock().unwrap().take() {
            if (p.moved||p.double_click) && win::GetForegroundWindow()==p.source && selection_watch_enabled(&state.config.lock().unwrap()) {
                let generation=state.selection_generation.load(Ordering::SeqCst);
                begin_selection(app,p.source,generation);
            }
            if !(p.moved||p.double_click) {*state.pending_since.lock().unwrap()=None;}  // A plain click drops the remembered selection.
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
        std::thread::sleep(Duration::from_millis(10));
    }
}

fn cancel_capture(state:&QuickState) {
    if let Some(cancel)=state.capture_cancel.lock().unwrap().take(){cancel.store(true,Ordering::SeqCst);}
}

/// Spawn the selection reader immediately and report its result asynchronously.
/// `cancel` kills the child process when the press turns into a normal click.
fn start_capture(app:&AppHandle,source:isize)->(Arc<AtomicBool>,mpsc::Receiver<Result<Value,String>>) {
    let cancel=Arc::new(AtomicBool::new(false));
    let (sender,receiver)=mpsc::channel();
    let watcher=cancel.clone();let app=app.clone();
    std::thread::spawn(move||{
        let result=(||->Result<Value,String>{
            if watcher.load(Ordering::SeqCst){return Err("cancelled".into())}
            let (_,_,tools)=super::resource_paths(&app)?;
            let exe=tools.join("quick-context/WinToolbox.QuickContext.exe");
            if !exe.is_file(){return Err("缺少选区读取组件，请更新完整便携版".into())}
            let mut command=Command::new(&exe);
            command.args(["--capture".into(),source.to_string()]).stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::null());
            let mut child=super::hidden(&mut command).spawn().map_err(|e|e.to_string())?;
            if watcher.load(Ordering::SeqCst){let _=child.kill();let _=child.wait();return Err("cancelled".into())}
            let out=child.stdout.take().ok_or("选区输出不可用")?;
            let reader=std::thread::spawn(move||{let mut data=String::new();out.take(1024*1024).read_to_string(&mut data).map(|_|data)});
            let start=Instant::now();
            loop {
                if watcher.load(Ordering::SeqCst){let _=child.kill();let _=child.wait();return Err("cancelled".into())}
                if let Some(status)=child.try_wait().map_err(|e|e.to_string())? {
                    let data=reader.join().map_err(|_|"选区读取失败".to_string())?.map_err(|e|e.to_string())?;
                    if !status.success(){return Err("无法完成剪贴板操作，请重试".into())}
                    return serde_json::from_str(data.trim_start_matches('\u{feff}').trim()).map_err(|_|"选区读取结果无效".into());
                }
                if start.elapsed()>Duration::from_secs(4){let _=child.kill();let _=child.wait();return Err("此应用响应较慢，未能读取选区；请重试".into())}
                std::thread::sleep(Duration::from_millis(10));
            }
        })();
        let _=sender.send(result);
    });
    (cancel,receiver)
}

/// A slow UIA provider must not keep the fixed menu entries hidden. Keep the
/// source focused while the provisional popup is visible so capture can finish.
fn await_capture(receiver:&mpsc::Receiver<Result<Value,String>>,on_pending:impl FnOnce()->Result<(),String>)->Result<Result<Value,String>,String> {
    match receiver.try_recv() {
        Ok(result)=>Ok(result),
        Err(mpsc::TryRecvError::Disconnected)=>Ok(Err("选区读取已取消".into())),
        Err(mpsc::TryRecvError::Empty)=>{
            on_pending()?;
            Ok(receiver.recv().unwrap_or_else(|_|Err("选区读取已取消".into())))
        }
    }
}

fn visible_context(app:&AppHandle,id:&Value)->bool {
    let matches={let state=app.state::<QuickState>();let current=state.snapshot.lock().unwrap();current["id"]==*id};
    matches && app.get_webview_window("quick").map(|w|w.is_visible().unwrap_or(false)).unwrap_or(false)
}

/// Read delayed-rendered clipboard data only after the popup is already usable.
fn refresh_clipboard(app:&AppHandle,id:Value) {
    let Ok(mut snapshot)=super::rpc_blocking(app,&app.state::<super::Bridge>(),"quick.clipboard".into(),json!({"id":id})) else{return};
    if !visible_context(app,&id){return}
    let state=app.state::<QuickState>();
    let mut current=state.snapshot.lock().unwrap();
    if current["id"]!=id{return}
    if let Some(job)=current.get("initial_job"){snapshot["initial_job"]=job.clone();}
    *current=snapshot.clone();
    drop(current);
    if let Some(window)=app.get_webview_window("quick"){let _=window.emit("quick-context",snapshot);}
}

fn trigger(app:&AppHandle,source:isize,x:i32,y:i32,generation:u64,receiver:mpsc::Receiver<Result<Value,String>>) {
    let state=app.state::<QuickState>();
    let result=(||->Result<(),String>{
        let mut provisional=None;
        let capture_result=await_capture(&receiver,||{
            if state.generation.load(Ordering::SeqCst)!=generation {return Ok(())}
            let mut snapshot=super::rpc_blocking(app,&app.state::<super::Bridge>(),"quick.capture".into(),json!({"clipboard":false}))?;
            if state.generation.load(Ordering::SeqCst)!=generation {return Ok(())}
            snapshot["capture_pending"]=json!(true);
            provisional=Some(snapshot["id"].clone());
            show_snapshot(app,snapshot,x,y,false)
        })?;
        let current=||{
            let enabled=state.config.lock().unwrap()["enabled"]==true;
            state.generation.load(Ordering::SeqCst)==generation && enabled
                && provisional.as_ref().map(|id|visible_context(app,id)).unwrap_or(true)
        };
        if !current(){return Ok(())}
        #[cfg(windows)] if provisional.is_some() && unsafe{let foreground=win::GetForegroundWindow();foreground!=source && !own_window(foreground)} {
            if let Some(window)=app.get_webview_window("quick"){let _=window.hide();}
            return Ok(())
        }
        let mut capture=capture_result.unwrap_or_else(|e|json!({"text":"","paths":[],"message":e}));
        if !capture.is_object(){capture=json!({})}
        let live_empty=capture["text"].as_str().map(str::trim).unwrap_or("").is_empty()
            && capture["paths"].as_array().map(Vec::is_empty).unwrap_or(true);
        let pending=if live_empty {take_pending(&state)}else{*state.pending_since.lock().unwrap()=None;None};
        let snapshot=if let Some(pending)=pending {
            super::rpc_blocking(app,&app.state::<super::Bridge>(),"quick.snapshot".into(),json!({"id":pending["id"]})).unwrap_or(pending)
        }else{
            capture["clipboard"]=json!(false);
            super::rpc_blocking(app,&app.state::<super::Bridge>(),"quick.capture".into(),capture)?
        };
        if !current(){return Ok(())}
        let id=snapshot["id"].clone();
        show_snapshot(app,snapshot,x,y,true)?;
        refresh_clipboard(app,id);
        Ok(())
    })();
    if let Err(error)=result {*state.error.lock().unwrap()=Some(error.clone());let _=app.emit("backend-event",json!({"type":"quick.error","message":error}));}
}

/// Remember a mouse-selected foreign text in the background without opening any window.
fn remember_selection(app:&AppHandle,source:isize,generation:u64) {
    let state=app.state::<QuickState>();
    // Background retries run independently of explicit menu triggers.
    let result=(||->Result<(),String>{
        *state.pending_since.lock().unwrap()=None;
        // Unlike the clipboard action, selection watching never reads or changes the clipboard.
        let mut capture=json!({"text":"","paths":[]});
        for attempt in 0..3 {
            capture=helper(app,&["--capture".into(),source.to_string()],None).unwrap_or_else(|_|json!({"text":"","paths":[]}));
            if state.selection_generation.load(Ordering::SeqCst)!=generation || !selection_watch_enabled(&state.config.lock().unwrap()) {return Ok(())}
            if capture.is_object() && !capture["text"].as_str().map(str::trim).unwrap_or("").is_empty() {break}
            // First-time UIA clients often land before the target app publishes its
            // accessibility tree (browsers in particular); retrying on the same
            // gesture is silent and keeps the remembered selection available.
            if attempt+1<3 {std::thread::sleep(Duration::from_millis(350));}
        }
        if !capture.is_object() || capture["text"].as_str().map(str::trim).unwrap_or("").is_empty() {return Ok(())}
        capture["clipboard"]=json!(false);
        let snapshot=super::rpc_blocking(app,&app.state::<super::Bridge>(),"quick.capture".into(),capture)?;
        if snapshot["foreign"]!=true || snapshot["protected"]==true {return Ok(())}
        if state.selection_generation.load(Ordering::SeqCst)!=generation || !selection_watch_enabled(&state.config.lock().unwrap()) {return Ok(())}
        *state.pending_selection.lock().unwrap()=Some(snapshot);
        *state.pending_since.lock().unwrap()=Some(Instant::now());
        Ok(())
    })();
    // A silent background watcher must not raise error popups on unsupported windows.
    if let Err(error)=result {eprintln!("划词选区未记录: {error}");}
}

fn quick_window(app:&AppHandle,state:&QuickState)->Result<tauri::WebviewWindow<tauri::Wry>,String> {
    let _guard=state.window_lock.lock().unwrap();
    if let Some(w)=app.get_webview_window("quick"){return Ok(w);}
    let w=WebviewWindowBuilder::new(app,"quick",WebviewUrl::App("index.html?quick=1".into()))
        .title("WinToolbox · 快捷菜单").inner_size(360.0,500.0).resizable(false).decorations(false)
        .always_on_top(true).skip_taskbar(true).focused(false).visible(false).build().map_err(|e|e.to_string())?;
    let handle=app.clone();let focused=Arc::new(AtomicBool::new(false));
    w.on_window_event(move|event|{
        if matches!(event,tauri::WindowEvent::Focused(true)){focused.store(true,Ordering::SeqCst);}
        if matches!(event,tauri::WindowEvent::Focused(false)) && focused.swap(false,Ordering::SeqCst) {
            let h=handle.clone();
            std::thread::spawn(move||{std::thread::sleep(Duration::from_millis(200));if let Some(w)=h.get_webview_window("quick"){if !w.is_focused().unwrap_or(true){let _=w.hide();}}});
        }
    });
    Ok(w)
}

/// Build the hidden popup during the hold so showing it later is nearly instant.
fn prepare_window(app:&AppHandle)->Result<(),String> {
    if app.get_webview_window("quick").is_some(){return Ok(())}
    let state=app.state::<QuickState>();
    let window=quick_window(app,&state)?;
    // A freshly mounted hidden page must not flash the previous context.
    *state.snapshot.lock().unwrap()=json!({});
    super::webview_memory::set_background(&window,true);
    Ok(())
}

fn show_snapshot(app:&AppHandle,snapshot:Value,x:i32,y:i32,focus:bool)->Result<(),String>{
        let state=app.state::<QuickState>();
        *state.snapshot.lock().unwrap()=snapshot.clone();
        let window=quick_window(app,&state)?;
        let monitors=app.available_monitors().map_err(|e|e.to_string())?;
        if let Some(m)=monitors.iter().find(|m|{let a=m.work_area();x>=a.position.x&&x<a.position.x+a.size.width as i32&&y>=a.position.y&&y<a.position.y+a.size.height as i32}).or(monitors.first()) {
            let a=m.work_area();let scale=m.scale_factor();let width=(360.0*scale).round() as u32;let height=(500.0*scale).round() as u32;
            let w=width.min(a.size.width);let h=height.min(a.size.height);
            window.set_size(tauri::PhysicalSize::new(w,h)).map_err(|e|e.to_string())?;
            let (left,top)=position(x,y,w,h,a.position.x,a.position.y,a.size.width,a.size.height);
            window.set_position(tauri::PhysicalPosition::new(left,top)).map_err(|e|e.to_string())?;
        }
        super::webview_memory::set_background(&window,false);
        window.set_focusable(focus).map_err(|e|e.to_string())?;
        window.show().map_err(|e|e.to_string())?;if focus{let _=window.set_focus();}
        let _=window.emit("quick-context",snapshot);
        Ok(())
}

pub fn file_locks_launch(app:&AppHandle,args:&[String])->bool {
    if args.get(1).map(String::as_str)!=Some("--file-locks"){return false}
    let paths:Vec<String>=args.iter().skip(2).filter(|p|std::path::Path::new(p).is_absolute()).take(100).cloned().collect();
    let handle=app.clone();
    std::thread::spawn(move||{
        let result=(||->Result<(),String>{
            let mut snapshot=super::rpc_blocking(&handle,&handle.state::<super::Bridge>(),"quick.capture".into(),json!({"paths":paths,"clipboard":false}))?;
            match super::rpc_blocking(&handle,&handle.state::<super::Bridge>(),"quick.run".into(),json!({"id":snapshot["id"],"action":"file_locks"})) {
                Ok(job)=>snapshot["initial_job"]=job,
                Err(error)=>snapshot["message"]=json!(error),
            }
            #[cfg(windows)] let (x,y)=unsafe{let mut p=win::Point::default();win::GetCursorPos(&mut p);(p.x,p.y)};
            #[cfg(not(windows))] let (x,y)=(50,50);
            let id=snapshot["id"].clone();
            show_snapshot(&handle,snapshot,x,y,true)?;
            refresh_clipboard(&handle,id);
            Ok(())
        })();
        if let Err(error)=result {eprintln!("文件占用菜单打开失败: {error}");let _=super::orb::open(&handle,"quick-settings");let _=handle.emit("backend-event",json!({"type":"quick.error","message":error}));}
    });
    true
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
                if event.state==ShortcutState::Released {s.held.store(false,Ordering::SeqCst);if !s.fired.load(Ordering::SeqCst){cancel_capture(&s);}return}
                if s.held.load(Ordering::SeqCst){return}
                #[cfg(windows)] unsafe {let mut p=win::Point::default();win::GetCursorPos(&mut p);begin_hold(app,win::GetForegroundWindow(),p.x,p.y);}
            }).map_err(|e|format!("快捷键无法注册，可能已被占用：{e}"))?;
        }
        if let Some(key)=old.take(){let _=app.global_shortcut().unregister(key.as_str());}
        *old=desired;
    }
    state.generation.fetch_add(1,Ordering::SeqCst);state.held.store(false,Ordering::SeqCst);
    cancel_capture(&state);
    state.selection_generation.fetch_add(1,Ordering::SeqCst);*state.selection_press.lock().unwrap()=None;
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
    use super::{position,selection_watch_enabled,pending_elapsed_ok,take_pending,await_capture,QuickState,SELECTION_PENDING_TTL};
    use serde_json::json;
    use std::{sync::mpsc,time::{Duration,Instant}};

    #[test] fn pending_menu_is_shown_before_slow_capture_completes(){
        let (tx,rx)=mpsc::channel();
        let (shown,visible)=mpsc::channel();
        let worker=std::thread::spawn(move||{
            visible.recv_timeout(Duration::from_secs(2)).expect("menu must show before selection arrives");
            tx.send(Ok(json!({"text":"late selection"}))).unwrap();
        });
        let capture=await_capture(&rx,||{shown.send(()).unwrap();Ok(())}).unwrap().unwrap();
        assert_eq!(capture["text"],"late selection");
        worker.join().unwrap();
    }
    #[test] fn provisional_popup_failure_is_reported(){
        let (_tx,rx)=mpsc::channel();
        assert_eq!(await_capture(&rx,||Err("popup unavailable".into())).unwrap_err(),"popup unavailable");
    }
    #[test] fn ready_capture_does_not_show_provisional_context(){
        let (tx,rx)=mpsc::channel();
        tx.send(Ok(json!({"text":"ready"}))).unwrap();
        assert_eq!(await_capture(&rx,||panic!("ready selection needs no loading menu")).unwrap().unwrap()["text"],"ready");
    }
    #[test] fn disconnected_capture_does_not_wait(){
        let (tx,rx)=mpsc::channel();drop(tx);
        assert!(await_capture(&rx,||panic!("reader already stopped")).unwrap().unwrap_err().contains("取消"));
    }
    #[test] fn prebuilding_popup_does_not_erase_remembered_selection(){
        let state=QuickState::default();
        *state.pending_since.lock().unwrap()=Some(Instant::now());
        *state.pending_selection.lock().unwrap()=Some(json!({"id":"remembered","foreign":true}));
        *state.snapshot.lock().unwrap()=json!({});
        assert_eq!(take_pending(&state).unwrap()["id"],"remembered");
        assert!(take_pending(&state).is_none());
    }
    #[test] fn stays_inside_negative_monitor(){assert_eq!(position(-3,1070,630,875,-1920,0,1920,1080),(-630,205));}
    #[test] fn small_monitor(){assert_eq!(position(20,20,400,500,0,0,400,500),(0,0));}
    #[test] fn selection_watch_requires_all_switches(){
        let config=json!({"enabled":true,"auto_translate":true,"selection_translate":true});
        assert!(selection_watch_enabled(&config));
        for key in ["enabled","auto_translate","selection_translate"] {
            let mut changed=config.clone();changed[key]=json!(false);
            assert!(!selection_watch_enabled(&changed),"watcher must stop when {key} is disabled");
        }
    }
    #[test] fn remembered_selection_expires(){
        assert!(pending_elapsed_ok(Duration::from_secs(59)));
        assert!(pending_elapsed_ok(SELECTION_PENDING_TTL));
        assert!(!pending_elapsed_ok(SELECTION_PENDING_TTL+Duration::from_secs(1)));
    }
}
