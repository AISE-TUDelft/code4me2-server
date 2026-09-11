from pathlib import Path

# SPECPATH is the directory containing this spec file (packaging/),
# so its parent is the server repository root.
project_root = Path(SPECPATH).parent
source_root = project_root / "src"

analysis = Analysis(
    [str(source_root / "code4me2_agent" / "cli.py")],
    pathex=[str(source_root)],
    binaries=[],
    datas=[],
    hiddenimports=[],
    excludes=["backend", "database", "celery", "torch", "transformers"],
    noarchive=False,
)

pyz = PYZ(analysis.pure)

executable = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="code4me2-agent",
    console=True,
    disable_windowed_traceback=False,
)

collection = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="code4me2-agent",
)
