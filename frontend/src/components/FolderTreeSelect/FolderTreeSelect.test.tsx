// SPDX-License-Identifier: GPL-3.0-only
import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { useState, type ReactElement } from "react";
import { describe, expect, it } from "vitest";

import { createQueryClient } from "../../api/queryClient";
import { errorResponse, foldersHandler, TEST_ORIGIN } from "../../test/handlers";
import { server } from "../../test/server";

import { FolderTreeSelect } from "./FolderTreeSelect";

function Harness({
  preselectPaths,
  onValue,
}: Readonly<{ preselectPaths?: string[]; onValue?: (ids: string[]) => void }>): ReactElement {
  const [value, setValue] = useState<string[]>([]);
  return (
    <QueryClientProvider client={createQueryClient(0)}>
      <FolderTreeSelect
        value={value}
        onChange={(ids) => {
          setValue(ids);
          onValue?.(ids);
        }}
        {...(preselectPaths === undefined ? {} : { preselectPaths })}
      />
    </QueryClientProvider>
  );
}

function switcherOf(label: string): HTMLElement {
  const title = screen.getByText(label);
  // eslint-disable-next-line testing-library/no-node-access
  const node = title.closest(".ant-select-tree-treenode");
  // eslint-disable-next-line testing-library/no-node-access
  const switcher = node?.querySelector<HTMLElement>(".ant-select-tree-switcher");
  if (switcher === null || switcher === undefined) {
    throw new Error(`sem switcher para ${label}`);
  }
  return switcher;
}

describe("FolderTreeSelect", () => {
  it("carrega só a raiz e cada nível uma única vez ao expandir", async () => {
    const calls: string[] = [];
    server.use(foldersHandler(calls));
    const user = userEvent.setup();
    render(<Harness />);

    await user.click(screen.getByRole("combobox"));
    expect(await screen.findByText("Pictures")).toBeInTheDocument();
    expect(calls).toEqual([""]);

    await user.click(switcherOf("Pictures"));
    expect(await screen.findByText("Camera Roll")).toBeInTheDocument();
    await user.click(switcherOf("Pictures"));
    await user.click(switcherOf("Pictures"));

    await waitFor(() => {
      expect(calls).toEqual(["", "FAKE-pictures"]);
    });
  });

  it("marca a Camera Roll resolvendo o caminho de exibição", async () => {
    const calls: string[] = [];
    server.use(foldersHandler(calls));
    const seen: string[][] = [];
    render(
      <Harness
        preselectPaths={["/Pictures/Camera Roll"]}
        onValue={(ids) => {
          seen.push(ids);
        }}
      />,
    );

    await waitFor(() => {
      expect(seen).toEqual([["FAKE-camera"]]);
    });
    expect(await screen.findByText("Camera Roll")).toBeInTheDocument();
    expect(calls).toEqual(["", "FAKE-pictures"]);
  });

  it("não marca nada quando o caminho não existe ou o nível falha", async () => {
    server.use(foldersHandler());
    const seen: string[][] = [];
    const { unmount } = render(
      <Harness
        preselectPaths={["/Outra/Pasta"]}
        onValue={(ids) => {
          seen.push(ids);
        }}
      />,
    );
    await screen.findByRole("combobox");
    await waitFor(() => {
      expect(seen).toEqual([]);
    });
    unmount();

    server.use(
      http.get(`${TEST_ORIGIN}/api/folders`, () => errorResponse("GRAPH_UNAVAILABLE", "x", 503)),
    );
    render(<Harness preselectPaths={["/Pictures"]} />);
    expect(await screen.findByRole("alert")).toBeInTheDocument();
  });

  it("permite marcar uma pasta", async () => {
    server.use(foldersHandler());
    const seen: string[][] = [];
    const user = userEvent.setup();
    render(
      <Harness
        onValue={(ids) => {
          seen.push(ids);
        }}
      />,
    );

    await user.click(screen.getByRole("combobox"));
    await user.click(await screen.findByText("Documents"));

    expect(seen.at(-1)).toEqual(["FAKE-docs"]);
  });

  it("exibe erro na raiz e recarrega ao tentar novamente", async () => {
    let fail = true;
    server.use(
      http.get(`${TEST_ORIGIN}/api/folders`, () =>
        fail
          ? errorResponse("GRAPH_UNAVAILABLE", "x", 503)
          : HttpResponse.json({
              folders: [{ id: "FAKE-docs", name: "Documents", child_count: 0 }],
            }),
      ),
    );
    const user = userEvent.setup();
    render(<Harness />);

    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText(/O OneDrive não respondeu/)).toBeInTheDocument();
    fail = false;
    await user.click(within(alert).getByRole("button", { name: "Tentar novamente" }));
    await user.click(screen.getByRole("combobox"));

    expect(await screen.findByText("Documents")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("sinaliza falha ao carregar um nível e permite expandir de novo", async () => {
    let fail = true;
    const calls: string[] = [];
    server.use(
      http.get(`${TEST_ORIGIN}/api/folders`, ({ request }) => {
        const parent = new URL(request.url).searchParams.get("parent_id");
        calls.push(parent ?? "");
        if (parent === null) {
          return HttpResponse.json({
            folders: [{ id: "FAKE-pictures", name: "Pictures", child_count: 1 }],
          });
        }
        return fail
          ? errorResponse("GRAPH_UNAVAILABLE", "x", 503)
          : HttpResponse.json({
              folders: [{ id: "FAKE-camera", name: "Camera Roll", child_count: 0 }],
            });
      }),
    );
    const user = userEvent.setup();
    render(<Harness />);

    await user.click(screen.getByRole("combobox"));
    await user.click(switcherOf("Pictures"));
    expect(await screen.findByText(/Pictures \(falha ao carregar/)).toBeInTheDocument();
    expect(calls).toEqual(["", "FAKE-pictures"]);

    fail = false;
    await user.click(switcherOf("Pictures (falha ao carregar; recolha e expanda novamente)"));
    await user.click(switcherOf("Pictures"));

    expect(await screen.findByText("Camera Roll")).toBeInTheDocument();
    expect(calls.filter((call) => call === "FAKE-pictures")).toHaveLength(2);
  });
});
