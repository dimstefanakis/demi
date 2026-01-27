from claudius.workspace.core import WorkspaceManager


def test_workspace_created(tmp_path):
    manager = WorkspaceManager(root_dir=tmp_path)
    workspace = manager.ensure_workspace(tenant_key="telegram:987654")

    assert workspace.root.exists()
    assert workspace.tasks_dir.exists()
    assert workspace.assets_dir.exists()
    assert workspace.site_dir.exists()

    # memory file created
    assert workspace.memory_path.exists()


def test_workspace_copies_design_md(tmp_path):
    template_root = tmp_path / "template"
    template_root.mkdir()
    (template_root / "DESIGN.md").write_text("design")

    manager = WorkspaceManager(root_dir=tmp_path / "data", template_root=template_root)
    workspace = manager.ensure_workspace(tenant_key="telegram:1")

    assert (workspace.root / "DESIGN.md").exists()
