import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import type { Todo, TodoApi } from "./api";
import { httpTodoApi } from "./api";
import "./styles.css";

type ViewKey = "open" | "today" | "upcoming" | "blocked" | "done" | "tagged";

const views: { key: ViewKey; label: string }[] = [
  { key: "open", label: "Inbox" },
  { key: "today", label: "Today" },
  { key: "upcoming", label: "Upcoming" },
  { key: "blocked", label: "Blocked" },
  { key: "done", label: "Completed" },
  { key: "tagged", label: "Tagged" }
];

export function App({ api = httpTodoApi }: { api?: TodoApi }) {
  const [todos, setTodos] = useState<Todo[]>([]);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [view, setView] = useState<ViewKey>("open");
  const [query, setQuery] = useState("");
  const [quickTitle, setQuickTitle] = useState("");
  const [busy, setBusy] = useState(false);
  const searchRef = useRef<HTMLInputElement>(null);
  const quickRef = useRef<HTMLInputElement>(null);

  async function refresh() {
    setBusy(true);
    try {
      const result = await api.listTodos({ status: view === "done" ? "done" : view === "blocked" ? "blocked" : "open" });
      setTodos(result.items);
      setSelectedId((current) => current ?? result.items[0]?.id ?? null);
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    refresh();
  }, [view]);

  const visibleTodos = useMemo(() => {
    const today = new Date().toISOString().slice(0, 10);
    const needle = query.trim().toLowerCase();
    return todos.filter((todo) => {
      if (view === "today" && todo.due_date !== today) return false;
      if (view === "upcoming" && (!todo.due_date || todo.due_date <= today)) return false;
      if (view === "tagged" && todo.tags.length === 0) return false;
      if (!needle) return true;
      return (
        todo.title.toLowerCase().includes(needle) ||
        todo.description.toLowerCase().includes(needle) ||
        todo.tags.some((tag) => tag.toLowerCase().includes(needle))
      );
    });
  }, [query, todos, view]);

  const selected = visibleTodos.find((todo) => todo.id === selectedId) ?? visibleTodos[0] ?? null;

  useEffect(() => {
    if (selected && selected.id !== selectedId) setSelectedId(selected.id);
  }, [selected, selectedId]);

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      const target = event.target as HTMLElement | null;
      const isTyping = target?.tagName === "INPUT" || target?.tagName === "TEXTAREA";
      if (event.key === "/" && !isTyping) {
        event.preventDefault();
        searchRef.current?.focus();
      }
      if (event.key === "n" && !isTyping) {
        event.preventDefault();
        quickRef.current?.focus();
      }
      if (event.key === "x" && !isTyping && selected) {
        event.preventDefault();
        completeSelected(selected.id);
      }
      if ((event.key === "j" || event.key === "k") && !isTyping) {
        event.preventDefault();
        moveSelection(event.key === "j" ? 1 : -1);
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [selected, visibleTodos]);

  async function addTask(event: FormEvent) {
    event.preventDefault();
    const title = quickTitle.trim();
    if (!title) return;
    const result = await api.createTodo({ title });
    setTodos((items) => [result.todo, ...items]);
    setSelectedId(result.todo.id);
    setQuickTitle("");
    quickRef.current?.blur();
  }

  async function completeSelected(id: number) {
    const result = await api.completeTodo(id);
    setTodos((items) => items.map((todo) => (todo.id === id ? result.todo : todo)));
  }

  async function saveSelected(patch: Partial<Todo>) {
    if (!selected) return;
    const result = await api.updateTodo(selected.id, patch);
    setTodos((items) => items.map((todo) => (todo.id === selected.id ? result.todo : todo)));
  }

  function moveSelection(delta: number) {
    if (visibleTodos.length === 0) return;
    const index = Math.max(0, visibleTodos.findIndex((todo) => todo.id === selected?.id));
    const next = visibleTodos[Math.min(visibleTodos.length - 1, Math.max(0, index + delta))];
    setSelectedId(next.id);
  }

  return (
    <main className="app-shell">
      <aside className="rail">
        <div className="brand">Halo Todo</div>
        <nav aria-label="Todo views">
          {views.map((item) => (
            <button
              key={item.key}
              className={view === item.key ? "active" : ""}
              onClick={() => setView(item.key)}
            >
              {item.label}
            </button>
          ))}
        </nav>
      </aside>

      <section className="list-pane">
        <form className="quick-add" onSubmit={addTask}>
          <input
            ref={quickRef}
            aria-label="Quick add title"
            value={quickTitle}
            onChange={(event) => setQuickTitle(event.target.value)}
            placeholder="Capture a task"
          />
          <button type="submit">Add task</button>
        </form>
        <input
          ref={searchRef}
          aria-label="Search tasks"
          className="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Search"
        />
        <div className="list-meta">{busy ? "Loading" : `${visibleTodos.length} tasks`}</div>
        <div className="task-list">
          {visibleTodos.map((todo) => (
            <button
              key={todo.id}
              className={`task-row ${todo.id === selected?.id ? "selected" : ""}`}
              onClick={() => setSelectedId(todo.id)}
            >
              <span className={`priority ${todo.priority}`}>{todo.priority}</span>
              <span className="task-title">{todo.title}</span>
              <span className="task-subline">
                {todo.due_date ?? "No due date"} {todo.ticket_id ? `Ticket #${todo.ticket_id}` : ""}
              </span>
            </button>
          ))}
        </div>
      </section>

      <TaskDetail todo={selected} onComplete={completeSelected} onSave={saveSelected} />
    </main>
  );
}

function TaskDetail({
  todo,
  onComplete,
  onSave
}: {
  todo: Todo | null;
  onComplete: (id: number) => void;
  onSave: (patch: Partial<Todo>) => void;
}) {
  if (!todo) return <aside className="detail-pane empty">No task selected</aside>;

  return (
    <aside className="detail-pane">
      <div className="detail-actions">
        <span className={`status ${todo.status}`}>{todo.status === "done" ? "Done" : todo.status}</span>
        <button onClick={() => onComplete(todo.id)}>Complete</button>
      </div>
      <input
        className="title-editor"
        value={todo.title}
        onChange={(event) => onSave({ title: event.target.value })}
        aria-label="Task title"
      />
      <textarea
        value={todo.description}
        onChange={(event) => onSave({ description: event.target.value })}
        aria-label="Task description"
      />
      <div className="fields">
        <label>
          Priority
          <select value={todo.priority} onChange={(event) => onSave({ priority: event.target.value })}>
            <option value="normal">normal</option>
            <option value="high">high</option>
            <option value="low">low</option>
          </select>
        </label>
        <label>
          Due
          <input value={todo.due_date ?? ""} onChange={(event) => onSave({ due_date: event.target.value })} />
        </label>
      </div>
      <div className="chips">
        {todo.client_id ? <span>Client #{todo.client_id}</span> : null}
        {todo.ticket_id ? <span>Ticket #{todo.ticket_id}</span> : null}
        {todo.tags.map((tag) => (
          <span key={tag}>{tag}</span>
        ))}
      </div>
      <section className="metadata">
        <h2>Source</h2>
        <code>{String(todo.source_metadata.source ?? "halocli")}</code>
      </section>
      <section className="notes">
        <h2>Notes</h2>
        {todo.notes.length === 0 ? <p>No notes yet.</p> : todo.notes.map((note, index) => <p key={index}>{note.body}</p>)}
      </section>
    </aside>
  );
}
