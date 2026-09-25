import { useEffect, useState } from "react";
import OrbSettingsPage from "./OrbSettings";
import { Download, ExternalLink, RefreshCw } from "lucide-react";
import { native, rpc, type Job } from "../api";
import { useApp } from "../context";
import { Button, CheckField, Modal, Notice, Section } from "../ui";
type Release = {
  current: string;
  version: string;
  available: boolean;
  size: number;
  notes: string;
  portable: boolean;
  ready?: boolean;
};
type UpdateJob = Job & { result?: Release };
type Startup = { enabled: boolean; disabled_by_windows: boolean; path: string };
const active = (job?: Job) =>
  !!job && ["queued", "running", "cancelling"].includes(job.status);
export default function GeneralSettings() {
  const { connected, info, jobs, run, track } = useApp();
  const [startup, setStartup] = useState<Startup>(),
    [busy, setBusy] = useState(false),
    [confirm, setConfirm] = useState(false);
  const check = jobs.find((j) => j.tool === "updates.check") as
    UpdateJob | undefined;
  const download = jobs.find((j) => j.tool === "updates.download") as
    UpdateJob | undefined;
  const release =
    check?.status === "completed" && check.result?.current === info?.version
      ? check.result
      : undefined;
  const ready =
    download?.status === "completed" &&
    download.result?.ready &&
    download.result.current === info?.version &&
    download.result.version !== info?.version;
  const working = busy || active(check) || active(download);
  useEffect(() => {
    if (connected)
      void run(() => rpc<Startup>("general.startup.get")).then((v) => {
        if (v) setStartup(v);
      });
  }, [connected, run]);
  async function toggle(enabled: boolean) {
    setBusy(true);
    const v = await run(() => rpc<Startup>("general.startup.set", { enabled }));
    if (v) setStartup(v);
    setBusy(false);
  }
  async function start(method: string, params: Record<string, unknown> = {}) {
    setBusy(true);
    const j = await run(() => rpc<Job>(method, params));
    if (j) track(j);
    setBusy(false);
  }
  const jobError =
    check?.status === "failed"
      ? check.error
      : download?.status === "failed"
        ? download.error
        : undefined;
  return (
    <>
      <OrbSettingsPage />
      <Section title="启动">
        <CheckField
          label="开机自启"
          hint="登录 Windows 后自动打开工具箱，仅对当前用户生效。"
          checked={startup?.enabled || false}
          disabled={!connected || !startup || busy}
          onChange={(v) => void toggle(v)}
        />
        {startup?.disabled_by_windows && startup.enabled && (
          <Notice tone="warning">
            Windows 任务管理器已禁用此启动项，请在“启动应用”中启用 WinToolbox。
          </Notice>
        )}
      </Section>
      <Section title="软件更新">
        <p>当前版本 {info?.version || "…"}</p>
        <div className="button-row">
          <Button
            disabled={!connected || working}
            onClick={() => void start("updates.check")}
          >
            <RefreshCw size={16} />
            检查更新
          </Button>
          <Button
            onClick={() =>
              void run(() =>
                native.openExternal(
                  "https://github.com/thdlrt/WinToolbox/releases",
                ),
              )
            }
          >
            <ExternalLink size={16} />
            GitHub 发布页
          </Button>
        </div>
        {release && (
          <>
            <p>
              {release.available
                ? `发现新版本 ${release.version} · ${(release.size / 1024 / 1024).toFixed(0)} MB`
                : `当前已是最新版本（GitHub ${release.version}）`}
            </p>
            {release.available && (
              <>
                <details>
                  <summary>更新内容</summary>
                  <p
                    style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}
                  >
                    {release.notes || "此版本未提供更新说明。"}
                  </p>
                </details>
                <Button
                  variant="primary"
                  disabled={working}
                  onClick={() =>
                    void start("updates.download", { version: release.version })
                  }
                >
                  <Download size={16} />
                  下载更新
                </Button>
              </>
            )}
          </>
        )}
        {(active(check) || active(download)) && (
          <Notice>
            {active(download)
              ? `下载与校验 ${Math.round(download!.progress)}%`
              : "正在检查 GitHub…"}
            <Button
              onClick={() =>
                void run(() =>
                  rpc<Job>("jobs.cancel", {
                    id: active(download) ? download!.id : check!.id,
                  }),
                ).then((j) => {
                  if (j) track(j);
                })
              }
            >
              取消
            </Button>
          </Notice>
        )}
        {jobError && (
          <Notice tone="warning">
            {typeof jobError === "string" ? jobError : JSON.stringify(jobError)}
          </Notice>
        )}
        {ready && (
          <Notice>
            版本 {download!.result!.version} 已下载并通过校验。
            <Button
              variant="primary"
              disabled={working}
              onClick={() => setConfirm(true)}
            >
              {download!.result!.portable ? "重启并更新" : "退出并安装"}
            </Button>
          </Notice>
        )}
        <p className="muted">
          从 thdlrt/WinToolbox 的 GitHub
          正式发布获取更新，不会自动安装。免安装版更新保留 data
          中的设置、模型和记录。
        </p>
      </Section>
      {confirm && (
        <Modal title="安装更新" onClose={() => setConfirm(false)}>
          <div className="modal-body">
            <p>
              保存当前工作后继续。工具箱将退出，
              {download?.result?.portable
                ? "更新程序文件并重新打开。"
                : "打开新版安装程序。"}
              后台任务需先完成或停止。
            </p>
            <div className="modal-footer">
              <Button onClick={() => setConfirm(false)}>取消</Button>
              <Button
                variant="primary"
                busy={busy}
                onClick={async () => {
                  setBusy(true);
                  await run(() => native.installUpdate(download!.id));
                  setBusy(false);
                }}
              >
                继续更新
              </Button>
            </div>
          </div>
        </Modal>
      )}
    </>
  );
}
