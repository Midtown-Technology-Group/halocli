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
    time_entries: [],
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
    time_entries: [],
    source_metadata: { source: "halocli" }
  }
];

function fakeApi(): TodoApi {
  const state = [...todos];
  return {
    async listTodos() {
      return {
        count: state.length,
        items: state.map((item) => ({
          ...item,
          notes: [...item.notes],
          tags: [...item.tags],
          time_entries: [...item.time_entries]
        }))
      };
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
        time_entries: [],
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
    },
    async logTime(id, payload) {
      const item = state.find((todo) => todo.id === id)!;
      const entry = { id: 9001, todo_id: id, duration_minutes: payload.minutes ?? 0, note: payload.note };
      item.time_entries.push(entry);
      return { time_entry: entry, todo: item };
    },
    async searchClients() {
      return { items: [{ id: 12, name: "Midtown Technology Group" }] };
    },
    async searchTickets(_query, clientId) {
      return { items: [{ id: 12345, summary: "Backup alert", client_id: clientId ?? 12, status: "Open" }] };
    },
    async me() {
      return { id: 37, name: "Thomas Bray", client_id: 12, client_name: "Midtown Technology Group" };
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

  test("quick-add can select a customer and related ticket", async () => {
    const user = userEvent.setup();
    render(<App api={fakeApi()} />);

    await user.click(await screen.findByRole("button", { name: "Choose customer" }));
    await user.click(screen.getAllByRole("option", { name: "Midtown Technology Group" })[0]);
    await user.click(screen.getByRole("button", { name: "Choose ticket" }));
    await user.click(screen.getAllByRole("option", { name: "Backup alert" })[0]);
    await user.type(screen.getByLabelText("Quick add title"), "Customer-linked task");
    await user.click(screen.getByRole("button", { name: "Add task" }));

    expect(await screen.findByText("Customer-linked task")).toBeInTheDocument();
    expect(screen.getByText("Client #12")).toBeInTheDocument();
    expect(screen.getAllByText("Ticket #12345").length).toBeGreaterThan(0);
  });

  test("work log submits a zero-duration time entry", async () => {
    const user = userEvent.setup();
    render(<App api={fakeApi()} />);

    await user.type(await screen.findByLabelText("Work log note"), "Reviewed alert context.");
    await user.clear(screen.getByLabelText("Minutes"));
    await user.type(screen.getByLabelText("Minutes"), "0");
    await user.click(screen.getByRole("button", { name: "Log work" }));

    expect(await screen.findByText("Reviewed alert context.")).toBeInTheDocument();
    expect(screen.getByText("0 min")).toBeInTheDocument();
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
