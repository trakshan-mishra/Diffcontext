import sys
from pathlib import Path

# Add the src directory to path for pytest
pytest_path = Path('real_benchmarks/pytest')
src_path = pytest_path / 'src'

if src_path.exists():
    print(f"✅ Found pytest src directory: {src_path}")
    # Count files in src
    py_files = list(src_path.rglob('*.py'))
    print(f"   Python files in src: {len(py_files)}")
    
    # Also check other potential locations
    for subdir in pytest_path.iterdir():
        if subdir.is_dir() and subdir.name not in ['docs', 'tests', '.git']:
            py_count = len(list(subdir.rglob('*.py')))
            if py_count > 0:
                print(f"   {subdir.name}: {py_count} Python files")
else:
    print("No src directory found - pytest may be shallow cloned")
    # Check if we need to clone with more depth
    import subprocess
    print("\nRe-cloning pytest with more depth...")
    subprocess.run(['rm', '-rf', 'real_benchmarks/pytest'], capture_output=True)
    subprocess.run(['git', 'clone', '--depth', '5', 'https://github.com/pytest-dev/pytest.git', 'real_benchmarks/pytest'], capture_output=True)
    
    # Check again
    src_path = Path('real_benchmarks/pytest') / 'src'
    if src_path.exists():
        py_files = list(src_path.rglob('*.py'))
        print(f"After re-clone: {len(py_files)} Python files found")
