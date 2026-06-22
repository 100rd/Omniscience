import pytest
from pathlib import Path
import sys

# Add scripts directory to path to import the linter
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from lint_stale_architecture import check_file, get_available_adrs, get_available_tests, check_docstrings_ast, check_adr_file

def test_get_available_adrs(tmp_path):
    docs_dir = tmp_path / "docs"
    decisions_dir = docs_dir / "decisions"
    decisions_dir.mkdir(parents=True)
    
    (decisions_dir / "0001-test.md").touch()
    (decisions_dir / "0002-another.md").touch()
    (decisions_dir / "not-an-adr.md").touch()
    
    adrs = get_available_adrs(docs_dir)
    assert adrs == {"0001", "0002"}

def test_get_available_tests(tmp_path):
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    
    test_file = tests_dir / "test_dummy.py"
    test_file.write_text("""
def test_alpha():
    pass

class TestBeta:
    def test_gamma(self):
        pass

def not_a_test():
    pass
""", encoding="utf-8")
    
    test_ids = get_available_tests(tests_dir)
    assert "test_alpha" in test_ids
    assert "TestBeta" in test_ids
    assert "test_gamma" in test_ids
    assert "not_a_test" not in test_ids

def test_check_file_no_errors(tmp_path):
    py_file = tmp_path / "app.py"
    py_file.write_text("""
def foo():
    # This matches ADR-0001 perfectly
    # test-id: test_alpha
    pass
""", encoding="utf-8")
    
    adrs = {"0001"}
    tests = {"test_alpha"}
    
    errors = check_file(py_file, adrs, tests)
    assert len(errors) == 0

def test_check_file_doc_drift(tmp_path):
    py_file = tmp_path / "app.py"
    py_file.write_text("""
def foo():
    # References deleted ADR-9999
    pass
""", encoding="utf-8")
    
    adrs = {"0001"}
    tests = set()
    
    errors = check_file(py_file, adrs, tests)
    assert len(errors) == 1
    assert errors[0][0] == 3
    assert "doc-drift: References unknown or deleted ADR-9999" in errors[0][1]

def test_check_file_contract_drift(tmp_path):
    py_file = tmp_path / "app.py"
    py_file.write_text("""
def foo():
    # test-id: test_missing
    pass
""", encoding="utf-8")
    
    adrs = set()
    tests = {"test_alpha"}
    
    errors = check_file(py_file, adrs, tests)
    assert len(errors) == 1
    assert errors[0][0] == 3
    assert "contract-drift: References unknown test-id 'test_missing'" in errors[0][1]

def test_check_file_stale_architecture(tmp_path):
    py_file = tmp_path / "app.py"
    py_file.write_text("""
def foo():
    # pgvector was removed
    pass
""", encoding="utf-8")
    
    adrs = set()
    tests = set()
    
    errors = check_file(py_file, adrs, tests)
    assert len(errors) == 1
    assert errors[0][0] == 3
    assert "Stale architecture comment/string detected" in errors[0][1]
    assert "pgvector was removed" in errors[0][1]

def test_check_docstrings_ast_doc_drift():
    content = '''
def my_func(a, b):
    """
    My function.

    Args:
        a: The first arg
        b: The second arg
        c: A missing arg that causes drift
    """
    pass
'''
    errors = check_docstrings_ast(content, Path("app.py"))
    assert len(errors) == 1
    assert "doc-drift: Docstring for 'my_func' documents arguments that do not exist in signature: c" in errors[0][1]

def test_check_docstrings_ast_no_drift():
    content = '''
def my_func(a, b):
    """
    My function.

    Args:
        a: The first arg
        b: The second arg
    """
    pass
'''
    errors = check_docstrings_ast(content, Path("app.py"))
    assert len(errors) == 0

def test_check_adr_file_doc_drift(tmp_path):
    root_dir = tmp_path
    docs_dir = root_dir / "docs" / "decisions"
    docs_dir.mkdir(parents=True)
    adr_file = docs_dir / "0001-test.md"
    
    # Create an existing python file
    pkg_dir = root_dir / "packages"
    pkg_dir.mkdir()
    (pkg_dir / "valid.py").touch()
    
    adr_file.write_text("""
This ADR references an existing file: `packages/valid.py`.
This ADR also references a missing file: `packages/missing.py`.
    """, encoding="utf-8")
    
    errors = check_adr_file(adr_file, root_dir)
    assert len(errors) == 1
    assert errors[0][0] == 3
    assert "doc-drift: ADR references missing file 'packages/missing.py'" in errors[0][1]

