// SPDX-License-Identifier: GPL-3.0-only
import { BellOutlined, MoonOutlined, SunOutlined } from "@ant-design/icons";
import { Badge, Button, Layout, Menu, Result, Space, Tag, Typography } from "antd";
import type { ReactElement } from "react";
import { Outlet, useLocation, useNavigate } from "react-router-dom";

import { useSession } from "../api/useSession";

import { errorMessage } from "./errors";
import { MENU_ITEMS } from "./menu";
import { useTheme } from "./theme";

const { Header, Sider, Content } = Layout;

export function AppLayout(): ReactElement {
  const session = useSession();
  const { mode, toggle } = useTheme();
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const selected = MENU_ITEMS.filter((item) => pathname.startsWith(item.key)).map(
    (item) => item.key,
  );

  return (
    <Layout style={{ minHeight: "100vh" }}>
      <Sider breakpoint="lg" collapsedWidth={64}>
        <Typography.Title level={4} style={{ color: "inherit", margin: 16 }}>
          OneDrive Cleaner
        </Typography.Title>
        <Menu
          theme="dark"
          mode="inline"
          aria-label="Navegação principal"
          items={MENU_ITEMS}
          selectedKeys={selected}
          onClick={({ key }) => void navigate(key)}
        />
      </Sider>
      <Layout>
        <Header style={{ display: "flex", justifyContent: "flex-end", alignItems: "center" }}>
          <Space size="middle">
            <Tag>Modo: não definido</Tag>
            <Badge count={0} showZero={false}>
              <BellOutlined aria-label="Alertas" style={{ color: "inherit" }} />
            </Badge>
            <Typography.Text style={{ color: "inherit" }}>
              {session.data?.account?.display_name ?? "Sem conta"}
            </Typography.Text>
            <Button
              type="text"
              aria-label="Alternar tema"
              icon={mode === "dark" ? <SunOutlined /> : <MoonOutlined />}
              onClick={toggle}
            />
          </Space>
        </Header>
        <Content style={{ margin: 24 }}>
          {session.isError ? (
            <Result
              status="error"
              title="Falha ao carregar a sessão"
              subTitle={errorMessage(session.error)}
              extra={
                <Button type="primary" onClick={() => void session.refetch()}>
                  Tentar novamente
                </Button>
              }
            />
          ) : (
            <Outlet />
          )}
        </Content>
      </Layout>
    </Layout>
  );
}
