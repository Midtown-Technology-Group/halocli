import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, test } from "vitest";
import { App } from "./App";
import type { TodoApi } from "./api";

const todos = [
  {
    id: 10038,
    title: "Independent todo list front end for HaloPSA",
    description: "Build the first UI slice.",
    status: "open",
    priority: "high",
    due_date: "2026-04-27",
    owner: 37,
    client_id: 12,
    ticket_id: 12345,
    tags: ["microsoft-todo", "Tasks"],
    notes: [],
    source_metadata: { source: "microsoft.todo" }
  },
  {
    id: 10039,
    title: "Caller verification",
    description: "",
    status: "open",
    priority: "normal",
    due_date: null,
    owner: 37,
    client_id: null,
    ticket_id: null,
    tags: [],
    notes: [],
    source_metadata: { source: "halocli" }
  }
];

function fakeApi(): TodoApi {
  const state = [...todos];
  return {
    async listTodos() {
      return { count: state.length, items: state.map((item) => ({ ...item, notes: [...item.notes], tags: [...item.tags] })) };
    },
    async createTodo(payload) {
      const created = {
        id: 20000,
        description: "",
        status: "open",
        priority: "normal",
        due_date: null,
        owner: null,
        client_id: null,
        ticket_id: null,
        tags: [],
        notes: [],
        source_metadata: { source: "halocli" },
        ...payload
      };
      state.unshift(created);
      return { todo: created };
    },
    async updateTodo(id, payload) {
      const item = state.find((todo) => todo.id === id)!;
      Object.assign(item, payload);
      return { todo: item };
    },
    async completeTodo(id) {
      const item = state.find((todo) => todo.id === id)!;
      item.status = "done";
      return { todo: item };
    },
    async addNote(id, note) {
      const item = state.find((todo) => todo.id === id)!;
      item.notes.push({ body: note });
      return { todo: item };
    }
  };
}

describe("Halo Todo app", () => {
  afterEach(() => cleanup());

  test("renders the triage layout and selected task detail", async () => {
    render(<App api={fakeApi()} />);

    expect(await screen.findByText("Inbox")).toBeInTheDocument();
    expect(screen.getByText("Independent todo list front end for HaloPSA")).toBeInTheDocument();
    expect(screen.getByText("Build the first UI slice.")).toBeInTheDocument();
    expect(screen.getByText("Ticket #12345")).toBeInTheDocument();
  });

  test("quick-add creates a task and keyboard completion completes the selected task", async () => {
    const user = userEvent.setup();
    render(<App api={fakeApi()} />);

    await user.type(await screen.findByLabelText("Quick add title"), "Review imported tasks");
    await user.click(screen.getByRole("button", { name: "Add task" }));

    expect(await screen.findByText("Review imported tasks")).toBeInTheDocument();

    await user.keyboard("x");

    await waitFor(() => {
      expect(screen.getByText("Done")).toBeInTheDocument();
    });
  });

  test("slash focuses search and filters task list", async () => {
    const user = userEvent.setup();
    render(<App api={fakeApi()} />);

    await screen.findByText("Caller verification");
    await user.keyboard("/");
    await user.type(screen.getByLabelText("Search tasks"), "caller");

    expect(screen.getByText("Caller verification")).toBeInTheDocument();
    expect(screen.queryByText("Independent todo list front end for HaloPSA")).not.toBeInTheDocument();
  });
});
