import { useEffect, useRef, type ReactNode } from "react";
import {
  AlertCircle,
  Check,
  CheckCircle2,
  ChevronRight,
  FolderOpen,
  LoaderCircle,
  X,
} from "lucide-react";
import { native } from "./api";

export const cx = (...names: (string | false | null | undefined)[]) =>
  names.filter(Boolean).join(" ");
export const basename = (path: string) => path.split(/[\\/]/).pop() || path;
export const dateText = (value?: string | number) => {
  if (!value) return "—";
  const date = new Date(
    typeof value === "number" && value < 1e12 ? value * 1000 : value,
  );
  return Number.isNaN(date.valueOf())
    ? "—"
    : date.toLocaleString("zh-CN", {
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
      });
};
export const statusLabel = (value: string) =>
  ({
    queued: "等待中",
    pending: "等待中",
    running: "处理中",
    cancelling: "正在停止",
    interrupted: "已中断",
    completed: "已完成",
    succeeded: "已完成",
    success: "已完成",
    failed: "失败",
    cancelled: "已取消",
    canceled: "已取消",
  })[value] || value;
export const toolLabel = (value: string) =>
  ({
    media: "音视频处理",
    "practice.generate": "段落朗读",
    "practice.fragment": "练习片段朗读",
    knowledge: "资料库索引",
    "knowledge.ingest": "资料库索引",
    "knowledge-ingest": "资料库索引",
    "knowledge.reindex": "重建资料库索引",
    "knowledge-reindex": "重建资料库索引",
    "setup.install": "本地预设安装",
    "models.install": "模型安装",
    model_install: "模型安装",
    "model-install": "模型安装",
    "models.export": "导出模型",
    "models.import": "导入模型",
    "expenses.attach": "保存记账附件",
    "webdav.upload": "上传 WebDAV 快照",
    "webdav.restore": "恢复 WebDAV 快照",
    "backups.export": "导出备份",
    "backups.import": "恢复备份",
    backup_export: "导出备份",
    backup_import: "恢复备份",
    "backup-export": "导出备份",
    "backup-import": "恢复备份",
    plugin: "扩展工具",
    "documents.runtime.install": "文档环境安装",
  })[value] || value;
export const activeJob = (status: string) =>
  ["queued", "pending", "running", "cancelling"].includes(status);

