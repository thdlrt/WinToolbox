#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
use serde::Deserialize;
use serde_json::{json,Value};
use std::{collections::HashMap,fs,io::{BufRead,BufReader,Write},path::{Path,PathBuf},process::{Child,ChildStdin,Command,Stdio},sync::{Arc,Mutex,mpsc,atomic::{AtomicBool,AtomicU64,Ordering}},time::Duration};
use tauri::{AppHandle,Manager,Emitter,WebviewUrl,WebviewWindowBuilder};
#[cfg(windows)] use std::os::windows::process::CommandExt;
type Reply=Result<Value,String>;
type Pending=Arc<Mutex<HashMap<u64,mpsc::Sender<Reply>>>>;
struct Backend { child:Child, input:Arc<Mutex<ChildStdin>>, pending:Pending }
#[derive(Default)] struct Bridge { backend:Mutex<Option<Backend>>, seq:AtomicU64 }
#[derive(Default)] struct CaptionsWindow { operation:Mutex<()>, click_through:AtomicBool, recovery_shortcut:AtomicBool }
fn hidden(command:&mut Command)->&mut Command { #[cfg(windows)] command.creation_flags(0x08000000); command }
fn exe_dir()->PathBuf { std::env::current_exe().unwrap_or_default().parent().unwrap_or(Path::new(".")).to_path_buf() }
fn data_dir()->PathBuf {
    if let Some(p)=std::env::var_os("WINTOOLBOX_DATA_DIR") {return PathBuf::from(p)}
    if exe_dir().join("portable.flag").is_file(){return exe_dir().join("data")}
    PathBuf::from(std::env::var_os("LOCALAPPDATA").unwrap_or_else(||".".into())).join("WinToolbox")
}
fn resource_paths(app:&AppHandle)->Result<(PathBuf,PathBuf,PathBuf),String>{
    let resource=app.path().resource_dir().map_err(|e|e.to_string())?;
    let bundled=resource.join("python/python.exe");
    if !cfg!(debug_assertions) && bundled.is_file(){return Ok((bundled,resource.join("core"),resource.join("tools")))}
    let project=PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
    let python=std::env::var_os("WINTOOLBOX_PYTHON").map(PathBuf::from).unwrap_or(project.join("core/.venv/Scripts/python.exe"));
    if !python.is_file(){return Err("缺少 Python 运行环境，请运行 scripts/dev.ps1 或重新安装完整版本。".into())}
    Ok((python,project.join("core"),resource.join("tools")))
}
fn ensure_backend(app:&AppHandle,state:&Bridge)->Result<(Arc<Mutex<ChildStdin>>,Pending),String>{
    let mut guard=state.backend.lock().map_err(|e|e.to_string())?;
    if let Some(b)=guard.as_mut(){ if b.child.try_wait().map_err(|e|e.to_string())?.is_none(){return Ok((b.input.clone(),b.pending.clone()))} }
    let (python,core,tools)=resource_paths(app)?;
    let data=data_dir();fs::create_dir_all(data.join("logs")).map_err(|e|e.to_string())?;
    let log=fs::OpenOptions::new().create(true).append(true).open(data.join("logs/backend.log")).map_err(|e|e.to_string())?;
    let mut cmd=Command::new(&python);
    cmd.args(["-u","-m","toolbox","--data-dir"]).arg(&data).current_dir(&core).env("PYTHONPATH",&core).env("PYTHONUTF8","1").env("WINTOOLBOX_PYTHON",&python).env("WINTOOLBOX_TOOLS",&tools).stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::from(log));
    if tools.join("ffmpeg.exe").is_file(){cmd.env("WINTOOLBOX_FFMPEG",tools.join("ffmpeg.exe")).env("WINTOOLBOX_FFPROBE",tools.join("ffprobe.exe"));}
    let mut child=hidden(&mut cmd).spawn().map_err(|e|format!("后台启动失败：{e}"))?;
    let input=Arc::new(Mutex::new(child.stdin.take().ok_or("后台输入不可用")?));
    let output=child.stdout.take().ok_or("后台输出不可用")?;
    let pending:Pending=Arc::new(Mutex::new(HashMap::new()));let answers=pending.clone();let handle=app.clone();
    std::thread::spawn(move||{
        for line in BufReader::new(output).lines(){
            let Ok(line)=line else{break};
            if let Ok(message)=serde_json::from_str::<Value>(&line){
                if let Some(id)=message.get("id").and_then(Value::as_u64){
                    if let Some(tx)=answers.lock().unwrap().remove(&id){let result=if let Some(err)=message.get("error"){Err(err.get("message").and_then(Value::as_str).unwrap_or("后台处理失败").to_string())}else{Ok(message.get("result").cloned().unwrap_or(Value::Null))};let _=tx.send(result);}
                }else if message.get("method").and_then(Value::as_str)==Some("event"){let _=handle.emit("backend-event",message.get("params").cloned().unwrap_or(Value::Null));}
            }
        }
        for (_,tx) in answers.lock().unwrap().drain(){let _=tx.send(Err("后台已退出，请重试。详细原因见设置中的日志目录。".into()));}
        let _=handle.emit("backend-event",json!({"type":"backend.stopped"}));
    });
    *guard=Some(Backend{child,input:input.clone(),pending:pending.clone()});Ok((input,pending))
}
fn rpc_blocking(app:&AppHandle,state:&Bridge,method:String,params:Value)->Reply{
    let (input,pending)=ensure_backend(app,state)?;
    let id=state.seq.fetch_add(1,Ordering::Relaxed)+1;let (tx,rx)=mpsc::channel();pending.lock().unwrap().insert(id,tx);
    let request=json!({"jsonrpc":"2.0","id":id,"method":method,"params":params});
    let sent=(||{let mut pipe=input.lock().map_err(|e|e.to_string())?;writeln!(pipe,"{}",request).map_err(|e|e.to_string())?;pipe.flush().map_err(|e|e.to_string())})();
    if let Err(e)=sent {pending.lock().unwrap().remove(&id);return Err(e)}
    let result=rx.recv_timeout(Duration::from_secs(300)).unwrap_or_else(|_|Err("操作等待超时，请查看当前工具的处理状态或检查网络连接。".into()));pending.lock().unwrap().remove(&id);result
}
#[tauri::command] async fn rpc(app:AppHandle,method:String,params:Option<Value>)->Reply {
    tauri::async_runtime::spawn_blocking(move||rpc_blocking(&app,&app.state::<Bridge>(),method,params.unwrap_or(json!({})))).await.map_err(|e|e.to_string())?
}
#[derive(Deserialize)] struct Filter {name:String,extensions:Vec<String>}
fn dialog(filters:Option<Vec<Filter>>)->rfd::FileDialog{let mut d=rfd::FileDialog::new();if let Some(filters)=filters{for f in filters{d=d.add_filter(f.name,&f.extensions);}}d}
#[tauri::command] async fn pick_files(multiple:Option<bool>,filters:Option<Vec<Filter>>)->Vec<String>{
    tauri::async_runtime::spawn_blocking(move||{let d=dialog(filters);if multiple.unwrap_or(true){d.pick_files().unwrap_or_default().iter().map(|p|p.to_string_lossy().into_owned()).collect()}else{d.pick_file().map(|p|vec![p.to_string_lossy().into_owned()]).unwrap_or_default()}}).await.unwrap_or_default()
}
#[tauri::command] async fn pick_directory()->Option<String>{tauri::async_runtime::spawn_blocking(||rfd::FileDialog::new().pick_folder().map(|p|p.to_string_lossy().into_owned())).await.ok().flatten()}
#[tauri::command] async fn pick_save(default_name:Option<String>,filters:Option<Vec<Filter>>)->Option<String>{tauri::async_runtime::spawn_blocking(move||dialog(filters).set_file_name(default_name.unwrap_or("backup.wtbak".into())).save_file().map(|p|p.to_string_lossy().into_owned())).await.ok().flatten()}
#[tauri::command] fn app_paths(app:AppHandle)->Value{json!({"data_dir":data_dir(),"resource_dir":app.path().resource_dir().ok(),"portable":exe_dir().join("portable.flag").exists()})}
#[tauri::command] fn open_path(path:String)->Result<(),String>{
    let p=PathBuf::from(&path);if !p.is_absolute()||!p.exists(){return Err("只能打开已存在的本地文件或目录。".into())}
    hidden(Command::new("explorer.exe").arg(p)).spawn().map_err(|e|e.to_string())?;Ok(())
}
#[tauri::command] fn open_external(url:String)->Result<(),String>{
    let parsed=tauri::Url::parse(&url).map_err(|_|"网页地址无效。".to_string())?;
    if !matches!(parsed.scheme(),"http"|"https")||parsed.host_str().is_none()||!parsed.username().is_empty()||parsed.password().is_some(){return Err("只支持普通 HTTP/HTTPS 网页链接。".into())}
    hidden(Command::new("explorer.exe").arg(parsed.as_str())).spawn().map_err(|e|e.to_string())?;Ok(())
}
#[tauri::command] async fn save_course_file(default_name:String,bytes:Vec<u8>)->Result<Option<String>,String>{
    if bytes.len()>16*1024*1024{return Err("导出文件不能超过 16 MiB。".into())}
    let extension=PathBuf::from(&default_name).extension().and_then(|e|e.to_str()).unwrap_or("").to_lowercase();
    if !matches!(extension.as_str(),"md"|"webm"|"m4a")||default_name.contains(['/', '\\', ':']){return Err("不支持的课程导出文件类型。".into())}
    tauri::async_runtime::spawn_blocking(move||{
        let Some(path)=rfd::FileDialog::new().set_file_name(default_name).add_filter("课程文件",&[extension.as_str()]).save_file() else{return Ok(None)};
        if path.extension().and_then(|e|e.to_str()).map(|e|e.to_lowercase())!=Some(extension){return Err("请保留课程文件的原扩展名。".into())}
        std::fs::write(&path,bytes).map_err(|e|format!("保存失败：{e}"))?;
        Ok(Some(path.to_string_lossy().into_owned()))
    }).await.map_err(|e|e.to_string())?
}
#[tauri::command] async fn set_overlay(app:AppHandle,visible:bool)->Result<(),String>{
    if let Some(w)=app.get_webview_window("overlay"){return if visible{w.show()}else{w.hide()}.map_err(|e|e.to_string())}
    if visible{WebviewWindowBuilder::new(&app,"overlay",WebviewUrl::App("index.html?overlay=1".into())).title("WinToolbox · 实时提词").inner_size(560.0,640.0).min_inner_size(380.0,320.0).always_on_top(true).build().map_err(|e|e.to_string())?;}Ok(())
}
fn captions_window_state(app:&AppHandle)->Value {
    let state=app.state::<CaptionsWindow>();
    json!({"visible":app.get_webview_window("captions").and_then(|w|w.is_visible().ok()).unwrap_or(false),"click_through":state.click_through.load(Ordering::Relaxed),"recovery_shortcut_available":state.recovery_shortcut.load(Ordering::Relaxed)})
}
fn emit_captions_window(app:&AppHandle) {
    let mut state=captions_window_state(app);state["type"]=json!("captions.window");let _=app.emit("backend-event",state);
}
#[tauri::command] fn get_captions_window(app:AppHandle)->Value {captions_window_state(&app)}
fn captions_bounds(x:i32,y:i32,width:u32,height:u32,scale:f64)->(u32,u32,i32,i32) {
    let scale=if scale.is_finite() && scale>0.0 {scale}else{1.0};
    let margin=(24.0*scale).round() as u32;
    let w=((900.0*scale).round() as u32).min(width.saturating_sub(margin*2)).max(1);
    let h=((190.0*scale).round() as u32).min(height.saturating_sub(margin*2)).max(1);
    (w,h,x+width.saturating_sub(w) as i32/2,y+height.saturating_sub(h+margin) as i32)
}
#[tauri::command] async fn set_captions_window(app:AppHandle,visible:bool)->Result<(),String> {
    // This command must stay async: WebView creation cannot run on the event-loop thread.
    let state=app.state::<CaptionsWindow>();
    let _operation=state.operation.lock().map_err(|e|e.to_string())?;
    if let Some(w)=app.get_webview_window("captions") {
        if visible {
            w.set_ignore_cursor_events(false).map_err(|e|e.to_string())?;
            state.click_through.store(false,Ordering::Relaxed);
            w.show().map_err(|e|e.to_string())?;
        }else{w.hide().map_err(|e|e.to_string())?;}
        emit_captions_window(&app);return Ok(())
    }
    if !visible {return Ok(())}
    let monitor=app.get_webview_window("main").and_then(|w|w.current_monitor().ok().flatten()).or_else(||app.primary_monitor().ok().flatten());
    let w=WebviewWindowBuilder::new(&app,"captions",WebviewUrl::App("index.html?captions=1".into()))
        .title("WinToolbox · 实时字幕").inner_size(900.0,190.0).min_inner_size(380.0,120.0)
        .transparent(true).decorations(false).shadow(false).always_on_top(true)
        .skip_taskbar(true).focused(false).focusable(false).visible(false).resizable(true)
        .build().map_err(|e|e.to_string())?;
    if let Some(monitor)=monitor {
        let area=monitor.work_area();
        let (width,height,x,y)=captions_bounds(area.position.x,area.position.y,area.size.width,area.size.height,monitor.scale_factor());
        w.set_size(tauri::PhysicalSize::new(width,height)).map_err(|e|e.to_string())?;
        w.set_position(tauri::PhysicalPosition::new(x,y)).map_err(|e|e.to_string())?;
    }
    let handle=app.clone();
    w.on_window_event(move|event|{
        if let tauri::WindowEvent::CloseRequested{api,..}=event {
            api.prevent_close();
            if let Some(window)=handle.get_webview_window("captions"){let _=window.hide();}
            handle.state::<CaptionsWindow>().click_through.store(false,Ordering::Relaxed);
            emit_captions_window(&handle);
            let h=handle.clone();std::thread::spawn(move||{let _=rpc_blocking(&h,&h.state::<Bridge>(),"captions.stop".into(),json!({}));});
        }
    });
    w.show().map_err(|e|e.to_string())?;emit_captions_window(&app);Ok(())
}
#[tauri::command] async fn set_captions_click_through(app:AppHandle,enabled:bool)->Result<(),String> {
    let state=app.state::<CaptionsWindow>();
    let _operation=state.operation.lock().map_err(|e|e.to_string())?;
    let w=app.get_webview_window("captions").ok_or("请先显示字幕窗口")?;
    w.set_ignore_cursor_events(enabled).map_err(|e|e.to_string())?;
    state.click_through.store(enabled,Ordering::Relaxed);emit_captions_window(&app);Ok(())
}
fn shutdown(app:&AppHandle){
    if let Some(mut b)=app.state::<Bridge>().backend.lock().unwrap().take(){
        if b.child.try_wait().ok().flatten().is_none(){
            let (tx,rx)=mpsc::channel();let id=u64::MAX;b.pending.lock().unwrap().insert(id,tx);
            if let Ok(mut input)=b.input.lock(){let _=writeln!(input,"{}",json!({"jsonrpc":"2.0","id":id,"method":"app.prepare_exit","params":{}}));let _=input.flush();}
            let _=rx.recv_timeout(Duration::from_secs(6));
            let _=hidden(Command::new("taskkill.exe").args(["/PID",&b.child.id().to_string(),"/T","/F"])).output();let _=b.child.wait();
        }
    }
}
fn main(){
    use tauri::menu::{Menu,MenuItem};use tauri::tray::TrayIconBuilder;
    use tauri_plugin_global_shortcut::{GlobalShortcutExt,ShortcutState};
    let app=tauri::Builder::default().manage(Bridge::default()).manage(CaptionsWindow::default())
      .plugin(tauri_plugin_single_instance::init(|app,_,_|{if let Some(w)=app.get_webview_window("main"){let _=w.show();let _=w.set_focus();}}))
      .plugin(tauri_plugin_global_shortcut::Builder::new().build())
      .invoke_handler(tauri::generate_handler![rpc,pick_files,pick_directory,pick_save,app_paths,open_path,open_external,save_course_file,set_overlay,get_captions_window,set_captions_window,set_captions_click_through])
      .setup(|app|{
          if let Some(main)=app.get_webview_window("main") {
              let handle=app.handle().clone();
              main.on_window_event(move|event|{
                  if let tauri::WindowEvent::CloseRequested{api,..}=event {
                      api.prevent_close();
                      // Closing the main window keeps background tools alive; tray Quit exits.
                      if let Some(window)=handle.get_webview_window("main") { let _=window.hide(); }
                  }
              });
          }
          let show=MenuItem::with_id(app,"show","打开 WinToolbox",true,None::<&str>)?;let overlay=MenuItem::with_id(app,"overlay","显示实时悬浮窗",true,None::<&str>)?;let quit=MenuItem::with_id(app,"quit","退出",true,None::<&str>)?;
          let captions=MenuItem::with_id(app,"captions","显示并解锁字幕窗",true,None::<&str>)?;
          let stop_captions=MenuItem::with_id(app,"stop_captions","停止实时字幕",true,None::<&str>)?;
          let menu=Menu::with_items(app,&[&show,&overlay,&captions,&stop_captions,&quit])?;
          let mut tray=TrayIconBuilder::new().tooltip("WinToolbox").menu(&menu).on_menu_event(|app,event|{match event.id.as_ref(){"show"=>{if let Some(w)=app.get_webview_window("main"){let _=w.show();let _=w.set_focus();}},"overlay"=>{let handle=app.clone();tauri::async_runtime::spawn(async move{let _=set_overlay(handle,true).await;});},"captions"=>{let handle=app.clone();tauri::async_runtime::spawn(async move{let _=set_captions_window(handle,true).await;});},"stop_captions"=>{let handle=app.clone();std::thread::spawn(move||{let _=rpc_blocking(&handle,&handle.state::<Bridge>(),"captions.stop".into(),json!({}));});},"quit"=>app.exit(0),_=>{}}});
          if let Some(icon)=app.default_window_icon(){tray=tray.icon(icon.clone())}tray.build(app)?;
          for (shortcut,action) in [("Ctrl+Alt+Space","answer"),("Ctrl+Alt+Escape","cancel"),("Ctrl+Alt+ArrowLeft","previous")]{
              let a=action.to_string();let _=app.global_shortcut().on_shortcut(shortcut,move|app,_,event|{if event.state==ShortcutState::Pressed{let _=app.emit("backend-event",json!({"type":"hotkey","action":a}));if a!="previous"{let h=app.clone();let m=if a=="answer"{"live.answer"}else{"live.cancel"}.to_string();std::thread::spawn(move||{let _=rpc_blocking(&h,&h.state::<Bridge>(),m,json!({}));});}}});
          }
          let recovery=app.global_shortcut().on_shortcut("Ctrl+Alt+L",move|app,_,event|{
              if event.state==ShortcutState::Pressed {let handle=app.clone();tauri::async_runtime::spawn(async move{let _=set_captions_window(handle,true).await;});}
          });
          app.state::<CaptionsWindow>().recovery_shortcut.store(recovery.is_ok(),Ordering::Relaxed);
          let _=app.global_shortcut().on_shortcut("Ctrl+Alt+C",move|app,_,event|{
              if event.state==ShortcutState::Pressed {
                  let visible=app.get_webview_window("captions").and_then(|w|w.is_visible().ok()).unwrap_or(false);
                  let handle=app.clone();tauri::async_runtime::spawn(async move{let _=set_captions_window(handle,!visible).await;});
              }
          });
          Ok(())
      }).build(tauri::generate_context!()).expect("WinToolbox 启动失败");
    app.run(|app,event|{if matches!(event,tauri::RunEvent::Exit){shutdown(app)}});
}

#[cfg(test)] mod caption_window_tests {
    use super::captions_bounds;
    #[test] fn default_bottom_position_stays_in_work_area(){assert_eq!(captions_bounds(0,0,1920,1040,1.0),(900,190,510,826));}
    #[test] fn negative_monitor_origin_and_dpi(){assert_eq!(captions_bounds(-2560,0,2560,1400,1.5),(1350,285,-1955,1079));}
    #[test] fn small_monitor_is_clamped(){let (w,h,x,y)=captions_bounds(30,40,600,220,1.0);assert!(w<=600&&h<=220&&x>=30&&y>=40&&x+w as i32<=630&&y+h as i32<=260);}
}
