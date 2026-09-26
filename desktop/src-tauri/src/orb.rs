use tauri::{AppHandle,Manager,Emitter,WebviewUrl,WebviewWindowBuilder,PhysicalPosition};
use serde_json::json;
use std::fs;
use std::sync::Mutex;
const SURFACE: f64 = 512.0;
const EXPANDED: f64 = 320.0;
const COMPACT: f64 = 128.0;
#[derive(Clone, Copy, serde::Serialize)]
pub struct OrbLayout { x:f64, y:f64, menu_x:f64, menu_y:f64 }
impl Default for OrbLayout { fn default()->Self {Self{x:256.0,y:256.0,menu_x:256.0,menu_y:256.0}} }
#[derive(Default)] pub struct OrbState { layout:Mutex<OrbLayout>, expanded:Mutex<bool> }

// Keep WebView2's swap chain and screen position stable throughout morphs.
// Only the native hit/draw region changes, after the renderer finishes closing.
#[cfg(windows)]
fn region(w:&tauri::WebviewWindow,layout:OrbLayout,expanded:bool)->Result<(),String>{
    use std::ffi::c_void;
    #[link(name="gdi32")] extern "system" { fn CreateRectRgn(l:i32,t:i32,r:i32,b:i32)->*mut c_void; fn DeleteObject(object:*mut c_void)->i32; }
    #[link(name="user32")] extern "system" { fn SetWindowRgn(hwnd:*mut c_void,rgn:*mut c_void,redraw:i32)->i32; }
    let scale=w.scale_factor().map_err(|e|e.to_string())?;
    let (x,y,size)=if expanded{(layout.menu_x-EXPANDED/2.0,layout.menu_y-EXPANDED/2.0,EXPANDED)}else{(layout.x-COMPACT/2.0,layout.y-COMPACT/2.0,COMPACT)};
    let hwnd=w.hwnd().map_err(|e|e.to_string())?.0;
    unsafe {
        let handle=CreateRectRgn((x*scale).floor() as i32,(y*scale).floor() as i32,((x+size)*scale).ceil() as i32,((y+size)*scale).ceil() as i32);
        if handle.is_null(){return Err("无法创建悬浮球区域".into());}
        if SetWindowRgn(hwnd,handle,1)==0 {DeleteObject(handle);return Err("无法更新悬浮球区域".into());}
        // SetWindowRgn owns the region after success.
    }
    Ok(())
}
#[cfg(not(windows))]
fn region(_w:&tauri::WebviewWindow,_layout:OrbLayout,_expanded:bool)->Result<(),String>{Ok(())}

fn placement(cx:i32,cy:i32,scale:f64,left:i32,top:i32,right:i32,bottom:i32)->((i32,i32),OrbLayout){
    let radius=(SURFACE*scale/2.0).round() as i32;let margin=(COMPACT*scale/2.0).round() as i32;
    let cx=cx.clamp(left+margin,(right-margin).max(left+margin));
    let cy=cy.clamp(top+margin,(bottom-margin).max(top+margin));
    let menu=(EXPANDED*scale/2.0).round() as i32;
    let mx=cx.clamp(left+menu,(right-menu).max(left+menu));let my=cy.clamp(top+menu,(bottom-menu).max(top+menu));
    ((cx-radius,cy-radius),OrbLayout{x:256.0,y:256.0,menu_x:256.0+(mx-cx) as f64/scale,menu_y:256.0+(my-cy) as f64/scale})
}

