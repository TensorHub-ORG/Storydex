from storage.workspace_io import WorkspaceIO
from typing import Optional


class EditorService:
    def __init__(self) -> None:
        self.workspace = WorkspaceIO()

    def read_text(self, relative_path: str) -> str:
        return self.workspace.read_text(relative_path)

    def read_document(self, relative_path: str, *, offset: Optional[int] = None, limit: Optional[int] = None):
        return self.workspace.read_document(relative_path, offset=offset, limit=limit)

    def write_text(self, relative_path: str, content: str):
        return self.workspace.write_text(relative_path, content)

    def create_file(self, relative_path: str, content: str = ""):
        return self.workspace.create_file(relative_path, content)

    def import_file_bytes(self, target_directory: str, file_name: str, content: bytes):
        return self.workspace.file_adapter.import_file_bytes(target_directory, file_name, content)

    def create_directory(self, relative_path: str):
        return self.workspace.create_directory(relative_path)

    def rename_path(self, from_relative_path: str, to_relative_path: str):
        return self.workspace.rename_path(from_relative_path, to_relative_path)

    def delete_path(self, relative_path: str):
        return self.workspace.delete_path(relative_path)

    def copy_path(self, from_relative_path: str, to_relative_path: str):
        return self.workspace.copy_path(from_relative_path, to_relative_path)

    def move_path(self, from_relative_path: str, to_relative_path: str):
        return self.workspace.move_path(from_relative_path, to_relative_path)

    def list_workspace_tree(self):
        return self.workspace.list_workspace_tree()
