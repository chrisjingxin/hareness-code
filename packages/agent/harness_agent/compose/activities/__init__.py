"""Compose Work Item 的能力执行单元（WP8 起逐步接入）。

每个 Activity 通过 ComposeWorkItemStore 持久化 attempt 与终态，从 workspace
Markdown 与 SQLite 事实恢复，不拥有 graph 或 SQLite 连接生命周期。
"""