pub fn open(app:&AppHandle,page:&str)->Result<(),String>{
    if let Some(w)=app.get_webview_window("main"){w.show().map_err(|e|e.to_string())?;let _=w.unminimize();let _=w.set_focus();w.emit("tool-open",json!({"page":page})).map_err(|e|e.to_string())?;}
    if let Some(w)=app.get_webview_window("orb"){let _=w.emit("orb-reset",());}
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
    let initial=monitor.map(|m|{let a=m.work_area();let scale=m.scale_factor();let margin=(COMPACT*scale) as i32;let (cx,cy)=saved_point.unwrap_or((a.position.x+a.size.width as i32-margin,a.position.y+a.size.height as i32-margin*2));let (position,layout)=placement(cx,cy,scale,a.position.x,a.position.y,a.position.x+a.size.width as i32,a.position.y+a.size.height as i32);(position,layout,scale)});
    // Wry registers OLE drop targets only while creating the WebView2 children.
    // Initially hidden dynamic windows have no target to register, and show()
    // never repairs it: https://github.com/tauri-apps/tauri/issues/14643.
    // Start visible, already positioned, without activating Explorer's drag source.
    let mut builder=WebviewWindowBuilder::new(&app,"orb",WebviewUrl::App("index.html?orb=1".into())).title("WinToolbox · 悬浮球")
        .inner_size(SURFACE,SURFACE).transparent(true).background_color(tauri::window::Color(0,0,0,0)).decorations(false).shadow(false).always_on_top(true)
        .skip_taskbar(true).resizable(false).visible(true).focused(false);
    if let Some(((x,y),_,scale))=initial{builder=builder.position(x as f64/scale,y as f64/scale);}
    let w=builder.build().map_err(|e|e.to_string())?;
    if let Some(((x,y),layout,_))=initial{w.set_position(PhysicalPosition::new(x,y)).map_err(|e|e.to_string())?;*app.state::<OrbState>().layout.lock().map_err(|e|e.to_string())?=layout;}
    let layout=*app.state::<OrbState>().layout.lock().map_err(|e|e.to_string())?;
    region(&w,layout,false)?;
    *app.state::<OrbState>().expanded.lock().map_err(|e|e.to_string())?=false;
    w.show().map_err(|e|e.to_string())
}

#[tauri::command]
pub fn orb_resize(app:AppHandle,expanded:bool,focus:Option<bool>)->Result<OrbLayout,String>{
    let w=app.get_webview_window("orb").ok_or("悬浮球未打开")?;
    let layout=*app.state::<OrbState>().layout.lock().map_err(|e|e.to_string())?;
    let state=app.state::<OrbState>();let mut previous=state.expanded.lock().map_err(|e|e.to_string())?;
    if *previous!=expanded{region(&w,layout,expanded)?;*previous=expanded;}
    if expanded && focus.unwrap_or(false){w.set_focus().map_err(|e|e.to_string())?;}
    Ok(layout)
}

#[tauri::command]
pub fn orb_save_position(app:AppHandle)->Result<OrbLayout,String>{
    let w=app.get_webview_window("orb").ok_or("悬浮球未打开")?;
    let position=w.outer_position().map_err(|e|e.to_string())?;let scale=w.scale_factor().map_err(|e|e.to_string())?;
    let state=app.state::<OrbState>();let mut layout=state.layout.lock().map_err(|e|e.to_string())?;
    let mut cx=position.x+(layout.x*scale).round() as i32;let mut cy=position.y+(layout.y*scale).round() as i32;
    if let Some(m)=w.current_monitor().map_err(|e|e.to_string())?{
        let a=m.work_area();let (pos,new_layout)=placement(cx,cy,scale,a.position.x,a.position.y,a.position.x+a.size.width as i32,a.position.y+a.size.height as i32);
        cx=pos.0+(new_layout.x*scale).round() as i32;cy=pos.1+(new_layout.y*scale).round() as i32;
        // Only constrain the visible ball; the transparent canvas may extend offscreen.
        if pos!=(position.x,position.y){w.set_position(PhysicalPosition::new(pos.0,pos.1)).map_err(|e|e.to_string())?;}
        *layout=new_layout;
    }
    fs::create_dir_all(super::data_dir()).map_err(|e|e.to_string())?;
    fs::write(super::data_dir().join("orb-position.json"),json!({"x":cx,"y":cy}).to_string()).map_err(|e|e.to_string())?;
    region(&w,*layout,*state.expanded.lock().map_err(|e|e.to_string())?)?;
    Ok(*layout)
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
    use super::{clamp,placement};
    #[test] fn edge_anchor_survives_fixed_canvas(){let ((x,y),a)=placement(64,700,1.0,0,0,1920,1080);assert_eq!(x,-192);assert_eq!(a.x,256.0);assert_eq!(a.menu_x,352.0);assert_eq!(y+a.y as i32,700);}
    #[test] fn dpi_and_negative_origin_preserve_center(){let ((x,y),a)=placement(-2500,500,1.5,-2560,0,0,1440);assert_eq!(x+(a.x*1.5).round() as i32,-2464);assert_eq!(y+(a.y*1.5).round() as i32,500);}
    #[test] fn keeps_negative_monitor_coordinates(){assert_eq!(clamp(-3000,1800,320,320,-2560,0,0,1440),(-2560,1120));}
    #[test] fn clamps_small_work_area(){assert_eq!(clamp(500,500,320,320,0,0,200,200),(0,0));}
}
