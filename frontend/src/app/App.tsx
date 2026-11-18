// SPDX-License-Identifier: GPL-3.0-only
import type { ReactElement } from "react";

import { AppProviders } from "./AppProviders";
import { AppRoutes } from "./routes";

export function App(): ReactElement {
  return (
    <AppProviders>
      <AppRoutes />
    </AppProviders>
  );
}
