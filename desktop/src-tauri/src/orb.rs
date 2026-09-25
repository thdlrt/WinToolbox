use tauri::{AppHandle,Manager,Emitter,WebviewUrl,WebviewWindowBuilder,PhysicalPosition,LogicalSize};
use serde_json::json;
use std::fs;
use std::sync::Mutex;
#[derive(Default)] pub struct OrbState { anchor:Mutex<Option<(i32,i32)>> }

pub fn open(app:&AppHandle,page:&str)->Result<(),String>{
    if let Some(w)=app.get_webview_window("main"){w.show().map_err(|e|e.to_string())?;let _=w.unminimize();let _=w.set_focus();w.emit("tool-open",json!({"page":page})).map_err(|e|e.to_string())?;}
    if let Some(w)=app.get_webview_window("orb"){let _=w.hide();}
    Ok(())
}

pub fn clamp(x:i32,y:i32,w:i32,h:i32,left:i32,top:i32,right:i32,bottom:i32)->(i32,i32){
    (x.clamp(left,(right-w).max(left)),y.clamp(top,(bottom-h).max(top)))
}

#[tauri::command]
pub async fn set_orb(app:AppHandle,visible:bool)->Result<(),String>{
    if let Some(w)=app.get_webview_window("orb"){if visible{w.emit("orb-reset",()).map_err(|e|e.to_string())?;return w.show().map_err(|e|e.to_string())}return w.hide().map_err(|e|e.to_string())}
    if !visible{return Ok(())}
    let saved=fs::read(super::data_dir().join("orb-position.json")).ok().and_then(|b|serde_json::from_slice::<serde_json::Value>(&b).ok());
    let monitors=app.available_monitors().map_err(|e|e.to_string())?;
    let saved_point=saved.and_then(|v|Some((v["x"].as_i64()? as i32,v["y"].as_i64()? as i32)));
    let monitor=monitors.iter().find(|m|saved_point.map(|(x,y)|{let a=m.work_area();x>=a.position.x&&y>=a.position.y&&x<a.position.x+a.size.width as i32&&y<a.position.y+a.size.height as i32}).unwrap_or(false)).or(monitors.first());
    let placement=monitor.map(|m|{let a=m.work_area();let scale=m.scale_factor();let size=(128.0*scale).round() as i32;let (cx,cy)=saved_point.unwrap_or((a.position.x+a.size.width as i32-size,a.position.y+a.size.height as i32-size*2));let (x,y)=clamp(cx-size/2,cy-size/2,size,size,a.position.x,a.position.y,a.position.x+a.size.width as i32,a.position.y+a.size.height as i32);(x,y,scale)});
    // Wry registers OLE drop targets only while creating the WebView2 children.
    // Initially hidden dynamic windows have no target to register, and show()
    // never repairs it: https://github.com/tauri-apps/tauri/issues/14643.
    // Start visible, already positioned, without activating Explorer's drag source.
    let mut builder=WebviewWindowBuilder::new(&app,"orb",WebviewUrl::App("index.html?orb=1".into())).title("WinToolbox · 悬浮球")
        .inner_size(128.0,128.0).transparent(true).decorations(false).shadow(false).always_on_top(true)
        .skip_taskbar(true).resizable(false).visible(true).focused(false);
    if let Some((x,y,scale))=placement{builder=builder.position(x as f64/scale,y as f64/scale);}
    let w=builder.build().map_err(|e|e.to_string())?;
    if let Some((x,y,_))=placement{w.set_position(PhysicalPosition::new(x,y)).map_err(|e|e.to_string())?;}
    w.show().map_err(|e|e.to_string())
}

#[tauri::command]
pub fn orb_resize(app:AppHandle,expanded:bool,focus:Option<bool>)->Result<(),String>{
    let w=app.get_webview_window("orb").ok_or("悬浮球未打开")?;
    let old=w.outer_size().map_err(|e|e.to_string())?;let pos=w.outer_position().map_err(|e|e.to_string())?;
    let logical=if expanded{320.0}else{128.0};let scale=w.scale_factor().map_err(|e|e.to_string())?;let size=(logical*scale).round() as i32;
    let state=app.state::<OrbState>();let mut anchor=state.anchor.lock().map_err(|e|e.to_string())?;
    let current=(pos.x+old.width as i32/2,pos.y+old.height as i32/2);
    if expanded && old.width <= (129.0*scale) as u32 {*anchor=Some(current);}
    let center=if expanded{current}else{anchor.take().unwrap_or(current)};
    let mut x=center.0-size/2;let mut y=center.1-size/2;
    if let Some(m)=w.current_monitor().map_err(|e|e.to_string())?{let a=m.work_area();(x,y)=clamp(x,y,size,size,a.position.x,a.position.y,a.position.x+a.size.width as i32,a.position.y+a.size.height as i32);}
    w.set_size(LogicalSize::new(logical,logical)).map_err(|e|e.to_string())?;w.set_position(PhysicalPosition::new(x,y)).map_err(|e|e.to_string())?;
    // Menus need activation to receive a later focus-loss notification. A file
    // drag must keep Explorer focused throughout its native OLE drag operation.
    if expanded && focus.unwrap_or(false){w.set_focus().map_err(|e|e.to_string())?;}
    Ok(())
}

#[tauri::command]
pub fn orb_save_position(app:AppHandle)->Result<(),String>{
    let w=app.get_webview_window("orb").ok_or("悬浮球未打开")?;let mut p=w.outer_position().map_err(|e|e.to_string())?;let s=w.outer_size().map_err(|e|e.to_string())?;
    if let Some(m)=w.current_monitor().map_err(|e|e.to_string())?{let a=m.work_area();let (x,y)=clamp(p.x,p.y,s.width as i32,s.height as i32,a.position.x,a.position.y,a.position.x+a.size.width as i32,a.position.y+a.size.height as i32);if x!=p.x||y!=p.y{p=PhysicalPosition::new(x,y);w.set_position(p).map_err(|e|e.to_string())?;}}
    fs::create_dir_all(super::data_dir()).map_err(|e|e.to_string())?;
    let state=app.state::<OrbState>();let anchor=state.anchor.lock().map_err(|e|e.to_string())?;let (x,y)=anchor.unwrap_or((p.x+s.width as i32/2,p.y+s.height as i32/2));
    fs::write(super::data_dir().join("orb-position.json"),json!({"x":x,"y":y}).to_string()).map_err(|e|e.to_string())
}

#[tauri::command]
pub async fn orb_action(app:AppHandle,action:String)->Result<(),String>{
    match action.as_str(){
        "home"|"relay"|"captions"|"settings"|"ram"|"orb-settings"|"filesync"|"memory"|"media"|"live"|"practice"|"phonetics"|"expenses"|"shizuku"|"fnconnect"|"gpu"|"codex"|"files"|"plugins"=>open(&app,&action),
        "caption-window"=>super::set_captions_window(app,true).await,
        "hide"=>set_orb(app,false).await,
        "quit"=>{app.exit(0);Ok(())},
        _=>Err("未知快捷操作".into())
    }
}

#[cfg(test)] mod tests {
    use super::clamp;
    #[test] fn keeps_negative_monitor_coordinates(){assert_eq!(clamp(-3000,1800,320,320,-2560,0,0,1440),(-2560,1120));}
    #[test] fn clamps_small_work_area(){assert_eq!(clamp(500,500,320,320,0,0,200,200),(0,0));}
}
