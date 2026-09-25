export type BoardTask = {
  kind: "task" | "knowledge";
  scope: string;
  project_id?: string;
  title: string;
  body: string;
  status?: string;
  created_at?: string;
  updated_at?: string;
  versions?: { created_at?: string }[];
};
export type BoardProject = {
  id: string;
  name: string;
  available: boolean;
  subscribed: boolean;
};
export type BoardFilters = {
  project: string;
  status: string;
  days: number;
  query: string;
};
export function taskTime(task: BoardTask): number {
  const dates = (task.versions || [])
    .map((v) => Date.parse(v.created_at || ""))
    .filter(Number.isFinite);
  return dates.length
    ? Math.max(...dates)
    : Date.parse(task.updated_at || task.created_at || "") || 0;
}
export function scopeTasks<T extends BoardTask>(
  tasks: T[],
  projects: BoardProject[],
  filters: BoardFilters,
  now = Date.now(),
): T[] {
  const projectById = new Map(projects.map((p) => [p.id, p]));
  const query = filters.query.trim().toLocaleLowerCase();
  return tasks
    .filter((task) => {
      if (task.kind !== "task") return false;
      const project = projectById.get(task.project_id || "");
      if (filters.project === "local" && !project?.available) return false;
      if (filters.project === "subscribed" && !project?.subscribed)
        return false;
      if (filters.project === "unassigned" && task.scope === "project")
        return false;
      if (
        filters.project.startsWith("project:") &&
        task.project_id !== filters.project.slice(8)
      )
        return false;
      if (
        filters.days > 0 &&
        (!taskTime(task) || taskTime(task) < now - filters.days * 86400000)
      )
        return false;
      return (
        !query ||
        `${task.title}\n${task.body}\n${project?.name || ""}`
          .toLocaleLowerCase()
          .includes(query)
      );
    })
    .sort(
      (a, b) =>
        taskTime(b) - taskTime(a) || a.title.localeCompare(b.title, "zh-CN"),
    );
}
export function shownStatuses(status: string): string[] {
  return status === "unfinished"
    ? ["active", "blocked"]
    : status === "all"
      ? ["active", "blocked", "done"]
      : [status];
}
