using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Automation;
using System.Windows.Automation.Text;
using System.Windows.Forms;

// Short-lived, STA selection reader. Never monitors or changes the clipboard
// while capturing; only an explicit --copy operation writes to it.
static class QuickContext {
    [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr window, out uint pid);
    [DllImport("user32.dll")] static extern IntPtr GetAncestor(IntPtr window, uint flags);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern int GetClassName(IntPtr window, StringBuilder name, int count);
    static readonly JavaScriptSerializer Json = new JavaScriptSerializer { MaxJsonLength = 1048576 };

    static object Capture(IntPtr source) {
        var empty = new { text = "", paths = new string[0], protectedField = false };
        if (source == IntPtr.Zero || GetForegroundWindow() != source) return empty;
        uint pid;
        GetWindowThreadProcessId(source, out pid);
        AutomationElement focused = AutomationElement.FocusedElement;
        if (focused != null && focused.Current.IsPassword)
            return new Dictionary<string, object> { {"text", ""}, {"paths", new string[0]}, {"protected", true} };
        string process = Process.GetProcessById((int)pid).ProcessName;
        if (process.Equals("explorer", StringComparison.OrdinalIgnoreCase)) {
            dynamic shell = Activator.CreateInstance(Type.GetTypeFromProgID("Shell.Application"));
            try {
                dynamic windows = shell.Windows();
                try {
                    for (int i = 0; i < (int)windows.Count; i++) {
                        dynamic browser = windows.Item(i);
                        try {
                            if ((long)browser.HWND != source.ToInt64()) continue;
                            dynamic document = browser.Document;
                            dynamic selected = document.SelectedItems();
                            var paths = new List<string>();
                            try {
                                for (int n = 0; n < Math.Min((int)selected.Count, 100); n++) {
                                    dynamic item = selected.Item(n);
                                    try { string path = (string)item.Path; if (File.Exists(path) || Directory.Exists(path)) paths.Add(path); }
                                    finally { Marshal.ReleaseComObject(item); }
                                }
                            } finally { Marshal.ReleaseComObject(selected); Marshal.ReleaseComObject(document); }
                            if (GetForegroundWindow() == source) return new { text = "", paths = paths.ToArray(), protectedField = false };
                            return empty;
                        } finally { Marshal.ReleaseComObject(browser); }
                    }
                    var className = new StringBuilder(256);
                    GetClassName(source, className, 256);
                    if (className.ToString() == "Progman" || className.ToString() == "WorkerW") {
                        object location = 0, root = 0; int desktopHandle = 0;
                        dynamic desktop = windows.FindWindowSW(ref location, ref root, 8, out desktopHandle, 1);
                        if (desktop != null) {
                            try {
                                dynamic document = desktop.Document;
                                dynamic selected = document.SelectedItems();
                                var paths = new List<string>();
                                try {
                                    for (int n = 0; n < Math.Min((int)selected.Count, 100); n++) {
                                        dynamic item = selected.Item(n);
                                        try { string path = (string)item.Path; if (File.Exists(path) || Directory.Exists(path)) paths.Add(path); }
                                        finally { Marshal.ReleaseComObject(item); }
                                    }
                                } finally { Marshal.ReleaseComObject(selected); Marshal.ReleaseComObject(document); }
                                if (GetForegroundWindow() == source) return new { text = "", paths = paths.ToArray(), protectedField = false };
                            } finally { Marshal.ReleaseComObject(desktop); }
                        }
                    }
                } finally { Marshal.ReleaseComObject(windows); }
            } finally { Marshal.ReleaseComObject(shell); }
        }
        if (focused == null || GetForegroundWindow() != source) return empty;
        var current = focused;
        for (int depth = 0; current != null && depth < 8; depth++) {
            int handle = current.Current.NativeWindowHandle;
            if (handle != 0 && GetAncestor(new IntPtr(handle), 2) != source) break;
            if (current.Current.IsPassword)
                return new Dictionary<string, object> { {"text", ""}, {"paths", new string[0]}, {"protected", true} };
            object pattern;
            if (current.TryGetCurrentPattern(TextPattern.Pattern, out pattern)) {
                var ranges = ((TextPattern)pattern).GetSelection();
                var text = new StringBuilder();
                foreach (var range in ranges) {
                    if (range.CompareEndpoints(TextPatternRangeEndpoint.Start, range, TextPatternRangeEndpoint.End) == 0) continue;
                    if (text.Length > 0) text.AppendLine();
                    text.Append(range.GetText(6001 - text.Length));
                    if (text.Length > 6000) return new { text = "", paths = new string[0], message = "选区超过 6000 字符，请缩小范围" };
                }
                if (text.Length > 0 && GetForegroundWindow() == source)
                    return new { text = text.ToString(), paths = new string[0], protectedField = false };
            }
            current = TreeWalker.ControlViewWalker.GetParent(current);
        }
        return empty;
    }

    [STAThread] static int Main(string[] args) {
        // GUI children have redirected pipes but no attached console code page.
        Console.SetOut(new StreamWriter(Console.OpenStandardOutput(), new UTF8Encoding(false)) { AutoFlush = true });
        Console.SetIn(new StreamReader(Console.OpenStandardInput(), new UTF8Encoding(false)));
        try {
            if (args.Length == 1 && args[0] == "--copy") {
                string text = Console.In.ReadToEnd();
                if (text.Length > 1000000) throw new Exception("复制内容过长");
                if (text.Length > 0) Clipboard.SetText(text);
                Console.WriteLine("{\"ok\":true}"); return 0;
            }
            if (args.Length != 2 || args[0] != "--capture") throw new Exception("Invalid invocation");
            object result = Capture(new IntPtr(long.Parse(args[1])));
            Console.WriteLine(Json.Serialize(result)); return 0;
        } catch (Exception) {
            // Unsupported/protected applications must never trigger Ctrl+C or
            // expose a whole input value as a fallback for selected text.
            Console.WriteLine("{\"text\":\"\",\"paths\":[],\"message\":\"无法读取此窗口选区，可使用快捷脚本页面手动输入\"}");
            return args.Length > 0 && args[0] == "--copy" ? 1 : 0;
        }
    }
}
