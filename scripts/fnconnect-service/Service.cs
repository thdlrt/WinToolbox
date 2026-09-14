using System;
using System.IO;
using System.IO.Pipes;
using System.Diagnostics;
using System.Collections.Generic;
using System.Security.AccessControl;
using System.Security.Principal;
using System.ServiceProcess;
using System.Threading;
using System.Web.Script.Serialization;
using System.Text;
using System.Text.RegularExpressions;
using System.Net;
using System.Net.Sockets;
using System.Runtime.InteropServices;

// Fixed-purpose local broker. No shell commands, executable paths, or raw core config in IPC.
public sealed class TunService : ServiceBase {
    const string PipeName = "WinToolbox.Tun.v1";
    readonly object gate = new object();
    readonly string root = AppDomain.CurrentDomain.BaseDirectory;
    readonly JavaScriptSerializer json = new JavaScriptSerializer { MaxJsonLength = 32768 };
    Process core;
    string session = "", error = "";
    DateTime lease;
    volatile bool closing;
    Timer timer;
    NamedPipeServerStream pipe;
    IntPtr job;
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode)] static extern IntPtr CreateJobObject(IntPtr a, string n);
    [DllImport("kernel32.dll")] static extern bool SetInformationJobObject(IntPtr h, int c, IntPtr p, uint s);
    [DllImport("kernel32.dll")] static extern bool AssignProcessToJobObject(IntPtr h, IntPtr p);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr h);
    [StructLayout(LayoutKind.Sequential)] struct BasicLimits { public long perProcess,perJob; public uint flags; public UIntPtr min,max; public uint active; public UIntPtr affinity; public uint priority,scheduling; }
    [StructLayout(LayoutKind.Sequential)] struct IoCounters { public ulong a,b,c,d,e,f; }
    [StructLayout(LayoutKind.Sequential)] struct ExtendedLimits { public BasicLimits basic; public IoCounters io; public UIntPtr processMemory,jobMemory,peakProcess,peakJob; }
    public TunService() { ServiceName="WinToolboxTun"; CanStop=true; CanShutdown=true; }
    public static void Main(string[] args) {
        if (args.Length==1 && args[0]=="--validate") {
            var service=new TunService();
            try { Console.WriteLine(service.json.Serialize(service.Config(service.json.Deserialize<Dictionary<string,object>>(Console.ReadLine())))); } catch(Exception ex) { Console.Error.WriteLine(ex.GetType().Name+": "+ex.Message); Environment.ExitCode=1; }
            return;
        }
        Run(new TunService());
    }
    protected override void OnStart(string[] args) {
        job=CreateJobObject(IntPtr.Zero,null);
        var limits=new ExtendedLimits(); limits.basic.flags=0x2000;
        int size=Marshal.SizeOf(limits); IntPtr ptr=Marshal.AllocHGlobal(size);
        try { Marshal.StructureToPtr(limits,ptr,false); if(job==IntPtr.Zero || !SetInformationJobObject(job,9,ptr,(uint)size)) throw new InvalidOperationException("Cannot create TUN process guard"); }
        finally { Marshal.FreeHGlobal(ptr); }
        timer=new Timer(_=> { lock(gate) { if(core!=null && (core.HasExited || (DateTime.UtcNow-lease).TotalSeconds>15)) { if(core.HasExited) error="TUN core exited"; StopCore(); } } },null,1000,1000);
        new Thread(Listen) { IsBackground=true }.Start();
    }
    protected override void OnStop() { closing=true; if(pipe!=null) pipe.Dispose(); if(timer!=null) timer.Dispose(); lock(gate) StopCore(); if(job!=IntPtr.Zero) CloseHandle(job); }
    protected override void OnShutdown() { OnStop(); }
    void StopCore() { if(core!=null) { try { if(!core.HasExited) { core.Kill(); core.WaitForExit(5000); } } finally { core.Dispose(); core=null; } } session=""; }
    Dictionary<string,object> State() { return new Dictionary<string,object>{{"phase",core!=null?"running":error!=""?"error":"stopped"},{"error",error},{"service",true},{"service_version",1}}; }
    void Listen() {
        var owner=new SecurityIdentifier(File.ReadAllText(Path.Combine(root,"owner.sid")).Trim());
        var acl=new PipeSecurity(); acl.SetAccessRuleProtection(true,false);
        acl.AddAccessRule(new PipeAccessRule(new SecurityIdentifier(WellKnownSidType.NetworkSid,null),PipeAccessRights.FullControl,AccessControlType.Deny));
        acl.AddAccessRule(new PipeAccessRule(owner,PipeAccessRights.ReadWrite,AccessControlType.Allow));
        acl.AddAccessRule(new PipeAccessRule(new SecurityIdentifier(WellKnownSidType.LocalSystemSid,null),PipeAccessRights.FullControl,AccessControlType.Allow));
        while(!closing) {
            try {
                using(var channel=new NamedPipeServerStream(PipeName,PipeDirection.InOut,1,PipeTransmissionMode.Byte,PipeOptions.Asynchronous,32768,32768,acl)) {
                    pipe=channel; channel.WaitForConnection();
                    using(var timeout=new Timer(_=> { try { channel.Dispose(); } catch {} },null,8000,Timeout.Infinite)) {
                        var bytes=new List<byte>(); int b;
                        while((b=channel.ReadByte())!=-1 && b!=10) { if(bytes.Count>=16384) throw new InvalidDataException("Request too large"); bytes.Add((byte)b); }
                        Dictionary<string,object> response;
                        try { var request=json.Deserialize<Dictionary<string,object>>(Encoding.UTF8.GetString(bytes.ToArray())); lock(gate) response=Handle(request); }
                        catch(Exception ex) { response=new Dictionary<string,object>{{"rpc_error",ex.Message}}; }
                        byte[] output=Encoding.UTF8.GetBytes(json.Serialize(response)+"\n"); channel.Write(output,0,output.Length); channel.Flush();
                    }
                }
            } catch { if(!closing) Thread.Sleep(100); }
        }
    }
    static string Text(Dictionary<string,object> p,string key) { object v; if(!p.TryGetValue(key,out v) || !(v is string)) throw new ArgumentException("Missing " + key); return (string)v; }
    Dictionary<string,object> Handle(Dictionary<string,object> p) {
        string op=Text(p,"op"); if(op=="status") return State();
        string id=Text(p,"session"); if(!Regex.IsMatch(id,"\\A[a-f0-9]{32}\\z")) throw new ArgumentException("Invalid session");
        if(core!=null && session!=id) throw new InvalidOperationException("TUN is in use by another connection");
        if(op=="start") {
            var config=Config(p); if(core!=null) return State();
            string runtime=Path.Combine(root,"runtime"); Directory.CreateDirectory(runtime);
            File.WriteAllText(Path.Combine(runtime,"config.json"),json.Serialize(config),new UTF8Encoding(false));
            var start=new ProcessStartInfo(Path.Combine(root,"mihomo-windows-amd64.exe"),"-d \""+runtime+"\" -f \""+Path.Combine(runtime,"config.json")+"\"") { UseShellExecute=false,CreateNoWindow=true,WorkingDirectory=root,RedirectStandardOutput=true,RedirectStandardError=true };
            error=""; core=Process.Start(start);
            if(!AssignProcessToJobObject(job,core.Handle)) { StopCore(); throw new InvalidOperationException("Cannot guard TUN process"); }
            core.OutputDataReceived+=(s,e)=>{}; core.ErrorDataReceived+=(s,e)=>{}; core.BeginOutputReadLine(); core.BeginErrorReadLine();
            session=id; lease=DateTime.UtcNow;
            if(core.WaitForExit(1500)) { error="TUN core failed to start"; StopCore(); throw new InvalidOperationException(error); }
        } else if(op=="stop") { StopCore(); error=""; }
        else if(op=="heartbeat") { if(core==null) throw new InvalidOperationException("TUN stopped"); lease=DateTime.UtcNow; }
        else throw new ArgumentException("Unsupported operation");
        return State();
    }
    static string Ip(string input) { IPAddress ip; if(!IPAddress.TryParse(input,out ip) || ip.AddressFamily!=AddressFamily.InterNetwork || input!=ip.ToString()) throw new ArgumentException("Invalid IPv4"); return input; }
    static uint IpNumber(string input) { byte[] b=IPAddress.Parse(Ip(input)).GetAddressBytes(); return ((uint)b[0]<<24)|((uint)b[1]<<16)|((uint)b[2]<<8)|b[3]; }
    static string IpString(uint n) { return String.Format("{0}.{1}.{2}.{3}",n>>24,(n>>16)&255,(n>>8)&255,n&255); }
    static string[] Strings(Dictionary<string,object> p,string key) { var a=p[key] as System.Collections.IList; if(a==null || a.Count==0 || a.Count>64) throw new ArgumentException("Invalid "+key); var result=new string[a.Count]; for(int i=0;i<a.Count;i++) result[i]=a[i] as string??throwString(); return result; }
    static string throwString() { throw new ArgumentException("Expected string"); }
    Dictionary<string,object> Config(Dictionary<string,object> p) {
        string host=Text(p,"host"),scope=Text(p,"scope");
        if(!Regex.IsMatch(host,"\\A[a-zA-Z0-9-]+\\.fnos\\.net\\z")) Ip(host);
        if(scope!="lan" && scope!="all") throw new ArgumentException("Invalid scope");
        string[] addresses=Strings(p,"transport_ips"),networks=Strings(p,"networks");
        var routes=new List<string>(); var rules=new List<string>();
        foreach(string address in addresses) rules.Add("IP-CIDR,"+Ip(address)+"/32,DIRECT,no-resolve");
        rules.Add("DOMAIN,"+host+",DIRECT");
        foreach(string network in networks) {
            string[] parts=network.Split('/'); int prefix;
            if(parts.Length!=2 || !Int32.TryParse(parts[1],out prefix) || prefix<8 || prefix>32) throw new ArgumentException("Invalid network");
            uint n=IpNumber(parts[0]),mask=UInt32.MaxValue<<(32-prefix);
            if((n&mask)!=n || !(n>>24==10 || (prefix>=12 && n>>20==0xac1) || (prefix>=16 && n>>16==0xc0a8))) throw new ArgumentException("Network must be a private IPv4 subnet");
            if(prefix==32) routes.Add(network); else { routes.Add(parts[0]+"/"+(prefix+1)); routes.Add(IpString(n+(1u<<(31-prefix)))+"/"+(prefix+1)); }
            if(scope=="lan") { rules.Add("AND,((IP-CIDR,"+network+"),(NETWORK,UDP)),REJECT"); rules.Add("IP-CIDR,"+network+",HOME,no-resolve"); }
        }
        if(scope=="all") { routes.AddRange(new[]{"0.0.0.0/1","128.0.0.0/1","::/1","8000::/1"}); rules.AddRange(new[]{"NETWORK,udp,REJECT","IP-CIDR6,::/0,REJECT,no-resolve","MATCH,HOME"}); } else rules.Add("MATCH,DIRECT");
        var config=json.Deserialize<Dictionary<string,object>>(File.ReadAllText(Path.Combine(root,"tun-template.json")));
        config["ipv6"]=scope=="all"; config["rules"]=rules; config["hosts"]=new Dictionary<string,object>{{host,addresses}};
        var tun=(Dictionary<string,object>)config["tun"]; tun["route-address"]=routes; tun["inet6-address"]=scope=="all"?new[]{"fdfe:dcba:9876::1/126"}:new string[0];
        ((Dictionary<string,object>)config["dns"])["fake-ip-filter"]=new[]{host};
        return config;
    }
}