export function Button({
  children,
  variant = "secondary",
  busy = false,
  className,
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "secondary" | "ghost" | "danger";
  busy?: boolean;
}) {
  return (
    <button
      className={cx("button", variant, className)}
      {...props}
      disabled={props.disabled || busy}
    >
      {busy && <LoaderCircle size={16} className="spin" />}
      {children}
    </button>
  );
}
export function IconButton({
  children,
  label,
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement> & { label: string }) {
  return (
    <button className="icon-button" title={label} aria-label={label} {...props}>
      {children}
    </button>
  );
}
export function Field({
  label,
  hint,
  children,
  className,
}: {
  label: string;
  hint?: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <label className={cx("field", className)}>
      <span className="field-label">{label}</span>
      {children}
      {hint && <span className="field-hint">{hint}</span>}
    </label>
  );
}
export function CheckField({
  checked,
  onChange,
  label,
  hint,
  disabled,
}: {
  checked: boolean;
  onChange: (value: boolean) => void;
  label: string;
  hint?: string;
  disabled?: boolean;
}) {
  return (
    <label className={cx("check-field", disabled && "disabled")}>
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
      />
      <span>
        <span className="check-label">{label}</span>
        {hint && <small>{hint}</small>}
      </span>
    </label>
  );
}
export function Switch({
  checked,
  onChange,
  label,
  disabled,
}: {
  checked: boolean;
  onChange: (value: boolean) => void;
  label: string;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      className={cx("switch", checked && "checked")}
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
    >
      <span />
    </button>
  );
}
export function Empty({
  title,
  children,
  action,
}: {
  title: string;
  children?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="empty-state">
      <p className="empty-title">{title}</p>
      {children && <p>{children}</p>}
      {action}
    </div>
  );
}
export function Notice({
  children,
  tone = "info",
}: {
  children: ReactNode;
  tone?: "info" | "warning" | "success";
}) {
  return (
    <div className={cx("notice", tone)}>
      {tone === "success" ? (
        <CheckCircle2 size={17} />
      ) : (
        <AlertCircle size={17} />
      )}
      <div>{children}</div>
    </div>
  );
}
export function Section({
  title,
  description,
  action,
  children,
  className,
}: {
  title?: string;
  description?: string;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={cx("section", className)}>
      {(title || action) && (
        <div className={cx("section-heading", !title && "actions-only")}>
          {title && (
            <div>
              <h2>{title}</h2>
              {description && <p>{description}</p>}
            </div>
          )}
          {action}
        </div>
      )}
      {children}
    </section>
  );
}
export function PageActions({ action }: { action: ReactNode }) {
  return <div className="page-actions">{action}</div>;
}
export function Status({ value }: { value: string }) {
  return (
    <span className={cx("status", value)}>
      <span />
      {statusLabel(value)}
    </span>
  );
}
export function Progress({ value }: { value: number }) {
  return (
    <div className="progress">
      <span style={{ width: `${Math.max(0, Math.min(100, value || 0))}%` }} />
    </div>
  );
}
export function Loading({ label = "正在加载…" }: { label?: string }) {
  return (
    <div className="loading">
      <LoaderCircle size={19} className="spin" />
      {label}
    </div>
  );
}
export function PathInput({
  value,
  onChange,
  placeholder = "选择文件夹",
  file = false,
  onError,
}: {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  file?: boolean;
  onError: (error: unknown) => void;
}) {
  const pick = async () => {
    try {
      const path = file
        ? (await native.files(false))[0]
        : await native.directory();
      if (path) onChange(path);
    } catch (error) {
      onError(error);
    }
  };
  return (
    <div className="input-action">
      <input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
      />
      <IconButton label={placeholder} onClick={pick}>
        <FolderOpen size={17} />
      </IconButton>
    </div>
  );
}
export function Modal({
  title,
  children,
  onClose,
  wide = false,
}: {
  title: string;
  children: ReactNode;
  onClose: () => void;
  wide?: boolean;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const focusable = () =>
      Array.from(
        ref.current?.querySelectorAll<HTMLElement>(
          'button, input, textarea, select, a[href], summary, [tabindex="0"]',
        ) || [],
      ).filter(
        (node) =>
          !node.matches(':disabled, [aria-hidden="true"]') &&
          node.getClientRects().length > 0,
      );
    const input = ref.current?.querySelector<HTMLElement>(
      "input:not(:disabled), textarea:not(:disabled), select:not(:disabled)",
    );
    (input || focusable()[0])?.focus();
    const handler = (event: KeyboardEvent) => {
      const dialogs = document.querySelectorAll('[role="dialog"]');
      if (dialogs[dialogs.length - 1] !== ref.current) return;
      if (event.key === "Escape" && !event.isComposing) {
        event.preventDefault();
        closeRef.current();
      }
      if (event.key === "Tab") {
        const nodes = focusable();
        if (!nodes.length) {
          event.preventDefault();
          ref.current?.focus();
          return;
        }
        const first = nodes[0],
          last = nodes[nodes.length - 1];
        if (
          event.shiftKey &&
          (document.activeElement === first ||
            !ref.current?.contains(document.activeElement))
        ) {
          event.preventDefault();
          last.focus();
        } else if (
          !event.shiftKey &&
          (document.activeElement === last ||
            !ref.current?.contains(document.activeElement))
        ) {
          event.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener("keydown", handler);
    return () => {
      document.removeEventListener("keydown", handler);
      if (previous?.isConnected) previous.focus();
    };
  }, []);
  return (
    <div
      className="modal-backdrop"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        ref={ref}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className={cx("modal", wide && "wide")}
      >
        <div className="modal-heading">
          <h2>{title}</h2>
          <IconButton label="关闭" onClick={onClose}>
            <X size={19} />
          </IconButton>
        </div>
        {children}
      </div>
    </div>
  );
}
export function StepMark({ n, done = false }: { n: number; done?: boolean }) {
  return (
    <span className={cx("step-mark", done && "done")}>
      {done ? <Check size={14} /> : n}
    </span>
  );
}
export function Go({
  children,
  onClick,
}: {
  children: ReactNode;
  onClick: () => void;
}) {
  return (
    <button className="text-link" onClick={onClick}>
      {children}
      <ChevronRight size={15} />
    </button>
  );
}
