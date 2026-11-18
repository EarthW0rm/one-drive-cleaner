// SPDX-License-Identifier: GPL-3.0-only
import { App as AntApp, ConfigProvider, Empty } from "antd";
import ptBR from "antd/locale/pt_BR";
import type { ReactElement } from "react";

export function App(): ReactElement {
  return (
    <ConfigProvider locale={ptBR}>
      <AntApp>
        <main>
          <h1>OneDrive Cleaner</h1>
          <Empty />
        </main>
      </AntApp>
    </ConfigProvider>
  );
}
