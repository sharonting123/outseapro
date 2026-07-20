from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from scripts.migrate_sqlite_to_postgres import export_sql
from soutui.auth import register
from soutui.store import Store


def test_management_api_sql_export_is_transactional_and_escaped(tmp_path):
    source_path = tmp_path / "source.db"
    source = Store(source_path)
    register(source, "obrien@example.com", "safe-password-1", "O'Brien")

    output_path = tmp_path / "import.sql"
    counts = export_sql(source_path, output_path, replace=True)
    sql = output_path.read_text(encoding="utf-8")

    assert sql.startswith("BEGIN;\n")
    assert sql.endswith("COMMIT;\n")
    assert "DELETE FROM users;" in sql
    assert "O''Brien" in sql
    assert counts["users"] == 1
