"""注册示例 MCP / Skill 资源（幂等，可重复跑）。

真实接入时把 spec 换成自家 server 的传输配置即可；这里给的是能点开页面
看到选项、能通过发布校验的最小集合。
"""

from ao import resources


def main() -> None:
    resources.upsert(
        kind="mcp",
        ref="documents",
        name="文档检索",
        description="本地文件系统 MCP（示例，依赖 npx）",
        spec={
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", "."],
        },
    )
    resources.upsert(
        kind="mcp",
        ref="team-calendar",
        name="团队日历",
        description="HTTP MCP 示例，端点需替换成真实地址",
        spec={"type": "http", "url": "https://example.invalid/mcp"},
    )
    resources.upsert(
        kind="skill",
        ref="weekly-report",
        name="周报写作",
        description="平台内置的周报结构与语气规范",
        spec={"description": "结论先行，数据带对比，控制在 800 字内"},
    )
    resources.upsert(
        kind="skill",
        ref="my-notes",
        name="我的笔记规范",
        description="示例：只对 u_001 可见的私有 skill",
        spec={"description": "私有写作偏好"},
        scope="private",
        owner_user_id="u_001",
    )
    print("已注册 4 个示例资源（3 global + 1 private）")


if __name__ == "__main__":
    main()
