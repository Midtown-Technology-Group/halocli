export type TodoStatus = "open" | "blocked" | "done" | "cancelled" | string;

export type Todo = {
  id: number;
  title: string;
  description: string;
  status: TodoStatus;
  priority: string;
  due_date: string | null;
  owner: number | null;
  client_id: number | null;
  ticket_id: number | null;
  tags: string[];
  notes: { body: string; created_at?: string }[];
  source_metadata: Record<string, unknown>;
};

export type TodoApi = {
  listTodos(params?: Record<string, string | number | boolean | null | undefined>): Promise<{ count: number; items: Todo[] }>;
  createTodo(payload: Partial<Todo> & { title: string }): Promise<{ todo: Todo }>;
  updateTodo(id: number, payload: Partial<Todo>): Promise<{ todo: Todo }>;
  completeTodo(id: number): Promise<{ todo: Todo }>;
  addNote(id: number, note: string): Promise<{ todo: Todo }>;
};

export const httpTodoApi: TodoApi = {
  async listTodos(params = {}) {
    const search = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      if (value !== null && value !== undefined && value !== "") search.set(key, String(value));
    }
    return request(`/api/todos?${search.toString()}`);
  },
  async createTodo(payload) {
    return request("/api/todos", { method: "POST", body: JSON.stringify(payload) });
  },
  async updateTodo(id, payload) {
    return request(`/api/todos/${id}`, { method: "PATCH", body: JSON.stringify(payload) });
  },
  async completeTodo(id) {
    return request(`/api/todos/${id}/complete`, { method: "POST" });
  },
  async addNote(id, note) {
    return request(`/api/todos/${id}/notes`, { method: "POST", body: JSON.stringify({ note }) });
  }
};

async function request(path: string, init: RequestInit = {}) {
  const response = await fetch(path, {
    headers: { "content-type": "application/json", ...(init.headers ?? {}) },
    ...init
  });
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}
