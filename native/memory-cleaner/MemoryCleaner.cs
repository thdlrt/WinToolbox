// Fixed-purpose, on-demand privileged broker. No plugins, commands or file paths
// are accepted over IPC. The scheduled task and installed executable are protected.
using System;
using System.IO;
using System.IO.Pipes;
using System.Text;
using System.Diagnostics;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Runtime.InteropServices;
using System.Reflection;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using Microsoft.Win32.SafeHandles;

[assembly: AssemblyTitle("WinToolbox Memory Cleaner")]
[assembly: AssemblyDescription("Fixed-purpose memory cleanup helper")]
[assembly: AssemblyVersion("1.0.0.0")]
internal static class MemoryCleaner {
    const string FileName = "WinToolbox.MemoryCleaner.exe";
    const int Protocol = 1;
    static readonly JavaScriptSerializer Json = new JavaScriptSerializer();
    static readonly SecurityIdentifier SystemSid = new SecurityIdentifier("S-1-5-18");
    static readonly SecurityIdentifier AdminSid = new SecurityIdentifier("S-1-5-32-544");
    static string Self { get { return Path.GetFullPath(Assembly.GetExecutingAssembly().Location); } }
    static string Root { get { return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), "WinToolboxMemoryCleaner"); } }
    static string DirectoryFor(string sid) { return Path.Combine(Root, sid); }
    static string InstalledFile(string sid) { return Path.Combine(DirectoryFor(sid), FileName); }
    static string TaskName(string sid) { return "WinToolbox-MemoryCleaner-" + sid; }
    static string PipeName(string sid) { return "WinToolbox.MemoryCleaner.v1." + sid; }
    static void ValidateSid(string sid) {
        if (!System.Text.RegularExpressions.Regex.IsMatch(sid, @"^S-1-(5-21|12-1)-[0-9]+-[0-9]+-[0-9]+-[0-9]+$")) throw new ArgumentException("Invalid account SID");
        new SecurityIdentifier(sid);
    }
    static bool Administrator() { return new WindowsPrincipal(WindowsIdentity.GetCurrent()).IsInRole(WindowsBuiltInRole.Administrator); }
    static dynamic Scheduler() { dynamic service = Activator.CreateInstance(Type.GetTypeFromProgID("Schedule.Service")); service.Connect(); return service; }
    static bool IsInstalled(string sid) {
        if (!File.Exists(InstalledFile(sid))) return false;
        RejectReparse(Root); RejectReparse(DirectoryFor(sid)); RejectReparse(InstalledFile(sid));
        dynamic task;
        try { task = Scheduler().GetFolder(@"\").GetTask(TaskName(sid)); }
        catch (COMException e) { if (e.ErrorCode == unchecked((int)0x80070002)) return false; throw; }
        dynamic definition = task.Definition;
        string principal=(string)definition.Principal.UserId;
        SecurityIdentifier principalSid=principal.StartsWith("S-",StringComparison.OrdinalIgnoreCase)?new SecurityIdentifier(principal):(SecurityIdentifier)new NTAccount(principal).Translate(typeof(SecurityIdentifier));
        return task.Enabled && definition.Actions.Count == 1 &&
            String.Equals((string)definition.Actions[1].Path, InstalledFile(sid), StringComparison.OrdinalIgnoreCase) &&
            (string)definition.Actions[1].Arguments == "--serve " + sid &&
            principalSid.Equals(SystemSid) && (int)definition.Principal.LogonType == 5;
    }
    static void RejectReparse(string path) {
        if ((Directory.Exists(path) || File.Exists(path)) && (File.GetAttributes(path) & FileAttributes.ReparsePoint) != 0) throw new IOException("Reparse path refused: " + path);
    }
    static DirectorySecurity ProtectedDirectoryAcl() {
        DirectorySecurity acl = new DirectorySecurity(); acl.SetAccessRuleProtection(true, false); acl.SetOwner(AdminSid);
        foreach (SecurityIdentifier sid in new [] { SystemSid, AdminSid }) acl.AddAccessRule(new FileSystemAccessRule(sid, FileSystemRights.FullControl, InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit, PropagationFlags.None, AccessControlType.Allow));
        acl.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier("S-1-5-32-545"), FileSystemRights.ReadAndExecute, InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit, PropagationFlags.None, AccessControlType.Allow));
        return acl;
    }
    static void ProtectDirectory(string path) {
        RejectReparse(path);
        if (!Directory.Exists(path)) Directory.CreateDirectory(path, ProtectedDirectoryAcl());
        Directory.SetAccessControl(path, ProtectedDirectoryAcl());
    }
    static void Install(string sid) {
        if (!Administrator()) throw new UnauthorizedAccessException("Administrator approval required");
        ProtectDirectory(Root); ProtectDirectory(DirectoryFor(sid)); RejectReparse(InstalledFile(sid));
        dynamic service = Scheduler(), folder = service.GetFolder(@"\");
        try { folder.GetTask(TaskName(sid)).Stop(0); } catch (COMException) { }
        string target = InstalledFile(sid);
        if (!String.Equals(Self, target, StringComparison.OrdinalIgnoreCase)) {
            // Delete only this known protected file, never recurse or follow links.
            for (int i=0;;i++) { try { if(File.Exists(target)) File.Delete(target); File.Copy(Self,target,false); break; } catch(IOException) { if(i>=30)throw; Thread.Sleep(100); } }
        }
        FileSecurity fileAcl = new FileSecurity(); fileAcl.SetAccessRuleProtection(true,false); fileAcl.SetOwner(AdminSid);
        fileAcl.AddAccessRule(new FileSystemAccessRule(SystemSid,FileSystemRights.FullControl,AccessControlType.Allow));
        fileAcl.AddAccessRule(new FileSystemAccessRule(AdminSid,FileSystemRights.FullControl,AccessControlType.Allow));
        fileAcl.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier("S-1-5-32-545"),FileSystemRights.ReadAndExecute,AccessControlType.Allow));
        File.SetAccessControl(target,fileAcl);
        dynamic definition = service.NewTask(0);
        definition.RegistrationInfo.Description = "WinToolbox: only fixed memory cleanup operations; on demand, no automatic cleanup.";
        definition.Settings.Enabled = true; definition.Settings.AllowDemandStart = true;
        definition.Settings.ExecutionTimeLimit = "PT0S"; definition.Settings.MultipleInstances = 2;
        definition.Settings.DisallowStartIfOnBatteries = false; definition.Settings.StopIfGoingOnBatteries = false;
        definition.Principal.UserId = "S-1-5-18"; definition.Principal.LogonType = 5; definition.Principal.RunLevel = 1;
        dynamic action = definition.Actions.Create(0); action.Path = target;
        action.Arguments = "--serve " + sid; action.WorkingDirectory = DirectoryFor(sid);
        // The owning account can read/run but cannot modify the elevated action.
        folder.RegisterTaskDefinition(TaskName(sid),definition,6,"SYSTEM",null,5,"D:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;GRGX;;;"+sid+")");
    }
    static void Uninstall(string sid) {
        if (!Administrator()) throw new UnauthorizedAccessException("Administrator approval required");
        RejectReparse(Root); RejectReparse(DirectoryFor(sid)); RejectReparse(InstalledFile(sid));
        dynamic folder = Scheduler().GetFolder(@"\");
        try { dynamic task=folder.GetTask(TaskName(sid)); task.Stop(0); folder.DeleteTask(TaskName(sid),0); }
        catch(COMException e) { if(e.ErrorCode!=unchecked((int)0x80070002))throw; }
        for(int i=0;;i++) { try { if(File.Exists(InstalledFile(sid)))File.Delete(InstalledFile(sid)); break; } catch(IOException){if(i>=30)throw;Thread.Sleep(100);} }
        if(Directory.Exists(DirectoryFor(sid)))Directory.Delete(DirectoryFor(sid),false);
    }
    [StructLayout(LayoutKind.Sequential)] struct SecurityAttributes { public int Length; public IntPtr Descriptor; [MarshalAs(UnmanagedType.Bool)] public bool Inherit; }
    [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern SafePipeHandle CreateNamedPipe(string name,uint mode,uint pipeMode,uint instances,uint outSize,uint inSize,uint timeout,ref SecurityAttributes attributes);
    [DllImport("kernel32.dll",SetLastError=true)] static extern bool GetNamedPipeServerProcessId(SafePipeHandle pipe,out uint id);
    [DllImport("kernel32.dll",SetLastError=true)] static extern IntPtr OpenProcess(uint access,bool inherit,uint id);
    [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern bool QueryFullProcessImageName(IntPtr process,int flags,StringBuilder buffer,ref int size);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
    static NamedPipeServerStream NewPipe(string sid) {
        PipeSecurity acl=new PipeSecurity();acl.SetAccessRuleProtection(true,false);
        acl.AddAccessRule(new PipeAccessRule(SystemSid,PipeAccessRights.FullControl,AccessControlType.Allow));
        // Do not grant FILE_CREATE_PIPE_INSTANCE (also known as FILE_APPEND_DATA).
        acl.AddAccessRule(new PipeAccessRule(new SecurityIdentifier(sid),PipeAccessRights.ReadData|PipeAccessRights.WriteData|PipeAccessRights.ReadAttributes|PipeAccessRights.WriteAttributes|PipeAccessRights.ReadExtendedAttributes|PipeAccessRights.WriteExtendedAttributes|PipeAccessRights.ReadPermissions|PipeAccessRights.Synchronize,AccessControlType.Allow));
        byte[] bytes=acl.GetSecurityDescriptorBinaryForm();GCHandle pin=GCHandle.Alloc(bytes,GCHandleType.Pinned);
        try {
            SecurityAttributes attributes=new SecurityAttributes{Length=Marshal.SizeOf(typeof(SecurityAttributes)),Descriptor=pin.AddrOfPinnedObject(),Inherit=false};
            SafePipeHandle handle=CreateNamedPipe(@"\\.\pipe\"+PipeName(sid),0x40080003,8,1,4096,4096,0,ref attributes);
            if(handle.IsInvalid)throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
            return new NamedPipeServerStream(PipeDirection.InOut,true,false,handle);
        } finally {pin.Free();}
    }
    static void VerifyServer(NamedPipeClientStream pipe,string sid) {
        uint pid;if(!GetNamedPipeServerProcessId(pipe.SafePipeHandle,out pid))throw new IOException("Cannot identify cleanup process");
        IntPtr process=OpenProcess(0x1000,false,pid);if(process==IntPtr.Zero)throw new IOException("Cannot verify cleanup process");
        try {StringBuilder name=new StringBuilder(32768);int size=name.Capacity;
            if(!QueryFullProcessImageName(process,0,name,ref size)||!String.Equals(name.ToString(),InstalledFile(sid),StringComparison.OrdinalIgnoreCase))throw new IOException("Unexpected cleanup process");
        }finally{CloseHandle(process);}
    }
    static int[] Commands(string mode) {
        if(mode=="default")return new[]{2,5};if(mode=="full")return new[]{2,3,4};throw new ArgumentException("Unsupported cleanup mode");
    }
    [StructLayout(LayoutKind.Sequential)] struct Luid { public uint Low; public int High; }
    [StructLayout(LayoutKind.Sequential)] struct Privilege { public uint Count; public Luid Id; public uint Attributes; }
    [DllImport("advapi32.dll",SetLastError=true)] static extern bool OpenProcessToken(IntPtr process,uint access,out IntPtr token);
    [DllImport("advapi32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern bool LookupPrivilegeValue(string system,string name,out Luid luid);
    [DllImport("advapi32.dll",SetLastError=true)] static extern bool AdjustTokenPrivileges(IntPtr token,bool disable,ref Privilege state,int length,IntPtr previous,IntPtr returned);
    [DllImport("ntdll.dll")] static extern int NtSetSystemInformation(int info,ref uint value,int length);
    static void EnablePrivilege() {
        IntPtr token;if(!OpenProcessToken(Process.GetCurrentProcess().Handle,0x28,out token))throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
        try {Privilege p=new Privilege{Count=1,Attributes=2};if(!LookupPrivilegeValue(null,"SeProfileSingleProcessPrivilege",out p.Id))throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
            bool ok=AdjustTokenPrivileges(token,false,ref p,0,IntPtr.Zero,IntPtr.Zero);int error=Marshal.GetLastWin32Error();if(!ok||error!=0)throw new System.ComponentModel.Win32Exception(error);
        }finally{CloseHandle(token);}
    }
    static object Clean(string mode) {
        int[] operations=Commands(mode);EnablePrivilege();
        foreach(int operation in operations){uint value=(uint)operation;int status=NtSetSystemInformation(80,ref value,4);if(status<0)throw new IOException("Memory region "+operation+": NTSTATUS 0x"+status.ToString("X8"));}
        return new{ok=true,protocol=Protocol,operations=operations,worker_pid=Process.GetCurrentProcess().Id};
    }
    static byte[] ReadRequest(Stream stream) {
        byte[] request=new byte[64];int count=0;
        while(count<request.Length){int n=stream.ReadByte();if(n<0)throw new IOException("Client disconnected");if(n==10){byte[] result=new byte[count];Array.Copy(request,result,count);return result;}request[count++]=(byte)n;}
        throw new IOException("Request too large");
    }
    static void Serve(string sid) {
        if(!WindowsIdentity.GetCurrent().User.Equals(SystemSid)||!String.Equals(Self,InstalledFile(sid),StringComparison.OrdinalIgnoreCase))throw new UnauthorizedAccessException("Only the installed SYSTEM task may serve");
        while(true)using(NamedPipeServerStream pipe=NewPipe(sid)) {
            // No process stays resident indefinitely and no cleanup runs on a timer.
            if(!pipe.WaitForConnectionAsync().Wait(60000))return;
            try {
                Task<byte[]> read=Task.Run(()=>ReadRequest(pipe));if(!read.Wait(10000))continue;
                string mode=Encoding.ASCII.GetString(read.Result);object result;
                try { result=mode=="ping"?new{ok=true,protocol=Protocol,worker_pid=Process.GetCurrentProcess().Id}:(object)Clean(mode); }
                catch(Exception e){result=new{ok=false,protocol=Protocol,error=e.Message};}
                byte[] response=Encoding.UTF8.GetBytes(Json.Serialize(result)+"\n");
                if(!pipe.WriteAsync(response,0,response.Length).Wait(10000))continue;
            }catch(IOException){}catch(AggregateException){}
        }
    }
    static object Request(string sid,string mode) {
        if(mode!="ping")Commands(mode);
        if(!IsInstalled(sid))throw new IOException("Cleanup component is not installed");
        dynamic task=Scheduler().GetFolder(@"\").GetTask(TaskName(sid));task.Run(null);
        // Explicit rights avoid requesting the pipe-instance creation right.
        using(NamedPipeClientStream pipe=new NamedPipeClientStream(".",PipeName(sid),PipeAccessRights.ReadData|PipeAccessRights.WriteData|PipeAccessRights.Synchronize,PipeOptions.Asynchronous,TokenImpersonationLevel.Anonymous,HandleInheritability.None)) {
            pipe.Connect(15000);VerifyServer(pipe,sid);
            byte[] data=Encoding.ASCII.GetBytes(mode+"\n");pipe.Write(data,0,data.Length);pipe.Flush();
            Task<string> read=Task.Run(()=> {using(StreamReader reader=new StreamReader(pipe,Encoding.UTF8,false,1024,true)) { char[] result=new char[4096];int count=0;while(count<result.Length){int n=reader.Read();if(n<0)throw new IOException("Cleanup result was not received");if(n==10)return new string(result,0,count);result[count++]=(char)n;}throw new IOException("Cleanup response too large");}});
            if(!read.Wait(90000))throw new TimeoutException("Cleanup timed out; not retried");
            return Json.DeserializeObject(read.Result);
        }
    }
    static int Main(string[] args) {
        try {
            if(args.Length>0&&(args[0]=="--identity"||args[0]=="--status"||args[0]=="--request"))Console.SetOut(new StreamWriter(Console.OpenStandardOutput(),new UTF8Encoding(false)){AutoFlush=true});
            if(args.Length==1&&args[0]=="--identity"){Console.WriteLine(Json.Serialize(new{ok=true,sid=WindowsIdentity.GetCurrent().User.Value}));return 0;}
            if(args.Length<2)throw new ArgumentException("Missing action or SID");string sid=args[1];ValidateSid(sid);
            switch(args[0]) {
                case "--install":if(args.Length!=2)throw new ArgumentException();Install(sid);return 0;
                case "--uninstall":if(args.Length!=2)throw new ArgumentException();Uninstall(sid);return 0;
                case "--serve":if(args.Length!=2)throw new ArgumentException();Serve(sid);return 0;
                case "--status":if(args.Length!=2)throw new ArgumentException();Console.WriteLine(Json.Serialize(new{ok=true,installed=IsInstalled(sid),present=File.Exists(InstalledFile(sid)),protocol=Protocol}));return 0;
                case "--request":if(args.Length!=3)throw new ArgumentException();Console.WriteLine(Json.Serialize(Request(sid,args[2])));return 0;
                default:throw new ArgumentException("Unsupported action");
            }
        }catch(Exception e){Exception actual=e is AggregateException?e.GetBaseException():e;Console.WriteLine(Json.Serialize(new{ok=false,error=actual.Message}));return actual.HResult!=0?actual.HResult:1;}
    }
}
