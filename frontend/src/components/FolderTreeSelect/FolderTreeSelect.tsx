// SPDX-License-Identifier: GPL-3.0-only
import { Alert, Button, Space, TreeSelect } from "antd";
import { useCallback, useEffect, useRef, useState, type Key, type ReactElement } from "react";

import { fetchFolders, type FolderModel } from "../../api/folders";
import { errorMessage } from "../../app/errors";

interface TreeNode {
  value: string;
  title: string;
  isLeaf: boolean;
  children?: TreeNode[];
}

interface LoadedNode {
  value?: string | number | null;
}

export interface FolderTreeSelectProps {
  readonly value: readonly string[];
  readonly onChange: (ids: string[]) => void;
  /** Caminhos de exibição (`/Pictures/Camera Roll`) a resolver e marcar na primeira carga. */
  readonly preselectPaths?: readonly string[] | undefined;
}

const ERROR_SUFFIX = " (falha ao carregar; recolha e expanda novamente)";

function toNode(
  folder: FolderModel,
  children: ReadonlyMap<string, FolderModel[]>,
  failed: ReadonlySet<string>,
): TreeNode {
  const loaded = children.get(folder.id);
  const node: TreeNode = {
    value: folder.id,
    title: failed.has(folder.id) ? `${folder.name}${ERROR_SUFFIX}` : folder.name,
    isLeaf: folder.child_count === 0 || loaded?.length === 0,
  };
  if (loaded !== undefined && loaded.length > 0) {
    node.children = loaded.map((child) => toNode(child, children, failed));
  }
  return node;
}

function segmentsOf(path: string): string[] {
  return path.split("/").filter((segment) => segment !== "");
}

/**
 * Seleção múltipla de pastas do OneDrive com carga sob demanda: cada nível só é pedido
 * (`GET /api/folders?parent_id=`) quando o nó é expandido.
 */
export function FolderTreeSelect({
  value,
  onChange,
  preselectPaths = [],
}: FolderTreeSelectProps): ReactElement {
  const [children, setChildren] = useState<ReadonlyMap<string, FolderModel[]>>(new Map());
  const [loadedKeys, setLoadedKeys] = useState<string[]>([]);
  const [failed, setFailed] = useState<ReadonlySet<string>>(new Set());
  const [rootError, setRootError] = useState<string | null>(null);
  const cache = useRef(new Map<string, Promise<FolderModel[]>>());
  const preselected = useRef(false);
  const valueRef = useRef(value);
  const onChangeRef = useRef(onChange);
  useEffect(() => {
    valueRef.current = value;
    onChangeRef.current = onChange;
  });

  const load = useCallback((parentId: string | undefined): Promise<FolderModel[]> => {
    const key = parentId ?? "";
    const cached = cache.current.get(key);
    if (cached !== undefined) {
      return cached;
    }
    const request = fetchFolders(parentId).then((folders) => {
      setChildren((previous) => new Map(previous).set(key, folders));
      return folders;
    });
    cache.current.set(key, request);
    request.catch(() => {
      cache.current.delete(key);
    });
    return request;
  }, []);

  const loadRoot = useCallback(async (): Promise<void> => {
    setRootError(null);
    try {
      await load(undefined);
    } catch (error) {
      setRootError(errorMessage(error));
    }
  }, [load]);

  useEffect(() => {
    load(undefined).catch((error: unknown) => {
      setRootError(errorMessage(error));
    });
  }, [load]);

  useEffect(() => {
    if (preselected.current || preselectPaths.length === 0) {
      return;
    }
    preselected.current = true;
    void (async () => {
      const ids: string[] = [];
      for (const path of preselectPaths) {
        const id = await resolvePath(load, segmentsOf(path));
        if (id !== null) {
          ids.push(id);
        }
      }
      if (ids.length > 0 && valueRef.current.length === 0) {
        onChangeRef.current(ids);
      }
    })();
  }, [preselectPaths, load]);

  const loadData = useCallback(
    async (node: LoadedNode): Promise<void> => {
      const id = String(node.value);
      try {
        await load(id);
        setLoadedKeys((previous) => [...previous, id]);
        setFailed((previous) => {
          const next = new Set(previous);
          next.delete(id);
          return next;
        });
      } catch {
        // Marcado como "carregado" para a árvore não repetir a chamada em laço; a pasta é
        // liberada para nova tentativa quando o utilizador a recolhe (`onTreeExpand`).
        setLoadedKeys((previous) => [...previous, id]);
        setFailed((previous) => new Set(previous).add(id));
      }
    },
    [load],
  );

  const onTreeExpand = useCallback(
    (expandedKeys: Key[]): void => {
      const expanded = new Set(expandedKeys.map(String));
      const retry = [...failed].filter((id) => !expanded.has(id));
      if (retry.length === 0) {
        return;
      }
      setLoadedKeys((previous) => previous.filter((id) => !retry.includes(id)));
      setFailed((previous) => new Set([...previous].filter((id) => !retry.includes(id))));
    },
    [failed],
  );

  const roots = children.get("") ?? [];
  const treeData = roots.map((folder) => toNode(folder, children, failed));

  return (
    <Space orientation="vertical" style={{ width: "100%" }}>
      {rootError === null ? null : (
        <Alert
          type="error"
          showIcon
          title={rootError}
          action={
            <Button
              size="small"
              onClick={() => {
                void loadRoot();
              }}
            >
              Tentar novamente
            </Button>
          }
        />
      )}
      <TreeSelect
        style={{ width: "100%" }}
        aria-label="Pastas do OneDrive"
        placeholder="Selecione as pastas a varrer"
        treeCheckable
        treeCheckStrictly
        labelInValue
        virtual={false}
        treeData={treeData}
        treeLoadedKeys={loadedKeys}
        loadData={loadData}
        onTreeExpand={onTreeExpand}
        value={value.map((id) => ({ value: id }))}
        onChange={(next: { value: string }[]) => {
          onChange(next.map((entry) => entry.value));
        }}
      />
    </Space>
  );
}

async function resolvePath(
  load: (parentId: string | undefined) => Promise<FolderModel[]>,
  segments: readonly string[],
): Promise<string | null> {
  let parent: string | undefined;
  for (const segment of segments) {
    try {
      const folders = await load(parent);
      const match = folders.find((folder) => folder.name === segment);
      if (match === undefined) {
        return null;
      }
      parent = match.id;
    } catch {
      return null;
    }
  }
  return parent ?? null;
}
