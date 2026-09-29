//! Lower the cache budget of hidden/minimized WebViews without suspending audio,
//! queued uploads, or JavaScript responsible for completing an active operation.
use tauri::WebviewWindow;

pub fn set_background(window: &WebviewWindow, background: bool) {
    #[cfg(windows)]
    {
        use windows_core::Interface;
        use webview2_com::Microsoft::Web::WebView2::Win32::{
            ICoreWebView2_19, COREWEBVIEW2_MEMORY_USAGE_TARGET_LEVEL_LOW,
            COREWEBVIEW2_MEMORY_USAGE_TARGET_LEVEL_NORMAL,
        };
        let _ = window.with_webview(move |view| unsafe {
            let result = (|| -> windows_core::Result<()> {
                let core = view.controller().CoreWebView2()?.cast::<ICoreWebView2_19>()?;
                core.SetMemoryUsageTargetLevel(if background {
                    COREWEBVIEW2_MEMORY_USAGE_TARGET_LEVEL_LOW
                } else {
                    COREWEBVIEW2_MEMORY_USAGE_TARGET_LEVEL_NORMAL
                })
            })();
            if let Err(error) = result { eprintln!("WebView memory policy: {error}"); }
        });
    }
    #[cfg(not(windows))]
    let _ = (window, background);
}

pub fn watch(app: tauri::AppHandle) {
    use tauri::Manager;
    std::thread::spawn(move || {
        let mut previous = std::collections::HashMap::new();
        loop {
            let windows = app.webview_windows();
            previous.retain(|label, _| windows.contains_key(label));
            for (label, window) in windows {
                // A failed visibility read must never put a foreground view in low mode.
                let background = !window.is_visible().unwrap_or(true)
                    || window.is_minimized().unwrap_or(false);
                if previous.get(&label) != Some(&background) {
                    set_background(&window, background);
                    previous.insert(label, background);
                }
            }
            std::thread::sleep(std::time::Duration::from_secs(2));
        }
    });
}
