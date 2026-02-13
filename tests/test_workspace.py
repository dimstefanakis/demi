from demi.workspace.core import WorkspaceManager


def test_workspace_created(tmp_path):
    manager = WorkspaceManager(root_dir=tmp_path)
    workspace = manager.ensure_workspace(tenant_key="telegram:987654")

    assert workspace.tenant_root == tmp_path / "telegram:987654"
    assert workspace.project_name is None
    assert workspace.root.exists()
    assert workspace.tasks_dir.exists()
    assert workspace.assets_dir.exists()
    assert workspace.site_dir.exists()

    # memory file created
    assert workspace.memory_path.exists()
    assert workspace.description_path.exists()


def test_workspace_copies_design_md(tmp_path):
    template_root = tmp_path / "template"
    (template_root / "docs").mkdir(parents=True)
    (template_root / "docs" / "DESIGN.md").write_text("design-from-docs")

    manager = WorkspaceManager(root_dir=tmp_path / "data", template_root=template_root)
    workspace = manager.ensure_workspace(tenant_key="telegram:1")

    assert (workspace.root / "DESIGN.md").exists()
    assert (workspace.root / "DESIGN.md").read_text() == "design-from-docs"


def test_workspace_prefers_docs_design_md_over_root(tmp_path):
    template_root = tmp_path / "template"
    (template_root / "docs").mkdir(parents=True)
    (template_root / "docs" / "DESIGN.md").write_text("docs-template")
    (template_root / "DESIGN.md").write_text("root-template")

    manager = WorkspaceManager(root_dir=tmp_path / "data", template_root=template_root)
    workspace = manager.ensure_workspace(tenant_key="telegram:2")

    assert (workspace.root / "DESIGN.md").read_text() == "docs-template"
