import { useState } from "react";
import { Search, ListTodo, CircleCheck, CircleAlert } from "lucide-react";
import { Button, Empty, Field, Notice, Section } from "../ui";
import { scopeTasks, shownStatuses, taskTime } from "../memoryBoard";
import type { Entry, Project } from "./ProjectMemory";

const states = [
  { id: "active", name: "进行中", icon: ListTodo },
  { id: "blocked", name: "受阻", icon: CircleAlert },
  { id: "done", name: "已完成", icon: CircleCheck },
];
export default function MemoryBoard({
  entries,
  projects,
  busy,
  loading,
  error,
  onOpen,
  onRetry,
}: {
  entries: Entry[];
  projects: Project[];
  busy: boolean;
  loading: boolean;
  error: string;
  onOpen: (entry: Entry) => void;
  onRetry: () => void;
}) {
  const [project, setProject] = useState("all"),
    [status, setStatus] = useState("unfinished"),
    [days, setDays] = useState(0),
    [query, setQuery] = useState("");
  const scoped = scopeTasks(entries, projects, {
    project,
    status,
    days,
    query,
  });
  const visible = shownStatuses(status);
  const columns = states.filter((s) => visible.includes(s.id));
  const count = (id: string) =>
    scoped.filter((t) => (t.status || "active") === id).length;
  const visibleCount = scoped.filter((t) =>
    visible.includes(t.status || "active"),
  ).length;
  const missing = projects.filter((p) => !p.subscribed).length;
  return (
    <Section className="memory-board" title="任务看板">
      <div className="memory-board-controls">
        <Field label="项目范围">
          <select value={project} onChange={(e) => setProject(e.target.value)}>
            <option value="all">全部已同步内容</option>
            <option value="subscribed">已订阅项目</option>
            <option value="local">本机有目录的项目</option>
            <option value="unassigned">全局任务</option>
            <optgroup label="指定项目">
              {projects.map((p) => (
                <option key={p.id} value={"project:" + p.id}>
                  {p.name}
                </option>
              ))}
            </optgroup>
          </select>
        </Field>
        <Field label="任务状态">
          <select value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="unfinished">未完成</option>
            <option value="all">全部状态</option>
            {states.map((s) => (
              <option value={s.id} key={s.id}>
                {s.name}
              </option>
            ))}
          </select>
        </Field>
        <Field label="最近更新">
          <select
            value={days}
            onChange={(e) => setDays(Number(e.target.value))}
          >
            <option value={0}>不限时间</option>
            <option value={7}>最近 7 天</option>
            <option value={30}>最近 30 天</option>
            <option value={90}>最近 90 天</option>
          </select>
        </Field>
      </div>
      <div className="memory-search">
        <Search size={16} />
        <input
          aria-label="搜索全部任务"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="搜索任务、正文或项目"
        />
      </div>
      <div className="memory-board-summary" role="status">
        <span>{loading ? "正在读取任务…" : `${visibleCount} 项任务`}</span>
        {missing > 0 && (
          <span>{missing} 个项目未订阅，可能尚未下载全部任务</span>
        )}
      </div>
      {error ? (
        <Notice tone="warning">
          {error}
          <Button onClick={onRetry}>重试</Button>
        </Notice>
      ) : (
        <>
          <div
            className="memory-board-columns"
            style={{
              gridTemplateColumns: `repeat(${columns.length}, minmax(0, 1fr))`,
            }}
          >
            {columns.map((state) => (
              <section
                className={"memory-board-column " + state.id}
                key={state.id}
                aria-label={state.name + "任务"}
              >
                <h3>
                  <state.icon size={16} />
                  {state.name}
                  <span>{count(state.id)}</span>
                </h3>
                <div>
                  {scoped
                    .filter((t) => (t.status || "active") === state.id)
                    .map((task) => (
                      <button
                        className="memory-task-card"
                        key={task.id}
                        disabled={busy}
                        onClick={() => onOpen(task)}
                      >
                        <span className="memory-task-project">
                          {task.scope === "global"
                            ? "跨项目"
                            : projects.find((p) => p.id === task.project_id)
                                ?.name || "项目未登记"}
                        </span>
                        <strong>{task.title}</strong>
                        <span className="memory-entry-preview">
                          {task.body.replace(/[#*`>]/g, "").slice(0, 100)}
                        </span>
                        <small>
                          {task.conflict
                            ? "版本冲突 · 请先合并"
                            : taskTime(task)
                              ? "更新 " +
                                new Date(taskTime(task)).toLocaleDateString(
                                  "zh-CN",
                                )
                              : ""}
                        </small>
                      </button>
                    ))}
                </div>
              </section>
            ))}
          </div>
          {!loading && !visibleCount && (
            <Empty
              title={
                status === "unfinished"
                  ? "所选范围没有未完成任务"
                  : "没有符合条件的任务"
              }
              action={
                status === "unfinished" && count("done") > 0 ? (
                  <Button onClick={() => setStatus("done")}>
                    查看已完成（{count("done")}）
                  </Button>
                ) : undefined
              }
            >
              {query || days
                ? "可调整搜索或时间范围。"
                : "任务完成后会进入“已完成”，仍可随时查看。"}
            </Empty>
          )}
        </>
      )}
    </Section>
  );
}
