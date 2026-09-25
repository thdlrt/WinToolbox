import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { rpc, native, errorText, type Job } from "../api";
import { useApp } from "../context";
import {
  Button,
  Field,
  Section,
  Notice,
  PathInput,
  CheckField,
  Modal,
  Empty,
  activeJob,
} from "../ui";
import {
  Plus,
  RefreshCw,
  Settings2,
  MoreHorizontal,
  Search,
  FileText,
  ListTodo,
  Check,
} from "lucide-react";
const MemoryMarkdown = lazy(() => import("./MemoryMarkdown"));
import "../projectMemory.css";
import MemoryBoard from "./MemoryBoard";
const knowledgeTypeLabel = (value?: string) =>
  (
    ({
      lesson: "经验",
      decision: "决策",
      preference: "偏好",
      fact: "事实",
      procedure: "方法",
      pitfall: "陷阱",
      reference: "参考",
      architecture: "架构",
      evidence: "证据",
      "skill-note": "技能笔记",
      "legacy-log": "历史记录",
    }) as Record<string, string>
  )[value || ""] ||
  value ||
  "知识";
type Principles = { text: string; hash: string; path?: string };
type PrinciplesTarget = "global" | "template";
type Scope = "project" | "global";
export type Entry = {
  id?: string;
  title: string;
  body: string;
  kind: "knowledge" | "task";
  scope: Scope;
  project_id?: string;
  knowledge_type?: string;
  status?: string;
  heads?: string[];
  conflict?: boolean;
  related_ids?: string[];
  attachments?: { hash: string; name?: string }[];
  promotion?: { state: string; decision?: { reason?: string } };
  created_at?: string;
  updated_at?: string;
  versions?: { op_id: string; data: Entry; created_at?: string }[];
};
export type Project = {
  id: string;
  name: string;
  available: boolean;
  subscribed: boolean;
  locations: {
    id: string;
    kind: string;
    path: string;
    host?: string;
    available?: boolean;
  }[];
};
type SshTarget = {
  id: string;
  host: string;
  remote_root: string;
  project_id?: string;
  project_path?: string;
  project_ids?: string[];
  enabled: boolean;
};
type Info = { library_id: string; device_id: string };
type Curation = {
  policy?: {
    conflict?: boolean;
    curation_policy?: { enabled: boolean; device_id: string };
  };
};
export default function ProjectMemoryPage() {
  const {
    connected,
    jobs,
    track,
    error,
    success,
    navigate,
    setBeforeNavigate,
  } = useApp();
  const [view, setView] = useState<"board" | "library">("board");
  const [actionId, setActionId] = useState("");
  const [syncConfigured, setSyncConfigured] = useState(false);
  const finishedActions = useRef(new Set<string>());
  const [syncTab, setSyncTab] = useState("webdav");
  const [previewBody, setPreviewBody] = useState(false);
  const [loadingEntries, setLoadingEntries] = useState(true);
  const [entryError, setEntryError] = useState("");
  const [relatedText, setRelatedText] = useState("");
  const moreRef = useRef<HTMLDetailsElement>(null);
  const busyRef = useRef(false);
  const editorOpener = useRef<HTMLElement | null>(null);
  const [info, setInfo] = useState<Info>(),
    [projects, setProjects] = useState<Project[]>([]),
    [entries, setEntries] = useState<Entry[]>([]),
    [selected, setSelected] = useState(""),
    [scope, setScope] = useState<Scope>("project");
  const [filters, setFilters] = useState<Record<string, string>>({}),
    [query, setQuery] = useState(""),
    [done, setDone] = useState(false),
    [draft, setDraft] = useState<Entry>(),
    [original, setOriginal] = useState(""),
    [busy, setBusy] = useState(false);
  const [panel, setPanel] = useState<
      "create" | "bind" | "sync" | "principles"
    >(),
    [name, setName] = useState(""),
    [path, setPath] = useState(""),
    [host, setHost] = useState(""),
    [kind, setKind] = useState("local"),
    [contextDir, setContextDir] = useState(""),
    [remoteRoot, setRemoteRoot] = useState(""),
    [library, setLibrary] = useState("");
  const [targets, setTargets] = useState<SshTarget[]>([]),
    [targetId, setTargetId] = useState(""),
    [targetEnabled, setTargetEnabled] = useState(true),
    [sshProjects, setSshProjects] = useState<string[]>([]),
    [sshBindingProject, setSshBindingProject] = useState("");
  const [syncError, setSyncError] = useState(""),
    [autoSync, setAutoSync] = useState(false),
    [curation, setCuration] = useState<Curation>({});
  const [principlesTarget, setPrinciplesTarget] =
    useState<PrinciplesTarget>("global");
  const [principles, setPrinciples] = useState<Principles>();
  const [principlesText, setPrinciplesText] = useState("");
  const panelForm = JSON.stringify({
    name,
    path,
    host,
    kind,
    contextDir,
    remoteRoot,
    library,
    targetId,
    targetEnabled,
    sshProjects,
    sshBindingProject,
  });
  const [panelEdited, setPanelEdited] = useState(false);
  const panelBaseline = useRef(panelForm);
  useEffect(() => {
    panelBaseline.current = panelForm;
    setPanelEdited(false);
  }, [panel]);
  const panelDirty =
    panelEdited &&
    !!panel &&
    ["create", "bind", "sync"].includes(panel) &&
    panelForm !== panelBaseline.current;
  const principlesDirty = !!principles && principlesText !== principles.text;
  const dirty =
      !!draft &&
      (JSON.stringify(draft) !== original ||
        relatedText !== (draft.related_ids || []).join(", ")),
    dirtyRef = useRef(dirty);
  dirtyRef.current = dirty || principlesDirty || panelDirty;
  const leave = useCallback(
    () => !dirtyRef.current || window.confirm("有未保存的内容，放弃修改？"),
    [],
  );
  useEffect(() => {
    setBeforeNavigate?.(leave);
    return () => setBeforeNavigate?.(undefined);
  }, [leave, setBeforeNavigate]);
  const memoryJobs = jobs.filter((j) =>
      j.tool.startsWith("memory."),
    ) as (Job & {
      result?: { warnings?: (string | { host?: string; message: string })[] };
    })[],
    stamp = memoryJobs.map((j) => j.id + j.status).join("|");
  const refresh = useCallback(async () => {
    const [i, p, s, c, t] = await Promise.all([
      rpc<Info>("memory.info"),
      rpc<{ projects: Project[] }>("memory.projects"),
      rpc<{
        last_error?: string;
        auto_sync: boolean;
        webdav_configured?: boolean;
      }>("memory.sync.status"),
      rpc<Curation>("memory.curation.status"),
      rpc<{ targets: SshTarget[] }>("memory.ssh.targets"),
    ]);
    setInfo(i);
    setProjects(p.projects);
    setSelected((previous) => previous || (p.projects[0]?.id ?? ""));
    setSyncError(s.last_error || "");
    setAutoSync(s.auto_sync);
    setSyncConfigured(!!s.webdav_configured);
    setCuration(c);
    setTargets(t.targets);
  }, []);
  const entryRequest = useRef(0);
  const refreshEntries = useCallback(async () => {
    const request = ++entryRequest.current;
    if (view !== "board" && scope === "project" && !selected) {
      setEntries([]);
      setLoadingEntries(false);
      return;
    }
    setLoadingEntries(true);
    setEntryError("");
    try {
      const result = await rpc<{ entries: Entry[] }>("memory.entries", {
        ...(view === "board"
          ? { kind: "task", include_done: true }
          : {
              scope,
              ...(scope === "project" ? { project_id: selected } : {}),
              include_done: done,
              query,
            }),
      });
      if (request === entryRequest.current)
        setEntries(
          result.entries.filter(
            (e) => !(e as Entry & { internal?: boolean }).internal,
          ),
        );
    } catch (reason) {
      if (request === entryRequest.current) setEntryError(errorText(reason));
    } finally {
      if (request === entryRequest.current) setLoadingEntries(false);
    }
  }, [view, scope, selected, done, query]);
  useEffect(() => {
    if (connected) void refresh().catch(error);
  }, [connected, refresh, error, stamp]);
  useEffect(() => {
    if (!connected) return;
    setLoadingEntries(true);
    const t = setTimeout(() => void refreshEntries().catch(error), 180);
    return () => {
      clearTimeout(t);
      entryRequest.current++;
    };
  }, [connected, refreshEntries, error, stamp]);
  async function act(fn: () => Promise<void>) {
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    try {
      await fn();
    } catch (e) {
      error(e);
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  }
  async function loadPrinciples(target: PrinciplesTarget) {
    if (principlesDirty && !window.confirm("原则有未保存的修改，放弃修改？"))
      return;
    await act(async () => {
      const value = await rpc<Principles>("memory.principles.get", { target });
      setPrinciplesTarget(target);
      setPrinciples(value);
      setPrinciplesText(value.text);
      setPanel("principles");
    });
  }
  async function start(method: string, params: Record<string, unknown> = {}) {
    const job = await rpc<Job>(method, params);
    setActionId(job.id);
    track(job, "正在处理…");
  }
  function choose(id: string, next: Scope = "project") {
    if (!leave()) return;
    setView("library");
    setSelected(id);
    setScope(next);
    setQuery("");
    setEntryError("");
    if (id !== selected || next !== scope) setEntries([]);
    setDraft(undefined);
  }
  function patch(value: Partial<Entry>) {
    setDraft((d) => (d ? { ...d, ...value } : d));
  }
  function create(entryKind: Entry["kind"]) {
    if (!leave()) return;
    editorOpener.current = document.activeElement as HTMLElement;
    setDraft({
      title: "",
      body: "",
      kind: entryKind,
      scope,
      ...(scope === "project" ? { project_id: selected } : {}),
      ...(entryKind === "task"
        ? { status: "active" }
        : { knowledge_type: "经验" }),
      heads: [],
      related_ids: [],
    });
    setOriginal("");
    setRelatedText("");
    setPreviewBody(false);
  }
  async function open(entry: Entry) {
    if (!leave()) return;
    editorOpener.current = document.activeElement as HTMLElement;
    await act(async () => {
      const value = await rpc<Entry>("memory.entry.get", { id: entry.id });
      setDraft(value);
      setRelatedText((value.related_ids || []).join(", "));
      setPreviewBody(false);
      setOriginal(JSON.stringify(value));
    });
  }
  async function save(resolve = false) {
    if (!draft?.title.trim()) return;
    await act(async () => {
      const entry = {
        ...draft,
        title: draft.title.trim(),
        related_ids: relatedText
          .split(/[,，]/)
          .map((v) => v.trim())
          .filter(Boolean),
      };
      delete entry.heads;
      delete entry.versions;
      delete entry.conflict;
      const saved = await rpc<Entry>(
        resolve ? "memory.conflict.resolve" : "memory.entry.save",
        {
          ...(resolve ? { id: draft.id } : {}),
          entry,
          parents: draft.heads || [],
        },
      );
      setDraft(saved);
      setRelatedText((saved.related_ids || []).join(", "));
      setOriginal(JSON.stringify(saved));
      await refreshEntries();
      success("已保存。");
    });
  }
  const sshParams = () => ({
    host,
    remote_root: remoteRoot,
    project_ids: sshProjects,
    ...(sshBindingProject && path
      ? { project_id: sshBindingProject, project_path: path }
      : {}),
  });
  const assigned = curation.policy?.curation_policy;
  const current = projects.find((p) => p.id === selected),
    filter = filters[scope === "project" ? selected : scope] || "all";
  const visibleEntries = entries.filter(
    (e) => filter === "all" || e.kind === filter,
  );
  const syncRunning = memoryJobs.some(
    (j) =>
      activeJob(j.status) &&
      ["memory.sync.run", "memory.ssh.sync"].includes(j.tool),
  );
  const currentAction = memoryJobs.find((job) => job.id === actionId);
  useEffect(() => {
    if (
      !currentAction ||
      activeJob(currentAction.status) ||
      finishedActions.current.has(currentAction.id)
    )
      return;
    finishedActions.current.add(currentAction.id);
    if (currentAction.error) error(currentAction.error);
    else if (currentAction.status === "completed") {
      const warnings = currentAction.result?.warnings;
      if (warnings?.length)
        error(
          warnings
            .map((w) =>
              typeof w === "string"
                ? w
                : (w.host ? w.host + "：" : "") + w.message,
            )
            .join("；"),
        );
      else success("处理完成。");
    }
  }, [currentAction, error, success]);
  const discovery = memoryJobs.find(
    (j) => j.tool === "memory.sync.libraries" && j.status === "completed",
  ) as (Job & { result?: { libraries: { library_id: string }[] } }) | undefined;
  return (
    <div className="memory-page">
      <div className="memory-topbar">
        <Button
          variant="primary"
          disabled={!connected || busy}
          onClick={() => {
            setName("");
            setPanel("create");
          }}
        >
          <Plus size={16} />
          登记项目
        </Button>
        <div className="memory-topbar-end">
          <Button
            disabled={!connected || busy || syncRunning}
            onClick={() => void act(() => start("memory.sync.run"))}
          >
            <RefreshCw size={15} className={syncRunning ? "spin" : ""} />
            {syncRunning ? "同步中" : "立即同步"}
          </Button>
          <Button
            disabled={!connected || busy}
            onClick={() => {
              setPath("");
              setHost("");
              setRemoteRoot("");
              setSshProjects(
                selected
                  ? [selected]
                  : projects.filter((p) => p.subscribed).map((p) => p.id),
              );
              setSshBindingProject(selected);
              setTargetId("");
              setSyncTab("webdav");
              setPanel("sync");
            }}
          >
            <Settings2 size={15} />
            同步设置
          </Button>
          <details
            className="memory-more"
            ref={moreRef}
            onKeyDown={(e) => {
              if (e.key === "Escape") {
                e.currentTarget.open = false;
                e.currentTarget.querySelector("summary")?.focus();
              }
            }}
            onBlur={(e) => {
              if (!e.currentTarget.contains(e.relatedTarget as Node))
                e.currentTarget.open = false;
            }}
          >
            <summary aria-label="更多操作">
              <MoreHorizontal size={18} />
            </summary>
            <div
              className="memory-menu"
              onClick={() => {
                if (moreRef.current) moreRef.current.open = false;
              }}
            >
              <Button
                variant="ghost"
                disabled={!connected || busy}
                onClick={() => void loadPrinciples("global")}
              >
                基本原则
              </Button>
              <Button
                variant="ghost"
                disabled={!connected || busy}
                onClick={() =>
                  void act(() =>
                    start(
                      "memory.export",
                      view === "library" && scope === "project" && selected
                        ? { project_id: selected }
                        : {},
                    ),
                  )
                }
              >
                {view === "library" && scope === "project" && selected
                  ? "导出当前项目"
                  : "导出全部记忆"}
              </Button>

              <Button
                variant="ghost"
                disabled={!connected || busy}
                onClick={() =>
                  void act(async () => {
                    await refresh();
                    await refreshEntries();
                  })
                }
              >
                刷新记录
              </Button>
            </div>
          </details>
        </div>
      </div>
      {syncError && (
        <Notice tone="warning">
          上次同步未完成：{syncError}。本地记录仍已保留。
        </Notice>
      )}
      <div className="memory-layout">
        <aside className="memory-projects">
          <Button
            variant={view === "board" ? "primary" : "ghost"}
            onClick={() => {
              if (!leave()) return;
              setDraft(undefined);
              setView("board");
            }}
          >
            任务看板
          </Button>
          <div className="button-row">
            <Button
              variant={
                view === "library" && scope === "global" ? "primary" : "ghost"
              }
              onClick={() => choose("", "global")}
            >
              全局记忆
            </Button>
          </div>
          {projects.map((p) => (
            <article
              className={
                "memory-project " +
                (view === "library" && scope === "project" && selected === p.id
                  ? "selected"
                  : "")
              }
              key={p.id}
            >
              <button className="memory-name" onClick={() => choose(p.id)}>
                {p.name}
              </button>
              <small>
                {p.available ? "本机已关联位置" : "仅同步 · 本机无目录"}
                {!p.subscribed ? " · 未订阅正文" : ""}
              </small>
              <div className="memory-filters">
                {[
                  ["all", "全部"],
                  ["knowledge", "知识"],
                  ["task", "任务"],
                ].map(([v, label]) => (
                  <button
                    aria-pressed={(filters[p.id] || "all") === v}
                    key={v}
                    onClick={() => {
                      if (!leave()) return;
                      setView("library");
                      setSelected(p.id);
                      setQuery("");
                      setScope("project");
                      setDraft(undefined);
                      setFilters((f) => ({ ...f, [p.id]: v }));
                    }}
                  >
                    {label}
                  </button>
                ))}
              </div>
            </article>
          ))}
          {!projects.length && (
            <p className="muted">登记项目，或从 WebDAV 加入已有记忆库。</p>
          )}
        </aside>
        <div className="memory-main">
          {view === "board" ? (
            <MemoryBoard
              entries={entries}
              projects={projects}
              busy={busy || !connected}
              loading={loadingEntries}
              error={entryError}
              onOpen={(entry) => void open(entry)}
              onRetry={() => void refreshEntries()}
            />
          ) : (
            <Section
              className="memory-list-panel"
              title={
                scope === "project" ? current?.name || "选择项目" : "全局记忆"
              }
              action={
                current &&
                scope === "project" && (
                  <Button
                    onClick={() => {
                      setPath("");
                      setHost("");
                      setKind("local");
                      setContextDir("");
                      setPanel("bind");
                    }}
                  >
                    项目设置
                  </Button>
                )
              }
            >
              {scope !== "project" && (
                <p className="memory-scope-help">
                  跨项目共享的任务和知识，会同步到同一记忆库的其他设备。
                </p>
              )}
              {(scope !== "project" || current) && (
                <>
                  <div className="memory-toolbar">
                    <div className="memory-search">
                      <Search size={16} />
                      <input
                        aria-label="搜索记忆"
                        placeholder="搜索标题和正文"
                        value={query}
                        onChange={(e) => setQuery(e.target.value)}
                      />
                    </div>
                    <Button
                      variant="primary"
                      disabled={!connected || busy}
                      onClick={() => create("knowledge")}
                    >
                      <Plus size={15} />
                      知识
                    </Button>
                    <Button
                      disabled={!connected || busy}
                      onClick={() => create("task")}
                    >
                      <Plus size={15} />
                      任务
                    </Button>
                  </div>
                  {scope !== "project" && (
                    <div className="memory-filters" aria-label="记录类型">
                      {[
                        ["all", "全部"],
                        ["knowledge", "知识"],
                        ["task", "任务"],
                      ].map(([value, label]) => (
                        <button
                          key={value}
                          aria-pressed={filter === value}
                          onClick={() =>
                            setFilters((f) => ({ ...f, [scope]: value }))
                          }
                        >
                          {label}
                        </button>
                      ))}
                    </div>
                  )}
                  <div className="memory-list-meta">
                    <span>
                      {loadingEntries
                        ? "正在读取…"
                        : `${visibleEntries.length} 条记录`}
                    </span>
                    <CheckField
                      checked={done}
                      onChange={setDone}
                      label="含已完成 / 归档"
                    />
                  </div>
                </>
              )}
              <div className="memory-entries">
                {visibleEntries.map((e) => (
                  <button
                    disabled={busy || !connected}
                    className={draft?.id === e.id ? "selected" : ""}
                    key={e.id}
                    onClick={() => void open(e)}
                  >
                    <span className="memory-entry-title">
                      {e.kind === "task" ? (
                        <ListTodo size={16} />
                      ) : (
                        <FileText size={16} />
                      )}
                      <strong>{e.title}</strong>
                    </span>
                    <span className="memory-entry-preview">
                      {(
                        e.body
                          .split("\n")
                          .find(
                            (line) =>
                              line.trim() &&
                              !/^(#|```|\||[-*]\s)/.test(line.trim()),
                          ) || e.body.split("\n")[0]
                      )
                        .replace(/[#*`>]/g, "")
                        .slice(0, 120) || "暂无正文"}
                    </span>
                    <small>
                      {e.kind === "knowledge"
                        ? knowledgeTypeLabel(e.knowledge_type)
                        : { active: "进行中", blocked: "受阻", done: "已完成" }[
                            e.status || "active"
                          ] || e.status}
                      {e.conflict ? " · 版本冲突" : ""}
                    </small>
                  </button>
                ))}
              </div>
              {entryError ? (
                <Notice tone="warning">
                  {entryError}
                  <Button onClick={() => void refreshEntries()}>重试</Button>
                </Notice>
              ) : (
                !loadingEntries &&
                !visibleEntries.length && (
                  <Empty
                    title={
                      scope === "project" && !current
                        ? "登记项目，开始记录"
                        : query
                          ? "没有找到匹配记录"
                          : filter === "task"
                            ? "暂无任务"
                            : filter === "knowledge"
                              ? "暂无知识"
                              : "暂无记录"
                    }
                  >
                    {query
                      ? "试试更短的关键词，或清空搜索。"
                      : current && !current.subscribed
                        ? "在项目设置中订阅正文，再点击立即同步。"
                        : "记录值得保留的经验，或创建需要跟进的任务。"}
                  </Empty>
                )
              )}
            </Section>
          )}
          {draft && (
            <Modal
              wide
              title={
                draft.id
                  ? draft.kind === "task"
                    ? "任务"
                    : "知识"
                  : "新建记录"
              }
              onClose={() => {
                if (busy || !leave()) return;
                setDraft(undefined);
                requestAnimationFrame(() => {
                  if (editorOpener.current?.isConnected)
                    editorOpener.current.focus();
                  else
                    document
                      .querySelector<HTMLInputElement>(".memory-search input")
                      ?.focus();
                });
              }}
            >
              <div
                className="modal-body memory-editor"
                onKeyDown={(e) => {
                  if ((e.ctrlKey || e.metaKey) && e.key === "s") {
                    e.preventDefault();
                    if (
                      !e.nativeEvent.isComposing &&
                      !draft.conflict &&
                      dirty &&
                      !busy
                    )
                      void save();
                  }
                }}
              >
                <fieldset
                  className="memory-editor-fields"
                  disabled={busy || !connected}
                >
                  {draft.conflict && (
                    <Notice tone="warning">
                      存在并行修改，请检查全部版本后保存为合并结果。
                    </Notice>
                  )}
                  <Field label="标题">
                    <input
                      value={draft.title}
                      onChange={(e) => patch({ title: e.target.value })}
                    />
                  </Field>
                  <div className="form-grid">
                    <Field label="内容">
                      <select
                        value={draft.kind}
                        onChange={(e) =>
                          patch(
                            e.target.value === "task"
                              ? { kind: "task", status: "active" }
                              : {
                                  kind: "knowledge",
                                  status: undefined,
                                  knowledge_type:
                                    draft.knowledge_type || "经验",
                                },
                          )
                        }
                      >
                        <option value="knowledge">知识</option>
                        <option value="task">任务</option>
                      </select>
                    </Field>
                    {draft.kind === "knowledge" ? (
                      <Field label="知识类型">
                        <select
                          value={draft.knowledge_type || "经验"}
                          onChange={(e) =>
                            patch({ knowledge_type: e.target.value })
                          }
                        >
                          {Array.from(
                            new Set([
                              draft.knowledge_type || "经验",
                              "lesson",
                              "procedure",
                              "decision",
                              "fact",
                              "pitfall",
                              "preference",
                              "reference",
                              "architecture",
                              "evidence",
                              "skill-note",
                            ]),
                          ).map((value) => (
                            <option value={value} key={value}>
                              {knowledgeTypeLabel(value)}
                            </option>
                          ))}
                        </select>
                      </Field>
                    ) : (
                      <Field label="状态">
                        <select
                          value={draft.status || "active"}
                          onChange={(e) => patch({ status: e.target.value })}
                        >
                          <option value="active">进行中</option>
                          <option value="blocked">受阻</option>
                          <option value="done">已完成（归档）</option>
                        </select>
                      </Field>
                    )}
                  </div>
                  <div className="memory-editor-tabs">
                    <span className="muted">正文</span>
                    <div className="memory-segments">
                      <button
                        type="button"
                        aria-pressed={!previewBody}
                        onClick={() => setPreviewBody(false)}
                      >
                        编辑
                      </button>
                      <button
                        type="button"
                        aria-pressed={previewBody}
                        onClick={() => setPreviewBody(true)}
                      >
                        预览
                      </button>
                    </div>
                  </div>
                  {previewBody ? (
                    <Suspense
                      fallback={
                        <div className="memory-markdown" role="status">
                          正在生成预览…
                        </div>
                      }
                    >
                      <MemoryMarkdown body={draft.body} onError={error} />
                    </Suspense>
                  ) : (
                    <Field label="正文（Markdown）">
                      <textarea
                        className="memory-body"
                        value={draft.body}
                        onChange={(e) => patch({ body: e.target.value })}
                      />
                    </Field>
                  )}

                  <details className="memory-related">
                    <summary>
                      关联记录
                      {draft.related_ids?.length
                        ? `（${draft.related_ids.length}）`
                        : ""}
                    </summary>
                    <Field label="关联记录 ID（逗号分隔）">
                      <input
                        value={relatedText}
                        onChange={(e) => setRelatedText(e.target.value)}
                      />
                    </Field>
                  </details>
                  <div className="button-row memory-editor-actions">
                    <Button
                      variant="primary"
                      busy={busy}
                      disabled={
                        !connected ||
                        !dirty ||
                        !draft.title.trim() ||
                        draft.conflict
                      }
                      onClick={() => void save()}
                    >
                      <Check size={15} />
                      {dirty ? "保存修改" : "已保存"}
                    </Button>
                    {draft.conflict && (
                      <Button
                        disabled={!draft.title.trim()}
                        busy={busy}
                        onClick={() => void save(true)}
                      >
                        保存为合并结果
                      </Button>
                    )}
                    <Button
                      disabled={busy}
                      onClick={() =>
                        void act(async () => {
                          const files = await native.files();
                          if (!files.length) return;
                          const attachments = [...(draft.attachments || [])];
                          for (const file of files)
                            attachments.push(
                              await rpc<{ hash: string; name?: string }>(
                                "memory.attachment.add",
                                { path: file },
                              ),
                            );
                          patch({ attachments });
                        })
                      }
                    >
                      添加附件
                    </Button>
                    {draft.id &&
                      draft.kind === "knowledge" &&
                      draft.scope === "project" && (
                        <Button
                          disabled={busy || dirty}
                          onClick={() =>
                            void act(async () => {
                              await rpc("memory.promote", { id: draft.id });
                              success("已提交全局候选。");
                            })
                          }
                        >
                          提炼到全局候选
                        </Button>
                      )}
                    {draft.id && draft.promotion?.state === "candidate" && (
                      <Button
                        disabled={busy || dirty}
                        onClick={() =>
                          void act(() =>
                            start("memory.curate", { id: draft.id }),
                          )
                        }
                      >
                        AI 整理
                      </Button>
                    )}
                    <span className="muted">
                      {dirty ? "未保存 · Ctrl+S 保存" : "修改已保存"}
                    </span>
                  </div>
                  {draft.attachments?.map((attachment, i) => (
                    <div
                      key={attachment.hash + String(i)}
                      className="button-row"
                    >
                      <Button
                        onClick={() =>
                          void act(async () => {
                            const value = await rpc<{ path: string }>(
                              "memory.attachment.path",
                              { hash: attachment.hash, name: attachment.name },
                            );
                            await native.open(value.path);
                          })
                        }
                      >
                        {attachment.name || attachment.hash.slice(0, 12)}
                      </Button>
                      <Button
                        variant="ghost"
                        onClick={() =>
                          patch({
                            attachments: draft.attachments?.filter(
                              (_, index) => index !== i,
                            ),
                          })
                        }
                      >
                        移除此附件关联
                      </Button>
                    </div>
                  ))}
                  {draft.promotion && (
                    <p className="muted">
                      {(
                        {
                          candidate: "待整理候选",
                          accepted: "已纳入全局记忆",
                          rejected: "已排除",
                        } as Record<string, string>
                      )[draft.promotion.state] || draft.promotion.state}
                      {draft.promotion.decision?.reason
                        ? " · " + draft.promotion.decision.reason
                        : ""}
                    </p>
                  )}
                  {draft.conflict &&
                    draft.versions?.map((v) => (
                      <details key={v.op_id}>
                        <summary>并行版本 · {v.op_id}</summary>
                        <h3>{v.data.title}</h3>
                        <pre className="memory-version">{v.data.body}</pre>
                        <Button
                          onClick={() => (
                            setRelatedText(
                              (v.data.related_ids || []).join(", "),
                            ),
                            patch({
                              ...v.data,
                              id: draft.id,
                              heads: draft.heads,
                              versions: draft.versions,
                              conflict: true,
                            })
                          )}
                        >
                          以此版本为合并起点
                        </Button>
                      </details>
                    ))}
                </fieldset>
              </div>
            </Modal>
          )}
        </div>
      </div>
      {currentAction && activeJob(currentAction.status) && (
        <div className="memory-action" role="status">
          <span>{currentAction.message || "正在处理…"}</span>
          <Button
            disabled={busy}
            onClick={() =>
              void act(async () => {
                track(
                  await rpc<Job>("jobs.cancel", { id: currentAction.id }),
                  "正在停止…",
                );
              })
            }
          >
            停止
          </Button>
        </div>
      )}
      {currentAction?.status === "completed" &&
        !!currentAction.artifacts?.length && (
          <div className="memory-action" role="status">
            {currentAction.artifacts.map((artifact, index) => (
              <Button
                key={index}
                onClick={() => void native.open(artifact.path).catch(error)}
              >
                打开{artifact.label || "导出结果"}
              </Button>
            ))}
            <Button variant="ghost" onClick={() => setActionId("")}>
              收起
            </Button>
          </div>
        )}
      {panel && (
        <Modal
          wide={panel !== "create"}
          title={
            {
              create: "登记项目",
              bind: "项目设置",
              sync: "设备与同步",
              principles: "基本原则",
            }[panel]
          }
          onClose={() => {
            if (busy) return;
            if (panelDirty && !window.confirm("设置有未保存的修改，放弃修改？"))
              return;
            if (
              panel === "principles" &&
              principlesDirty &&
              !window.confirm("原则有未保存的修改，放弃修改？")
            )
              return;
            if (panel === "principles") {
              setPrinciples(undefined);
              setPrinciplesText("");
            }
            setPanel(undefined);
          }}
        >
          <div
            className="modal-body memory-settings"
            onChangeCapture={(e) => {
              if (
                (e.target as HTMLElement).matches(
                  'input:not([type="checkbox"]),textarea,select',
                )
              )
                setPanelEdited(true);
            }}
          >
            {panel === "principles" && (
              <>
                <Field label="编辑范围">
                  <select
                    value={principlesTarget}
                    disabled={busy}
                    onChange={(e) =>
                      void loadPrinciples(e.target.value as PrinciplesTarget)
                    }
                  >
                    <option value="global">个人全局原则</option>
                    <option value="template">新项目模板</option>
                  </select>
                </Field>
                <p className="muted">
                  个人全局原则只修改本机全局 AGENTS.md
                  的托管段，保留其他内容。新项目模板仅在项目首次初始化时使用，不覆盖已有项目原则，也不复制全局原则。
                </p>
                <Field label="原则内容（Markdown）">
                  <textarea
                    className="memory-body"
                    value={principlesText}
                    disabled={!principles || busy}
                    onChange={(e) => setPrinciplesText(e.target.value)}
                  />
                </Field>
                {principles?.path && (
                  <p className="muted memory-version">{principles.path}</p>
                )}
                <Button
                  variant="primary"
                  busy={busy}
                  disabled={!principles || !principlesDirty}
                  onClick={() =>
                    void act(async () => {
                      if (!principles) return;
                      const saved = await rpc<Principles>(
                        "memory.principles.save",
                        {
                          target: principlesTarget,
                          text: principlesText,
                          hash: principles.hash,
                        },
                      );
                      setPrinciples(saved);
                      setPrinciplesText(saved.text);
                      success("原则已保存。");
                    })
                  }
                >
                  保存原则
                </Button>
                <Button
                  disabled={busy}
                  onClick={() => void loadPrinciples(principlesTarget)}
                >
                  重新读取
                </Button>
              </>
            )}
            {panel === "create" && (
              <>
                <Field label="项目名称">
                  <input
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                  />
                </Field>
                <p className="muted">
                  登记后可关联已有目录；其他设备可选择只查看知识。
                </p>
                <Button
                  busy={busy}
                  disabled={!name.trim()}
                  onClick={() =>
                    void act(async () => {
                      const p = await rpc<Project>("memory.project.create", {
                        name: name.trim(),
                      });
                      await refresh();
                      setView("library");
                      setSelected(p.id);
                      setScope("project");
                      setDraft(undefined);
                      setQuery("");
                      setPanel(undefined);
                    })
                  }
                >
                  登记
                </Button>
              </>
            )}
            {panel === "bind" && (
              <>
                {current && scope === "project" && (
                  <>
                    <div className="memory-locations">
                      {(current.locations || []).map((l) => (
                        <div key={l.id}>
                          <span>
                            {l.host ? l.host + ":" : ""}
                            {l.path}
                          </span>
                          {l.kind === "local" &&
                            current.available &&
                            l.available !== false && (
                              <Button
                                onClick={() =>
                                  void native.open(l.path).catch(error)
                                }
                              >
                                打开
                              </Button>
                            )}
                          {l.kind === "ssh" && (
                            <Button
                              onClick={() => {
                                setHost(l.host || "");
                                setPath(l.path);
                                setSshProjects([current.id]);
                                setSshBindingProject(current.id);
                                setTargetId("");
                                setPanel("sync");
                              }}
                            >
                              远程同步
                            </Button>
                          )}
                        </div>
                      ))}
                    </div>
                    <CheckField
                      checked={current.subscribed}
                      onChange={(enabled) =>
                        void act(async () => {
                          await rpc("memory.project.subscribe", {
                            project_id: current.id,
                            enabled,
                          });
                          await refresh();
                        })
                      }
                      label="此设备同步项目正文"
                      hint="取消订阅停止后续拉取，保留已缓存内容。"
                    />
                  </>
                )}
                <hr className="memory-divider" />
                <Field label="位置类型">
                  <select
                    value={kind}
                    onChange={(e) => setKind(e.target.value)}
                  >
                    <option value="local">本机目录</option>
                    <option value="ssh">SSH 远程目录</option>
                  </select>
                </Field>
                {kind === "ssh" && (
                  <Field
                    label="服务器（SSH 主机）"
                    hint="使用你已有的 SSH 别名，例如 njucg2；认证使用系统 SSH 配置。"
                  >
                    <input
                      value={host}
                      onChange={(e) => setHost(e.target.value)}
                    />
                  </Field>
                )}
                <Field label="已有项目目录">
                  {kind === "local" ? (
                    <PathInput
                      value={path}
                      onChange={setPath}
                      onError={error}
                    />
                  ) : (
                    <input
                      value={path}
                      onChange={(e) => setPath(e.target.value)}
                      placeholder="/home/user/project"
                    />
                  )}
                </Field>
                {kind === "local" && (
                  <Field label="知识连接目录">
                    <select
                      value={contextDir}
                      onChange={(e) => setContextDir(e.target.value)}
                    >
                      <option value="">自动</option>
                      <option value=".agent">.agent</option>
                      <option value=".agents">.agents</option>
                    </select>
                  </Field>
                )}
                <p className="muted">
                  位置只保存在此设备。移动目录后重新关联同一项目。
                </p>
                <Button
                  busy={busy}
                  disabled={!path || (kind === "ssh" && !host)}
                  onClick={() =>
                    void act(async () => {
                      await rpc("memory.project.bind", {
                        project_id: selected,
                        path,
                        kind,
                        ...(kind === "ssh" ? { host } : {}),
                        ...(kind === "local" && contextDir
                          ? { context_dir: contextDir }
                          : {}),
                      });
                      await refresh();
                      setPanel(undefined);
                    })
                  }
                >
                  关联
                </Button>
              </>
            )}
            {panel === "sync" && (
              <>
                <div
                  className="memory-settings-tabs"
                  role="tablist"
                  aria-label="同步设置分类"
                  onKeyDown={(e) => {
                    const tabs = ["webdav", "ssh", "ai"];
                    const index = tabs.indexOf(syncTab);
                    let next = index;
                    if (e.key === "ArrowRight") next = (index + 1) % 3;
                    else if (e.key === "ArrowLeft") next = (index + 2) % 3;
                    else if (e.key === "Home") next = 0;
                    else if (e.key === "End") next = 2;
                    else return;
                    e.preventDefault();
                    setSyncTab(tabs[next]);
                    (
                      e.currentTarget.querySelectorAll("button")[
                        next
                      ] as HTMLButtonElement
                    ).focus();
                  }}
                >
                  {[
                    ["webdav", "其他电脑"],
                    ["ssh", "SSH 服务器"],
                    ["ai", "AI 整理"],
                  ].map(([value, label]) => (
                    <button
                      role="tab"
                      tabIndex={syncTab === value ? 0 : -1}
                      key={value}
                      aria-selected={syncTab === value}
                      onClick={() => setSyncTab(value)}
                    >
                      {label}
                    </button>
                  ))}
                </div>
                <details className="memory-device-info">
                  <summary>库与设备标识</summary>
                  <dl className="memory-identities">
                    <dt>记忆库</dt>
                    <dd>{info?.library_id}</dd>
                    <dt>此设备</dt>
                    <dd>{info?.device_id}</dd>
                  </dl>
                </details>
                <Section
                  className={
                    syncTab === "webdav"
                      ? "memory-settings-panel"
                      : "memory-hidden"
                  }
                >
                  <p className="muted">
                    {syncConfigured
                      ? "已使用工具箱现有的 WebDAV 同步服务。"
                      : "先在工具箱设置中配置 WebDAV 同步服务。"}
                    日常只需打开自动同步。另一台电脑加入同一个记忆库后，就能共享项目记录和全局记忆。
                  </p>
                  <CheckField
                    checked={autoSync}
                    disabled={busy}
                    onChange={(enabled) =>
                      void act(async () => {
                        await rpc("memory.sync.configure", {
                          auto_sync: enabled,
                        });
                        await refresh();
                      })
                    }
                    label="自动同步（每 5 分钟）"
                    hint="工具箱运行期间同步；离线保留记录并在联网后重试。"
                  />
                  {syncError && <Notice tone="warning">{syncError}</Notice>}
                  <div className="button-row">
                    <Button
                      onClick={() => {
                        if (!leave()) return;
                        sessionStorage.setItem(
                          "wintoolbox-settings-tab",
                          "data",
                        );
                        navigate("settings");
                      }}
                    >
                      WebDAV 连接设置
                    </Button>
                    <Button
                      busy={busy || syncRunning}
                      onClick={() => void act(() => start("memory.sync.run"))}
                    >
                      同步当前记忆库
                    </Button>
                    <Button
                      busy={busy}
                      onClick={() =>
                        void act(() => start("memory.sync.libraries"))
                      }
                    >
                      查找已有记忆库
                    </Button>
                  </div>
                  <details className="memory-join">
                    <summary>另一台电脑怎么接入？</summary>
                    <ol className="memory-help-steps">
                      <li>在另一台电脑配置同一个 WebDAV 地址和目录。</li>
                      <li>点击“查找已有记忆库”，选择下面的库 ID，再加入。</li>
                      <li>
                        同步后为需要的项目启用“同步项目正文”。只阅读知识不必关联本机目录。
                      </li>
                    </ol>
                    <Field
                      label="选择已有记忆库"
                      hint="仅空记忆库可加入，防止意外混合两个库。"
                    >
                      <select
                        value={library}
                        onChange={(e) => setLibrary(e.target.value)}
                      >
                        <option value="">先查找已有记忆库</option>
                        {discovery?.result?.libraries.map((l) => (
                          <option key={l.library_id} value={l.library_id}>
                            {l.library_id === info?.library_id
                              ? "当前使用的记忆库"
                              : "记忆库 " + l.library_id.slice(0, 8)}
                          </option>
                        ))}
                      </select>
                    </Field>
                    <Button
                      disabled={!library || busy}
                      onClick={() =>
                        void act(async () => {
                          await rpc("memory.sync.join", {
                            library_id: library,
                          });
                          await refresh();
                          setDraft(undefined);
                          setSelected("");
                          success("已加入记忆库，可以开始同步。");
                        })
                      }
                    >
                      加入
                    </Button>
                  </details>
                </Section>
                <Section
                  className={
                    syncTab === "ai" ? "memory-settings-panel" : "memory-hidden"
                  }
                >
                  <CheckField
                    checked={
                      !!assigned?.enabled &&
                      assigned.device_id === info?.device_id
                    }
                    disabled={busy}
                    onChange={(enabled) =>
                      void act(async () => {
                        await rpc("memory.curation.configure", { enabled });
                        await refresh();
                      })
                    }
                    label="由这台设备自动整理候选"
                    hint="使用已配置模型；指定设备会同步到其他设备，避免重复审批。"
                  />
                  {assigned?.enabled &&
                    assigned.device_id !== info?.device_id && (
                      <p className="muted">
                        当前执行设备：{assigned.device_id}
                      </p>
                    )}
                  {curation.policy?.conflict && (
                    <Notice tone="warning">
                      执行设备设置有冲突，自动整理暂停。
                    </Notice>
                  )}
                  <Button
                    disabled={
                      busy ||
                      !assigned?.enabled ||
                      assigned.device_id !== info?.device_id
                    }
                    onClick={() =>
                      void act(async () => {
                        const result = await rpc<{ jobs: Job[] }>(
                          "memory.curation.run",
                        );
                        for (const job of result.jobs) track(job);
                        if (!result.jobs.length)
                          success("没有待处理的新候选。");
                      })
                    }
                  >
                    处理待整理候选
                  </Button>
                </Section>
                <Section
                  className={
                    syncTab === "ssh"
                      ? "memory-settings-panel"
                      : "memory-hidden"
                  }
                >
                  <p className="muted">
                    只有项目在远程服务器上、服务器也需要独立记录时，才需要这里。普通的多电脑同步使用“其他电脑”即可。
                  </p>
                  <ol className="memory-help-steps">
                    <li>填写能通过 SSH 登录的服务器，勾选要交换记录的项目。</li>
                    <li>
                      首次点击“安装记录工具”，再点击“双向同步”。可同时关联服务器上已有的项目目录。
                    </li>
                    <li>
                      保存连接后，日常“立即同步”会自动连接服务器。电脑关机期间，服务器仍可独立记录。
                    </li>
                  </ol>
                  <Field label="已保存连接">
                    <select
                      value={targetId}
                      onChange={(e) => {
                        const target = targets.find(
                          (t) => t.id === e.target.value,
                        );
                        if (
                          panelDirty &&
                          !window.confirm("连接有未保存的修改，放弃修改？")
                        )
                          return;
                        panelBaseline.current = JSON.stringify({
                          ...JSON.parse(panelForm),
                          targetId: e.target.value,
                          host: target?.host || "",
                          remoteRoot: target?.remote_root || "",
                          path: target?.project_path || "",
                          targetEnabled: target?.enabled ?? true,
                          sshProjects: target?.project_ids || [],
                          sshBindingProject: target?.project_id || "",
                        });
                        setTargetId(e.target.value);
                        setPanelEdited(false);
                        setHost(target?.host || "");
                        setRemoteRoot(target?.remote_root || "");
                        setPath(target?.project_path || "");
                        setTargetEnabled(target?.enabled ?? true);
                        setSshProjects(target?.project_ids || []);
                        setSshBindingProject(target?.project_id || "");
                      }}
                    >
                      <option value="">新连接</option>
                      {targets.map((t) => (
                        <option key={t.id} value={t.id}>
                          {t.host} · {t.enabled ? "已启用" : "已停用"}
                        </option>
                      ))}
                    </select>
                  </Field>
                  <Field
                    label="服务器（SSH 主机）"
                    hint="使用你已有的 SSH 别名，例如 njucg2；认证使用系统 SSH 配置。"
                  >
                    <input
                      value={host}
                      onChange={(e) => setHost(e.target.value)}
                      placeholder="SSH 配置别名或 user@host"
                    />
                  </Field>
                  <Field
                    label="记录工具在服务器上的存放位置"
                    hint="例如 /home/你的用户名/.local/share/wintoolbox-agent。这是工具自动管理记录的文件夹，不是代码项目目录；每台服务器填一次。"
                  >
                    <input
                      value={remoteRoot}
                      onChange={(e) => setRemoteRoot(e.target.value)}
                      placeholder="/home/user/.local/share/wintoolbox-agent"
                    />
                  </Field>
                  <details>
                    <summary>同步的项目（{sshProjects.length}）</summary>
                    {projects.map((p) => (
                      <CheckField
                        key={p.id}
                        checked={sshProjects.includes(p.id)}
                        onChange={(enabled) => {
                          setPanelEdited(true);
                          setSshProjects((ids) =>
                            enabled
                              ? [...ids, p.id]
                              : ids.filter((id) => id !== p.id),
                          );
                        }}
                        label={p.name}
                      />
                    ))}
                  </details>
                  <Field label="关联远程目录的项目（可选）">
                    <select
                      value={sshBindingProject}
                      onChange={(e) => setSshBindingProject(e.target.value)}
                    >
                      <option value="">不关联目录</option>
                      {projects
                        .filter((p) => sshProjects.includes(p.id))
                        .map((p) => (
                          <option key={p.id} value={p.id}>
                            {p.name}
                          </option>
                        ))}
                    </select>
                  </Field>
                  <Field
                    label="远程项目目录（可选）"
                    hint="填写后在远程已有项目中登记当前项目标识，不会创建项目目录。"
                  >
                    <input
                      value={path}
                      disabled={!sshBindingProject}
                      onChange={(e) => setPath(e.target.value)}
                      placeholder={
                        sshBindingProject
                          ? "/home/user/project"
                          : "先选择项目以关联目录"
                      }
                    />
                  </Field>
                  <div className="button-row">
                    <Button
                      disabled={!host || !remoteRoot || busy}
                      onClick={() =>
                        void act(() =>
                          start("memory.ssh.install", {
                            host,
                            remote_root: remoteRoot,
                          }),
                        )
                      }
                    >
                      1. 安装记录工具
                    </Button>
                    <Button
                      disabled={!host || !remoteRoot || busy}
                      onClick={() =>
                        void act(() => start("memory.ssh.sync", sshParams()))
                      }
                    >
                      2. 双向同步
                    </Button>
                  </div>
                  <CheckField
                    checked={targetEnabled}
                    onChange={setTargetEnabled}
                    label="同步时先连接此服务器"
                  />
                  <div className="button-row">
                    <Button
                      disabled={!host || !remoteRoot || busy}
                      onClick={() =>
                        void act(async () => {
                          const saved = await rpc<{ target: SshTarget }>(
                            "memory.ssh.configure",
                            {
                              ...(targetId ? { id: targetId } : {}),
                              ...sshParams(),
                              enabled: targetEnabled,
                            },
                          );
                          setTargetId(saved.target.id);
                          setPanelEdited(false);
                          panelBaseline.current = JSON.stringify({
                            ...JSON.parse(panelForm),
                            targetId: saved.target.id,
                          });
                          await refresh();
                          success("已保存此设备的 SSH 连接。");
                        })
                      }
                    >
                      3. 保存连接
                    </Button>
                    {targetId && (
                      <Button
                        onClick={() =>
                          void act(async () => {
                            await rpc("memory.ssh.remove", { id: targetId });
                            setTargetId("");
                            await refresh();
                          })
                        }
                      >
                        移除连接
                      </Button>
                    )}
                  </div>
                </Section>
              </>
            )}
          </div>
        </Modal>
      )}
    </div>
  );
}
